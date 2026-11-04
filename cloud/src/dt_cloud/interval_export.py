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

import io
import json
import os
import resource
import shutil
import sys
import tempfile
import time
from bisect import bisect_left, bisect_right
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from click import IntRange, argument, command, option

from . import interval_compact as ic
from . import interval_store as ist
from .append_runner import latest_key
from .blobstore import Store, _retrying
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
#: Row groups `object_identities` reads at once: one ranged GET plus one group's decode and filter each, so at most this
#: many unfiltered groups (8K rows) are in memory.
IDENTITY_READERS = 32
#: The fold's DuckDB limit, past which it spills to a temporary dir (the filtered rows themselves are Arrow, outside it).
IDENTITY_MEM = "16GB"


def _items(bucket: str, bands: Iterable[str], exact: Iterable[str]) -> tuple[list[str], frozenset[str]]:
    """The full-path forms of `bands` (sorted, prefix-free: a band inside another is dropped) and of the `exact` keys no
    band covers. A path's rows are wanted iff it is an exact key, or the largest band ≤ it is a prefix of it: any band
    that is a prefix of a path sorts ≤ it, and any string between a prefix and a path it prefixes has that prefix too, so
    with prefix-free bands no other band can sit between them. `str` order is code point order, which is UTF-8 byte
    order (the order `starts_with` and the files' sort compare in)."""
    out: list[str] = []
    for band in sorted({f"{bucket}/{b}" for b in bands}):
        if not (out and band.startswith(out[-1])):
            out.append(band)
    return out, frozenset(path for k in exact if not _band_of(path := f"{bucket}/{k}", out))


def _band_of(path: str, bands: list[str]) -> bool:
    """Whether `path` is under one of the sorted, prefix-free `bands`."""
    i = bisect_right(bands, path) - 1
    return i >= 0 and path.startswith(bands[i])


def _wanted(paths: pa.ChunkedArray | pa.Array, bands: list[str], exact: frozenset[str]) -> pa.Array:
    """A mask of `paths`: those under `bands` or in `exact` (`_items`). Row by row, never assuming `paths`' order."""
    return pa.array([p in exact or _band_of(p, bands) for p in paths.to_numpy(zero_copy_only=False)], pa.bool_())


def _spans(bands: list[str], exact: frozenset[str]) -> list[tuple[str, str]]:
    """The `[lo, hi]` path spans of `_items`, sorted and merged: disjoint, so both `lo` and `hi` ascend."""
    out: list[tuple[str, str]] = []
    for lo, hi in sorted([*((b, f"{b}\U0010ffff") for b in bands), *((k, k) for k in exact)]):
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def _meets(p_min: str, p_max: str, spans: list[tuple[str, str]], his: list[str]) -> bool:
    """Whether `[p_min, p_max]` meets a span of the merged `spans` (`his`: their `hi`s): of the spans with `hi ≥ p_min`,
    the first has the smallest `lo`."""
    i = bisect_left(his, p_min)
    return i < len(spans) and spans[i][0] <= p_max


@lru_cache(maxsize=32)
def _group_bounds(root: str, key: str) -> list[tuple[int, str, str, int]]:
    """The served sort `key`'s `.groups.parquet`: `(rg, p_min, p_max, vf_min)`. An immutable file, read by every bucket's
    hold: 8K-row groups of a whole fleet are far more than one bucket's items."""
    t = pq.read_table(Store(root).open(key.removesuffix(".parquet") + ".groups.parquet"), columns=["rg", "p_min", "p_max", "vf_min"])
    return list(zip(*(t[c].to_pylist() for c in t.column_names)))


def _groups(root: str, key: str, spans: list[tuple[str, str]], ts: int) -> list[int]:
    """The row groups of the served sort `key` whose paths may meet a merged `[lo, hi]` span (`_spans`) and whose versions
    may be live at `ts`."""
    his = [hi for _, hi in spans]
    # A run's closed rows cancel versions still open in the base. Dropping a group because all its vt <= ts
    # would resurrect those base rows, possibly returning both a replacement and its stale identity.
    return [rg for rg, p_min, p_max, vf_min in _group_bounds(root, key) if vf_min <= ts and _meets(p_min, p_max, spans, his)]


@lru_cache(maxsize=32)
def _footer(root: str, key: str) -> tuple[pq.FileMetaData, int]:
    """The served sort `key`'s footer and file size. An immutable file; parsing a whole store's footer once, not per read."""
    store = Store(root)
    with store.open(key) as f:
        return pq.read_metadata(f), store.size(key)


