"""One scan's per-scan sorts materialized from the interval store (specs/lean-scans.md §1.2 B3, prerequisite P1):
`dt-cloud interval-store export -o STORE SCAN_ID DEST`.

The store holds every scan's rows as versions (`interval_store` §2.1): with the owner axis the slice table `sv`, one
row per `(depth, path, usr)` — the per-scan sort's own rows — else the path table `pvl`, one row per `(depth, path)`.
A scan D's rows are the versions live at D (`vf ≤ t_D < vt`) over the base's served sort and every run's (the
reader's `combineLive`: a version's rows across tiers are one, the smallest `vt`). The served sorts are read, not the
range files, so an export works from any copy of the store (R2 holds the served files only).

Written in the scan's own source format (`scans.json` `version`):
- **v2** (objects + dirs): the layer-2 columns the stores and the per-scan readers read (`path`, `usr` where the
  generation is sliced, `size`, `mtime`, `kind`, `depth`, `n_files`, `n_children`, `n_desc`, `mtime_mean`,
  `last_read`, `sum_storage_class_id_{2,3,4}`), cut by the engine's `write_tiers` into `path-index.parquet`
  (+ `path-index-bysize.parquet`), as `path-index` / `index-write` cut them: the same rows in the same orders, 8K-row
  groups. Columns no store carries are not reproduced (`created`, `uri`, classes other than 2–4: B5). A generation
  that carries the objects' `generation` (`scans.json` `"generation": true`, gcs) exports it as the last column, from
  the `path` sort's version live at the scan (on a sliced generation, joined to the slices by path): the version's,
  so a scan from before the lister recorded it gets the one listed later within the same version.
- **v1** (dirs only): `path`, `depth`, `usr`, `b`, `o`, `wts`, `wb`, `c2`, `c3`, `c4`, `a`, sorted
  `(depth, path, usr)`: `path-index.parquet` only (a v1 index had coarse tiers, not `bysize`).

Exact in the store's terms: the export's rows, read back as the store reads a scan (`interval_store.slice_rows_sql` /
`path_rows_sql`), have the original sort's count and Σ hash (`test_interval_export.py`, every fixture scan) — what
every append already checks per range. A v2 row's mean stamp weighs its whole size or nothing (`wb` ∈ {0, size});
a version that says otherwise can't be written as v2 and fails the export.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from click import IntRange, argument, command, option

from . import interval_compact as ic
from . import interval_store as ist
from .append_runner import latest_key
from .blobstore import Store
from .static_names import connect, err, q

PREFIX = ist.PREFIX
#: The v2 export's columns, from a version row (`sv` or `pvl` columns): the layer-2 shape `write_tiers` takes (`path`,
#: then the labels, then `size`).
V2_COLS = [
    ("size", "size"),
    ("mtime", "nullif(mtime, -1)"),
    ("kind", "kind"),
    ("depth", "depth::INTEGER"),
    ("n_files", "n_files"),
    ("n_children", "nullif(n_children, -1)"),
    ("n_desc", "nullif(n_desc, -1)"),
    ("mtime_mean", "CASE WHEN wb > 0 THEN wts / wb END"),
    ("last_read", "nullif(last_read, -1)"),
    *((f"sum_storage_class_id_{k}", c) for k, c in ist.CLASS_COLS.items()),
]
V1_COLS = [
    ("depth", "depth::INTEGER"), ("usr", "usr"), ("b", "size"), ("o", "n_files"), ("wts", "wts"), ("wb", "wb"),
    *((f"c{k}", c) for k, c in ist.CLASS_COLS.items()), ("a", "nullif(last_read, -1)"),
]
TIERS = ("path", "bysize")
#: Each tier's file in a scan's index dir (`index_footer.STORE_SORTS`).
TIER_FILES = {"path": "path-index.parquet", "bysize": "path-index-bysize.parquet"}


def scan_entry(store: Store, prefix: str, scan: str) -> tuple[dict, list[dict]]:
    """Scan `scan`'s entry (`id`, `ts`, `version`) in the generation at `prefix`, and the newest manifest's runs: from
    the base's `scans.json`, else the manifest's `stamps` (the version from the run's `deltas/<id>/scans.json`, kept on
    GCS and by one-machine stores; an appended scan is v2 when that is gone, appends having begun after the v2 cutover).
    Raises when the generation doesn't hold the scan."""
    base = store.read_json(f"{prefix}/scans.json")
    newest = latest_key([k.removeprefix(f"{prefix}/") for k in store.keys(f"{prefix}/manifests/")])
    m = store.read_json(f"{prefix}/{newest}") if newest else None
    runs = m["runs"] if m else []
    for s in base["scans"]:
        if s["id"] == scan:
            return {"id": scan, "ts": s["ts"], "version": s.get("version", 2)}, runs
    if m and scan in m.get("stamps", {}):
        doc = f"{prefix}/deltas/{scan}/scans.json"
        version = next((s.get("version", 2) for s in store.read_json(doc)["scans"] if s["id"] == scan), 2) if store.exists(doc) else 2
        return {"id": scan, "ts": m["stamps"][scan], "version": version}, runs
    raise SystemExit(f"{store.uri(prefix)}: holds no scan {scan}")


