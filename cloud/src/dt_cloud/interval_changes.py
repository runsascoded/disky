"""Change counters per scan (specs/change-metrics.md §3.1, §10): the objects each scan added, deleted and modified since
the scan before it, at the store root and each depth-1 path, with their bytes.

An object (a `kind = 'file'` path version) is **added** at scan t where a version of its path opens at t and none closed
there, **deleted** where one closed at t and none opened, and **modified** where one closed and one opened whose identity
differs: both carry a version token (`version`, or the legacy `generation`) and the tokens differ; else (a token NULL)
the sizes differ, or the mtimes do where they are write times (`mtime: "write"`; `"coarse"`, as hf's repo-wide
`lastModified`, never decides). Anything else that opens a version (a read-day split, an owner or class change, a churn
split, a NULL → token transition with the same size and mtime) is a touch, not counted. A file ↔ directory flip is the
file row's add or delete.

The same events come from any path versions where each version is opened at its `vf` and closed at its `vt` (≠ `OPEN`):
a run's deltas (opened: `op = 1`; closed: every row with a `vt`, the close records and, in a merged run, versions it
both opened and closed) and a generation's folded pieces (every piece opened; consecutive pieces of one version meet,
a touch). One query (`events_sql`) serves the append, the build and the backfill.

**Where they live.** A scan's counters are an entry `{since, mtime, paths: {"": {...}, "<depth-1 path>": {...}}}` (the
root always, a depth-1 path where any count is nonzero), keyed by scan id:
- an appended scan's in its run's `meta.json` (`"changes"`), a merged run's the union of its inputs', and every listed
  run's in the manifest (`"changes"`, as its `stamps`): what the reader reads, no file more;
- a generation's own (its build's, a compaction's carried from the base and the runs, a backfill's) in
  `<gen>/changes.json` (`{"v": 1, "scans": {...}}`), written once beside its other files.

Counting starts after the churn base (`interval_churn.churn_from`): a scan at or before it (and the first) has no entry,
absent rather than zero.
"""
from __future__ import annotations

import json
from pathlib import Path
from time import monotonic

from click import Choice, IntRange, command, option

from . import interval_store as ist
from .static_names import OPEN, err, q

#: The key in a run's `meta.json` and a manifest: `{scan id: entry}`.
KEY = "changes"
#: A generation's own counters: `<gen>/changes.json`.
DOC = "changes.json"
VERSION = 1
#: What a scan's mtimes say about a rewrite: `write` (GCS `timeCreated`, S3 / R2 `LastModified`: every write moves it),
#: `coarse` (hf's repo-wide dates: never decides a mod).
MTIMES = ("write", "coarse")
MTIME_DEFAULT = "write"
MTIME_HELP = ("What the scans' mtimes say about a rewrite (the change counters, specs/change-metrics.md §1.2): `write` (every "
              "write moves it: GCS, S3, R2) or `coarse` (a date shared by many objects, e.g. hf's repo `lastModified`: a mod is "
              "then decided by a version token, else the size)")
COUNTS = ("added", "deleted", "modified")
BYTES = ("added_bytes", "deleted_bytes", "modified_bytes")
#: How a mod was decided: both tokens, else the size, else the mtime.
BASES = ("token", "size", "mtime")


def token_sql(identity: ist.Identity) -> str:
    """The version token as VARCHAR: the generation's identity column (`version`, or the legacy `generation`), NULL
    where it has none."""
    return f"CAST({ist.ident(ist.identity_column(identity))} AS VARCHAR)" if identity else "NULL::VARCHAR"


