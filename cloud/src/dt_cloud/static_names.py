"""Static name search (specs/architecture/static-name-search.md): the
suffix-ordered, denormalized postings, rebuilt from primary sources.

Two stages, each embarrassingly parallel on GCP Batch (`job/static-names.sh`):

1. **Intervals.** Every published scan's `path` sort (the deployment profile's key templates, e.g.
   `listing/<scan>/index/<gen>/path-index.parquet` or `cw-l2/<scan>/index/<gen>/path-index.parquet`)
   becomes SCD-2 version intervals: one row per `(depth, path, usr)` version with `[vf, vt)`
   (`vt` = 2106 while open) and its values: a key's rows merged per scan (`MERGED`), and
   a new version whenever any value changes (the weighted mean stamp compared to the
   second, rounded half to even) or the key was absent from a scan in between. The
   kernel is `pyrmts.intervals` (gaps-and-islands, key-range planning and predicates,
   one-scan append), run per `(depth, path)` key range (`ranges.json`), so a range task
   reads only its row groups of each scan. `append` adds
   one scan to a range's intervals (open versions × the scan: a full join), the per-scan
   delta, and must equal a rebuild.
2. **Suffixes.** From the intervals: one row per (lowercase name suffix of three or more
   characters, version): `(s, depth, path, usr, vf, vt, size, n_files)`, sorted by `s`,
   in shard files split by the suffix's first three characters (`shards.json`, cut from
   the intervals stage's per-range prefix histograms), 8K-row groups, zstd, and a
   per-row-group sidecar `(file, rg, s_min, s_max, offset, length, rows)` (for D1).
   Every rare literal's rows are then one contiguous byte range of one or two files.

Every output is written through pyarrow with fixed row-group sizes and a total sort
order, so a rerun on the same inputs (and image) is byte-identical.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import sys
from functools import partial
from pathlib import Path
from time import monotonic
from typing import Iterable, Iterator

import pyarrow as pa
import pyarrow.parquet as pq
from click import IntRange, argument, group, option

from .hex_runs import HexRule, first_occurrence, kept_sql, occurs, rule_from_json
from .scan_id import SCAN_ID, check_order, scan_epoch, scan_label  # noqa: F401 (re-exported: the stages' scan-id helpers)
from .static_profile import data_bucket, layouts as profile_layouts, profile, scratch_bucket

err = partial(print, file=sys.stderr, flush=True)

PREFIX = "static-names"
OPEN = 4291747200  # 2106-01-01 00:00:00 UTC: a version's `vt` while open
U64 = 1 << 64
#: The values whose change opens a version; `mtime_mean` is stored rounded.
VALUE_COLS = ["kind", "size", "n_files", "n_children", "n_desc", "mtime", "mtime_mean", "mtime_w", "last_read", "c2", "c3", "c4"]
KEY_COLS = ["depth", "path", "usr"]
INTERVAL_RG = 65536
SX_RG = 8192
CODEC = "zstd"

INTERVAL_SCHEMA = pa.schema([
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("usr", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("vt", pa.int64(), nullable=False),
    pa.field("kind", pa.string(), nullable=False),
    pa.field("size", pa.int64(), nullable=False),
    pa.field("n_files", pa.int64(), nullable=False),
    pa.field("n_children", pa.int64(), nullable=False),
    pa.field("n_desc", pa.int64(), nullable=False),
    pa.field("mtime", pa.int64(), nullable=False),
    pa.field("mtime_mean", pa.float64(), nullable=False),
    pa.field("mtime_w", pa.int64(), nullable=False),
    pa.field("last_read", pa.int32(), nullable=False),
    pa.field("c2", pa.int64(), nullable=False),
    pa.field("c3", pa.int64(), nullable=False),
    pa.field("c4", pa.int64(), nullable=False),
])
SX_SCHEMA = pa.schema([
    pa.field("s", pa.string(), nullable=False),
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("usr", pa.string(), nullable=False),
    pa.field("vf", pa.timestamp("ms", tz="UTC"), nullable=False),
    pa.field("vt", pa.timestamp("ms", tz="UTC"), nullable=False),
    pa.field("size", pa.int64(), nullable=False),
    pa.field("n_files", pa.int64(), nullable=False),
])
SIDECAR_SCHEMA = pa.schema([
    pa.field("file", pa.string(), nullable=False),
    pa.field("rg", pa.int32(), nullable=False),
    pa.field("s_min", pa.string(), nullable=False),
    pa.field("s_max", pa.string(), nullable=False),
    pa.field("offset", pa.int64(), nullable=False),
    pa.field("length", pa.int64(), nullable=False),
    pa.field("rows", pa.int32(), nullable=False),
])


def q(s: str) -> str:
    """A DuckDB string literal."""
    return "'" + s.replace("'", "''") + "'"




# ── Scans ──────────────────────────────────────────────────────────────────


#: Where scans' `path` sorts live in the data bucket: key templates (`static_profile.Profile.layouts`), `{id}` the
#: scan id (one path segment), `{gen}` the store generation (a scan's newest wins; a template without one counts as the
#: oldest). The base's `scans.json` records them, so a run's `prepare` finds the next scan the same way.


def layout_glob(layout: str) -> tuple[str, re.Pattern]:
    """A layout template as a GCS `match_glob` and a regex capturing `id` (and `gen`)."""
    if layout.count("{id}") != 1 or layout.count("{gen}") > 1:
        raise ValueError(f"layout {layout!r}: needs one {{id}} and at most one {{gen}}")
    parts = re.split(r"(\{id\}|\{gen\})", layout)
    glob = "".join("*" if p in ("{id}", "{gen}") else p for p in parts)
    rx = "".join("(?P<id>[^/]+)" if p == "{id}" else "(?P<gen>[^/]+)" if p == "{gen}" else re.escape(p) for p in parts)
    return glob, re.compile(rx)


def pick_scans(objects: Iterable[tuple[str, dict]], layouts: Iterable[str], *, start: str | None = None,
               through: str | None = None) -> list[dict]:
    """Each scan's newest `path` sort among `(key, pins)` listings: keys matched against `layouts` (ids that are not
    scan ids, e.g. `cw-l2/over-time-dev/`, are skipped), the greatest `{gen}` per id, ids in `[start, through]`.
    Sorted by id, which must also sort them strictly by time (two ids at one instant, e.g. `2026-10-01` and
    `2026-10-01T0000`, are an error: a version's `vf` would not say which scan opened it)."""
    rxs = [layout_glob(t)[1] for t in layouts]
    found: dict[str, dict] = {}
    for key, pins in objects:
        m = next((m for rx in rxs if (m := rx.fullmatch(key))), None)
        if m is None:
            continue
        sid = m["id"]
        if not SCAN_ID.fullmatch(sid):
            err(f"scans: skipping {key} ({sid!r} is not a scan id)")
            continue
        if (start and sid < start) or (through and sid > through):
            continue
        gen = m.groupdict().get("gen") or ""
        cur = found.get(sid)
        if cur is None or gen > cur["gen"]:
            found[sid] = {"id": sid, "gen": gen, "src": key, **pins}
    scans = [found[d] for d in sorted(found)]
    check_order(found)
    for s in scans:
        del s["gen"]
        s["ts"] = scan_epoch(s["id"])
    return scans


def list_scans(bucket: str, *, layouts: Iterable[str], start: str | None = None,
               through: str | None = None) -> dict:
    """Every scan's `path` sort: per scan id the newest
    generation under `layouts` (`pick_scans`); pinned by GCS generation, size, md5 and crc32c."""
    from google.cloud import storage

    client = storage.Client()
    layouts = list(layouts)

    def objects():
        for t in layouts:
            for b in client.list_blobs(bucket, match_glob=layout_glob(t)[0]):
                yield b.name, {"generation": int(b.generation), "size": int(b.size),
                               "md5": base64.b64decode(b.md5_hash).hex() if b.md5_hash else None, "crc32c": b.crc32c}

    scans = pick_scans(objects(), layouts, start=start, through=through)
    return {"bucket": bucket, "layouts": layouts, "scans": scans}


# ── Key ranges ─────────────────────────────────────────────────────────────


RANGE_COLS = ["depth", "path"]


def range_preds(r: dict) -> list[str]:
    """Range `r` (`ranges.json`: `[lo, hi)` over `(depth, path)`, `hi` None = unbounded) as DuckDB
    conjunctive predicates (`pyrmts.intervals.key_range_pieces`): read each, `UNION ALL` them, so each
    filter prunes a `(depth, path)`-sorted file by its row-group statistics."""
    from pyrmts.intervals import key_range_pieces

    return key_range_pieces(RANGE_COLS, r["lo"], r["hi"])


def plan_ranges(scans: dict, k: int, mount: str | None = None) -> dict:
    """`k` key ranges of about equal input rows over every scan: cut at row-group starts of the newest
    scan of each source format (v1: dirs only; v2: every object), weighted by how many scans have
    that format (`pyrmts.intervals.plan_ranges`). Boundaries are `(depth, path)` keys; range `i` is
    `[b_i, b_{i+1})`, the first from `(0, "")`."""
    from pyrmts.intervals import plan_ranges as plan

    by_version: dict[int, list[dict]] = {}
    for s in scans["scans"]:
        by_version.setdefault(s["version"], []).append(s)
    footers = []
    for v, ss in by_version.items():
        newest = ss[-1]
        md = _metadata(scans["bucket"], newest["src"], mount)
        footers.append((md, len(ss)))
        err(f"plan-ranges: v{v} {newest['id']}: {md.num_row_groups:,} row groups × {len(ss)} scans")
    return plan(footers, RANGE_COLS, k, floor=(0, ""))


def _metadata(bucket: str, key: str, mount: str | None) -> "pq.FileMetaData":
    """A parquet's footer metadata: from the mount, else two ranged GCS reads (never the whole file)."""
    if mount:
        return pq.ParquetFile(f"{mount}/{key}").metadata
    from google.cloud import storage

    blob = storage.Client().bucket(bucket).get_blob(key)
    size = int(blob.size)
    tail = blob.download_as_bytes(start=size - 8, end=size - 1)
    flen = int.from_bytes(tail[:4], "little")
    foot = blob.download_as_bytes(start=max(0, size - max(8 + flen, 1 << 16)), end=size - 1)
    return pq.ParquetFile(Spans(size, [(size - len(foot), foot)])).metadata


def _src(bucket: str, key: str, mount: str | None) -> str:
    return f"{mount}/{key}" if mount else f"gs://{bucket}/{key}"


# ── Interval kernel ────────────────────────────────────────────────────────

V2_SELECT = """depth::UTINYINT AS depth, path, coalesce({usr}, '') AS usr,
    CASE WHEN kind = 'file' THEN 'file' ELSE 'dir' END AS kind, size::BIGINT AS size, n_files::BIGINT AS n_files,
    coalesce({n_children}, -1)::BIGINT AS n_children, coalesce({n_desc}, -1)::BIGINT AS n_desc, coalesce({mtime}, -1)::BIGINT AS mtime,
    coalesce({mtime_mean}, 0)::DOUBLE AS mtime_mean, CASE WHEN {mtime_mean} IS NULL THEN 0 ELSE size END::BIGINT AS mtime_w,
    coalesce({last_read}, -1)::INTEGER AS last_read, coalesce({c2}, 0)::BIGINT AS c2, coalesce({c3}, 0)::BIGINT AS c3, coalesce({c4}, 0)::BIGINT AS c4"""
#: Columns a v2 sort may lack (read as NULL): cw's carry no owner (`usr`) and a single storage-class pivot.
V2_OPTIONAL = {"usr": "usr", "n_children": "n_children", "n_desc": "n_desc", "mtime": "mtime", "mtime_mean": "mtime_mean", "last_read": "last_read",
               "c2": "sum_storage_class_id_2", "c3": "sum_storage_class_id_3", "c4": "sum_storage_class_id_4"}
V1_SELECT = """depth::UTINYINT AS depth, path, coalesce(usr, '') AS usr, 'dir' AS kind, b::BIGINT AS size, o::BIGINT AS n_files,
    -1::BIGINT AS n_children, -1::BIGINT AS n_desc, -1::BIGINT AS mtime,
    CASE WHEN coalesce(wb, 0) > 0 THEN coalesce(wts, 0) / wb ELSE 0 END::DOUBLE AS mtime_mean, coalesce(wb, 0)::BIGINT AS mtime_w,
    coalesce(a, -1)::INTEGER AS last_read, coalesce(c2, 0)::BIGINT AS c2, coalesce(c3, 0)::BIGINT AS c3, coalesce(c4, 0)::BIGINT AS c4"""
#: A scan's rows for one key merged into one, the mean stamp then rounded to the second (half to even) — the
#: value the change test compares.
MERGED = """any_value(kind) AS kind, sum(size)::BIGINT AS size, sum(n_files)::BIGINT AS n_files, max(n_children) AS n_children,
    max(n_desc) AS n_desc, max(mtime) AS mtime,
    round_even(CASE WHEN count(*) = 1 THEN any_value(mtime_mean) ELSE sum(mtime_mean * mtime_w) / greatest(sum(mtime_w), 1) END, 0) AS mtime_mean,
    sum(mtime_w)::BIGINT AS mtime_w, max(last_read) AS last_read, sum(c2)::BIGINT AS c2, sum(c3)::BIGINT AS c3, sum(c4)::BIGINT AS c4"""


def source_version(con, path: str) -> int:
    """2 for a store generation's `path` sort (`size`, `kind`), 1 for a v1 index (`b`, `o`)."""
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({q(path)})").fetchall()}
    if {"size", "kind", "n_files"} <= cols:
        return 2
    if {"b", "o"} <= cols:
        return 1
    raise ValueError(f"{path}: neither a v2 store sort nor a v1 index ({sorted(cols)})")


def scan_sql(con, path: str, preds: list[str], version: int | None = None) -> str:
    """One scan's rows in the range (`range_preds`), merged per key."""
    version = version or source_version(con, path)
    if version == 2:
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({q(path)})").fetchall()}
        select = V2_SELECT.format(**{k: (c if c in cols else "NULL") for k, c in V2_OPTIONAL.items()})
    else:
        select = V1_SELECT
    parts = [f"SELECT {select} FROM read_parquet({q(path)}) WHERE {p}" for p in preds]
    return f"SELECT depth, path, usr, {MERGED} FROM ({' UNION ALL '.join(parts)}) GROUP BY depth, path, usr"


def intervals_sql(con, sources: list[tuple[str, int, int | None]], preds: list[str], state_cols: list[str] = VALUE_COLS) -> str:
    """The range's intervals over `sources` (`(path, epoch, version)`, oldest first) as a query of
    `(depth, path, usr, vf, vt, *state_cols)` rows (unsorted): `pyrmts.intervals`' gaps-and-islands over
    the per-scan merged rows, scan indices mapped to epochs (`vt` = the first scan after the run, or
    `OPEN`). Every state column is equal within a run (the stamp is stored rounded), so the run's values
    are deterministic. `state_cols=ANSWER_COLS` gives the coalesced versions (`CINTERVAL_SCHEMA`) directly."""
    from pyrmts.intervals import islands_sql, long_sql, stamped_sql

    srcs = [scan_sql(con, p, preds, v) for p, _, v in sources]
    if list(state_cols) != VALUE_COLS:
        # `islands_sql` windows `SELECT *`: drop the values no run reads before they ride through the sort.
        srcs = [f"SELECT {', '.join([*KEY_COLS, *state_cols])} FROM ({src})" for src in srcs]
    runs = islands_sql(long_sql(srcs), KEY_COLS, state_cols)
    return stamped_sql(runs, KEY_COLS, state_cols, [e for _, e, _ in sources], OPEN)


def connect(threads: int, mem: str, tmp: str | Path | None):
    import duckdb

    con = duckdb.connect()
    con.execute(f"SET threads={threads}; SET memory_limit='{mem}'; SET preserve_insertion_order=false")
    con.execute("SET parquet_metadata_cache=true")
    if tmp:
        Path(tmp).mkdir(parents=True, exist_ok=True)
        con.execute(f"SET temp_directory={q(str(tmp))}")
    return con


def write_sorted(batches: Iterable[pa.RecordBatch], out: Path, schema: pa.Schema, rg_rows: int,
                 on_group=None, dictionary: list[str] | bool = False) -> int:
    """Write already-ordered batches as exact `rg_rows`-row groups (the last may be short), so the file's
    bytes depend only on its rows. `on_group(table)` sees each row group as written. Returns rows."""
    out.parent.mkdir(parents=True, exist_ok=True)
    pending: list[pa.RecordBatch] = []
    n_pending = 0
    total = 0
    with pq.ParquetWriter(out, schema, compression=CODEC, use_dictionary=dictionary, write_statistics=True,
                          coerce_timestamps="ms", allow_truncated_timestamps=False) as w:
        def flush(final: bool) -> None:
            nonlocal pending, n_pending
            if not pending:
                return
            t = pa.Table.from_batches(pending, schema=schema).combine_chunks()
            off = 0
            while t.num_rows - off >= rg_rows or (final and off < t.num_rows):
                g = t.slice(off, min(rg_rows, t.num_rows - off))
                w.write_table(g, row_group_size=rg_rows)
                if on_group:
                    on_group(g)
                off += g.num_rows
            rest = t.slice(off)
            pending, n_pending = ([rest.combine_chunks().to_batches()[0]] if rest.num_rows else []), rest.num_rows

        for b in batches:
            if b.num_rows == 0:
                continue
            b = b.cast(schema) if b.schema != schema else b
            pending.append(b)
            n_pending += b.num_rows
            total += b.num_rows
            if n_pending >= rg_rows:
                flush(False)
        flush(True)
    return total


def _batches(con, sql: str, size: int = 1 << 17) -> Iterator[pa.RecordBatch]:
    reader = con.execute(sql).to_arrow_reader(size)
    for b in reader:
        yield b


def _sx_cast(b: pa.RecordBatch) -> pa.RecordBatch:
    """Interval epoch seconds → the suffix files' millisecond timestamps."""
    import pyarrow.compute as pc

    cols = []
    for f in SX_SCHEMA:
        c = b.column(f.name)
        if f.name in ("vf", "vt"):
            c = pc.multiply(c.cast(pa.int64()), 1000)
        cols.append(c.cast(f.type))
    return pa.record_batch(cols, schema=SX_SCHEMA)


NAME = "lower(string_split(path, '/')[-1])"


def gen_rule(scans: dict) -> HexRule | None:
    """A generation's hex-run rule, as its `scans.json` records it (`hex_runs`; absent = the full index)."""
    return rule_from_json(scans.get("hex_runs"))


_GEN_RULES: dict[tuple[str, str], HexRule | None] = {}


def gen_rule_at(bucket: str, gen: str) -> HexRule | None:
    """`gen_rule` of `gs://bucket/static-names/<gen>/scans.json` (read once per process)."""
    if (bucket, gen) not in _GEN_RULES:
        _GEN_RULES[(bucket, gen)] = gen_rule(read_json(f"gs://{bucket}/{PREFIX}/{gen}/scans.json"))
    return _GEN_RULES[(bucket, gen)]


def hist_sql(table: str, rule: HexRule | None = None) -> str:
    """Suffix rows per three-character prefix (versions × suffix positions of ≥ 3 characters, depth ≥ 1; under `rule`,
    the positions it keeps)."""
    return f"""SELECT substring(l, p, 3) AS p3, count(*)::BIGINT AS n FROM (
            SELECT l, unnest(generate_series(1, length(l) - 2)) AS p FROM (SELECT {NAME} AS l FROM {table} WHERE depth >= 1) WHERE length(l) >= 3
        ) WHERE {kept_sql('l', 'p', rule)} GROUP BY p3 ORDER BY p3"""


def digests(con, table: str) -> dict:
    """Per scan epoch: versions opened (count, Σ md5 mod 2⁶⁴ of `depth|path|usr|vf|size|n_files`) and closed
    (of `depth|path|usr|vf|vt`)."""
    from pyrmts.intervals import interval_digests

    return interval_digests(con, table, [*KEY_COLS, "vf", "size", "n_files"], [*KEY_COLS, "vf", "vt"], OPEN)


def build_range(scans: dict, ranges: dict, i: int, out: Path, *, mount: str | None, threads: int, mem: str, tmp: Path | None,
                con=None, coalesced: bool = False, rule: HexRule | None = None) -> dict:
    """One key range's intervals over every scan in `scans`: `intervals/r####.parquet` (sorted
    `(depth, path, usr, vf)`), `hist/r####.parquet` and `digest/r####.json` under `out`. Pass `con` to
    build several ranges on one connection: each scan's footer is then parsed once (DuckDB's
    `parquet_metadata_cache`), not once per range.

    `coalesced`: the coalesced versions straight from the scans instead (one pass; runs keyed on
    `ANSWER_COLS` only): `cintervals/r####.parquet` and `chist/r####.parquet`, byte-identical to
    `coalesce_range` over the full intervals. No `digest/`."""
    t0 = monotonic()
    r = ranges["ranges"][i]
    preds = range_preds(r)
    con = con or connect(threads, mem, tmp)
    sources = [(_src(scans["bucket"], s["src"], mount), s["ts"], s.get("version")) for s in scans["scans"]]
    if coalesced:
        name = f"r{i:04d}"
        con.execute("DROP TABLE IF EXISTS civ")
        con.execute(f"CREATE TABLE civ AS {intervals_sql(con, sources, preds, ANSWER_COLS)}")
        rows = write_sorted(_batches(con, "SELECT * FROM civ ORDER BY depth, path, usr, vf"), out / "cintervals" / f"{name}.parquet",
                            CINTERVAL_SCHEMA, INTERVAL_RG, dictionary=["usr"])
        (out / "chist").mkdir(parents=True, exist_ok=True)
        pq.write_table(con.execute(hist_sql("civ", rule)).to_arrow_table(), out / "chist" / f"{name}.parquet", compression=CODEC)
        suffix_rows = con.execute(suffix_count_sql("civ")).fetchone()[1]
        con.execute("DROP TABLE civ")
        doc = {"range": r, "rows": rows, "suffix_rows": suffix_rows, "s": round(monotonic() - t0, 1)}
        err(f"range {i}: {rows:,} coalesced versions, {suffix_rows:,} suffix rows in {doc['s']}s")
        return doc
    con.execute("DROP TABLE IF EXISTS iv")
    con.execute(f"CREATE TABLE iv AS {intervals_sql(con, sources, preds)}")
    t_kernel = monotonic() - t0
    doc = _finish_range(con, "iv", out, i, {"range": r, "kernel_s": round(t_kernel, 1)}, t0)
    con.execute("DROP TABLE iv")
    return doc


def _finish_range(con, table: str, out: Path, i: int, doc: dict, t0: float) -> dict:
    name = f"r{i:04d}"
    rows = write_sorted(_batches(con, f"SELECT * FROM {table} ORDER BY depth, path, usr, vf"), out / "intervals" / f"{name}.parquet",
                        INTERVAL_SCHEMA, INTERVAL_RG, dictionary=["usr", "kind"])
    (out / "hist").mkdir(parents=True, exist_ok=True)
    pq.write_table(con.execute(hist_sql(table)).to_arrow_table(), out / "hist" / f"{name}.parquet", compression=CODEC)
    doc = {**doc, "rows": rows, "digests": digests(con, table), "s": round(monotonic() - t0, 1)}
    (out / "digest").mkdir(parents=True, exist_ok=True)
    (out / "digest" / f"{name}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
    err(f"range {i}: {rows:,} intervals in {doc['s']}s")
    return doc


def append_range(prev: Path, scan: dict, ranges: dict, i: int, out: Path, *, bucket: str, mount: str | None,
                 threads: int, mem: str, tmp: Path | None) -> dict:
    """Append one scan to a range's intervals (`prev`, `INTERVAL_SCHEMA` sorted): the open versions and the
    scan's merged rows in the range, fully joined by key — a version whose key is gone or whose values
    changed closes at the scan, and a new or changed key opens a version. Writes the range's new
    intervals (equal to a rebuild through the scan), hist and digest, plus `delta/<scan>/r####.parquet`:
    the opened versions (`op` = 1) and the closed ones (`op` = −1, with their new `vt`)."""
    from pyrmts.intervals import append_intervals, delta_sql

    t0 = monotonic()
    r = ranges["ranges"][i]
    con = connect(threads, mem, tmp)
    D = scan["ts"]
    src = _src(bucket, scan["src"], mount)
    con.execute(f"CREATE TABLE old AS SELECT * FROM read_parquet({q(str(prev))})")
    n_open, n_close = append_intervals(con, "SELECT * FROM old", scan_sql(con, src, range_preds(r), scan.get("version")),
                                       KEY_COLS, VALUE_COLS, D, OPEN, out="ivs")
    name = f"r{i:04d}"
    delta = out / "delta" / scan["id"] / f"{name}.parquet"
    delta_schema = INTERVAL_SCHEMA.append(pa.field("op", pa.int8(), nullable=False))
    write_sorted(_batches(con, f"SELECT * FROM ({delta_sql('ivs', D)}) ORDER BY depth, path, usr, vf"),
                 delta, delta_schema, INTERVAL_RG, dictionary=["usr", "kind"])
    return _finish_range(con, "ivs", out, i, {"range": r, "appended": scan["id"], "opened": n_open, "closed": n_close}, t0)


# ── Coalesced versions ─────────────────────────────────────────────────────

#: What a name answer reads of a version: its key, liveness and these values. Versions that differ only in
#: other values (`last_read`, the mean stamp, storage classes, …) are merged when adjacent (`vt` = the next
#: `vf`), so a name index over the coalesced versions answers exactly as one over every version.
ANSWER_COLS = ["size", "n_files"]
CINTERVAL_SCHEMA = pa.schema([INTERVAL_SCHEMA.field(c) for c in ["depth", "path", "usr", "vf", "vt", *ANSWER_COLS]])


def coalesce_sql(src: str) -> str:
    """`src`'s intervals (`INTERVAL_SCHEMA` rows, a table or `read_parquet(…)`) with adjacent versions of a key
    merged while `size` and `n_files` hold: a run opens at a key's first version, after an absence (`vf` ≠ the
    previous `vt`) or when either value changes. `CINTERVAL_SCHEMA` rows, unsorted."""
    same = " AND ".join(f"lag({c}) OVER w = {c}" for c in ANSWER_COLS)
    vals = ", ".join(f"any_value({c}) AS {c}" for c in ANSWER_COLS)
    return f"""WITH m AS (
            SELECT depth, path, usr, vf, vt, {', '.join(ANSWER_COLS)},
                CASE WHEN lag(vt) OVER w = vf AND {same} THEN 0 ELSE 1 END AS __is_new
            FROM {src} WINDOW w AS (PARTITION BY depth, path, usr ORDER BY vf)
        ), g AS (
            SELECT *, sum(__is_new) OVER (PARTITION BY depth, path, usr ORDER BY vf ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS __grp FROM m
        )
        SELECT depth, path, usr, min(vf) AS vf, max(vt) AS vt, {vals} FROM g GROUP BY depth, path, usr, __grp"""


def term_rows_sql(table: str, terms: list[str]) -> str:
    """Per term: the suffix rows its range holds over `table`'s versions (Σ versions × occurrences in the
    lowercase name, overlapping ones included: each is a suffix starting with the term), depth ≥ 1."""
    if not terms:
        return "SELECT NULL::VARCHAR AS term, 0::BIGINT AS n WHERE false"
    parts = []
    for t in terms:
        n = len(t)
        parts.append(f"""SELECT {q(t)} AS term, coalesce(sum(list_count(list_filter(range(1, length(l) - {n} + 2),
                lambda p: substring(l, p, {n}) = {q(t)}))), 0)::BIGINT AS n
            FROM (SELECT {NAME} AS l FROM {table} WHERE depth >= 1) WHERE contains(l, {q(t)})""")
    return " UNION ALL ".join(parts)


def suffix_count_sql(table: str) -> str:
    """`(versions, suffix rows)` at depth ≥ 1: a version's suffix rows are its name's positions of ≥ 3 characters."""
    return f"""SELECT count(*)::BIGINT, coalesce(sum(greatest(length(l) - 2, 0)), 0)::BIGINT FROM (SELECT {NAME} AS l FROM {table} WHERE depth >= 1)"""


def coalesce_range(src: str, i: int, out: Path, con, terms: list[str] | None = None, rule: HexRule | None = None) -> dict:
    """One range's coalesced versions: `cintervals/r####.parquet` (sorted `(depth, path, usr, vf)`),
    `chist/r####.parquet` (suffix rows per three-character prefix) and `cstats/r####.json` (versions and
    suffix rows before and after, and each of `terms`' range rows before and after)."""
    t0 = monotonic()
    name = f"r{i:04d}"
    con.execute("DROP TABLE IF EXISTS iv; DROP TABLE IF EXISTS civ")
    con.execute(f"CREATE TABLE iv AS SELECT depth, path, usr, vf, vt, {', '.join(ANSWER_COLS)} FROM read_parquet({q(src)})")
    con.execute(f"CREATE TABLE civ AS {coalesce_sql('iv')}")
    rows = write_sorted(_batches(con, "SELECT * FROM civ ORDER BY depth, path, usr, vf"), out / "cintervals" / f"{name}.parquet",
                        CINTERVAL_SCHEMA, INTERVAL_RG, dictionary=["usr"])
    (out / "chist").mkdir(parents=True, exist_ok=True)
    pq.write_table(con.execute(hist_sql("civ", rule)).to_arrow_table(), out / "chist" / f"{name}.parquet", compression=CODEC)
    stats: dict = {"range": i, "versions": [con.execute("SELECT count(*) FROM iv").fetchone()[0], rows]}
    stats["dir_versions"] = [con.execute(f"SELECT count(*) FROM {t} WHERE depth >= 1").fetchone()[0] for t in ("iv", "civ")]
    stats["suffix_rows"] = [con.execute(suffix_count_sql(t)).fetchone()[1] for t in ("iv", "civ")]
    if terms:
        before = dict(con.execute(term_rows_sql("iv", terms)).fetchall())
        after = dict(con.execute(term_rows_sql("civ", terms)).fetchall())
        stats["terms"] = {t: [int(before.get(t, 0)), int(after.get(t, 0))] for t in terms}
    stats["s"] = round(monotonic() - t0, 1)
    (out / "cstats").mkdir(parents=True, exist_ok=True)
    (out / "cstats" / f"{name}.json").write_text(json.dumps(stats, sort_keys=True) + "\n")
    con.execute("DROP TABLE iv; DROP TABLE civ")
    err(f"coalesce range {i}: {stats['versions'][0]:,} → {rows:,} versions, suffix rows {stats['suffix_rows'][0]:,} → {stats['suffix_rows'][1]:,} in {stats['s']}s")
    return stats


def coalesce_append(prev: Path, delta: Path, scan: dict, out: Path, i: int, con, rule: HexRule | None = None) -> dict:
    """Append scan `D` to a range's coalesced versions (`prev`, `CINTERVAL_SCHEMA` sorted) from the intervals'
    delta for `D` (`delta/<D>/r####.parquet`: `op` 1 = a version opened at `D`, −1 = one closed at `D`). Per
    key: closed and reopened with the same answer values continues its coalesced version; closed alone (or
    reopened with others) closes it at `D`; opened alone (or with others) opens one. Writes the range's
    `cintervals/` (equal to coalescing the appended intervals) and `cdelta/<D>/r####.parquet` (the coalesced
    versions opened, `op` 1, and closed, `op` −1)."""
    name = f"r{i:04d}"
    D = scan["ts"]
    con.execute("DROP TABLE IF EXISTS cold; DROP TABLE IF EXISTS dl; DROP TABLE IF EXISTS cev; DROP TABLE IF EXISTS cnew")
    con.execute(f"CREATE TABLE cold AS SELECT * FROM read_parquet({q(str(prev))})")
    last = con.execute("SELECT max(vf) FROM cold").fetchone()[0]
    if last is not None and last >= D:
        raise ValueError(f"range {i}: coalesced versions already reach {last} ≥ the appended scan {D}")
    vals = ", ".join(ANSWER_COLS)
    con.execute(f"CREATE TABLE dl AS SELECT depth, path, usr, op, {vals} FROM read_parquet({q(str(delta))})")
    same = " AND ".join(f"o.{c} = c.{c}" for c in ANSWER_COLS)
    # per key: its closing interval's values (c) and its opening one's (o)
    con.execute(f"""CREATE TABLE cev AS SELECT coalesce(o.depth, c.depth) AS depth, coalesce(o.path, c.path) AS path,
            coalesce(o.usr, c.usr) AS usr, c.depth IS NOT NULL AS closed, o.depth IS NOT NULL AS opened,
            c.depth IS NOT NULL AND o.depth IS NOT NULL AND {same} AS continues, {', '.join(f'o.{c}' for c in ANSWER_COLS)}
        FROM (SELECT * FROM dl WHERE op = 1) AS o FULL OUTER JOIN (SELECT * FROM dl WHERE op = -1) AS c USING (depth, path, usr)""")
    con.execute(f"""CREATE TABLE cnew AS
        SELECT o.depth, o.path, o.usr, o.vf,
            CASE WHEN o.vt = {OPEN} AND e.closed AND NOT e.continues THEN {D} ELSE o.vt END::BIGINT AS vt, {', '.join(f'o.{c}' for c in ANSWER_COLS)}
        FROM cold AS o LEFT JOIN cev AS e ON o.vt = {OPEN} AND o.depth = e.depth AND o.path = e.path AND o.usr = e.usr
        UNION ALL SELECT depth, path, usr, {D}::BIGINT AS vf, {OPEN}::BIGINT AS vt, {vals} FROM cev WHERE opened AND NOT continues""")
    rows = write_sorted(_batches(con, "SELECT * FROM cnew ORDER BY depth, path, usr, vf"), out / "cintervals" / f"{name}.parquet",
                        CINTERVAL_SCHEMA, INTERVAL_RG, dictionary=["usr"])
    dschema = CINTERVAL_SCHEMA.append(pa.field("op", pa.int8(), nullable=False))
    write_sorted(_batches(con, f"""SELECT *, 1::TINYINT AS op FROM cnew WHERE vf = {D}
                    UNION ALL SELECT *, -1::TINYINT AS op FROM cnew WHERE vt = {D} ORDER BY depth, path, usr, vf, op"""),
                 out / "cdelta" / scan["id"] / f"{name}.parquet", dschema, INTERVAL_RG, dictionary=["usr"])
    (out / "chist").mkdir(parents=True, exist_ok=True)
    pq.write_table(con.execute(hist_sql("cnew", rule)).to_arrow_table(), out / "chist" / f"{name}.parquet", compression=CODEC)
    n_open, n_close = con.execute(f"SELECT count(*) FILTER (WHERE vf = {D}), count(*) FILTER (WHERE vt = {D}) FROM cnew").fetchone()
    con.execute("DROP TABLE cold; DROP TABLE dl; DROP TABLE cev; DROP TABLE cnew")
    return {"range": i, "appended": scan["id"], "rows": rows, "opened": int(n_open), "closed": int(n_close)}


# ── Suffix shards ──────────────────────────────────────────────────────────


def plan_shards(hists: list[Path], target_rows: int, tasks: int) -> dict:
    """Shards of about `target_rows` suffix rows, each a run of three-character prefixes (a prefix is
    never split, so a literal's range is in one shard), grouped into `tasks` contiguous task groups."""
    import duckdb

    con = duckdb.connect()
    rows = con.execute(f"""SELECT p3, sum(n)::BIGINT FROM read_parquet([{', '.join(q(str(h)) for h in hists)}])
        GROUP BY p3 ORDER BY p3""").fetchall()
    total = sum(n for _, n in rows)
    shards: list[dict] = []
    cur = None
    for p3, n in rows:
        if cur is None or cur["rows"] + n > target_rows and cur["rows"] > 0:
            cur = {"lo": p3, "rows": 0, "prefixes": 0}
            shards.append(cur)
        cur["rows"] += n
        cur["prefixes"] += 1
    for k, s in enumerate(shards):
        s["i"] = k
        s["hi"] = shards[k + 1]["lo"] if k + 1 < len(shards) else None
    per = total / tasks
    groups: list[list[int]] = [[]]
    acc = 0
    for s in shards:
        if acc >= per * len(groups) and groups[-1] and len(groups) < tasks:
            groups.append([])
        groups[-1].append(s["i"])
        acc += s["rows"]
    biggest = max(rows, key=lambda r: r[1]) if rows else (None, 0)
    return {"ranges": len(hists), "target_rows": target_rows, "total_rows": total, "prefixes": len(rows), "largest_prefix": list(biggest),
            "shards": [{k: s[k] for k in ("i", "lo", "hi", "rows", "prefixes")} for s in shards],
            "tasks": [{"t": t, "shards": g, "rows": sum(shards[i]["rows"] for i in g)} for t, g in enumerate(groups)]}


def suffix_sql(files: list[str], rng: str | None = None, rule: HexRule | None = None) -> str:
    """Every suffix position of three or more characters of the intervals' lowercase names (depth ≥ 1; under `rule`,
    the positions it keeps), as `(s, depth, path, usr, vf, vt, size, n_files, p3)` rows (`p3` = the suffix's first
    three characters), optionally restricted to a `p3` range."""
    lst = "[" + ", ".join(q(f) for f in files) + "]"
    return f"""SELECT substring(l, p) AS s, depth, path, usr, vf, vt, size, n_files, p3 FROM (
            SELECT *, substring(l, p, 3) AS p3 FROM (
                SELECT depth, path, usr, vf, vt, size, n_files, l, unnest(generate_series(1, length(l) - 2)) AS p
                FROM (SELECT depth, path, usr, vf, vt, size, n_files, {NAME} AS l FROM read_parquet({lst}) WHERE depth >= 1)
                WHERE length(l) >= 3)
            WHERE {kept_sql('l', 'p', rule)} {f'AND {rng}' if rng else ''})"""


def _shard_table(con, plan: dict) -> None:
    """`sh(lo, shard, grp)`: each shard's first prefix and the task group building it (for an ASOF join)."""
    grp = {i: t["t"] for t in plan["tasks"] for i in t["shards"]}
    con.execute("DROP TABLE IF EXISTS sh")
    con.execute("CREATE TABLE sh (lo VARCHAR, shard INTEGER, grp INTEGER)")
    con.executemany("INSERT INTO sh VALUES (?, ?, ?)", [(s["lo"], s["i"], grp[s["i"]]) for s in plan["shards"]])


def map_range(interval_file: str, plan: dict, i: int, out: Path, con, rule: HexRule | None = None) -> dict:
    """The map side of the suffix shuffle: one range's intervals → its suffix rows tagged with their shard,
    written per task group as `sxmap/g###/r####-*.parquet` under `out` (each group's reduce task reads
    only its own directory), so the intervals are expanded once in all, not once per task."""
    t0 = monotonic()
    _shard_table(con, plan)
    part = out / f"map-{i}"
    if part.exists():
        shutil.rmtree(part)
    out.mkdir(parents=True, exist_ok=True)
    con.execute(f"""COPY (SELECT x.s, x.depth, x.path, x.usr, x.vf, x.vt, x.size, x.n_files, sh.shard, sh.grp
        FROM ({suffix_sql([interval_file], rule=rule)}) AS x ASOF JOIN sh ON x.p3 >= sh.lo
    ) TO {q(str(part))} (FORMAT parquet, PARTITION_BY (grp), COMPRESSION zstd)""")
    rows = 0
    for d in sorted(part.glob("grp=*")):
        g = int(d.name.split("=")[1])
        dst = out / "sxmap" / f"g{g:03d}"
        dst.mkdir(parents=True, exist_ok=True)
        for k, f in enumerate(sorted(d.glob("*.parquet"))):
            rows += pq.ParquetFile(f).metadata.num_rows
            shutil.move(str(f), dst / f"r{i:04d}-{k}.parquet")
    shutil.rmtree(part)
    doc = {"range": i, "rows": rows, "s": round(monotonic() - t0, 1)}
    err(f"map range {i}: {rows:,} suffix rows in {doc['s']}s")
    return doc


def build_shards(inputs: list[str], plan: dict, t: int, out: Path, *, threads: int, mem: str, tmp: Path) -> dict:
    """The reduce side: task `t`'s map outputs (`sxmap/g{t}/`, rows tagged with their shard) partitioned
    by shard on local disk in one pass; then each shard sorted `(s, path, usr, vf)` and written as
    `sx/s####.parquet` in `SX_RG`-row groups, with its per-row-group sidecar `sidecar/s####.parquet`."""
    t0 = monotonic()
    task = plan["tasks"][t]
    shards = [plan["shards"][i] for i in task["shards"]]
    con = connect(threads, mem, tmp)
    part = tmp / f"part-{t}"
    if part.exists():
        shutil.rmtree(part)
    tmp.mkdir(parents=True, exist_ok=True)
    lst = "[" + ", ".join(q(f) for f in inputs) + "]"
    if inputs:
        con.execute(f"""COPY (SELECT s, depth, path, usr, vf, vt, size, n_files, shard FROM read_parquet({lst}))
            TO {q(str(part))} (FORMAT parquet, PARTITION_BY (shard), COMPRESSION zstd)""")
    t_pass = monotonic() - t0
    docs = []
    for s in shards:
        t1 = monotonic()
        name = f"s{s['i']:04d}"
        src = part / f"shard={s['i']}"
        dst = out / "sx" / f"{name}.parquet"
        stats: list[tuple[str, str, int]] = []

        def on_group(g: pa.Table) -> None:
            col = g.column("s")
            stats.append((col[0].as_py(), col[g.num_rows - 1].as_py(), g.num_rows))

        if src.exists():
            sql = f"SELECT s, depth, path, usr, vf, vt, size, n_files FROM read_parquet({q(str(src / '*.parquet'))}) ORDER BY s, path, usr, vf"
            rows = write_sorted((_sx_cast(b) for b in _batches(con, sql)), dst, SX_SCHEMA, SX_RG, on_group=on_group, dictionary=["usr"])
        else:
            rows = write_sorted(iter(()), dst, SX_SCHEMA, SX_RG, on_group=on_group, dictionary=["usr"])
        side = sidecar_rows(dst, f"sx/{name}.parquet", stats)
        (out / "sidecar").mkdir(parents=True, exist_ok=True)
        pq.write_table(side, out / "sidecar" / f"{name}.parquet", compression=CODEC)
        if src.exists():
            shutil.rmtree(src)
        doc = {"shard": s["i"], "rows": rows, "expected": s["rows"], "groups": len(stats), "bytes": dst.stat().st_size,
               "s": round(monotonic() - t1, 1)}
        if rows != s["rows"]:
            raise RuntimeError(f"shard {s['i']}: {rows:,} rows written, {s['rows']:,} planned")
        err(f"shard {s['i']}: {rows:,} rows, {doc['bytes']:,} B in {doc['s']}s")
        docs.append(doc)
    return {"task": t, "pass_s": round(t_pass, 1), "shards": docs, "s": round(monotonic() - t0, 1)}


def sidecar_rows(path: Path, name: str, stats: list[tuple[str, str, int]]) -> pa.Table:
    """Per row group: its `s` range (from the rows written, never truncated statistics) and the
    contiguous byte span of its column chunks."""
    md = pq.ParquetFile(path).metadata
    if md.num_row_groups != len(stats):
        raise RuntimeError(f"{path}: {md.num_row_groups} row groups, {len(stats)} recorded")
    rows = {k: [] for k in SIDECAR_SCHEMA.names}
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        starts = [rg.column(c).dictionary_page_offset or rg.column(c).data_page_offset for c in range(rg.num_columns)]
        ends = [st + rg.column(c).total_compressed_size for c, st in enumerate(starts)]
        lo, hi, n = stats[g]
        if n != rg.num_rows:
            raise RuntimeError(f"{path} rg {g}: {rg.num_rows} rows, {n} recorded")
        for k, v in zip(SIDECAR_SCHEMA.names, (name, g, lo, hi, min(starts), max(ends) - min(starts), n)):
            rows[k].append(v)
    return pa.table(rows, schema=SIDECAR_SCHEMA)


# ── Reader (the Worker's logic) ────────────────────────────────────────────


class Spans:
    """A seekable file over a few cached byte spans (the footer and the fetched range)."""

    def __init__(self, size: int, spans: list[tuple[int, bytes]]):
        self.size, self.spans, self.pos = size, spans, 0
        self.closed = False

    def seekable(self): return True
    def readable(self): return True
    def writable(self): return False
    def tell(self): return self.pos
    def close(self): self.closed = True

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self.size - self.pos
        n = min(n, self.size - self.pos)
        for start, data in self.spans:
            if start <= self.pos and self.pos + n <= start + len(data):
                out = data[self.pos - start:self.pos - start + n]
                self.pos += n
                return out
        raise IOError(f"read outside cached spans: {self.pos}+{n}")


class Reader:
    """Answer a literal from the suffix shards: the sidecar locates the row groups whose `s` range can
    hold suffixes starting with it (contiguous, in one or two files), one ranged read per file, then
    the first-hit filter and per-bucket sums for each date."""

    def __init__(self, fetch, size_of, sidecar: pa.Table, rule: HexRule | None = None):
        self.fetch, self.size_of, self.rule = fetch, size_of, rule
        side = sidecar.sort_by([("file", "ascending"), ("rg", "ascending")]).to_pylist()
        self.groups = side
        self.mins = [g["s_min"] for g in side]
        self.maxs = [g["s_max"] for g in side]
        self.footers: dict[str, tuple[int, bytes]] = {}

    def footer(self, file: str) -> tuple[int, bytes]:
        if file not in self.footers:
            size = self.size_of(file)
            tail = self.fetch(file, size - 8, size)
            flen = int.from_bytes(tail[:4], "little")
            # pyarrow's first read is a speculative tail of up to 64 KiB: cache at least that much
            self.footers[file] = (size, self.fetch(file, max(0, size - max(8 + flen, 1 << 16)), size))
        return self.footers[file]

    def rows(self, term: str) -> tuple[list[dict], dict]:
        from bisect import bisect_left

        key = term.lower()
        a = bisect_left(self.maxs, key)
        b = bisect_left(self.mins, key + "\U0010ffff")
        sel = self.groups[a:b]
        by_file: dict[str, list[dict]] = {}
        for g in sel:
            by_file.setdefault(g["file"], []).append(g)
        out: list[dict] = []
        io = {"groups": len(sel), "bytes": 0, "rows_read": 0, "files": len(by_file)}
        for file, gs in by_file.items():
            lo = gs[0]["offset"]
            hi = gs[-1]["offset"] + gs[-1]["length"]
            data = self.fetch(file, lo, hi)
            io["bytes"] += len(data)
            size, foot = self.footer(file)
            pf = pq.ParquetFile(Spans(size, [(size - len(foot), foot), (lo, data)]))
            tab = pf.read_row_groups([g["rg"] for g in gs])
            io["rows_read"] += tab.num_rows
            out += [r for r in tab.to_pylist() if r["s"].startswith(key)]
        return out, io

    def answer(self, term: str, dates: list[str]) -> dict:
        """Per date: `{bucket: [bytes, objects]}` of the first hits live on that scan."""
        key = term.lower()
        hit, io = self.rows(key)
        return answer_rows(key, hit, io, dates, self.rule)


def answer_rows(key: str, rows: list[dict], io: dict, dates: list[str], rule: HexRule | None = None) -> dict:
    """`Reader.answer` over a literal's range rows (`key` lowercased): the rows whose name it occurs in (`hex_runs.occurs`
    under the generation's `rule`; plain containment without one), and per date the first hits live on it summed per
    bucket (the parent test is `occurs` too)."""
    hit = [r for r in rows if first_occurrence(key, r["path"].rsplit("/", 1)[-1].lower(), rule) >= 0]
    answers = {}
    for d in dates:
        D = scan_epoch(d) * 1000
        seen, totals = set(), {}
        for r in hit:
            vf, vt = _ms(r["vf"]), _ms(r["vt"])
            if not (vf <= D < vt) or r["depth"] < 1:
                continue
            k = (r["path"], r["usr"], vf)
            if k in seen:
                continue
            seen.add(k)
            parent = r["path"].rsplit("/", 1)[0] if "/" in r["path"] else ""
            if occurs(key, parent.lower(), rule):
                continue
            bkt = r["path"].split("/", 1)[0]
            b_, o_ = totals.get(bkt, (0, 0))
            totals[bkt] = (b_ + r["size"], o_ + r["n_files"])
        answers[d] = {k: list(v) for k, v in sorted(totals.items())}
    return {"q": key, "io": io, "rows_matching": len(hit), "answers": answers}


def _ms(v) -> int:
    return int(v.timestamp() * 1000) if hasattr(v, "timestamp") else int(v)


def gcs_reader(bucket: str, prefix: str, rule: HexRule | None = None) -> Reader:
    from google.cloud import storage

    b = storage.Client().bucket(bucket)
    side = pq.read_table(_download(b, f"{prefix}/sidecar.parquet"))
    sizes: dict[str, int] = {}

    def size_of(file: str) -> int:
        if file not in sizes:
            blob = b.get_blob(f"{prefix}/{file}")
            sizes[file] = int(blob.size)
        return sizes[file]

    def fetch(file: str, lo: int, hi: int) -> bytes:
        return b.blob(f"{prefix}/{file}").download_as_bytes(start=lo, end=hi - 1)

    return Reader(fetch, size_of, side, rule)


def _download(bucket, key: str) -> pa.BufferReader:
    return pa.BufferReader(bucket.blob(key).download_as_bytes())


# ── GCS IO ─────────────────────────────────────────────────────────────────


def upload_tree(local: Path, bucket: str, prefix: str, *, workers: int = 8, last: tuple[str, ...] = ()) -> list[dict]:
    """Upload every file under `local` to `gs://bucket/prefix/<rel>`; returns `{key, size, md5}` per file. `last`: relative
    paths uploaded only after every other file is (a completeness marker)."""
    from concurrent.futures import ThreadPoolExecutor

    from google.cloud import storage

    b = storage.Client().bucket(bucket)
    files = sorted(p for p in local.rglob("*") if p.is_file() and p.relative_to(local).as_posix() not in last)
    tail = [local / r for r in last if (local / r).is_file()]

    def one(p: Path) -> dict:
        key = f"{prefix}/{p.relative_to(local).as_posix()}"
        blob = b.blob(key)
        blob.chunk_size = 64 << 20
        blob.upload_from_filename(str(p), checksum="crc32c")
        blob.reload()
        return {"key": key, "size": int(blob.size), "md5": base64.b64decode(blob.md5_hash).hex() if blob.md5_hash else None}

    with ThreadPoolExecutor(workers) as ex:
        out = list(ex.map(one, files))
    return out + [one(p) for p in tail]


def read_json(uri: str) -> dict:
    if uri.startswith("gs://"):
        from google.cloud import storage

        bucket, key = uri[5:].split("/", 1)
        return json.loads(storage.Client().bucket(bucket).blob(key).download_as_bytes())
    return json.loads(Path(uri).read_text())


def read_text(uri: str) -> str:
    if uri.startswith("gs://"):
        from google.cloud import storage

        bucket, key = uri[5:].split("/", 1)
        return storage.Client().bucket(bucket).blob(key).download_as_bytes().decode()
    return Path(uri).read_text()


def manifest(bucket: str, prefix: str, sub: str) -> list[dict]:
    """`{key, size, md5, generation}` of every object under `prefix/sub/`, sorted."""
    from google.cloud import storage

    out = []
    for blob in storage.Client().list_blobs(bucket, prefix=f"{prefix}/{sub}/"):
        out.append({"key": blob.name.removeprefix(prefix + "/"), "size": int(blob.size), "md5": base64.b64decode(blob.md5_hash).hex() if blob.md5_hash else None,
                    "crc32c": blob.crc32c, "generation": int(blob.generation)})
    return sorted(out, key=lambda o: o["key"])


# ── CLI ────────────────────────────────────────────────────────────────────


@group("static-names")
def cli() -> None:
    """Static name search: interval and suffix-shard builds (specs/architecture/static-name-search.md)."""


def _task(index: int | None) -> int:
    if index is not None:
        return index
    v = os.environ.get("BATCH_TASK_INDEX")
    if v is None:
        raise SystemExit("no task index: pass -i or run as a Batch task ($BATCH_TASK_INDEX)")
    return int(v)


@cli.command("profile")
@option("-f", "--field", "field_name", help="Print only this field (bare: `gen` → its value), e.g. a deploy's check of the site's STATIC_GEN")
def profile_cmd(field_name: str | None) -> None:
    """Print the resolved deployment profile ($STATIC_NAMES_PROFILE, env overrides on top) as JSON — a deployment's
    `job/static-names.json` starts as `STATIC_NAMES_PROFILE=<example> STATIC_NAMES_GEN=<gen> … profile`; the R2 endpoint
    is left out (always env). `-f`: one field; a missing one (`gen` unset) is an error naming its variable."""
    from .static_profile import ENV, to_mapping

    p = profile()
    if field_name is None:
        # Never the R2 endpoint: it names the Cloudflare account and is always env (`R2_ENDPOINT`), so an ambient one
        # mustn't land in a tracked profile file.
        print(json.dumps({k: v for k, v in to_mapping(p).items() if k != "r2_endpoint"}, indent=1))
        return
    if field_name not in ENV:
        raise SystemExit(f"static names profile: no field {field_name!r} (one of {', '.join(ENV)})")
    if field_name == "compact_level":
        from .static_profile import level_arg
        print(level_arg(p.compact_level))
        return
    v = p.need(field_name)
    print(v if isinstance(v, str) else json.dumps(to_mapping(p)[field_name]))


@cli.command("scans")
@option("-b", "--bucket", default=data_bucket, help="Data bucket")
@option("-L", "--layout", "layouts", multiple=True, help="Key template of the scans' `path` sorts, `{id}` the scan id and `{gen}` the store generation; repeat (default: the profile's, $STATIC_NAMES_LAYOUTS)")
@option("-s", "--start", help="First scan id (inclusive)")
@option("-t", "--through", help="Last scan id (inclusive)")
def scans_cmd(bucket: str, layouts: tuple[str, ...], start: str | None, through: str | None) -> None:
    """Print the scans manifest (JSON): each scan's newest `path` sort, pinned by generation and md5, and the hex-run
    rule the generation is built with (the profile's, `hex_runs`; none recorded = the full index)."""
    from google.cloud import storage

    from .hex_runs import rule_json

    doc = {**list_scans(bucket, layouts=layouts or profile_layouts(), start=start, through=through), **rule_json(profile().hex_rule())}
    b = storage.Client().bucket(bucket)
    for s in doc["scans"]:
        s["version"] = _version_from_footer(b, s["src"])
    print(json.dumps(doc, indent=1))


def _version_from_footer(bucket, key: str) -> int:
    """The source format from the parquet schema (footer read; the column names decide)."""
    blob = bucket.get_blob(key)
    size = int(blob.size)
    tail = blob.download_as_bytes(start=size - 8, end=size - 1)
    flen = int.from_bytes(tail[:4], "little")
    # The schema leads the footer's thrift: a few KB reach every column name.
    head = blob.download_as_bytes(start=size - 8 - flen, end=min(size - 9, size - 8 - flen + 16383))
    if b"n_files" in head and b"kind" in head:
        return 2
    if b"wts" in head and b"wb" in head:
        return 1
    raise ValueError(f"{key}: unknown source format")


@cli.command("ranges")
@option("-k", "--ranges", "k", default=128, type=IntRange(min=1), help="Number of key ranges")
@option("-m", "--mount", help="Local mount of the data bucket (default: read gs:// directly)")
@argument("scans_json")
def ranges_cmd(k: int, mount: str | None, scans_json: str) -> None:
    """Print `k` `(depth, path)` key ranges of about equal input rows (JSON)."""
    print(json.dumps(plan_ranges(read_json(scans_json), k, mount), indent=1))


@cli.command("intervals")
@option("-b", "--bucket", default=data_bucket, help="Output bucket")
@option("-C", "--coalesced", is_flag=True, help="The coalesced versions straight from the scans (`cintervals/`, `chist/`; `cdone/r####.json` marks a range done), not the full intervals")
@option("-g", "--gen", required=True, help="Output generation: gs://BUCKET/static-names/GEN/")
@option("-f", "--force", is_flag=True, help="Rebuild ranges whose digest is already uploaded")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task: task t builds ranges [t·n, (t+1)·n)")
@option("-o", "--out", default="/stage/out", help="Local output dir (uploaded, then removed)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-r", "--range", "only", help="Comma-separated range indices (overrides -i/-n; a partial build)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
def intervals_cmd(bucket, coalesced, gen, force, index, mount, mem, per_task, out, threads, only, tmp, no_upload) -> None:
    """Build key ranges' intervals over every scan of the generation's `scans.json`, one connection
    per task (each range uploaded as it finishes; its digest, written last, marks it done). `-C`: the
    coalesced versions in one pass instead, byte-identical to `intervals` then `coalesce`."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    scans = read_json(f"gs://{bucket}/{prefix}/scans.json")
    ranges = read_json(f"gs://{bucket}/{prefix}/ranges.json")
    if only:
        todo = [int(x) for x in only.split(",")]
    else:
        t = _task(index)
        todo = list(range(t * per_task, min((t + 1) * per_task, ranges["k"])))
    b = storage.Client().bucket(bucket)
    con = connect(threads, mem, tmp)
    import duckdb

    err(f"intervals: duckdb {duckdb.__version__}, pyarrow {pa.__version__}, ranges {todo}")
    marker = "cdone" if coalesced else "digest"
    for i in todo:
        if not force and b.blob(f"{prefix}/{marker}/r{i:04d}.json").exists():
            err(f"range {i}: already built")
            continue
        outp = Path(out) / f"r{i}"
        doc = build_range(scans, ranges, i, outp, mount=mount, threads=threads, mem=mem, tmp=Path(tmp), con=con, coalesced=coalesced,
                          rule=gen_rule(scans))
        if coalesced:
            if not no_upload:
                upload_tree(outp, bucket, prefix)
                b.blob(f"{prefix}/cdone/r{i:04d}.json").upload_from_string(json.dumps(doc, sort_keys=True) + "\n")
                shutil.rmtree(outp)
            print(json.dumps(doc), flush=True)
            continue
        if not no_upload:
            digest = outp / "digest"
            moved = Path(out) / f"r{i}-digest"
            shutil.move(str(digest), moved)
            upload_tree(outp, bucket, prefix)
            upload_tree(moved.parent / moved.name, bucket, f"{prefix}/digest")
            shutil.rmtree(outp)
            shutil.rmtree(moved)
        print(json.dumps({k: v for k, v in doc.items() if k != "digests"}), flush=True)


@cli.command("append")
@option("-b", "--bucket", default=data_bucket, help="Output bucket")
@option("-f", "--from-gen", "from_gen", required=True, help="Generation whose intervals the scan is appended to")
@option("-g", "--gen", required=True, help="Output generation (its scans.json = FROM's plus the scan)")
@option("-i", "--index", type=int, help="Range index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
def append_cmd(bucket, from_gen, gen, index, mount, mem, out, threads, tmp, no_upload) -> None:
    """Append the newest scan of GEN's `scans.json` to one range of FROM's intervals."""
    prefix = f"{PREFIX}/{gen}"
    scans = read_json(f"gs://{bucket}/{prefix}/scans.json")
    ranges = read_json(f"gs://{bucket}/{prefix}/ranges.json")
    prev_scans = read_json(f"gs://{bucket}/{PREFIX}/{from_gen}/scans.json")
    if scans["scans"][:-1] != prev_scans["scans"]:
        raise SystemExit(f"{gen}'s scans are not {from_gen}'s plus one")
    i = _task(index)
    src = Path(mount) / PREFIX / from_gen / "intervals" / f"r{i:04d}.parquet" if mount else None
    if src is None:
        raise SystemExit("append reads the previous intervals through the bucket mount (-m)")
    outp = Path(out) / f"r{i}"
    doc = append_range(src, scans["scans"][-1], ranges, i, outp, bucket=scans["bucket"], mount=mount, threads=threads, mem=mem, tmp=Path(tmp))
    if not no_upload:
        upload_tree(outp, bucket, prefix)
        shutil.rmtree(outp)
    print(json.dumps(doc))


@cli.command("coalesce")
@option("-b", "--bucket", default=data_bucket, help="Output bucket (the scratch bucket for a measurement)")
@option("-f", "--force", is_flag=True, help="Redo ranges whose stats are already uploaded")
@option("-g", "--gen", required=True, help="Output generation: gs://BUCKET/static-names/GEN/{cintervals,chist,cstats}/")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-I", "--intervals-gen", required=True, help="Generation whose intervals to coalesce (read through the mount)")
@option("-m", "--mount", required=True, help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-r", "--range", "only", help="Comma-separated range indices (overrides -i/-n)")
@option("-t", "--terms", help="A file of literals (one per line; a path or gs:// URL) whose range rows to count before and after")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def coalesce_cmd(bucket, force, gen, index, intervals_gen, mount, mem, per_task, out, threads, only, terms, tmp) -> None:
    """Coalesce ranges' intervals to the versions a name answer distinguishes (`size`, `n_files`): each
    range's `cintervals/`, `chist/` and `cstats/` (written last: marks the range done)."""
    from google.cloud import storage

    ranges = read_json(f"gs://{data_bucket()}/{PREFIX}/{intervals_gen}/ranges.json")
    todo = [int(x) for x in only.split(",")] if only else list(range(_task(index) * per_task, min((_task(index) + 1) * per_task, ranges["k"])))
    lits = [x for x in read_text(terms).splitlines() if x.strip()] if terms else None
    prefix = f"{PREFIX}/{gen}"
    b = storage.Client().bucket(bucket)
    con = connect(threads, mem, tmp)
    for i in todo:
        if not force and b.blob(f"{prefix}/cstats/r{i:04d}.json").exists():
            err(f"coalesce range {i}: already done")
            continue
        outp = Path(out) / f"c{i}"
        stats = coalesce_range(f"{mount}/{PREFIX}/{intervals_gen}/intervals/r{i:04d}.parquet", i, outp, con, lits, gen_rule_at(bucket, gen))
        moved = Path(out) / f"c{i}-stats"
        shutil.move(str(outp / "cstats"), moved)
        upload_tree(outp, bucket, prefix)
        upload_tree(moved, bucket, f"{prefix}/cstats")
        shutil.rmtree(outp)
        shutil.rmtree(moved)
        print(json.dumps(stats), flush=True)


@cli.command("coalesce-report")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation holding `cstats/` and `chist/`")
@option("-H", "--hist-gen", required=True, help="Generation holding the uncoalesced `hist/`")
@option("-v", "--threshold", "thresholds", multiple=True, type=int, default=[100_000, 150_000], help="Range-row thresholds to count prefixes at; repeat")
def coalesce_report_cmd(bucket, gen, hist_gen, thresholds) -> None:
    """Sum the coalesce stage's per-range stats (versions, suffix rows, per-term range rows, before → after) and
    compare three-character prefixes' rows before and after (how many reach each threshold). JSON."""
    import tempfile

    import duckdb
    from google.cloud import storage

    client = storage.Client()
    tot: dict = {"ranges": 0, "versions": [0, 0], "dir_versions": [0, 0], "suffix_rows": [0, 0], "terms": {}}
    for blob in client.list_blobs(bucket, prefix=f"{PREFIX}/{gen}/cstats/"):
        d = json.loads(blob.download_as_bytes())
        tot["ranges"] += 1
        for k in ("versions", "dir_versions", "suffix_rows"):
            tot[k] = [tot[k][0] + d[k][0], tot[k][1] + d[k][1]]
        for t, (x, y) in d.get("terms", {}).items():
            cur = tot["terms"].setdefault(t, [0, 0])
            cur[0] += x
            cur[1] += y
    with tempfile.TemporaryDirectory() as tmpd:
        sets = {}
        for name, g, sub in (("before", hist_gen, "hist"), ("after", gen, "chist")):
            dd = Path(tmpd) / name
            dd.mkdir()
            for blob in client.list_blobs(data_bucket() if name == "before" else bucket, prefix=f"{PREFIX}/{g}/{sub}/"):
                blob.download_to_filename(str(dd / Path(blob.name).name))
            sets[name] = str(dd / "*.parquet")
        con = duckdb.connect()
        rows = con.execute(f"""SELECT coalesce(a.p3, b.p3), coalesce(a.n, 0), coalesce(b.n, 0) FROM
            (SELECT p3, sum(n) AS n FROM read_parquet({q(sets['before'])}) GROUP BY p3) AS a FULL OUTER JOIN
            (SELECT p3, sum(n) AS n FROM read_parquet({q(sets['after'])}) GROUP BY p3) AS b USING (p3)""").fetchall()
    tot["prefixes"] = {str(v): [sum(1 for _, x, _ in rows if x >= v), sum(1 for _, _, y in rows if y >= v)] for v in thresholds}
    tot["heaviest"] = [[p, int(x), int(y)] for p, x, y in sorted(rows, key=lambda r: -r[1])[:20]]
    tot["ratio"] = {k: round(tot[k][1] / max(tot[k][0], 1), 4) for k in ("versions", "dir_versions", "suffix_rows")}
    print(json.dumps(tot, indent=1))


@cli.command("derive")
@option("-b", "--bucket", default=data_bucket, help="Data bucket")
@option("-f", "--from-gen", "from_gen", required=True, help="Generation whose scans, key ranges and coalesced versions to reuse")
@option("-g", "--gen", required=True, help="New generation (refused if its `scans.json` exists)")
@option("-n", "--dry-run", is_flag=True, help="Print the new `scans.json`; write nothing")
def derive_cmd(bucket, from_gen, gen, dry_run) -> None:
    """Start generation GEN over FROM's scans with the profile's hex-run rule: FROM's coalesced versions (`cintervals/`,
    which the rule doesn't change; copied in the bucket, so GEN's stages and runs read their own) and `ranges.json`, then
    GEN's `scans.json` (FROM's, with the rule recorded), last. The build goes on at `hist`."""
    from google.cloud import storage

    from .hex_runs import rule_json

    b = storage.Client().bucket(bucket)
    if b.blob(f"{PREFIX}/{gen}/scans.json").exists():
        raise SystemExit(f"gs://{bucket}/{PREFIX}/{gen}/scans.json exists: a generation is never rewritten")
    scans = read_json(f"gs://{bucket}/{PREFIX}/{from_gen}/scans.json")
    doc = {**{k: v for k, v in scans.items() if k != "hex_runs"}, **rule_json(profile().hex_rule()), "derived_from": from_gen}
    ranges = read_text(f"gs://{bucket}/{PREFIX}/{from_gen}/ranges.json")
    src = sorted((x for x in b.client.list_blobs(bucket, prefix=f"{PREFIX}/{from_gen}/cintervals/")), key=lambda x: x.name)
    if len(src) != json.loads(ranges)["k"]:
        raise SystemExit(f"{from_gen} has {len(src)} cintervals files for {json.loads(ranges)['k']} ranges")
    if not dry_run:
        for x in src:
            dst = f"{PREFIX}/{gen}/cintervals/{Path(x.name).name}"
            if not b.blob(dst).exists():
                b.copy_blob(x, b, dst)
        b.blob(f"{PREFIX}/{gen}/ranges.json").upload_from_string(ranges)
        b.blob(f"{PREFIX}/{gen}/scans.json").upload_from_string(json.dumps(doc, indent=1) + "\n")
    print(json.dumps({k: v for k, v in doc.items() if k != "scans"} | {"scans": len(doc["scans"]), "cintervals": len(src),
                                                                        "cintervals_bytes": sum(int(x.size) for x in src)}, indent=1))


@cli.command("hist")
@option("-b", "--bucket", default=data_bucket, help="Output bucket")
@option("-f", "--force", is_flag=True, help="Redo ranges already counted")
@option("-g", "--gen", required=True, help="Generation (its `scans.json` rule): writes `chist/r####.parquet`")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-I", "--intervals-gen", help="Generation whose `cintervals/` to count (default: GEN's own)")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def hist_cmd(bucket, force, gen, index, intervals_gen, mount, mem, per_task, threads, tmp) -> None:
    """Per range, the suffix rows per three-character prefix of `-I`'s coalesced versions under GEN's hex-run rule
    (`chist/`, for `plan-shards -H chist`): a derived generation's histograms."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    rule = gen_rule(read_json(f"gs://{bucket}/{prefix}/scans.json"))
    intervals_gen = intervals_gen or gen
    ranges = read_json(f"gs://{bucket}/{PREFIX}/{intervals_gen}/ranges.json")
    t = _task(index)
    b = storage.Client().bucket(bucket)
    con = connect(threads, mem, tmp)
    for i in range(t * per_task, min((t + 1) * per_task, ranges["k"])):
        key = f"{prefix}/chist/r{i:04d}.parquet"
        if not force and b.blob(key).exists():
            err(f"hist range {i}: already counted")
            continue
        t0 = monotonic()
        src = f"read_parquet({q(f'{mount}/{PREFIX}/{intervals_gen}/cintervals/r{i:04d}.parquet')})"
        tab = con.execute(hist_sql(src, rule)).to_arrow_table()
        sink = pa.BufferOutputStream()
        pq.write_table(tab, sink, compression=CODEC)
        b.blob(key).upload_from_string(sink.getvalue().to_pybytes())
        n = sum(tab.column("n").to_pylist())
        print(json.dumps({"range": i, "suffix_rows": n, "s": round(monotonic() - t0, 1)}), flush=True)


@cli.command("plan-shards")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-H", "--hist", "hist_dir", default="hist", help="Histogram subdir (`chist`: the coalesced versions')")
@option("-n", "--target-rows", default=50_000_000, type=int, help="Suffix rows per shard file")
@option("-t", "--tasks", default=32, type=int, help="Batch tasks (contiguous groups of shards)")
def plan_shards_cmd(bucket, gen, hist_dir, target_rows, tasks) -> None:
    """Print the shard plan (JSON) from the generation's per-range prefix histograms."""
    import tempfile

    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    b = storage.Client().bucket(bucket)
    with tempfile.TemporaryDirectory() as d:
        paths = []
        for blob in storage.Client().list_blobs(bucket, prefix=f"{prefix}/{hist_dir}/"):
            p = Path(d) / Path(blob.name).name
            b.blob(blob.name).download_to_filename(str(p))
            paths.append(p)
        if not paths:
            raise SystemExit(f"no histograms under gs://{bucket}/{prefix}/{hist_dir}/")
        print(json.dumps(plan_shards(sorted(paths), target_rows, tasks), indent=1))


@cli.command("suffix-map")
@option("-b", "--bucket", default=data_bucket, help="Output bucket")
@option("-C", "--coalesced", is_flag=True, help="Expand GEN's coalesced versions (`cintervals/`) instead of intervals")
@option("-f", "--force", is_flag=True, help="Redo ranges already mapped")
@option("-g", "--gen", required=True, help="Generation (its `shards.json` plan; intervals from -I or GEN)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-I", "--intervals-gen", help="Generation whose intervals to read (default: GEN)")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-r", "--range", "only", help="Comma-separated range indices (overrides -i/-n)")
@option("-S", "--scratch", default=scratch_bucket, help="Bucket for the shuffle (`sxmap/`, `sxmap-done/`)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
def suffix_map_cmd(bucket, coalesced, force, gen, index, intervals_gen, mount, mem, per_task, out, threads, only, scratch, tmp) -> None:
    """Expand ranges' intervals into suffix rows tagged with their shard, written per reduce task
    (`gs://SCRATCH/static-names/GEN/sxmap/g###/`); a range's `sxmap-done/r####.json` there marks it done."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/shards.json")
    rule = gen_rule(read_json(f"gs://{bucket}/{prefix}/scans.json"))
    ig = intervals_gen or gen
    ranges = read_json(f"gs://{bucket}/{PREFIX}/{ig}/ranges.json")
    if only:
        todo = [int(x) for x in only.split(",")]
    else:
        t = _task(index)
        todo = list(range(t * per_task, min((t + 1) * per_task, ranges["k"])))
    b = storage.Client().bucket(scratch)
    con = connect(threads, mem, tmp)
    for i in todo:
        mark = f"{prefix}/sxmap-done/r{i:04d}.json"
        if not force and b.blob(mark).exists():
            err(f"map range {i}: already mapped")
            continue
        src = f"{mount}/{PREFIX}/{ig}/{'cintervals' if coalesced else 'intervals'}/r{i:04d}.parquet"
        outp = Path(out) / f"m{i}"
        doc = map_range(src, plan, i, outp, con, rule)
        upload_tree(outp, scratch, prefix)
        shutil.rmtree(outp)
        b.blob(mark).upload_from_string(json.dumps(doc) + "\n")
        print(json.dumps(doc), flush=True)


@cli.command("shards")
@option("-b", "--bucket", default=data_bucket, help="Output bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", required=True, help="Local mount of the bucket")
@option("-M", "--mem", default="100GB", help="DuckDB memory limit")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-P", "--partial", is_flag=True, help="Accept fewer mapped ranges than ranges (a dev build over some ranges)")
@option("-S", "--scratch", default=scratch_bucket, help="Bucket holding the shuffle (mounted beside -m: `<-m>/../SCRATCH`)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill + partition dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
def shards_cmd(bucket, gen, index, mount, mem, out, threads, partial, scratch, tmp, no_upload) -> None:
    """Build one task group's suffix shards (and their sidecars) from its map outputs."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    plan = read_json(f"gs://{bucket}/{prefix}/shards.json")
    t = _task(index)
    done = sum(1 for _ in storage.Client().list_blobs(scratch, prefix=f"{prefix}/sxmap-done/"))
    k = plan.get("ranges")
    if k is not None and done != k and not partial:
        raise SystemExit(f"{done} of {k} ranges mapped: pass -P for a partial (dev) build")
    files = sorted(str(f) for f in (Path(mount).parent / scratch / prefix / "sxmap" / f"g{t:03d}").glob("*.parquet"))
    outp = Path(out) / f"t{t}"
    doc = build_shards(files, plan, t, outp, threads=threads, mem=mem, tmp=Path(tmp))
    if not no_upload:
        upload_tree(outp, bucket, prefix)
        shutil.rmtree(outp)
    print(json.dumps(doc))


@cli.command("sidecar")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
def sidecar_cmd(bucket, gen) -> None:
    """Concatenate the per-shard sidecars into `sidecar.parquet` (sorted `(file, rg)`, which is `s`
    order) and print the suffix manifest (JSON): every shard file and the sidecar, with md5s."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    client = storage.Client()
    b = client.bucket(bucket)
    tables = [pq.read_table(_download(b, blob.name)) for blob in client.list_blobs(bucket, prefix=f"{prefix}/sidecar/")]
    side = pa.concat_tables(tables).sort_by([("file", "ascending"), ("rg", "ascending")])
    mins, maxs = side.column("s_min").to_pylist(), side.column("s_max").to_pylist()
    for k in range(1, len(mins)):
        if mins[k] < maxs[k - 1]:
            raise SystemExit(f"sidecar row {k}: s_min {mins[k]!r} < previous s_max {maxs[k - 1]!r}: shards overlap")
    sink = pa.BufferOutputStream()
    pq.write_table(side, sink, compression=CODEC, row_group_size=1 << 20)
    blob = b.blob(f"{prefix}/sidecar.parquet")
    blob.upload_from_string(sink.getvalue().to_pybytes(), content_type="application/vnd.apache.parquet")
    files = manifest(bucket, prefix, "sx")
    blob.reload()
    print(json.dumps({"gen": gen, "rows": int(sum(side.column("rows").to_pylist())), "row_groups": side.num_rows,
                      "bytes": sum(f["size"] for f in files), "files": files,
                      "sidecar": {"key": "sidecar.parquet", "size": int(blob.size), "md5": base64.b64decode(blob.md5_hash).hex() if blob.md5_hash else None,
                                  "generation": int(blob.generation)}}, indent=1))


@cli.command("manifest")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-g", "--gen", required=True, help="Generation")
@argument("subdirs", nargs=-1, required=True)
def manifest_cmd(bucket, gen, subdirs) -> None:
    """Print `{subdir: [{key, size, md5, generation}]}` for the generation's outputs (JSON); `digest`
    adds the per-scan opened/closed totals summed over ranges (counts, digests mod 2⁶⁴)."""
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    doc: dict = {"gen": gen}
    for sub in subdirs:
        doc[sub] = manifest(bucket, prefix, sub)
    if "digest" in subdirs:
        b = storage.Client().bucket(bucket)
        totals: dict[str, dict] = {}
        rows = 0
        for f in doc["digest"]:
            d = json.loads(b.blob(f"{prefix}/{f['key']}").download_as_bytes())
            rows += d["rows"]
            for ts, kinds in d["digests"].items():
                for kind, (n, h) in kinds.items():
                    cur = totals.setdefault(ts, {}).setdefault(kind, [0, 0])
                    cur[0] += n
                    cur[1] = (cur[1] + h) % U64
        doc["rows"] = rows
        doc["per_scan"] = {scan_label(int(ts)): v for ts, v in sorted(totals.items(), key=lambda kv: int(kv[0]))}
    print(json.dumps(doc, indent=1))


#: What the Worker reads (`r2-copy`): the shards, their sidecars, the plan and scans, and the catalog's served files
#: (not its census or per-shard cells).
R2_SERVED = ("sx/", "sidecar/", "sidecar.parquet", "shards.json", "scans.json", "catalog/cells.parquet", "catalog/index.parquet",
             "catalog/meta.json", "catalog/members.json",
             # the drilldown (static_roots): roots, rollups, their two-level indexes, aliases, meta
             "drill/meta.json", "drill/aliases.parquet", "drill/long/roots/", "drill/long/rollups/", "drill/short/roots/",
             "drill/short/rollups/", "drill/long-roots-index", "drill/long-rollups-index", "drill/short-roots-index",
             "drill/short-rollups-index",
             # anchored search (static_anchors): the name index, the `q$` / `^q$` rollups, their indexes, meta (not `anchors/keys*`, GCS only)
             "names/", "anchors/meta.json", "anchors/rollups/", "anchors/end-rollups-index", "anchors/exact-rollups-index",
             # its starts-with catalog (`^q`'s fleet root): rollups, index, meta (its own liveness marker)
             "anchors/start/meta.json", "anchors/start/rollups/", "anchors/start/start-rollups-index",
             # the runs' manifests (`static_append`; a run's own files are copied with `-g GEN/deltas/<run>`)
             "manifests/")


@cli.command("r2-verify")
@option("-b", "--bucket", default=data_bucket, help="Source GCS bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-m", "--manifest", "scan", required=True, help="The manifest: `manifests/<this>.json` (a scan id, or a merge's revision `<id>.mNNN`)")
@option("-w", "--workers", default=16, type=int, help="Parallel checks")
def r2_verify_cmd(bucket, gen, scan, workers) -> None:
    """Check that every served file (`R2_SERVED`) of every run a manifest lists is on R2 as on GCS (size, and md5 where both
    know it), before the manifest itself is copied: a manifest goes to R2 only once each run it lists is whole there.
    Exit 1, listing what's missing or different."""
    from .append_runner import r2_missing, r2_objects

    prefix = f"{PREFIX}/{gen}"
    runs = read_json(f"gs://{bucket}/{prefix}/manifests/{scan}.json")["runs"]
    objs = [o for r in runs for o in r2_objects(bucket, [f"{prefix}/{r['key']}/"], lambda k, r=r: k.removeprefix(f"{prefix}/{r['key']}/").startswith(R2_SERVED))]
    bad = r2_missing(objs, workers=workers)
    doc = {"gen": gen, "manifest": scan, "runs": [r["key"] for r in runs], "objects": len(objs), "missing": bad}
    print(json.dumps(doc, indent=1))
    if bad:
        err(f"r2-verify {gen} {scan}: {len(bad)} of {len(objs)} served files not on R2 (e.g. {bad[0]})")
        raise SystemExit(1)


@cli.command("r2-copy")
@option("-b", "--bucket", default=data_bucket, help="Source GCS bucket")
@option("-g", "--gen", required=True, help="Generation")
@option("-n", "--dry-run", is_flag=True, help="List what would be copied")
@option("-o", "--only", help="Copy only the served keys under this generation-relative prefix (e.g. `manifests/`)")
@option("-w", "--workers", default=8, type=int, help="Parallel copies")
@option("-x", "--exclude", multiple=True, help="Skip the served keys under this generation-relative prefix (repeatable; e.g. a run's `drill/meta.json`, copied last)")
def r2_copy_cmd(bucket, gen, dry_run, only, workers, exclude) -> None:
    """Copy the generation's served files (shards, sidecar, plan, scans) GCS → R2 under the same keys,
    skipping objects already there with the same size and md5 (`append_runner.r2_copy`: `publish.copy_one`'s streaming
    copy, the GCS md5 stamped as metadata). R2 via `R2_ENDPOINT`, `R2_BUCKET` and AWS_* (or R2_*) keys."""
    from .append_runner import r2_copy, r2_objects

    prefix = f"{PREFIX}/{gen}"

    def keep(key: str) -> bool:
        rel = key.removeprefix(prefix + "/")
        return rel.startswith(R2_SERVED) and (only is None or rel.startswith(only)) and not any(rel.startswith(x) for x in exclude)

    objs = r2_objects(bucket, [prefix + "/"], keep)
    doc = r2_copy(bucket, objs, workers=workers, dry_run=dry_run)
    err(f"r2-copy {gen}: {len(objs)} objects, {doc['copied'] if not dry_run else len(doc['keys'])} to copy ({doc['bytes']:,} B)")
    if dry_run:
        for k in doc["keys"]:
            print(k)
        return
    print(json.dumps({"gen": gen, **doc}))


@cli.command("query")
@option("-b", "--bucket", default=data_bucket, help="Bucket")
@option("-d", "--date", "dates", multiple=True, required=True, help="Scan date; repeat")
@option("-g", "--gen", required=True, help="Generation")
@argument("terms", nargs=-1, required=True)
def query_cmd(bucket, dates, gen, terms) -> None:
    """Answer literals from the suffix shards on GCS (sidecar → one range per file): one JSON line per
    term with its I/O and per-date `{bucket: [bytes, objects]}`."""
    r = gcs_reader(bucket, f"{PREFIX}/{gen}", gen_rule(read_json(f"gs://{bucket}/{PREFIX}/{gen}/scans.json")))
    for t in terms:
        t0 = monotonic()
        out = r.answer(t, list(dates))
        out["s"] = round(monotonic() - t0, 3)
        print(json.dumps(out), flush=True)


if __name__ == "__main__":
    cli()