def local_files(store: Store, keys: list[str], work: Path) -> list[str]:
    """`keys` as local paths: in place for a local store, else copied into `work` (keys kept)."""
    if not store.url:
        return [str(Path(store.root) / k) for k in keys]
    out = []
    for k in keys:
        dst = work / k
        if not (dst.is_file() and dst.stat().st_size == store.size(k)):
            store.get(k, dst)
        out.append(str(dst))
    return out


def live_sql(files: list[str], sliced: bool, ts: int) -> str:
    """The versions live at `ts` over the served sorts `files` (base first, then runs): a version's rows across tiers are
    one (`IDENT`), the smallest `vt` (a run's close record over the row it closes)."""
    ident = "depth, path, usr, vf" if sliced else "depth, path, vf"
    union = " UNION ALL ".join(f"SELECT * FROM read_parquet({q(f)})" for f in files)
    return f"""SELECT * FROM (SELECT * FROM ({union}) QUALIFY row_number() OVER (PARTITION BY {ident} ORDER BY vt) = 1)
        WHERE vf <= {ts} AND {ts} < vt"""


def store_gen(store: Store, gen: str | None) -> str:
    """The generation a read names: `gen`, else the store's current; followed to its newest successor."""
    if gen is None:
        gen, how = ic.resolve(store)
        if gen is None:
            raise SystemExit(f"{store.root}/{PREFIX}: no current generation ({how}): name one with -g")
    return ic.chain(store, gen)[-1]


#: An object identity row (`object_identities`): what a sweep's hold compares (`sweep_manifest.AsOfHold.ref`).
IDENTITY_COLS = ["depth", "path", "vf", "vt", "kind", "mtime", "wts", "wb"]


def _groups(store: Store, key: str, spans: list[tuple[str, str]], ts: int) -> list[int]:
    """The row groups of the served sort `key` (from its `.groups.parquet`) whose paths may meet a `[lo, hi]` span and
    whose versions may be live at `ts`."""
    g = pq.read_table(store.open(key.removesuffix(".parquet") + ".groups.parquet"),
                      columns=["rg", "p_min", "p_max", "vf_min", "vt_max"]).to_pylist()
    return [r["rg"] for r in g if r["vf_min"] <= ts < r["vt_max"] and any(r["p_max"] >= lo and r["p_min"] <= hi for lo, hi in spans)]