def events_sql(src: str, opened: str, *, tok: str, mtime: str) -> str:
    """The file events in `src` (path versions: `depth, path, vf, vt, kind, size, mtime`, and `tok` the version token as
    SQL), the versions passing `opened` (SQL) opened at their `vf`, every one with a `vt` closed there: `(t, path, event,
    basis, size, psize)` per path and stamp `t` where something opened or closed; `event` NULL for a touch."""
    if mtime not in MTIMES:
        raise ValueError(f"mtime: one of {MTIMES}, not {mtime!r}")
    write = "true" if mtime == "write" else "false"
    return f"""WITH f AS (SELECT depth, path, vf, vt, size, mtime, {tok} AS tok, ({opened}) AS opened FROM {src} WHERE kind = 'file'),
        o AS (SELECT depth, path, vf AS t, size, mtime, tok FROM f WHERE opened),
        c AS (SELECT depth, path, vt AS t, size, mtime, tok FROM f WHERE vt <> {OPEN})
        SELECT coalesce(o.t, c.t) AS t, coalesce(o.path, c.path) AS path,
            CASE WHEN c.path IS NULL THEN 'added' WHEN o.path IS NULL THEN 'deleted'
                 WHEN o.tok IS NOT NULL AND c.tok IS NOT NULL THEN CASE WHEN o.tok <> c.tok THEN 'modified' END
                 WHEN o.size <> c.size OR ({write} AND o.mtime <> c.mtime) THEN 'modified' END AS event,
            CASE WHEN o.tok IS NOT NULL AND c.tok IS NOT NULL THEN 'token' WHEN o.size <> c.size THEN 'size' ELSE 'mtime' END AS basis,
            o.size AS size, c.size AS psize
        FROM o FULL JOIN c ON o.depth = c.depth AND o.path = c.path AND o.t = c.t"""


def counts_sql(events: str, stamps: list[int]) -> str:
    """`events` (`events_sql`) at `stamps`, summed per stamp at the root (`''`) and each file's depth-1 path."""
    agg = [
        *(f"count(*) FILTER (WHERE event = '{e}')::BIGINT AS {e}" for e in COUNTS),
        "coalesce(sum(size) FILTER (WHERE event = 'added'), 0)::BIGINT AS added_bytes",
        "coalesce(sum(psize) FILTER (WHERE event = 'deleted'), 0)::BIGINT AS deleted_bytes",
        "coalesce(sum(size) FILTER (WHERE event = 'modified'), 0)::BIGINT AS modified_bytes",
        *(f"count(*) FILTER (WHERE event = 'modified' AND basis = '{b}')::BIGINT AS by_{b}" for b in BASES),
    ]
    within = ", ".join(str(int(t)) for t in stamps) or "NULL"
    return f"""SELECT t, p, {', '.join(agg)}
        FROM (SELECT t, event, basis, size, psize, unnest(['', split_part(path, '/', 1)]) AS p FROM ({events})
              WHERE event IS NOT NULL AND t IN ({within}))
        GROUP BY t, p ORDER BY t, p"""


def zero() -> dict:
    """A path's counters with nothing changed."""
    return {**{k: 0 for k in (*COUNTS, *BYTES)}, "mods_by": {b: 0 for b in BASES}}


def counters(con, sources: list[tuple[str, str]], want: dict[int, tuple[str, str]], *, identity: ist.Identity, mtime: str) -> dict[str, dict]:
    """The counters (`{scan id: entry}`) of the scans `want` names (stamp → `(scan id, the scan before it)`), from
    `sources` (`(relation, opened predicate)`: a generation's pieces, `true`; a run's deltas, `op = 1`), unioned: a
    version a run closes may have opened in the base. Every wanted scan has an entry, its root's counters at least."""
    if not want:
        return {}
    cols = f"depth, path, vf, vt, kind, size, mtime, {token_sql(identity)} AS tok"
    src = " UNION ALL ".join(f"SELECT {cols}, ({opened}) AS opened FROM {rel}" for rel, opened in sources)
    rows = con.execute(counts_sql(events_sql(f"({src})", "opened", tok="tok", mtime=mtime), sorted(want))).fetchall()
    out = {sid: {"since": since, "mtime": mtime, "paths": {"": zero()}} for _, (sid, since) in sorted(want.items())}
    for t, p, *vals in rows:
        n = dict(zip((*COUNTS, *BYTES), vals[:6]))
        out[want[t][0]]["paths"][p] = {**n, "mods_by": dict(zip(BASES, vals[6:]))}
    return out


def counts(prev: dict, scan: dict) -> bool:
    """Whether `scan` gets counters after `prev` (`scans.json` entries): both list objects (`version` ≥ 2). Not the first
    scan, nor where objects are first listed (`interval_churn.counts_at`: every object would appear), nor a
    directory-only scan: counting starts after the churn base."""
    return prev.get("version", 1) >= 2 and scan.get("version", 1) >= 2


def wanted(scans: list[dict]) -> dict[int, tuple[str, str]]:
    """The scans of `scans` (`scans.json` entries, in order) that get counters (`counts`), stamp → `(id, the scan before
    it)`."""
    return {int(s["ts"]): (s["id"], p["id"]) for p, s in zip(scans, scans[1:]) if counts(p, s)}


