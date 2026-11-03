"""The level index (specs/diff-progressive.md §3b): the interval store's served sort `level`, what `/api/diff/level` reads.

One row per child version **transition**, keyed `(depth, path, ts, name)`: `depth`/`path` the parent's, `ts` the scan
stamp of the transition, `name` the child's name under it. Each folded path version (`pvl` piece) opens a transition at
its `vf` (it appears, or replaces the version that ended there), and a version whose `vt` no later version of the path
continues from closes one there (the path went away). A transition carries the child's values after it (`size`,
`n_files`, `kind`; zero for a closing), before it (`size_prev`, `n_files_prev`, `kind_prev`; zero / NULL where the
child was absent), its cumulative churn after it (`churn_cum`: the opening version's, or the closed one's `churn_end`)
and at its previous transition (`churn_prev`: the version it replaces' `churn_cum`, or for an appearance its own
`churn_cum` less its size, what that scan's churn added). `op`: 0 appeared, 1 changed, 2 went away. A piece that moves
none of those (a `last_read`- or `mtime`-only split) is no transition.

So a directory P's children's change over `(D1, D2]` is its rows in the one key range `[(dP, P, D1 + 1), (dP, P, D2)]`
of each file that holds a scan in the range (the base, each run): per child, the first transition's `*_prev` are its
values at D1, the last's its values at D2, and its range churn is the last `churn_cum` less the first `churn_prev`. A
directory's own change rides in the same range: each transition of a path that is a directory on either side of it (or
a closing) is also written under the path itself, `name` '' (first at its stamp). No row outside the range is needed,
and no file without a scan in it is read.

Each file's transitions are those at its own scans: the base's versions opened at its scans and closed at them; a run's
(`interval_append`) the versions it opened (`op` 1 in its deltas) and its close records, whose previous version is in the
same run when it continues one. Base + runs hold every transition once, as a fresh build of the same scans does.

A generation carries the sort when its `scans.json` says so (`"level": true`, `interval-store build -l`, kept by
compaction) or the sort is in its `served/` (`backfill-level`, which writes `served/level.*` into an existing generation
and its runs and nothing else); its appends then cut it into every run, and its compaction into the new generation.
"""
from __future__ import annotations

import json
from pathlib import Path
from time import monotonic

import pyarrow as pa
from click import IntRange, command, option

from .static_names import OPEN, err, q

LEVEL = "level"
#: `scans.json`'s flag: the generation's served sorts include `level`.
LEVEL_FLAG = "level"
#: The sort's record beside it (`served/level.json`), what tells a generation `backfill-level` added the sort to.
LEVEL_DOC = f"served/{LEVEL}.json"
#: The sort's order: the parent's key, the transition's stamp, the child's name.
LEVEL_KEY = ["depth", "path", "ts", "name"]
LEVEL_SCHEMA = pa.schema([
    pa.field("depth", pa.uint8(), nullable=False),  # the parent's
    pa.field("path", pa.string(), nullable=False),  # the parent's ('' the root)
    pa.field("ts", pa.int64(), nullable=False),  # the transition's scan stamp
    pa.field("name", pa.string(), nullable=False),  # the child's name under the parent ('' the parent itself)
    pa.field("op", pa.int8(), nullable=False),  # 0 appeared, 1 changed, 2 went away
    pa.field("kind", pa.string(), nullable=False),  # after (a closing: the version that ended)
    pa.field("kind_prev", pa.string()),  # before (NULL: absent)
    pa.field("size", pa.int64(), nullable=False),
    pa.field("n_files", pa.int64(), nullable=False),
    pa.field("size_prev", pa.int64(), nullable=False),
    pa.field("n_files_prev", pa.int64(), nullable=False),
    pa.field("churn_cum", pa.int64(), nullable=False),
    pa.field("churn_prev", pa.int64(), nullable=False),
])
#: The `pvl` columns the transitions are computed from (a run's deltas add `op`).
SOURCE_COLS = ["depth", "path", "vf", "vt", "kind", "size", "n_files", "churn_cum", "churn_end"]
#: A run's deltas: the versions it opened (`op` 1); its close records (`op` −1) end versions opened earlier.
RUN_OPENED = "op = 1"


def leveled(scans: dict, gen_dir: str | Path | None = None) -> bool:
    """Whether a generation carries the `level` sort: its `scans.json` says so, or (`gen_dir`, its dir or a mirror holding
    `LEVEL_DOC`) `backfill-level` added it."""
    return bool(scans.get(LEVEL_FLAG)) or (gen_dir is not None and Path(gen_dir, LEVEL_DOC).is_file())