def object_identities(store_root: str, scan: str, bucket: str, bands: list[str] | tuple[str, ...], exact: list[str] | tuple[str, ...] = (),
                      *, gen: str | None = None) -> pa.Table:
    """The objects of `bucket` under `bands` (name prefixes) or named by `exact` as scan `scan` saw them, from the
    interval store at `store_root` (generation `gen`, default the current): `(name, generation, created)` sorted by
    name — the versions live at the scan in the `path` sort (base and the newest manifest's runs, the smallest `vt`),
    objects only (a key that is also a folder's prefix is folded into the folder: never listed). `generation` is the
    version's (NULL without one, or where the generation doesn't carry it); `created` the object's created second (its
    mean stamp: one object, `wts / wb`), else its `mtime`. Reads only the row groups whose paths meet the items (each
    sort's `.groups.parquet`): what a sweep's hold needs once the scan's raw listing is gone."""
    import duckdb

    store = Store(store_root)
    gen = store_gen(store, gen)
    prefix = f"{PREFIX}/{gen}"
    entry, runs = scan_entry(store, prefix, scan)
    base = store.read_json(f"{prefix}/scans.json")
    has_gen = ist.has_generation(base)
    ts = int(entry["ts"])
    spans = [(f"{bucket}/{b}", f"{bucket}/{b}\U0010ffff") for b in bands] + [(f"{bucket}/{k}", f"{bucket}/{k}") for k in exact]
    cols = [*IDENTITY_COLS, *([ist.GENERATION] if has_gen else [])]
    parts = []
    for key in [f"{prefix}/served/{ist.GENERATION_SORT}.parquet", *(f"{prefix}/{r['key']}/served/{ist.GENERATION_SORT}.parquet" for r in runs)]:
        if rgs := _groups(store, key, spans, ts):
            parts.append(pq.ParquetFile(store.open(key)).read_row_groups(rgs, columns=cols))
    out_schema = pa.schema([("name", pa.string()), ("generation", pa.int64()), ("created", pa.timestamp("us", tz="UTC"))])
    if not parts:
        return out_schema.empty_table()
    con = duckdb.connect()
    con.register("rows", pa.concat_tables(parts))
    gcol = ist.GENERATION if has_gen else "NULL::BIGINT"
    under = " OR ".join([*(f"starts_with(path, {q(lo)})" for lo, _ in spans[:len(bands)]), *(f"path = {q(lo)}" for lo, _ in spans[len(bands):])])
    t = con.execute(f"""SELECT substr(path, {len(bucket) + 2}) AS name, {gcol} AS generation,
            make_timestamp((CASE WHEN wb > 0 THEN round(wts / wb, 0) ELSE mtime END)::BIGINT * 1000000) AS created
        FROM (SELECT * FROM rows QUALIFY row_number() OVER (PARTITION BY depth, path, vf ORDER BY vt) = 1)
        WHERE vf <= {ts} AND {ts} < vt AND kind = 'file' AND ({under or 'false'})
        ORDER BY name""").to_arrow_table()
    return t.cast(out_schema)


