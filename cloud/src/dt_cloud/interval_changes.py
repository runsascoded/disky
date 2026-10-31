"""Transitions (specs/diff-from-changes.md §1–2): one row per path per scan at which its diff columns changed, both
sides on the row, for a Δ-mode diff that reads only what changed between two scans.

A version opening at `b` (`vf = b`) is the transition's new side, a version closing at `b` (`vt = b`) its old side:
joined on `(depth, path, b)`, a side missing where the path appeared (no close) or went away (no open). A version open
at the generation's first scan is no transition, nor is a close at `OPEN`. Transitions whose two sides agree on every
diff column (a change in `mtime`, `n_desc`, owners, read day … only) are dropped: their Δ is zero in everything a
diff draws, and the ones kept still chain (each kept one's old side is the previous kept one's new side, in those
columns).

A scan that first lists objects after scans that listed directories only (a v1 index's successor, `scans.json`
`version`) opens a version for every object that already existed: those openings are the format's, not changes (their
bytes were already in their directories' sizes), so a file appearing at such a scan is no transition (`listed_from`).
Its directories' transitions there stay, and its own later ones too (a file deleted after it reads as deleted).

Sorted `(b, ⌊log2 max(size, size_prev)⌋ desc, path)`, size 0 last: a range read takes a block of `b`, and inside it a
size floor and a path range prune as `bysize` does.
"""
from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from click import IntRange, command, option

from . import static_names as sn
from .interval_store import CHANGES, CLS, STORE_VERSION, write_groups
from .static_names import OPEN, connect, err

#: The columns a diff draws: what a transition carries on both sides.
DIFF_COLS = ["kind", "size", "n_files", *CLS]
#: Rows per row group (`SERVED_RG`'s size, the served sorts').
RG_ROWS = 8192
#: The served sort's name (`served/changes.parquet`).
SORT = CHANGES
#: A transition row: the new side's diff columns, then the old side's (`*_prev`); a missing side is NULL.
SCHEMA = pa.schema([
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("b", pa.int64(), nullable=False),
    pa.field("kind", pa.string(), nullable=False),
    *(pa.field(c, pa.int64()) for c in DIFF_COLS[1:]),
    *(pa.field(f"{c}_prev", pa.int64()) for c in DIFF_COLS[1:]),
])


def listed_from(scans: list[dict]) -> list[int]:
    """The epochs (`ts`) of the scans that first list objects after scans that didn't (`version` 1: dirs only)."""
    return [s["ts"] for p, s in zip(scans, scans[1:]) if p.get("version", 1) == 1 and s.get("version", 1) >= 2]


def transitions_sql(src: str, first: int, objects_from: list[int] = ()) -> str:
    """Transitions of the versions in relation `src` (`depth, path, vf, vt` + `DIFF_COLS`); `first` = the
    generation's first scan's epoch; `objects_from` = `listed_from`'s epochs, where files appearing are no transition."""
    unlisted = f" OR (c.path IS NULL AND o.kind = 'file' AND b IN ({', '.join(map(str, objects_from))}))" if objects_from else ""
    new = ", ".join(f"o.{c}" for c in DIFF_COLS[1:])
    old = ", ".join(f"c.{c} AS {c}_prev" for c in DIFF_COLS[1:])
    same = " AND ".join(f"o.{c} = c.{c}" for c in DIFF_COLS)
    return f"""
        WITH v AS (SELECT depth, path, vf, vt, {", ".join(DIFF_COLS)} FROM {src}),
        o AS (SELECT depth, path, vf AS b, {", ".join(DIFF_COLS)} FROM v WHERE vf > {first}),
        c AS (SELECT depth, path, vt AS b, {", ".join(DIFF_COLS)} FROM v WHERE vt < {OPEN})
        SELECT depth, path, b, coalesce(o.kind, c.kind) AS kind, {new}, {old}
        FROM o FULL OUTER JOIN c USING (depth, path, b)
        WHERE NOT ((o.path IS NOT NULL AND c.path IS NOT NULL AND {same}){unlisted})
    """


#: A transition's size key: the larger side (what the size floor tests, `b_min`/`b_max` in its group index).
TOP = "greatest(coalesce(size, 0), coalesce(size_prev, 0))"