def record_level(scans: dict) -> dict:
    """`scans` (a `scans.json`) flagged as carrying the `level` sort, the flag before `scans` (where a compaction's copy of
    the base's keys puts it, so both write the same bytes)."""
    out = {}
    for k, v in scans.items():
        if k == LEVEL_FLAG:
            continue
        if k == "scans":
            out[LEVEL_FLAG] = True
        out[k] = v
    return out


def rows_sql(src: str, opened: str = "true") -> str:
    """The `level` rows (`LEVEL_SCHEMA`, unordered) of the folded path versions in relation `src` (`SOURCE_COLS`, every
    version of each path it holds; `opened`: which of them opened in the file — a run's `RUN_OPENED`). Paths at depth ≥ 1
    (the root has no row of its own)."""
    same = "pvt = vf AND pkind = kind AND psize = size AND pn = n_files AND pc = churn_cum"
    t = f"""WITH p AS (SELECT depth, path, vf, vt, kind, size, n_files, churn_cum, churn_end, ({opened}) AS opened,
            lag(vt) OVER w AS pvt, lag(kind) OVER w AS pkind, lag(size) OVER w AS psize, lag(n_files) OVER w AS pn,
            lag(churn_cum) OVER w AS pc, lead(vf) OVER w AS nvf
            FROM {src} WHERE depth >= 1 WINDOW w AS (PARTITION BY depth, path ORDER BY vf)),
        t AS (SELECT depth, path, vf AS ts, (CASE WHEN pvt = vf THEN 1 ELSE 0 END)::TINYINT AS op, kind,
                CASE WHEN pvt = vf THEN pkind END AS kind_prev, size, n_files,
                CASE WHEN pvt = vf THEN psize ELSE 0 END AS size_prev, CASE WHEN pvt = vf THEN pn ELSE 0 END AS n_files_prev,
                churn_cum, CASE WHEN pvt = vf THEN pc ELSE churn_cum - size END AS churn_prev
            FROM p WHERE opened AND NOT coalesce({same}, false)
            UNION ALL
            SELECT depth, path, vt AS ts, 2::TINYINT AS op, kind, kind AS kind_prev, 0 AS size, 0 AS n_files, size AS size_prev,
                n_files AS n_files_prev, churn_end AS churn_cum, churn_cum AS churn_prev
            FROM p WHERE vt <> {OPEN} AND nvf IS DISTINCT FROM vt)"""
    vals = "op, kind, kind_prev, size, n_files, size_prev, n_files_prev, churn_cum, churn_prev"
    parent = "array_to_string(list_slice(string_split(path, '/'), 1, depth - 1), '/')"
    return f"""{t}
        SELECT (depth - 1)::UTINYINT AS depth, {parent} AS path, ts, string_split(path, '/')[-1] AS name, {vals} FROM t
        UNION ALL
        SELECT depth, path, ts, '' AS name, {vals} FROM t WHERE kind = 'dir' OR kind_prev = 'dir' OR op = 2"""


def seg_sql(bounds: list[list] | None) -> str:
    """Each `level` row's segment over its key (`depth`, `path`, the parent's): with `bounds` (a generation's key ranges'
    `lo`s, `ranges.json`, in order), the number of them after the first at or below the row's key, so the segments are
    disjoint key ranges in key order, cut in parallel (`interval_store.write_served`); without, one segment."""
    arms = [f"(depth > {int(d)} OR (depth = {int(d)} AND path >= {q(p)}))::INT" for d, p in (bounds or [])[1:]]
    return " + ".join(arms) if arms else "0"


def range_bounds(ranges: dict) -> list[list]:
    """A generation's key ranges' `lo`s (`ranges.json`), in order."""
    return [r["lo"] for r in ranges["ranges"]]


# ── backfill-level: the sort added to an existing generation ───────────────