def export_scan(store_root: str, scan: str, dest: str, *, gen: str | None = None, tiers: tuple[str, ...] = TIERS,
                work: Path | None = None, threads: int = 4, mem: str = "4GB") -> dict:
    """Scan `scan`'s per-scan sorts from the store at `store_root` (generation `gen`, default the store's current,
    followed to its newest successor) into `dest` (a dir or URL): `path-index.parquet` (+ `path-index-bysize.parquet`
    for a v2 scan, when `tiers` names it). Returns `{scan, gen, version, sliced, rows, files: {name: bytes}}`."""
    from disk_tree.find.tiers import write_tiers

    store = Store(store_root)
    if gen is None:
        gen, how = ic.resolve(store)
        if gen is None:
            raise SystemExit(f"{store.root}/{PREFIX}: no current generation ({how}): name one with -g")
    gen = ic.chain(store, gen)[-1]
    prefix = f"{PREFIX}/{gen}"
    entry, runs = scan_entry(store, prefix, scan)
    base = store.read_json(f"{prefix}/scans.json")
    sliced = ist.sliced(base)
    # The objects' native version (a v2 scan of a generation that carries it): from the path sort's live versions.
    generation = ist.has_generation(base) and entry["version"] == 2
    sort = "slices" if sliced else "path"
    own = work is None
    work = Path(tempfile.mkdtemp(prefix="iv-export-")) if own else work
    try:
        def served(sort: str) -> list[str]:
            keys = [f"{prefix}/served/{sort}.parquet", *(f"{prefix}/{r['key']}/served/{sort}.parquet" for r in runs)]
            return local_files(store, keys, work / "store")

        con = connect(threads, mem, work / "duckdb")
        con.execute(f"CREATE TABLE live AS {live_sql(served(sort), sliced, entry['ts'])}")
        if generation and sliced:
            gen_of = f"SELECT depth, path, {ist.GENERATION} FROM ({live_sql(served(ist.GENERATION_SORT), False, entry['ts'])})"
            con.execute(f"CREATE OR REPLACE TABLE live AS SELECT l.*, g.{ist.GENERATION} FROM live l LEFT JOIN ({gen_of}) g USING (depth, path)")
        usr = ["nullif(usr, '') AS usr"] if sliced else []
        out = work / "out"
        out.mkdir(parents=True, exist_ok=True)
        if entry["version"] == 2:
            bad = con.execute("SELECT count(*) FROM live WHERE wb <> 0 AND wb <> size").fetchone()[0]
            if bad:
                raise SystemExit(f"export {scan}: {bad} versions weigh their mean stamp over part of their size; not a v2 scan's rows")
            sel = ", ".join(["path", *usr, *(f"{e} AS {c}" for c, e in V2_COLS), *([ist.GENERATION] if generation else [])])
            raw = work / "raw.parquet"
            con.execute(f"COPY (SELECT {sel} FROM live ORDER BY depth, path{', usr' if sliced else ''}) TO {q(str(raw))} (FORMAT PARQUET)")
            wanted = tuple(t for t in TIERS if t in tiers)
            written = write_tiers(str(raw), str(out / "export"), tiers=wanted, mem=mem, threads=threads, tmp_dir=str(work / "tiers-tmp"))
            names = {}
            for t in wanted:
                os.replace(out / f"export.{t}.parquet", out / TIER_FILES[t])
                names[TIER_FILES[t]] = (out / TIER_FILES[t]).stat().st_size
            rows = next(iter(written.values()))
        else:
            cols = V1_COLS if sliced else [(c, "NULL::VARCHAR" if c == "usr" else e) for c, e in V1_COLS]
            sel = ", ".join(["path", *(f"{e} AS {c}" for c, e in cols)])
            dst = out / TIER_FILES["path"]
            con.execute(f"COPY (SELECT {sel} FROM live ORDER BY depth, path, usr NULLS FIRST) TO {q(str(dst))} "
                        "(FORMAT PARQUET, ROW_GROUP_SIZE 8192)")
            rows = con.execute(f"SELECT count(*) FROM read_parquet({q(str(dst))})").fetchone()[0]
            names = {dst.name: dst.stat().st_size}
        dst_store = Store(dest)
        for name in names:
            dst_store.put(out / name, name)
        return {"scan": scan, "gen": gen, "version": entry["version"], "sliced": sliced, "rows": rows, "files": names}
    finally:
        if own:
            shutil.rmtree(work, ignore_errors=True)


@command("export")
@option("-g", "--gen", help="The generation (default: the store's current, `interval-store/CURRENT.json`), followed to its newest successor")
@option("-M", "--mem", default="4GB", help="DuckDB memory limit")
@option("-o", "--out", "store_root", required=True, help="The store: a dir or URL (`r2://bucket[/prefix]`, `s3://…`, `gs://…`), the readers' INDEX_R2 root")
@option("-p", "--threads", default=4, type=IntRange(min=1), help="DuckDB threads")
@option("-t", "--tiers", default="path,bysize", help="Sorts to write (a v2 scan: `path`, `bysize`; a v1 scan has `path` only)")
@option("-w", "--work", help="Local work dir (default: a temporary one, removed after)")
@argument("scan")
@argument("dest")
def export_cmd(gen, mem, store_root, threads, tiers, work, scan, dest) -> None:
    """SCAN's per-scan sorts, materialized from the interval store into DEST (a dir or URL): `path-index.parquet` and
    (a v2 scan) `path-index-bysize.parquet`, the rows `path-index` / `index-write` wrote for it in the stores' columns.
    Prints a JSON summary."""
    want = tuple(t for t in tiers.split(",") if t)
    if bad := [t for t in want if t not in TIERS]:
        raise SystemExit(f"export: unknown tier(s) {bad} (one of {', '.join(TIERS)})")
    doc = export_scan(store_root, scan, dest, gen=gen, tiers=want, work=Path(work) if work else None, threads=threads, mem=mem)
    err(f"export {scan} (gen {doc['gen']}, v{doc['version']}): {doc['rows']:,} rows → {dest}: "
        + ", ".join(f"{n} {b:,} B" for n, b in doc["files"].items()))
    print(json.dumps(doc))