def pieces(root: str | Path) -> str:
    """A generation's folded path versions (`pvl/` range files under `root`)."""
    return f"read_parquet({q(f'{root}/pvl/r*.parquet')})"


def deltas(root: str | Path) -> str:
    """A run's `pvl` deltas under `root`."""
    return f"read_parquet({q(f'{root}/pvl/r*.parquet')})"


def gen_counters(con, root: str | Path, scans: list[dict], *, identity: ist.Identity, mtime: str) -> dict[str, dict]:
    """A generation's counters from its folded pieces (`root/pvl/`), for its scans past the churn base (`wanted`)."""
    return counters(con, [(pieces(root), "true")], wanted(scans), identity=identity, mtime=mtime)


def run_counters(con, root: str | Path, scan: dict, since: str, *, identity: ist.Identity, mtime: str) -> dict[str, dict]:
    """An appended scan's counters from its run's `pvl` deltas (`root`), against `since`, the scan before it."""
    return counters(con, [(deltas(root), "op = 1")], {int(scan["ts"]): (scan["id"], since)}, identity=identity, mtime=mtime)


def doc(scans: dict[str, dict]) -> dict:
    """`changes.json` over `scans` (`{scan id: entry}`)."""
    return {"v": VERSION, "scans": dict(sorted(scans.items()))}


def dumps(d: dict) -> str:
    return json.dumps(d, indent=1, sort_keys=True) + "\n"


def write_doc(path: Path, scans: dict[str, dict]) -> bool:
    """`changes.json` at `path` when `scans` holds any; whether it was written."""
    if not scans:
        return False
    path.write_text(dumps(doc(scans)))
    return True


def read_doc(path: Path) -> dict[str, dict]:
    """A generation's own counters (`changes.json` under it), `{}` without one."""
    return json.loads(path.read_text())["scans"] if path.is_file() else {}


def carried(gen_dir: Path, runs: list[dict]) -> dict[str, dict]:
    """What a fold of `gen_dir` (its base and `runs`: a compaction, a backfill into a new generation) carries into the new
    generation's `changes.json`: the base's own (`changes.json`) and each run's (`meta.json` `"changes"`)."""
    out = dict(read_doc(gen_dir / DOC))
    for r in runs:
        meta = gen_dir / r["key"] / "meta.json"
        if meta.is_file():
            out.update(json.loads(meta.read_text()).get(KEY) or {})
    return out


def merge(entries: list[dict], since: str) -> dict:
    """Consecutive scans' entries as one against `since`: the gross counts sum (per path, and how mods were decided)."""
    paths: dict[str, dict] = {}
    for e in entries:
        for p, c in e["paths"].items():
            t = paths.setdefault(p, zero())
            for k in (*COUNTS, *BYTES):
                t[k] += c[k]
            for b in BASES:
                t["mods_by"][b] += c["mods_by"][b]
    return {"since": since, "mtime": entries[-1]["mtime"], "paths": dict(sorted(paths.items()))}


def rebase(counters: dict[str, dict], order: list[str], keep: set[str]) -> dict[str, dict]:
    """`counters` over the scans `order` lists, for the scans in `keep` (a generation thinned to them): a kept scan's entry
    is its own plus those of the dropped scans since the kept one before it (gross counts add over scan pairs), against
    that kept scan; none where one of them has no counters, nor for the first kept scan. Keeping every scan changes
    nothing."""
    out: dict[str, dict] = {}
    pend: list[dict | None] = []
    prev: str | None = None
    for sid in order:
        e = counters.get(sid)
        if sid not in keep:
            pend.append(e)
            continue
        if e is not None and prev is not None and all(x is not None for x in pend):
            out[sid] = merge([*pend, e], prev) if pend else e  # type: ignore[list-item]
        pend, prev = [], sid
    return out


def union(metas: list[dict]) -> dict[str, dict] | None:
    """A merged run's counters: its inputs' (`meta.json` `"changes"`), None when none had any."""
    if not any(KEY in m for m in metas):
        return None
    out: dict[str, dict] = {}
    for m in metas:
        out.update(m.get(KEY) or {})
    return dict(sorted(out.items()))


# ── backfill-changes: a generation's counters, where its appends wrote none ──