def order_sql() -> str:
    """The `changes` sort's order: scan, then size bucket descending (size 0 last), then path."""
    return f"b, CASE WHEN {TOP} > 0 THEN floor(log2({TOP}))::INT ELSE -1 END DESC, path"


def _bounds(t: pa.Table) -> dict:
    """A group's `GROUPS_SCHEMA` bounds: its scan in the time columns (every one of `vf`/`vt` min and max: a group holds
    one scan's transitions), its size keys in `b`, segment 0."""
    def mm(c):
        r = pc.min_max(c)
        return r["min"].as_py(), r["max"].as_py()

    d, p, b = mm(t.column("depth")), mm(t.column("path")), mm(t.column("b"))
    top = pc.max_element_wise(pc.fill_null(t.column("size"), 0), pc.fill_null(t.column("size_prev"), 0))
    s = mm(top)
    return {"seg": 0, "d_min": d[0], "d_max": d[1], "p_min": p[0], "p_max": p[1], "b_min": s[0], "b_max": s[1],
            "u_min": None, "u_max": None, "vf_min": b[0], "vf_max": b[1], "vt_min": b[0], "vt_max": b[1], "rows": t.num_rows}


def write_changes(con, src: str, out: Path, first: int, objects_from: list[int] = (), *, rg_rows: int = RG_ROWS) -> dict:
    """The `changes` sort of the versions in `src` to `out` (+ `.groups.parquet`): `rg_rows`-row groups, cut where the
    scan changes so a group holds one scan's transitions (its time bounds are that scan)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    bounds: list[dict] = []
    sql = f"SELECT {', '.join(SCHEMA.names)} FROM ({transitions_sql(src, first, objects_from)}) ORDER BY {order_sql()}"
    with pq.ParquetWriter(out, SCHEMA, compression=sn.CODEC, use_dictionary=["kind"], write_statistics=["depth", "b"]) as w:
        pending: list[pa.Table] = []

        def flush(t: pa.Table, final: bool) -> pa.Table:
            off = 0
            while t.num_rows - off >= rg_rows or (final and off < t.num_rows):
                g = t.slice(off, min(rg_rows, t.num_rows - off))
                w.write_table(g, row_group_size=rg_rows)
                bounds.append(_bounds(g))
                off += g.num_rows
            return t.slice(off)

        cur = None
        for batch in sn._batches(con, sql):
            if not batch.num_rows:
                continue
            t = pa.Table.from_batches([batch]).cast(SCHEMA)
            bs = t.column("b").combine_chunks()
            m = len(bs)
            change = pc.indices_nonzero(pc.not_equal(bs.slice(1), bs.slice(0, m - 1))).to_pylist() if m > 1 else []
            cuts = [0, *(x + 1 for x in change), m]
            for a, z in zip(cuts, cuts[1:]):
                v = bs[a].as_py()
                if v != cur and pending:
                    rest = flush(pa.concat_tables(pending), True)
                    pending = [rest] if rest.num_rows else []
                cur = v
                pending.append(t.slice(a, z - a))
                if sum(x.num_rows for x in pending) >= rg_rows:
                    rest = flush(pa.concat_tables(pending).combine_chunks(), False)
                    pending = [rest] if rest.num_rows else []
        if pending:
            flush(pa.concat_tables(pending).combine_chunks(), True)
        w.add_key_value_metadata({"store": "interval", "version": str(STORE_VERSION), "sort": SORT, "order": order_sql(), "open": str(OPEN)})
    return write_groups(out, bounds, SORT, lambda rows: {"scans": {"groups": len({r["vf_min"] for r in rows}), "rows": sum(r["row_end"] - r["row_start"] for r in rows)}})


def cut_changes(con, src: str, out: Path, first: int, objects_from: list[int] = (), *, rg_rows: int = RG_ROWS) -> dict:
    """`write_changes`, and the transitions' counts by kind of change."""
    sd = write_changes(con, src, out, first, objects_from, rg_rows=rg_rows)
    rows, opens, closes, both = con.execute(
        "SELECT count(*), count(size), count(size_prev), count(*) FILTER (WHERE size IS NOT NULL AND size_prev IS NOT NULL)"
        f" FROM read_parquet('{out}')").fetchone()
    return {"rows": rows, "appeared": rows - closes, "went": rows - opens, "changed": both,
            "bytes": sd["bytes"], "groups": sd["groups"], "groups_bytes": sd["groups_bytes"]}