def backfill(store, gen: str, work: Path, *, rg_rows: int | None = None, threads: int = 4, mem: str = "4GB", runs_only: bool = False) -> dict:
    """The `level` sort cut into generation `gen` of `store` (a dir or URL), additively: `served/level.{parquet,
    groups.parquet,json}` from the base's `pvl/` range files, and the same under each run the newest manifest lists from its
    `pvl/` deltas. Refused on a generation without churn or one whose base already has the sort; a run that has it is
    skipped. Nothing else is written (`scans.json`, the manifests and `CURRENT.json` are left as they are): the sort is
    found by its files. Each dir's files go up data first, its `level.json` last. Returns a summary."""
    from . import interval_append as ia
    from . import interval_store as ist
    from . import static_names as sn
    from . import tree_mem as tm
    from .append_runner import latest_key
    from .blobstore import mirror
    from .interval_churn import has_churn

    t0 = monotonic()
    prefix = f"{ist.PREFIX}/{gen}"
    listing = store.listing(prefix)
    if f"{prefix}/scans.json" not in listing:
        raise SystemExit(f"{store.root}/{prefix}/scans.json: no such generation")
    scans = store.read_json(f"{prefix}/scans.json")
    if not has_churn(scans):
        raise SystemExit(f"{store.root}/{prefix}: carries no churn (`interval-store backfill-churn` first): the level sort is cut from it")
    if f"{prefix}/ranges.json" not in listing:
        raise SystemExit(f"{store.root}/{prefix}/ranges.json: none; a generation borrowing its range files can't take the level sort")
    have = lambda d: f"{prefix}/{d}{LEVEL_DOC}" in listing or f"{prefix}/{d}served/{LEVEL}.parquet" in listing  # noqa: E731
    if have("") and not runs_only:
        raise SystemExit(f"{store.root}/{prefix}/served/{LEVEL}.*: the generation has the level sort")
    names = sorted(k.removeprefix(f"{prefix}/") for k in listing if k.startswith(f"{prefix}/manifests/"))
    newest = latest_key(names)
    runs = [r["key"] for r in store.read_json(f"{prefix}/{newest}")["runs"]] if newest else []
    todo_runs = [r for r in runs if not have(f"{r}/")]
    # The range files DuckDB reads: in place for a local store, else mirrored (only `pvl/`, and the runs' `pvl/` deltas).
    want = ("ranges.json", *(() if runs_only else ("pvl/",)), *(f"{r}/pvl/" for r in todo_runs))
    if store.url:
        src = work / "src"
        mirror(store, prefix, src, keep=lambda rel: rel.startswith(want))
    else:
        src = Path(store.root) / prefix
    out = work / "out"
    out.mkdir(parents=True, exist_ok=True)
    tree = tm.Budget.measure(sn.mem_bytes(mem))
    con = sn.connect(threads, mem, work / "duckdb")
    rg = rg_rows or ist.SERVED_RG
    docs: dict[str, dict] = {}
    if not runs_only:
        sorts, _ = ist.cut_gen(con, src, {**scans, LEVEL_FLAG: True}, rg_rows=rg, tree=tree, sorts=[LEVEL], served=out / "served")
        docs[""] = sorts[LEVEL]
    con = sn.connect(threads, mem, work / "duckdb")
    for r in todo_runs:
        d = ia.cut_level(con, str(src / r), out / r / "served", rg_rows=rg)
        docs[r] = {k: d[k] for k in ("rows", "groups", "bytes")}
    # Each dir's data, then its record (`level.json`: what marks the dir as having the sort).
    for d in [*(f"{r}/" for r in todo_runs), *(() if runs_only else ("",))]:
        for x in (".parquet", ".groups.parquet", ".json"):
            store.put(out / f"{d}served" / f"{LEVEL}{x}", f"{prefix}/{d}served/{LEVEL}{x}")
    s = round(monotonic() - t0, 1)
    base_rows = "skipped" if runs_only else f"{docs['']['rows']:,} rows"
    err(f"backfill-level {gen}: base {base_rows}, {len(todo_runs)} of {len(runs)} runs in {s}s")
    return {"gen": gen, "base": docs.get(""), "runs": {k: v for k, v in docs.items() if k}, "skipped_runs": [r for r in runs if r not in todo_runs], "s": s}


@command("backfill-level")
@option("-g", "--gen", required=True, help="The generation to add the level sort to (`<store>/interval-store/GEN/`): only `served/level.*` (and each listed run's) are written")
@option("-M", "--mem", default="4GB", help="Memory budget of the whole process tree (`tree_mem`): DuckDB gets what this process, the cut's workers and the Arrow buffers leave")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`), the readers' INDEX_R2 root")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-r", "--rg-rows", type=IntRange(min=1), help="Rows per served row group (default the store's)")
@option("-R", "--runs-only", is_flag=True, help="Only the listed runs that lack the sort (the base has it)")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after); a URL store's range files are mirrored into it")
def backfill_cmd(gen, mem, store_root, threads, rg_rows, runs_only, work) -> None:
    """Add the level sort (`served/level.*`, specs/diff-progressive.md §3b) to an existing generation that carries churn,
    and to each run its newest manifest lists: cut from the base's folded path versions and the runs' deltas. Never moves
    `CURRENT.json` and never rewrites a file; later appends and compactions carry the sort. Prints a JSON summary."""
    from .blobstore import Store
    from .static_local import _check_gen, workdir

    _check_gen(gen)
    with workdir(work) as w:
        print(json.dumps(backfill(Store(store_root), gen, w, rg_rows=rg_rows, threads=threads, mem=mem, runs_only=runs_only), indent=1))