class _GroupFile(io.RawIOBase):
    """A read-only view of the file `key`, `size` bytes long, holding one fetched span `[lo, lo + len(data))`: what one row
    group's decode reads, without a request per column chunk. A read outside the span goes to the store."""

    def __init__(self, store: Store, key: str, size: int, lo: int, data: bytes):
        self.store, self.key, self.end, self.lo, self.data, self.pos = store, key, size, lo, data, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self.pos = {io.SEEK_SET: 0, io.SEEK_CUR: self.pos, io.SEEK_END: self.end}[whence] + offset
        return self.pos

    def read(self, n: int = -1) -> bytes:
        hi = self.end if n < 0 else min(self.pos + n, self.end)
        a, b = self.pos - self.lo, hi - self.lo
        out = self.data[a:b] if 0 <= a and b <= len(self.data) else self.store.read_range(self.key, self.pos, hi)
        self.pos += len(out)
        return out


def _chunk_span(rg: pq.RowGroupMetaData, cols: list[str]) -> tuple[int, int]:
    """The byte span `[lo, hi)` holding `cols`' column chunks in the row group `rg`."""
    chunks = [c for c in map(rg.column, range(rg.num_columns)) if c.path_in_schema in cols]
    starts = [c.dictionary_page_offset if c.has_dictionary_page and 0 < c.dictionary_page_offset < c.data_page_offset else c.data_page_offset for c in chunks]
    return min(starts), max(s + c.total_compressed_size for s, c in zip(starts, chunks))


def _group_rows(store: Store, key: str, rg: int, cols: list[str], bands: list[str], exact: frozenset[str]) -> tuple[pa.Table, int]:
    """Row group `rg` of `key` by one ranged read, kept only under the items: `(rows, rows the group held)`. By path alone:
    a run's close record has its base row's path, so both stay for the fold."""
    meta, size = _footer(store.root, key)
    lo, hi = _chunk_span(meta.row_group(rg), cols)
    data = _retrying(lambda: store.read_range(key, lo, hi))
    t = pq.ParquetFile(_GroupFile(store, key, size, lo, data), metadata=meta).read_row_group(rg, columns=cols, use_threads=False)
    return t.filter(_wanted(t["path"], bands, exact)), t.num_rows


def held_rows(store: Store, key: str, rgs: list[int], cols: list[str], bands: list[str], exact: frozenset[str], *,
              readers: int = IDENTITY_READERS) -> tuple[list[pa.Table], int]:
    """The rows under the items of `key`'s row groups `rgs`, in file order, `readers` groups at a time; each group is
    filtered before it is kept, so what accumulates is the items' rows alone. Returns them and the rows the groups held.
    A failed read raises: nothing is returned for part of a sort."""
    _footer(store.root, key)  # Once, not by every reader racing to the cache.
    with ThreadPoolExecutor(max_workers=readers) as pool:
        got = list(pool.map(lambda rg: _group_rows(store, key, rg, cols, bands, exact), rgs))
    return [t for t, _ in got], sum(n for _, n in got)