def backfill(store, gen: str, work: Path, *, mtime: str, threads: int = 4, mem: str = "4GB", also: list = ()) -> dict:
    """`<gen>/changes.json` for generation `gen` of `store`: the counters of every scan past the churn base that its newest
    manifest doesn't carry (its base's, and runs appended before the counters), from the base's folded pieces and the
    listed runs' deltas, unioned. Refused when the generation has one; never rewrites a file (also written to each store
    of `also`, e.g. the R2 copy, refused there likewise). Returns a summary."""
    from . import interval_append as ia
    from . import static_names as sn
    from . import tree_mem as tm
    from .append_runner import latest_key
    from .blobstore import mirror

    t0 = monotonic()
    prefix = f"{ist.PREFIX}/{gen}"
    for s in (store, *also):
        if s.exists(f"{prefix}/{DOC}"):
            raise SystemExit(f"{s.root}/{prefix}/{DOC} exists: a generation's counters are written once")
    base = store.read_json(f"{prefix}/scans.json")
    names = [k.removeprefix(f"{prefix}/") for k in store.keys(f"{prefix}/manifests/")]
    newest = latest_key(names)
    m = store.read_json(f"{prefix}/{newest}") if newest else {}
    runs = m.get("runs", [])
    have = set(m.get(KEY) or {})
    read = lambda key: store.read_json(f"{prefix}/{key}")  # noqa: E731
    entries = [*base["scans"], *({**ia.scan_entry(read, base, sid), "ts": m["stamps"][sid]} for r in runs for sid in r["scans"])]
    want = {t: v for t, v in wanted(entries).items() if v[0] not in have}
    keep = ("pvl/", *(f"{r['key']}/pvl/" for r in runs))
    if store.url:
        src = work / "src"
        mirror(store, prefix, src, keep=lambda rel: rel.startswith(keep))
    else:
        src = Path(store.root) / prefix
    tree = tm.Budget.measure(sn.mem_bytes(mem))
    con = sn.connect(threads, tm.stage_mem(tree, f"backfill-changes {gen}"), work / "duckdb")
    sources = [(pieces(src), "true"), *((deltas(src / r["key"]), "op = 1") for r in runs)]
    got = counters(con, sources, want, identity=ist.has_generation(base), mtime=mtime)
    if got:
        text = dumps(doc(got))
        for s in (store, *also):
            s.write_text(f"{prefix}/{DOC}", text)
    s = round(monotonic() - t0, 1)
    err(f"backfill-changes {gen}: {len(got)} scans counted ({len(have)} already in {newest or 'no manifest'}) in {s}s")
    return {"gen": gen, "scans": sorted(got), "carried": sorted(have), "runs": [r["key"] for r in runs], "written": bool(got), "s": s}


@command("backfill-changes")
@option("-a", "--also", "also_roots", multiple=True, help="Also write the file to this copy of the store (e.g. the R2 copy of a GCS store); repeat")
@option("-g", "--gen", required=True, help="The generation (`<store>/interval-store/GEN/`): only `changes.json` is written")
@option("-M", "--mem", default="4GB", help="Memory budget of the whole process tree (`tree_mem`): DuckDB gets what this process leaves")
@option("-o", "--out", "store_root", required=True, help="The store: a dir (a bucket's mount reads in place) or URL (`r2://…`, `s3://…`, `gs://…`: its range files mirrored)")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-T", "--mtime", type=Choice(MTIMES), default=MTIME_DEFAULT, envvar="INTERVAL_STORE_MTIME", show_envvar=True, help=MTIME_HELP)
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after)")
def backfill_cmd(also_roots, gen, mem, store_root, threads, mtime, work) -> None:
    """Write generation GEN's change counters (`changes.json`, specs/change-metrics.md §3.1): every scan past the churn
    base that its newest manifest doesn't carry, from its folded path versions and its listed runs' deltas. Never moves
    `CURRENT.json` and never rewrites a file: later appends carry their own counters, and compactions both. Prints a JSON
    summary."""
    from . import tree_mem as tm
    from .blobstore import Store
    from .static_local import _check_gen, workdir

    _check_gen(gen)
    with tm.watch(mem, f"backfill-changes {gen}"), workdir(work) as w:
        print(json.dumps(backfill(Store(store_root), gen, w, mtime=mtime, threads=threads, mem=mem, also=[Store(a) for a in also_roots]), indent=1))


def note(label: str, entry: dict) -> str:
    """A scan's root counters as a log line: `+812 −40 ~113 objects`."""
    r = entry["paths"][""]
    return f"{label}: +{r['added']:,} −{r['deleted']:,} ~{r['modified']:,} objects"