@command("changes")
@option("-s", "--scans", "scans_json", required=True, help="The generation's `scans.json`: its first scan (versions opened then are no transition) and where objects are first listed (`listed_from`)")
@option("-m", "--mem", default="16GB", help="DuckDB memory limit")
@option("-o", "--out", required=True, help="Output dir: one `<name>.parquet` per input")
@option("-t", "--threads", default=8, type=IntRange(min=1), help="DuckDB threads")
@option("-T", "--tmp", default=None, help="DuckDB spill dir")
@option("-r", "--rg-rows", default=RG_ROWS, type=IntRange(min=1), help="Rows per row group")
@option("-i", "--inputs", "inputs", multiple=True, required=True, help="A generation's version files (`pv/r####.parquet`), each cut on its own")
def changes_cmd(scans_json, mem, out, threads, tmp, rg_rows, inputs) -> None:
    """Cut each version file's transitions (the `changes` sort, specs/diff-from-changes.md) and report rows and bytes:
    one JSON line per input, then the totals."""
    doc = json.loads(Path(scans_json).read_text())
    scans = doc["scans"] if isinstance(doc, dict) else doc
    first, objects_from = scans[0]["ts"], listed_from(scans)
    o = Path(out)
    o.mkdir(parents=True, exist_ok=True)
    con = connect(threads, mem, tmp)
    tot: dict = {}
    for i in inputs:
        dst = o / Path(i).name
        s = {"input": i, **cut_changes(con, f"read_parquet('{i}')", dst, first, objects_from, rg_rows=rg_rows),
             "input_rows": pq.ParquetFile(i).metadata.num_rows, "input_bytes": Path(i).stat().st_size}
        print(json.dumps(s), flush=True)
        for k, v in s.items():
            if isinstance(v, int):
                tot[k] = tot.get(k, 0) + v
    err(f"changes: {len(inputs)} inputs")
    print(json.dumps({"total": tot}), flush=True)


def backfill_keys(store, prefix: str) -> list[str]:
    """The generation-relative dirs that serve a `changes` sort: `""` (the base), then each run the newest manifest lists."""
    from .append_runner import manifest_keys

    keys = manifest_keys([k.removeprefix(f"{prefix}/") for k in store.listing(f"{prefix}/manifests/")])
    return ["", *(r["key"] for r in (store.read_json(f"{prefix}/{keys[-1]}")["runs"] if keys else []))]


def served_prefix(prefix: str, key: str) -> str:
    """Dir `key`'s (`""`: the base) `changes` files' key prefix."""
    return f"{prefix}/{key + '/' if key else ''}served/{SORT}"


def r2_publish_changes(store, key_prefix: str, *, check: bool = False) -> list[str]:
    """`key_prefix`'s `changes` files on `store` (a `gs://` bucket) → R2 (`append_runner.r2_copy`, same keys),
    `changes.json` last; with `check`, only the keys R2 lacks (or holds differently), nothing copied."""
    from .append_runner import r2_copy, r2_missing, r2_objects

    uri = store.uri("")
    if not uri.startswith("gs://"):
        raise SystemExit(f"backfill-changes -R: {uri} is not a gs:// bucket (R2 is copied from one)")
    bucket = uri.removeprefix("gs://").split("/")[0]
    objs = r2_objects(bucket, [key_prefix])
    if check:
        return r2_missing(objs) if objs else [f"{key_prefix}.json"]
    body = [o for o in objs if not o.key.endswith(".json")]
    r2_copy(bucket, body)
    r2_copy(bucket, [o for o in objs if o.key.endswith(".json")], workers=1)
    if bad := r2_missing(objs):
        raise SystemExit(f"r2 {key_prefix}: {len(bad)} files not on R2 after the copy (e.g. {bad[0]})")
    return []