def object_identities(store_root: str, scan: str, bucket: str, bands: list[str] | tuple[str, ...], exact: list[str] | tuple[str, ...] = (),
                      *, gen: str | None = None, readers: int = IDENTITY_READERS) -> pa.Table:
    """The objects of `bucket` under `bands` (name prefixes) or named by `exact` as scan `scan` saw them, from the
    interval store at `store_root` (generation `gen`, default the current): `(name, generation, created)` sorted by
    name — the versions live at the scan in the `path` sort (base and the newest manifest's runs, the smallest `vt`),
    objects only (a key that is also a folder's prefix is folded into the folder: never listed). `generation` is the
    version's (NULL without one, or where the generation doesn't carry it); `created` the object's created second (its
    mean stamp: one object, `wts / wb`), else its `mtime`. Reads only the row groups whose paths meet the items (each
    sort's `.groups.parquet`), `readers` at a time, one ranged read each, keeping only the items' rows: memory goes with
    those rows (plus the fold's, spilling past `IDENTITY_MEM`), not the sort's. What a sweep's hold needs once the scan's
    raw listing is gone."""
    start = time.monotonic()
    store = Store(store_root)
    gen = store_gen(store, gen)
    prefix = f"{PREFIX}/{gen}"
    entry, runs = scan_entry(store, prefix, scan)
    base = store.read_json(f"{prefix}/scans.json")
    has_gen = ist.has_generation(base)
    identity = ist.identity_column(has_gen)
    ts = int(entry["ts"])
    full_bands, full_exact = _items(bucket, bands, exact)
    spans = _spans(full_bands, full_exact)
    cols = [*IDENTITY_COLS, *([identity] if has_gen else [])]
    parts: list[pa.Table] = []
    groups = decoded = 0
    for key in [f"{prefix}/served/{ist.GENERATION_SORT}.parquet", *(f"{prefix}/{r['key']}/served/{ist.GENERATION_SORT}.parquet" for r in runs)]:
        if rgs := _groups(store.root, key, spans, ts):
            got, n = held_rows(store, key, rgs, cols, full_bands, full_exact, readers=readers)
            parts += got
            groups += len(rgs)
            decoded += n
    out_schema = pa.schema([("name", pa.string()), (identity, pa.string() if isinstance(has_gen, pa.Field) else pa.int64()), ("created", pa.timestamp("us", tz="UTC"))])
    if not parts:
        return out_schema.empty_table()
    rows = pa.concat_tables(parts)
    del parts
    read_s = time.monotonic() - start
    gcol = identity if has_gen else "NULL::BIGINT"
    with tempfile.TemporaryDirectory(prefix="identities-") as tmp:
        con = connect(os.cpu_count() or 1, IDENTITY_MEM, tmp)
        con.register("rows", rows)
        t = con.execute(f"""SELECT substr(path, {len(bucket) + 2}) AS name, {gcol} AS {identity},
                make_timestamp((CASE WHEN wb > 0 THEN round(wts / wb, 0) ELSE mtime END)::BIGINT * 1000000) AS created
            FROM (SELECT * FROM rows QUALIFY row_number() OVER (PARTITION BY depth, path, vf ORDER BY vt) = 1)
            WHERE vf <= {ts} AND {ts} < vt AND kind = 'file'
            ORDER BY name""").to_arrow_table()
        con.close()
    err(f"object_identities {bucket} @{scan}: {len(spans):,} spans, {groups:,} groups ({decoded:,} rows) → {rows.num_rows:,} rows kept "
        f"({rows.nbytes / 2**30:.2f} GiB) → {t.num_rows:,} objects; read {read_s:.0f} s, total {time.monotonic() - start:.0f} s, "
        f"peak RSS {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (2**30 if sys.platform == 'darwin' else 2**20):.1f} GiB")
    return t.cast(out_schema)


def export_scan(store_root: str, scan: str, dest: str, *, gen: str | None = None, tiers: tuple[str, ...] = TIERS,
                work: Path | None = None, threads: int = 4, mem: str = "4GB") -> dict:
    """Scan `scan`'s per-scan sorts from the store at `store_root` (generation `gen`, default the store's current,
    followed to its newest successor) into `dest` (a dir or URL): `path-index.parquet` (+ `path-index-bysize.parquet`
    for a v2 scan, when `tiers` names it). Returns `{scan, gen, version, sliced, rows, files: {name: bytes}}`."""
    from disk_tree import listing_format as lf
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
    generation = ist.has_generation(base) if entry["version"] == 2 else False
    identity = ist.identity_column(generation)
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
            gen_of = f"SELECT depth, path, {identity} FROM ({live_sql(served(ist.GENERATION_SORT), False, entry['ts'])})"
            con.execute(f"CREATE OR REPLACE TABLE live AS SELECT l.*, g.{identity} FROM live l LEFT JOIN ({gen_of}) g USING (depth, path)")
        usr = ["nullif(usr, '') AS usr"] if sliced else []
        out = work / "out"
        out.mkdir(parents=True, exist_ok=True)
        if entry["version"] == 2:
            bad = con.execute("SELECT count(*) FROM live WHERE wb <> 0 AND wb <> size").fetchone()[0]
            if bad:
                raise SystemExit(f"export {scan}: {bad} versions weigh their mean stamp over part of their size; not a v2 scan's rows")
            sel = ", ".join(["path", *usr, *(f"{e} AS {c}" for c, e in V2_COLS), *([identity] if generation else [])])
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
                        f"(FORMAT PARQUET, {lf.duckdb_codec()}, ROW_GROUP_SIZE 8192)")
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