@command("backfill-changes")
@option("-b", "--bucket", help="The store: a bucket name (gs://) or URL, or a dir (default: the profile's)")
@option("-g", "--gen", required=True, help="Generation")
@option("-m", "--mount", help="Local mount of the store's bucket (the range and delta files are read from it)")
@option("-M", "--mem", default="80GB", help="DuckDB memory limit")
@option("-n", "--dry-run", is_flag=True, help="Print what is missing (on the store, and with -R on R2), cut and copy nothing")
@option("-o", "--out", default="/stage/out", help="Local output dir (uploaded, then removed)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-P", "--profile", help="Deployment profile JSON (default $INTERVAL_STORE_PROFILE)")
@option("-r", "--rg-rows", default=RG_ROWS, type=IntRange(min=1), help="Rows per served row group")
@option("-R", "--r2", is_flag=True, help="Also copy each dir's `changes` files to the serving R2 bucket ($R2_* credentials, as `r2_publish`), `changes.json` last: a GCS store's deployments serve from R2")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def backfill_changes_cmd(bucket, gen, mount, mem, dry_run, out, threads, profile, rg_rows, r2, tmp) -> None:
    """Add the `changes` sort (specs/diff-from-changes.md) to a generation cut before it: its base (from `pv/`) and each run
    its newest manifest lists (from the run's `pvl` deltas), beside their other served sorts. Additive: a sort already
    there is never rewritten, `changes.json` goes up last, so a rerun resumes; `-R` copies each to R2 as well (a sort on
    the store but not on R2 is copied, not cut again). Refuses a generation already compacted into another (its prune
    would leave what it adds). One JSON line per dir done."""
    import shutil

    from .blobstore import Store
    from .interval_append import cut_run_changes
    from .interval_store import PREFIX, _bucket, _root

    store = Store(_bucket(bucket, profile))
    prefix = f"{PREFIX}/{gen}"
    if store.exists(f"{prefix}/successor.json"):
        raise SystemExit(f"backfill-changes {prefix}: compacted into {store.read_json(f'{prefix}/successor.json')['gen']}; backfill that")
    keys = backfill_keys(store, prefix)
    cut = [k for k in keys if not store.exists(f"{served_prefix(prefix, k)}.json")]
    copy = [k for k in keys if k in cut or r2_publish_changes(store, served_prefix(prefix, k), check=True)] if r2 else []
    err(f"backfill-changes {prefix}: {len(keys)} dirs; {len(cut)} to cut" + (f", {len(copy)} to copy to R2" if r2 else ""))
    if dry_run:
        print(json.dumps({"gen": gen, "cut": cut, **({"r2": copy} if r2 else {})}), flush=True)
        return
    root = _root(store, prefix, mount)
    scans = store.read_json(f"{prefix}/scans.json")["scans"]
    con = connect(threads, mem, tmp) if cut else None
    for key in keys:
        doc = None
        # A compaction that finished since the start: its predecessor prune may already have run, so a file added now
        # would be left behind (the check above covers only one finished before).
        if (key in cut or key in copy) and store.exists(f"{prefix}/successor.json"):
            raise SystemExit(f"backfill-changes {prefix}: compacted into {store.read_json(f'{prefix}/successor.json')['gen']} meanwhile; stopped before "
                             f"{key or 'base'} (backfill that)")
        if key in cut:
            dst = Path(out) / (key or "base") / "served"
            dst.mkdir(parents=True, exist_ok=True)
            if key:
                doc = cut_run_changes(con, f"{root}/{key}", dst, rg_rows=rg_rows)
            else:
                doc = write_changes(con, f"read_parquet('{root}/pv/r*.parquet')", dst / f"{SORT}.parquet", scans[0]["ts"], listed_from(scans),
                                    rg_rows=rg_rows)
                (dst / f"{SORT}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
            store.put_tree(dst, f"{prefix}/{key + '/' if key else ''}served", last=(f"{SORT}.json",))
            shutil.rmtree(dst)
        if key in copy:
            r2_publish_changes(store, served_prefix(prefix, key))
        if doc or key in copy:
            print(json.dumps({"key": key or "base", **({k: doc[k] for k in ("rows", "groups", "bytes")} if doc else {}), "r2": key in copy}), flush=True)
