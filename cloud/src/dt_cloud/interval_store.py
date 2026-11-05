"""The change-interval path store (specs/interval-store.md): every scan's path-store rows as versions of
one row per `(depth, path)` — objects and dirs, owner slices folded into `us` — with `[vf, vt)` validity,
instead of a full per-scan copy. The build reuses the static name index's key ranges and pyrmts'
gaps-and-islands kernel (`static_names`); `cut` lays the versions out as the served sorts (`path`,
`bysize`, `reads`) with a `.groups.parquet` beside each, which a reader plans its range reads from. A generation built
with the attribution table (`ATTRIBUTION`, specs/attr-interval-table.md) also versions each covered scan's roster
attribution map and serves it as the `attribution` sort.

    dt-cloud interval-store build-ranges -g GEN [-i TASK -n PER_TASK | -r RANGES]   # per range: pv/, rd/, digest/
    dt-cloud interval-store churn -g GEN                                            # after fold: churn_cum on pvl/ (one task), `"churn": true` in scans.json
    dt-cloud interval-store cut -g GEN [-s SORT]                                    # served/{path,bysize,reads}.parquet + .groups.parquet
    dt-cloud interval-store build|add -g GEN -L LAYOUT -s SCANS -o STORE            # one machine, every stage (`interval_local`)

The store (the profile's `bucket`, or `-b`) is a URL root (`gs://`, `r2://`, `s3://`) or a local dir; a bare bucket name
is `gs://` (`blobstore.store_url`). Every key is read, written and listed through `blobstore.Store`; the range files are
read by DuckDB from `-m` (a mount of the store) or the store's URL (`gs://` or a local dir).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq
from click import Choice, IntRange, group, option

from . import heartbeat as hb
from . import static_names as sn
from . import tree_mem as tm
from .blobstore import Store, store_url
from .image_sha import expect_sha_options, log_job_stamp
from .interval_churn import has_churn, with_churn
from . import interval_level as ilv
from .interval_level import LEVEL, LEVEL_SCHEMA, leveled
from .static_names import OPEN, U64, connect, q, range_preds, write_sorted

err = partial(print, file=sys.stderr, flush=True)

PREFIX = "interval-store"


def load_profile(name: str | None) -> dict:
    """A deployment's profile (a JSON file it carries, e.g. its `job/interval-store.json`): its bucket and the
    scans the parity check samples. `$INTERVAL_STORE_PROFILE` names it when `-P` doesn't."""
    name = name or os.environ.get("INTERVAL_STORE_PROFILE")
    if not name:
        raise SystemExit("no profile: pass -P PATH or set $INTERVAL_STORE_PROFILE")
    return json.loads(Path(name).read_text())


def _bucket(bucket: str | None, profile: str | None) -> str:
    """The store's root URL: `-b`, else the profile's `bucket` (a bare bucket name: `gs://`)."""
    return store_url(bucket or load_profile(profile)["bucket"])


def _root(store: Store, prefix: str, mount: str | None) -> str:
    """Where DuckDB reads the generation's range files: the store's mount, else the store itself (`gs://`, a dir)."""
    return f"{mount}/{prefix}" if mount else store.uri(prefix)


#: A served sort's row-group size: the per-scan path store's (decode cost per group is the same).
SERVED_RG = 8192
#: A served sort's rows per data page, every column's pages aligned on them, with a page index (ColumnIndex +
#: OffsetIndex) so a reader decodes only the pages its rect and range can touch (specs/diff-progressive.md §3b, 2b.1).
#: Measured over gcs's 804 level requests: rows decoded fall about linearly with the page (p50 per request 132,717 at
#: whole 8,192-row groups; 9,216 at 1,024; 2,304 at 256; 1,152 at 128) while the ranged GETs stay ~flat; 256 is 44×
#: fewer rows than whole groups for 32 pages a group. Read at call time, so a test can cut tiny pages.
SERVED_PAGE_ROWS = 256
#: The range files' row-group size (an intermediate: only `cut` and the verifier read them).
RANGE_RG = 65536

KEY_COLS = ["depth", "path"]
#: The store's storage-class byte columns, by class id: their one definition. Every schema, statement, hash and reader
#: of the store names them from here (and the site's interval reader from `IV_CLASS_COLS`, `_lib/index.ts`), so a
#: deployment whose classes have names (gcs: Nearline, Coldline, Archive) renames them in one place; a new generation
#: (`backfill-generation`, which reads an older one's class columns by position) carries the rename.
CLASS_COLS = {2: "c2", 3: "c3", 4: "c4"}
CLS = list(CLASS_COLS.values())
#: The object's native version (GCS `generation`), opt-in per generation (`scans.json` `"generation": true`, set by a
#: build over sources that carry it, or by `backfill-generation`): absent from every file of a generation without it.
#: On object versions only (NULL on dirs), in the path versions (`pv/`, `pvl/`, a run's `pvl` deltas and state) and the
#: served `path` sort, as their last column (a reader's column span never reaches it). Carried from the version's first
#: scan that has one, never a change column: a GCS rewrite is a new object, created (and `updated`, the `mtime`) after
#: the scan that saw the old one, so a new generation always opens a version by `mtime` already.
GENERATION = "generation"
GENERATION_FIELD = pa.field(GENERATION, pa.int64())
OBJECT_VERSION_FIELD = pa.field("version", pa.string())
Identity = bool | pa.Field


def identity_column(identity: bool | pa.Field) -> str:
    return identity.name if isinstance(identity, pa.Field) else GENERATION


def change_cols(identity: bool | pa.Field) -> list[str]:
    return [*CHANGE_COLS, identity.name] if isinstance(identity, pa.Field) else CHANGE_COLS
#: The values whose change opens a version. `dr` is the size-weighted mean stamp rounded to the second
#: (the change test; the exact `wts` is carried from the version's first scan), `us` the owner slices.
#: `last_read` is not here: it moves from scan to scan on read paths and lives in its own intervals (`rd/`).
CHANGE_COLS = ["kind", "size", "n_files", "n_children", "n_desc", "mtime", "dr", "wb", *CLS, "us"]
CARRIED = {"wts": "first"}
STATE_COLS = [*CHANGE_COLS, "wts"]

PV_SCHEMA = pa.schema([
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("vt", pa.int64(), nullable=False),
    pa.field("kind", pa.string(), nullable=False),
    pa.field("size", pa.int64(), nullable=False),
    pa.field("n_files", pa.int64(), nullable=False),
    pa.field("n_children", pa.int64(), nullable=False),  # -1: the source had none (a v1 index)
    pa.field("n_desc", pa.int64(), nullable=False),
    pa.field("mtime", pa.int64(), nullable=False),
    pa.field("wts", pa.float64(), nullable=False),  # Σ mtime_mean·mtime_w over the path's rows
    pa.field("wb", pa.int64(), nullable=False),  # Σ mtime_w
    *(pa.field(c, pa.int64(), nullable=False) for c in CLS),
    pa.field("us", pa.string(), nullable=False),  # owner slices (`us_sql_from_raw`)
])
RD_SCHEMA = pa.schema([
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("vt", pa.int64(), nullable=False),
    pa.field("last_read", pa.int32(), nullable=False),
])
PV_COLS = PV_SCHEMA.names
#: Path versions with the read day folded in (`fold`): a version per run of equal change columns *and*
#: `last_read` (-1: never read). What the served path sorts hold.
PVL_SCHEMA = PV_SCHEMA.append(pa.field("last_read", pa.int32(), nullable=False))
#: Owner-slice versions (`build --slices`): one row per `(depth, path, usr)` — the per-scan store's own
#: rows, a v1 index's duplicates summed — with every value, `last_read` in the key, `usr` NULL where no
#: owner is named. What the slice sorts hold (a user lens, owner pools, owner totals).
SV_SCHEMA = pa.schema([
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("usr", pa.string()),
    *[f for f in PVL_SCHEMA if f.name not in ("depth", "path", "us")],
])
#: Owner slices with their path's total at each time (`fold -S`): what the total-keyed slice sort holds.
SVT_SCHEMA = SV_SCHEMA.append(pa.field("tot", pa.int64(), nullable=False))
SV_KEY = ["depth", "path", "usr"]
SV_CHANGE = [c for c in CHANGE_COLS if c != "us"] + ["last_read"]
SV_STATE = [*SV_CHANGE, "wts"]
SV_HASH = f"hash(depth, path, usr, kind, size, n_files, n_children, n_desc, mtime, dr, wb, {', '.join(CLS)}, last_read)"

#: The roster attribution table (specs/attr-interval-table.md): every attributing prefix's `(user, source, evidence)`
#: as versions, keyed `(depth, path)` as the path versions (so it shares their key ranges), from each covered scan's
#: `attribution.parquet` (`extras.ATTRIBUTION_FILE`). Opt-in per generation: a base built with it says
#: `"attribution": true` in `scans.json` (each covered scan's entry naming its file, `ATTRIBUTION_SRC`); a run of a
#: generation without it in the base opens it (the manifests' `"attribution"`: the run scans it covers). A scan without
#: a file is *uncovered*: it adds no input, so a version runs on across it, and readers answer "unknown" there.
ATTRIBUTION = "attribution"
ATTRIBUTION_SRC = "attribution_src"
ATTRIBUTION_CHANGE = ["user", "source", "evidence"]
AT_SCHEMA = pa.schema([
    pa.field("depth", pa.uint8(), nullable=False),
    pa.field("path", pa.string(), nullable=False),
    pa.field("vf", pa.int64(), nullable=False),
    pa.field("vt", pa.int64(), nullable=False),
    pa.field("user", pa.string()),  # NULL: a rule attributing the prefix to nobody
    pa.field("source", pa.string()),
    pa.field("evidence", pa.string()),
])
#: What a version and a scan's row are compared by (`user` quoted wherever SQL names it).
AT_HASH = 'hash(depth, path, "user", source, evidence)'


def ident(name: str) -> str:
    """A quoted DuckDB identifier."""
    return '"' + name.replace('"', '""') + '"'


def qcols(names) -> str:
    """Column names as a quoted DuckDB select list (`user` is a keyword)."""
    return ", ".join(ident(c) for c in names)


def attributed(scans: dict) -> bool:
    """Whether a generation's base (its `scans.json`) has the attribution table."""
    return bool(scans.get(ATTRIBUTION))


def attribution_src(scan: dict) -> str | None:
    """A scan's `attribution.parquet` beside its `path` sort (`<dir>/path-index.parquet` → `<dir>/attribution.parquet`)."""
    from .extras import ATTRIBUTION_FILE

    d = scan["src"].rsplit("/", 1)[0] if "/" in scan["src"] else ""
    return f"{d}/{ATTRIBUTION_FILE}" if d else ATTRIBUTION_FILE


def attribution_rows_sql(src: str, preds: list[str]) -> str:
    """A covered scan's attributing prefixes in the range (`preds`): `(depth, path, "user", source, evidence)`."""
    cols = 'depth::UTINYINT AS depth, path, "user", source, evidence'
    return " UNION ALL ".join(f"SELECT {cols} FROM read_parquet({q(src)}) WHERE {p}" for p in preds)


def empty_sql(schema: pa.Schema) -> str:
    """A relation of `schema`'s columns with no rows (a table's input where none exists yet)."""
    duck = {pa.uint8(): "UTINYINT", pa.int8(): "TINYINT", pa.int32(): "INTEGER", pa.int64(): "BIGINT", pa.float64(): "DOUBLE", pa.string(): "VARCHAR"}
    return "SELECT " + ", ".join(f"NULL::{duck[f.type]} AS {ident(f.name)}" for f in schema) + " WHERE false"


def with_generation(schema: pa.Schema, generation: Identity) -> pa.Schema:
    """`schema`, with `generation` as its last column when the generation carries it."""
    return schema.append(generation if isinstance(generation, pa.Field) else GENERATION_FIELD) if generation else schema


def has_generation(scans: dict) -> Identity:
    """Whether a generation (its `scans.json`) carries the objects' native version (`GENERATION`)."""
    if scans.get("object_version"):
        if scans["object_version"] != {"name": "version", "type": "string"}:
            raise ValueError(f"unsupported object-version field: {scans['object_version']}")
        return OBJECT_VERSION_FIELD
    return bool(scans.get(GENERATION))


def file_has_generation(path: str | Path) -> Identity:
    """Whether a parquet file (a range file, a run's delta) carries `GENERATION`."""
    names = pq.read_schema(path).names
    return OBJECT_VERSION_FIELD if "version" in names else GENERATION in names


def source_has_generation(con, src: str) -> Identity:
    """Whether a scan's per-scan sort carries `GENERATION` (a v2 `path` sort from a listing that has it, `path-index -G`)."""
    names = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({q(src)})").fetchall()}
    return OBJECT_VERSION_FIELD if "version" in names else GENERATION in names


def generation_of(con, scans: dict, mount: str | None) -> Identity:
    """A generation's `GENERATION` flag: its `scans.json`'s when recorded, else whether some scan's sort carries the
    column (a base build's measurement, recorded by it)."""
    if GENERATION in scans or "object_version" in scans:
        return has_generation(scans)
    fields = [source_has_generation(con, sn._src(scans["bucket"], s["src"], mount)) for s in scans["scans"] if s.get("version") == 2]
    return OBJECT_VERSION_FIELD if any(isinstance(f, pa.Field) for f in fields) else any(fields)


def record_generation(doc: dict, generation: Identity) -> dict:
    """`doc` (a `scans.json`) with `"generation": true` when the generation carries it; without the key otherwise."""
    out = {k: v for k, v in doc.items() if k not in (GENERATION, "object_version")}
    if isinstance(generation, pa.Field):
        return {**out, "object_version": {"name": generation.name, "type": str(generation.type)}}
    return {**out, GENERATION: True} if generation else out


def state_cols(generation: Identity) -> list[str]:
    """The path versions' state columns (`STATE_COLS`, `GENERATION` carried when the generation has it)."""
    return [*STATE_COLS, identity_column(generation)] if generation else STATE_COLS


def carried(generation: Identity) -> dict[str, str]:
    """The path versions' carried columns: `wts`, and `GENERATION` (the first scan's that has one) when present."""
    return {**CARRIED, GENERATION: "first"} if generation and not isinstance(generation, pa.Field) else CARRIED

# ── Per-scan rows → one row per path ───────────────────────────────────────

#: A scan's raw rows in the reader's terms (`index.ts` `toRowV1` / `toRowV2`): `mw` is the bytes the
#: mean stamp weighs and `wt` = `mtime_mean · mw` (`view.ts` `merge` adds both only where `mw > 0`).
V2_ROW = """depth::UTINYINT AS depth, path, coalesce({usr}, '') AS usr, kind = 'file' AS is_file, size::BIGINT AS size,
    n_files::BIGINT AS n_files, coalesce({n_children}, -1)::BIGINT AS n_children, coalesce({n_desc}, -1)::BIGINT AS n_desc,
    coalesce({mtime}, -1)::BIGINT AS mtime, CASE WHEN {mtime_mean} IS NULL THEN 0 ELSE size END::BIGINT AS mw,
    CASE WHEN {mtime_mean} IS NULL THEN 0 ELSE {mtime_mean}::DOUBLE * size::DOUBLE END AS wt,
    coalesce({last_read}, -1)::INTEGER AS last_read, """ + ", ".join(f"coalesce({{c{k}}}, 0)::BIGINT AS {c}" for k, c in CLASS_COLS.items())
V1_ROW = """depth::UTINYINT AS depth, path, coalesce(usr, '') AS usr, false AS is_file, b::BIGINT AS size, o::BIGINT AS n_files,
    -1::BIGINT AS n_children, -1::BIGINT AS n_desc, -1::BIGINT AS mtime, coalesce(wb, 0)::BIGINT AS mw,
    CASE WHEN coalesce(wb, 0) > 0 THEN (coalesce(wts, 0)::DOUBLE / wb::DOUBLE) * wb::DOUBLE ELSE 0 END AS wt,
    coalesce(a, -1)::INTEGER AS last_read, """ + ", ".join(f"coalesce(c{k}, 0)::BIGINT AS {c}" for k, c in CLASS_COLS.items())
#: The class sums over a group of raw rows.
CLS_SUMS = ", ".join(f"sum({c})::BIGINT AS {c}" for c in CLS)


def _raw_sql(con, src: str, preds: list[str], version: int | None, generation: Identity) -> str:
    """A scan's raw rows in the range (`V2_ROW` / `V1_ROW`), with the object's `GENERATION` (NULL on a dir row, and
    where the sort has none) when the generation carries it."""
    version = version or sn.source_version(con, src)
    identity = identity_column(generation)
    if version == 2:
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({q(src)})").fetchall()}
        sel = V2_ROW.format(**{k: (c if c in cols else "NULL") for k, c in sn.V2_OPTIONAL.items()})
        from disk_tree.object_version import version_sql

        expr = version_sql(cols) if isinstance(generation, pa.Field) else GENERATION if GENERATION in cols else "NULL"
        gen = f"CASE WHEN kind = 'file' THEN {expr} END"
    else:
        sel, gen = V1_ROW, "NULL"
    if generation:
        sel += f", ({gen})::{'VARCHAR' if isinstance(generation, pa.Field) else 'BIGINT'} AS {identity}"
    return " UNION ALL ".join(f"SELECT {sel} FROM read_parquet({q(src)}) WHERE {p}" for p in preds)


def path_rows_sql(con, src: str, preds: list[str], version: int | None = None, generation: Identity = False) -> str:
    """One scan's rows in the range, one per `(depth, path)`: the sums and maxima the reader's `merge`
    takes over a path's rows (owner slices, a v1 index's duplicate rows), `kind` = `dir` if any row is,
    `us` = the owner slices, `dr` = the mean stamp to the second; with `generation`, an object's `GENERATION`
    last (NULL on a dir, a key that is also a folder's prefix included)."""
    raw = _raw_sql(con, src, preds, version, generation)
    identity = identity_column(generation)
    gen = f", CASE WHEN bool_and(is_file) THEN max({identity}) END AS {identity}" if generation else ""
    one = f"""SELECT depth, path, CASE WHEN bool_and(is_file) THEN 'file' ELSE 'dir' END AS kind, sum(size)::BIGINT AS size,
        sum(n_files)::BIGINT AS n_files, max(n_children) AS n_children, max(n_desc) AS n_desc, max(mtime) AS mtime,
        sum(wt)::DOUBLE AS wts, sum(mw)::BIGINT AS wb, max(last_read) AS last_read,
        {CLS_SUMS}{gen}
        FROM raw GROUP BY depth, path"""
    return f"""WITH raw AS ({raw}), one AS ({one}), sl AS ({us_sql_from_raw()})
        SELECT one.depth, one.path, kind, size, n_files, n_children, n_desc, mtime,
            CASE WHEN wb > 0 THEN round(wts / wb, 0) ELSE 0 END::DOUBLE AS dr, wb, {', '.join(CLS)}, sl.us, wts, last_read{f', {identity}' if generation else ''}
        FROM one JOIN sl USING (depth, path)"""


def us_sql_from_raw() -> str:
    """Per `(depth, path)` of the CTE `raw` (raw rows with `usr`, `size`): the owner slices as one
    string — `''` with no named owner; the owner's name when the path is exactly one row and it is
    named (its bytes are the path's); else JSON `[["usr", bytes], …]` by `usr`, every named owner
    (zero bytes included: the reader's per-user map holds them). Unnamed bytes are the path's minus
    the named ones."""
    return """SELECT depth, path,
        CASE WHEN sum(nr) FILTER (WHERE usr <> '') IS NULL THEN ''
             WHEN sum(nr) = 1 THEN any_value(usr) FILTER (WHERE usr <> '')
             ELSE '[' || string_agg(CASE WHEN usr <> '' THEN '[' || to_json(usr)::VARCHAR || ',' || b::VARCHAR || ']' END, ',' ORDER BY usr) || ']' END AS us
        FROM (SELECT depth, path, usr, sum(size)::BIGINT AS b, count(*) AS nr FROM raw GROUP BY depth, path, usr)
        GROUP BY depth, path"""


def slice_rows_sql(con, src: str, preds: list[str], version: int | None = None) -> str:
    """One scan's owner slices in the range, one row per `(depth, path, usr)` (`usr` '' where none is
    named): `path_rows_sql`'s sums and maxima per slice instead of per path."""
    raw = _raw_sql(con, src, preds, version, False)
    return f"""WITH raw AS ({raw})
        SELECT depth, path, usr, CASE WHEN bool_and(is_file) THEN 'file' ELSE 'dir' END AS kind, sum(size)::BIGINT AS size,
            sum(n_files)::BIGINT AS n_files, max(n_children) AS n_children, max(n_desc) AS n_desc, max(mtime) AS mtime,
            CASE WHEN sum(mw) > 0 THEN round(sum(wt) / sum(mw), 0) ELSE 0 END::DOUBLE AS dr, sum(mw)::BIGINT AS wb,
            {CLS_SUMS}, sum(wt)::DOUBLE AS wts, max(last_read) AS last_read
        FROM raw GROUP BY depth, path, usr"""


#: Per-version hash of the change columns: what a version and a scan's row are compared by.
ROW_HASH = f"hash(depth, path, kind, size, n_files, n_children, n_desc, mtime, dr, wb, {', '.join(CLS)}, us)"
RD_HASH = "hash(depth, path, last_read)"


def _prefix_digests(con, table: str, h: str, stamps: list[int]) -> list[list[int]]:
    """Per scan j: `[count, Σ h mod 2⁶⁴]` of the versions live at scan j (`vf ≤ t_j < vt`), from a
    difference array over the versions' `vf` and `vt` — O(versions), never one row per scan."""
    idx = {t: j for j, t in enumerate(stamps)}
    d_n = [0] * (len(stamps) + 1)
    d_h = [0] * (len(stamps) + 1)
    for vf, n, s in con.execute(f"SELECT vf, count(*), sum({h}::HUGEINT) FROM {table} GROUP BY vf").fetchall():
        d_n[idx[vf]] += n
        d_h[idx[vf]] += int(s)
    for vt, n, s in con.execute(f"SELECT vt, count(*), sum({h}::HUGEINT) FROM {table} WHERE vt <> {OPEN} GROUP BY vt").fetchall():
        d_n[idx[vt]] -= n
        d_h[idx[vt]] -= int(s)
    out, n, s = [], 0, 0
    for j in range(len(stamps)):
        n += d_n[j]
        s += d_h[j]
        out.append([n, s % U64])
    return out


def build_range(scans: dict, ranges: dict, i: int, out: Path, con, *, mount: str | None) -> dict:
    """One key range over every scan: `pv/r####.parquet` (path versions, `PV_SCHEMA`, sorted
    `(depth, path, vf)`), `rd/r####.parquet` (`last_read` versions, `RD_SCHEMA`) and
    `digest/r####.json`: counts, and per scan the rows and Σ hash of the scan's per-path rows next to
    those of the versions live at it — equal iff the versions reconstruct every scan exactly. The path versions carry
    `GENERATION` when the generation does (`generation_of`)."""
    from pyrmts.intervals import islands_sql, stamped_sql

    t0 = monotonic()
    r = ranges["ranges"][i]
    preds = range_preds(r)
    srcs = [(sn._src(scans["bucket"], s["src"], mount), s["ts"], s.get("version")) for s in scans["scans"]]
    stamps = [ts for _, ts, _ in srcs]
    gen = generation_of(con, scans, mount)
    for t in ("lng", "pv", "rd"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    # One scan at a time: a single 71-way `UNION ALL` runs every scan's per-path aggregation at once and
    # ran out of memory on the widest ranges (`failed to pin block`, 59.6 GiB of 60).
    for j, (p, _, v) in enumerate(srcs):
        sql = f"SELECT {j}::BIGINT AS __scan, * FROM ({path_rows_sql(con, p, preds, v, gen)})"
        con.execute(f"CREATE TABLE lng AS {sql}" if j == 0 else f"INSERT INTO lng {sql}")
    t_long = monotonic() - t0
    src_dig = {j: [n, int(s) % U64] for j, n, s in con.execute(f"SELECT __scan, count(*), sum({ROW_HASH}::HUGEINT) FROM lng GROUP BY __scan").fetchall()}
    rd_src = {j: [n, int(s) % U64] for j, n, s in con.execute(f"SELECT __scan, count(*), sum({RD_HASH}::HUGEINT) FROM lng WHERE last_read >= 0 GROUP BY __scan").fetchall()}
    runs = islands_sql("SELECT * FROM lng", KEY_COLS, state_cols(gen), carried=carried(gen))
    con.execute(f"CREATE TABLE pv AS {stamped_sql(runs, KEY_COLS, state_cols(gen), stamps, OPEN)}")
    rd_runs = islands_sql("SELECT __scan, depth, path, last_read FROM lng WHERE last_read >= 0", KEY_COLS, ["last_read"])
    con.execute(f"CREATE TABLE rd AS {stamped_sql(rd_runs, KEY_COLS, ['last_read'], stamps, OPEN)}")
    t_kernel = monotonic() - t0 - t_long
    con.execute("DROP TABLE lng")
    pv_dig = _prefix_digests(con, "pv", ROW_HASH, stamps)
    rd_dig = _prefix_digests(con, "rd", RD_HASH, stamps)
    per_scan = []
    for j, s in enumerate(scans["scans"]):
        a, b = src_dig.get(j, [0, 0]), pv_dig[j]
        ra, rb = rd_src.get(j, [0, 0]), rd_dig[j]
        per_scan.append({"id": s["id"], "rows": a[0], "live": b[0], "eq": a == b, "reads": ra[0], "reads_live": rb[0], "reads_eq": ra == rb})
    name = f"r{i:04d}"
    pv_schema = with_generation(PV_SCHEMA, gen)
    cols = ", ".join(pv_schema.names)
    n_pv = write_sorted(sn._batches(con, f"SELECT {cols} FROM pv ORDER BY depth, path, vf"), out / "pv" / f"{name}.parquet",
                        pv_schema, RANGE_RG, dictionary=["kind", "us"])
    n_rd = write_sorted(sn._batches(con, "SELECT depth, path, vf, vt, last_read FROM rd ORDER BY depth, path, vf"), out / "rd" / f"{name}.parquet",
                        RD_SCHEMA, RANGE_RG)
    kinds = {k: {"versions": n, "open": o, "paths": p} for k, n, o, p in con.execute(
        f"SELECT kind, count(*), count(*) FILTER (WHERE vt = {OPEN}), count(DISTINCT path) FROM pv GROUP BY kind ORDER BY kind").fetchall()}
    doc = {
        "range": r, "i": i, "versions": n_pv, "reads": n_rd,
        "open": con.execute(f"SELECT count(*) FROM pv WHERE vt = {OPEN}").fetchone()[0],
        "reads_open": con.execute(f"SELECT count(*) FROM rd WHERE vt = {OPEN}").fetchone()[0],
        # Versions naming an owner: none in any range, and the generation has no owner axis (`owners_of`).
        "owners": con.execute("SELECT count(*) FROM pv WHERE us <> ''").fetchone()[0],
        "kinds": kinds, "scans": per_scan,
        "eq": all(s["eq"] and s["reads_eq"] for s in per_scan),
        "long_s": round(t_long, 1), "kernel_s": round(t_kernel, 1), "s": round(monotonic() - t0, 1),
    }
    con.execute("DROP TABLE pv")
    con.execute("DROP TABLE rd")
    (out / "digest").mkdir(parents=True, exist_ok=True)
    (out / "digest" / f"{name}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
    err(f"range {i}: {n_pv:,} versions ({doc['open']:,} open), {n_rd:,} read versions, eq={doc['eq']} in {doc['s']}s "
        f"(long {doc['long_s']}s, kernel {doc['kernel_s']}s)")
    return doc


def build_slices_range(scans: dict, ranges: dict, i: int, out: Path, con, *, mount: str | None) -> dict:
    """Range `i`'s owner-slice versions over every scan: `sv/r####.parquet` (`SV_SCHEMA`, sorted
    `(depth, path, usr, vf)`) and `sv-digest/r####.json` — per scan the rows and Σ hash of its slices
    next to those of the versions live at it (equal iff every scan reconstructs)."""
    from pyrmts.intervals import islands_sql, stamped_sql

    t0 = monotonic()
    r = ranges["ranges"][i]
    preds = range_preds(r)
    srcs = [(sn._src(scans["bucket"], s["src"], mount), s["ts"], s.get("version")) for s in scans["scans"]]
    stamps = [ts for _, ts, _ in srcs]
    for t in ("lng", "sv"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    for j, (p, _, v) in enumerate(srcs):
        sql = f"SELECT {j}::BIGINT AS __scan, * FROM ({slice_rows_sql(con, p, preds, v)})"
        con.execute(f"CREATE TABLE lng AS {sql}" if j == 0 else f"INSERT INTO lng {sql}")
    t_long = monotonic() - t0
    src_dig = {j: [n, int(h) % U64] for j, n, h in con.execute(f"SELECT __scan, count(*), sum({SV_HASH}::HUGEINT) FROM lng GROUP BY __scan").fetchall()}
    runs = islands_sql("SELECT * FROM lng", SV_KEY, SV_STATE, carried=CARRIED)
    con.execute(f"CREATE TABLE sv AS {stamped_sql(runs, SV_KEY, SV_STATE, stamps, OPEN)}")
    t_kernel = monotonic() - t0 - t_long
    con.execute("DROP TABLE lng")
    dig = _prefix_digests(con, "sv", SV_HASH, stamps)
    per_scan = []
    for j, sc in enumerate(scans["scans"]):
        a, b = src_dig.get(j, [0, 0]), dig[j]
        per_scan.append({"id": sc["id"], "rows": a[0], "live": b[0], "eq": a == b})
    name = f"r{i:04d}"
    sel = ", ".join("nullif(usr, '') AS usr" if c == "usr" else c for c in SV_SCHEMA.names)
    n = write_sorted(sn._batches(con, f"SELECT {sel} FROM sv ORDER BY depth, path, usr, vf"), out / "sv" / f"{name}.parquet",
                     SV_SCHEMA, RANGE_RG, dictionary=["kind", "usr"])
    doc = {
        "range": r, "i": i, "versions": n,
        "open": con.execute(f"SELECT count(*) FROM sv WHERE vt = {OPEN}").fetchone()[0],
        "scans": per_scan, "eq": all(x["eq"] for x in per_scan),
        "long_s": round(t_long, 1), "kernel_s": round(t_kernel, 1), "s": round(monotonic() - t0, 1),
    }
    con.execute("DROP TABLE sv")
    (out / "sv-digest").mkdir(parents=True, exist_ok=True)
    (out / "sv-digest" / f"{name}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
    err(f"slices {i}: {n:,} versions ({doc['open']:,} open), eq={doc['eq']} in {doc['s']}s (long {doc['long_s']}s, kernel {doc['kernel_s']}s)")
    return doc


def build_attribution_range(scans: dict, ranges: dict, i: int, out: Path, con, *, mount: str | None) -> dict:
    """Range `i`'s attribution versions over the covered scans (those whose entry names an `attribution.parquet`,
    `ATTRIBUTION_SRC`): `attribution/r####.parquet` (`AT_SCHEMA`, sorted `(depth, path, vf)`) and
    `attribution-digest/r####.json` — per covered scan the rows and Σ hash of its prefixes in the range next to those of
    the versions live at it (equal iff every covered scan reconstructs). The kernel sees the covered scans alone,
    consecutive, so a version runs on across an uncovered scan, and closes at the next covered scan where it changes or
    is gone; an append does the same (`interval_append.append_range`)."""
    from pyrmts.intervals import islands_sql, stamped_sql

    t0 = monotonic()
    r = ranges["ranges"][i]
    preds = range_preds(r)
    covered = [s for s in scans["scans"] if s.get(ATTRIBUTION_SRC)]
    stamps = [s["ts"] for s in covered]
    for t in ("attr_long", "attr_versions"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    inputs = [f"SELECT {n}::BIGINT AS __scan, * FROM ({attribution_rows_sql(sn._src(scans['bucket'], s[ATTRIBUTION_SRC], mount), preds)})"
              for n, s in enumerate(covered)]
    con.execute(f"CREATE TABLE attr_long AS {' UNION ALL '.join(inputs) if inputs else 'SELECT 0::BIGINT AS __scan, * EXCLUDE (vf, vt) FROM (' + empty_sql(AT_SCHEMA) + ')'}")
    src_dig = {n: [c, int(h) % U64] for n, c, h in con.execute(f"SELECT __scan, count(*), sum({AT_HASH}::HUGEINT) FROM attr_long GROUP BY __scan").fetchall()}
    if covered:
        runs = islands_sql("SELECT * FROM attr_long", KEY_COLS, ATTRIBUTION_CHANGE)
        con.execute(f"CREATE TABLE attr_versions AS {stamped_sql(runs, KEY_COLS, ATTRIBUTION_CHANGE, stamps, OPEN)}")
    else:
        con.execute(f"CREATE TABLE attr_versions AS {empty_sql(AT_SCHEMA)}")
    con.execute("DROP TABLE attr_long")
    dig = _prefix_digests(con, "attr_versions", AT_HASH, stamps)
    per_scan = []
    for n, s in enumerate(covered):
        a, b = src_dig.get(n, [0, 0]), dig[n]
        per_scan.append({"id": s["id"], "rows": a[0], "live": b[0], "eq": a == b})
    name = f"r{i:04d}"
    n_at = write_sorted(sn._batches(con, f"SELECT {qcols(AT_SCHEMA.names)} FROM attr_versions ORDER BY depth, path, vf"), out / ATTRIBUTION / f"{name}.parquet",
                        AT_SCHEMA, RANGE_RG, dictionary=["user", "source"])
    doc = {"range": r, "i": i, "versions": n_at, "open": con.execute(f"SELECT count(*) FROM attr_versions WHERE vt = {OPEN}").fetchone()[0],
           "scans": per_scan, "eq": all(x["eq"] for x in per_scan), "s": round(monotonic() - t0, 1)}
    con.execute("DROP TABLE attr_versions")
    (out / "attribution-digest").mkdir(parents=True, exist_ok=True)
    (out / "attribution-digest" / f"{name}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
    err(f"attribution {i}: {n_at:,} versions ({doc['open']:,} open) over {len(covered)} covered scans, eq={doc['eq']} in {doc['s']}s")
    return doc


# ── Read days folded into the path versions ──────────────────────────────


def fold_sql(pv: str, rd: str, generation: Identity = False) -> str:
    """`pv`'s versions split where `rd` (the read-day versions of the same paths) changes, each piece
    carrying its `last_read` (-1 where none is live), adjacent pieces with the same path version and
    read day merged: `PVL_SCHEMA` rows, unsorted. Equal to versioning the scans with `last_read` as one
    more change column (`test_fold_equals_versioning_with_read_days`). Paths without read versions pass
    through untouched. With `generation`, `pv` carries `GENERATION` (last), and so does every piece."""
    pv_cols = with_generation(PV_SCHEMA, generation).names
    cols = ", ".join(f"p.{c}" for c in pv_cols if c not in ("vf", "vt"))
    return f"""WITH rp AS (SELECT DISTINCT depth, path FROM {rd}),
        pr AS (SELECT p.* FROM {pv} p SEMI JOIN rp USING (depth, path)),
        b AS (SELECT depth, path, vf AS t FROM pr UNION SELECT depth, path, vt FROM pr
              UNION SELECT depth, path, vf FROM {rd} UNION SELECT depth, path, vt FROM {rd}),
        seg AS (SELECT depth, path, t AS lo, lead(t) OVER (PARTITION BY depth, path ORDER BY t) AS hi FROM b),
        j AS (SELECT s.depth, s.path, s.lo, s.hi, p.vf AS pvf, {cols}, coalesce(r.last_read, -1) AS last_read
              FROM seg s JOIN pr p ON p.depth = s.depth AND p.path = s.path AND p.vf <= s.lo AND s.hi <= p.vt
              LEFT JOIN {rd} r ON r.depth = s.depth AND r.path = s.path AND r.vf <= s.lo AND s.hi <= r.vt
              WHERE s.hi IS NOT NULL),
        m AS (SELECT *, CASE WHEN lag(hi) OVER w = lo AND lag(pvf) OVER w = pvf AND lag(last_read) OVER w = last_read THEN 0 ELSE 1 END AS new
              FROM j WINDOW w AS (PARTITION BY depth, path ORDER BY lo)),
        g AS (SELECT *, sum(new) OVER (PARTITION BY depth, path ORDER BY lo ROWS UNBOUNDED PRECEDING) AS run FROM m)
        SELECT depth, path, min(lo) AS vf, max(hi) AS vt, {", ".join(f"any_value({c}) AS {c}" for c in pv_cols if c not in ("depth", "path", "vf", "vt"))}, any_value(last_read) AS last_read
        FROM g GROUP BY depth, path, pvf, run
        UNION ALL
        SELECT p.*, -1 AS last_read FROM {pv} p ANTI JOIN rp USING (depth, path)"""


def fold_range(root: str, i: int, out: Path, con) -> dict:
    """Range `i`'s `pvl/r####.parquet` from its `pv/` and `rd/` (sorted `(depth, path, vf)`); `GENERATION` (last)
    when `pv/` carries it."""
    t0 = monotonic()
    name = f"r{i:04d}"
    pv, rd = f"read_parquet({q(f'{root}/pv/{name}.parquet')})", f"read_parquet({q(f'{root}/rd/{name}.parquet')})"
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {pv}").fetchall()}
    gen = OBJECT_VERSION_FIELD if "version" in cols else GENERATION in cols
    schema = with_generation(PVL_SCHEMA, gen)
    con.execute("DROP TABLE IF EXISTS pvl")
    con.execute(f"CREATE TABLE pvl AS {fold_sql(pv, rd, gen)}")
    n = write_sorted(sn._batches(con, f"SELECT {', '.join(schema.names)} FROM pvl ORDER BY depth, path, vf"), out / "pvl" / f"{name}.parquet",
                     schema, RANGE_RG, dictionary=["kind", "us"])
    n_pv = con.execute(f"SELECT count(*) FROM {pv}").fetchone()[0]
    con.execute("DROP TABLE pvl")
    doc = {"i": i, "pv": n_pv, "pvl": n, "s": round(monotonic() - t0, 1)}
    err(f"fold {i}: {n_pv:,} → {n:,} versions in {doc['s']}s")
    return doc


def slice_totals_sql(sv: str, pv: str) -> str:
    """`sv`'s slice versions split where their path's total (`pv`'s `size`) changes, each piece carrying it
    as `tot`: `SVT_SCHEMA` rows, unsorted. Every live slice lies inside a live path version (a scan's
    slices sum to its path row), so the pieces cover the slices exactly."""
    cols = ", ".join(f"s.{c}" for c in SV_SCHEMA.names if c not in ("vf", "vt"))
    return f"""SELECT {cols}, greatest(s.vf, p.vf) AS vf, least(s.vt, p.vt) AS vt, p.size AS tot
        FROM {sv} s JOIN {pv} p ON p.depth = s.depth AND p.path = s.path AND p.vf < s.vt AND s.vf < p.vt"""


def slice_totals_range(root: str, i: int, out: Path, con) -> dict:
    """Range `i`'s `svt/r####.parquet` from its `sv/` and `pv/`, sorted `(depth, path, usr, vf)`; the
    pieces of each slice version must tile it (checked)."""
    t0 = monotonic()
    name = f"r{i:04d}"
    sv, pv = f"read_parquet({q(f'{root}/sv/{name}.parquet')})", f"read_parquet({q(f'{root}/pv/{name}.parquet')})"
    con.execute("DROP TABLE IF EXISTS svt")
    con.execute(f"CREATE TABLE svt AS {slice_totals_sql(sv, pv)}")
    sel = ", ".join(SVT_SCHEMA.names)
    n = write_sorted(sn._batches(con, f"SELECT {sel} FROM svt ORDER BY depth, path, usr NULLS FIRST, vf"), out / "svt" / f"{name}.parquet",
                     SVT_SCHEMA, RANGE_RG, dictionary=["kind", "usr"])
    # The pieces tile each slice version (they're its intersections with disjoint path versions, so equal
    # total spans mean full cover).
    span_sv, n_sv = con.execute(f"SELECT sum(vt - vf)::HUGEINT, count(*) FROM {sv}").fetchone()
    span_svt = con.execute("SELECT sum(vt - vf)::HUGEINT FROM svt").fetchone()[0]
    con.execute("DROP TABLE svt")
    if span_sv != span_svt:
        raise RuntimeError(f"range {i}: slice pieces span {span_svt}, the slices {span_sv}")
    doc = {"i": i, "sv": n_sv, "svt": n, "s": round(monotonic() - t0, 1)}
    err(f"slice totals {i}: {n_sv:,} → {n:,} versions in {doc['s']}s")
    return doc


# ── Served sorts ───────────────────────────────────────────────────────────

#: A version's size bucket, `⌊log2 size⌋` as a bit length (exact for any int64; the path store's
#: `bysize` key, `find/tiers.py`); NULL for size 0, which sorts last.
BUCKET = "CASE WHEN size > 0 THEN length(bin(size)) - 1 END"
#: Each served sort: its source rows, its order, and which column bounds a group's sizes. Every sort is
#: segments in one file — the versions open at the generation's last scan, then the closed ones — so a
#: read at that scan prunes every closed group by `vt_max` alone.
SORTS = {
    "path": ("pvl", "depth, path, vf", "size"),
    "bysize": ("pvl", f"({BUCKET}) DESC NULLS LAST, path, vf", "size"),
    "reads": ("rd", "depth, path, vf", None),
    # Owner slices, as the per-scan store's `path`, `bysize` (keyed on the path's total) and `bysize-user` sorts.
    "slices": ("sv", "depth, path, usr NULLS FIRST, vf", "size"),
    # Keyed on the path's total (`bysize-path-total.md`): a path's slices sit together, `b_max` = MAX(tot).
    "slices-bytotal": ("svt", f"({BUCKET.replace('size', 'tot')}) DESC NULLS LAST, path, usr NULLS FIRST, vf", "tot"),
    "slices-bysize-user": ("sv", f"usr NULLS FIRST, ({BUCKET}) DESC NULLS LAST, path, vf", "size"),
    # Roster attribution (`ATTRIBUTION`), path first: a subtree's prefixes are one span per segment, an ancestor a point.
    ATTRIBUTION: (ATTRIBUTION, "path, vf", None),
    # The level index (`interval_level`): each child version transition under its parent, `(depth, path)` the parent's,
    # computed from the folded path versions (`interval_level.rows_sql`). Only in a generation that carries it (`leveled`).
    LEVEL: ("pvl", ilv.LEVEL_ORDER, None),
}
#: Each range-file dir's schema.
SUB_SCHEMA = {"pv": PV_SCHEMA, "pvl": PVL_SCHEMA, "rd": RD_SCHEMA, "sv": SV_SCHEMA, "svt": SVT_SCHEMA, ATTRIBUTION: AT_SCHEMA}
#: The attribution range files, and the sort cut from them: only in a generation with the table (`attributed`).
ATTRIBUTION_SUBS = (ATTRIBUTION,)
#: The staged `cut`'s tasks by index (`-i`): every sort, then `stats`, then the attribution and level sorts (after `stats`,
#: so the indices a staged build has always used hold).
CUT_TASKS = [*(s for s in SORTS if s not in (ATTRIBUTION, LEVEL)), "stats", ATTRIBUTION, LEVEL]
#: The owner-slice range files (`build -S`, `fold -S`), and the sorts cut from them. A generation whose scans name no
#: owner (`usr` NULL or '' on every row: cw-s3, r2.rbw.sh) has no owner axis and builds none of them: its slices would
#: be its path rows again, each slice sort about the size of `path`.
SLICE_SUBS = ("sv", "svt")
#: The served sort that carries `GENERATION` in a generation that has it: the identity lookup's (a sweep's hold,
#: `export`), by path. The others never do: no reader of theirs needs it.
GENERATION_SORT = "path"


def served_schema(sort: str, generation: Identity = False, churn: bool = False) -> pa.Schema:
    """A served sort's columns: its range files' (`SUB_SCHEMA`), `churn_cum` in the folded path versions' sorts when the
    generation carries it (`interval_churn`), and `GENERATION` last in `GENERATION_SORT` when it carries that. The level
    sort's are its own (`interval_level.LEVEL_SCHEMA`)."""
    if sort == LEVEL:
        return LEVEL_SCHEMA
    sub = SORTS[sort][0]
    return with_generation(with_churn(SUB_SCHEMA[sub], churn and sub == "pvl"), generation if sort == GENERATION_SORT else False)


def owners_of(digests) -> bool:
    """Whether a generation has the owner axis, from its ranges' `build_range` digests: some version names an owner.
    A digest from before the count (no `owners`) counts as sliced, as its generation was cut."""
    return any(d.get("owners", 1) for d in digests)


def sliced(scans: dict) -> bool:
    """Whether a generation (its `scans.json`) has the owner axis: its `slices`, recorded by the base build. A
    generation from before the flag was cut with every sort."""
    return bool(scans.get("slices", True))


def served_sorts(has_slices: bool, has_attribution: bool = False, has_level: bool = False) -> list[str]:
    """The sorts a generation serves: every `SORTS` entry, the slice sorts only with the owner axis, the attribution sort
    only with its table, the level sort only where the generation carries it (`interval_level.leveled`)."""
    return [s for s, (sub, _, _) in SORTS.items() if (has_slices or sub not in SLICE_SUBS) and (has_attribution or sub not in ATTRIBUTION_SUBS)
            and (has_level or s != LEVEL)]


def owners_refused(scan: str) -> str:
    """Why scan `scan`, which names owners, isn't appended to a generation without the owner axis."""
    return (f"{scan}: names owners (`usr`), but the generation has no owner axis (`scans.json` `\"slices\": false`); "
            "its owners need a new base generation (`interval-store build`)")


def names_owners(con, src: str) -> bool:
    """Whether the scan `src` (a `path` sort) names an owner on any row (`usr` neither NULL nor ''). Footer statistics
    prune the row groups of an all-NULL `usr`; the first named row ends the read."""
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({q(src)})").fetchall()}
    if "usr" not in cols:
        return False
    return con.execute(f"SELECT count(*) FROM (SELECT 1 FROM read_parquet({q(src)}) WHERE usr <> '' LIMIT 1)").fetchone()[0] > 0


def gen_sliced(store: Store, prefix: str) -> bool:
    """A staged generation's owner axis: its `scans.json`'s `slices` once recorded (`cut`), else measured from every
    range's `build_range` digest (refused until all are built)."""
    scans = store.read_json(f"{prefix}/scans.json")
    if "slices" in scans:
        return sliced(scans)
    k = store.read_json(f"{prefix}/ranges.json")["k"]
    keys = [f"{prefix}/digest/r{i:04d}.json" for i in range(k)]
    if missing := [x for x in keys if not store.exists(x)]:
        raise SystemExit(f"{prefix}: {len(missing)} of {k} ranges' path versions not built (`build-ranges`); "
                         "whether the generation has owner slices is measured there")
    return owners_of(store.read_json(x) for x in keys)


def record_slices(store: Store, prefix: str, scans: dict, has_slices: bool, generation: Identity = False) -> None:
    """`scans.json` under `prefix` with the generation's `slices` (the reader's flag) and `GENERATION` (only when it
    has it: `record_generation`), unless it says so already."""
    key = f"{prefix}/scans.json"
    cur = store.read_json(key) if store.exists(key) else None
    if cur is None or cur.get("slices") != has_slices or has_generation(cur) != generation:
        store.write_text(key, json.dumps(record_generation({**scans, "slices": has_slices}, generation), indent=1) + "\n")

GROUPS_SCHEMA = pa.schema([
    pa.field("rg", pa.int32(), nullable=False),
    pa.field("seg", pa.int32(), nullable=False),
    pa.field("d_min", pa.int32(), nullable=False),
    pa.field("d_max", pa.int32(), nullable=False),
    pa.field("p_min", pa.string(), nullable=False),
    pa.field("p_max", pa.string(), nullable=False),
    pa.field("b_min", pa.int64(), nullable=False),
    pa.field("b_max", pa.int64(), nullable=False),
    pa.field("u_min", pa.string()),
    pa.field("u_max", pa.string()),
    pa.field("vf_min", pa.int64(), nullable=False),
    pa.field("vf_max", pa.int64(), nullable=False),
    pa.field("vt_min", pa.int64(), nullable=False),
    pa.field("vt_max", pa.int64(), nullable=False),
    pa.field("row_start", pa.int64(), nullable=False),
    pa.field("row_end", pa.int64(), nullable=False),
    pa.field("rg_json", pa.string(), nullable=False),
])
GROUPS_STAT_COLS = ["d_min", "d_max", "p_min", "p_max", "b_min", "b_max", "u_min", "u_max", "vf_min", "vf_max", "vt_min", "vt_max"]
#: The served store's `index_schema.version` analogue: 3 = interval rows (`vf`/`vt`, one row per path).
STORE_VERSION = 3
#: A dyadic segment's id: `1 + level · SEG_LEVEL + block` (segment 0 is the open versions).
SEG_LEVEL = 1 << 16


def seg_sql(stamps: list[int] | None) -> str:
    """Each version's segment, as SQL over `vf`/`vt`: 0 while open. Without `stamps`, every closed
    version is segment 1. With them (the generation's scan epochs, ascending), a closed version live at
    scans `i..j` (by index) goes to the smallest dyadic block of scans holding both — level
    `k = bit_length(i ^ j)`, block `i >> k`, segment `1 + k·SEG_LEVEL + block`. A block's versions are
    live only inside it, so a read at a scan touches one block per level (⌈log2 n⌉ + 1 of them) and the
    groups' `vf_min`/`vt_max` prune the rest; within a block a version crosses its midpoint, so it is
    live at most of the block's scans."""
    if not stamps:
        return f"CASE WHEN vt = {OPEN} THEN 0 ELSE 1 END"
    if stamps != sorted(set(stamps)):
        raise ValueError("scan stamps must be ascending and distinct")
    lst = "[" + ", ".join(str(int(t)) for t in stamps) + "]::BIGINT[]"
    i = f"(list_position({lst}, vf) - 1)"
    j = f"(list_position({lst}, vt) - 2)"
    k = f"(CASE WHEN {i} = {j} THEN 0 ELSE length(bin(xor({i}, {j}))) END)"
    return f"CASE WHEN vt = {OPEN} THEN 0 ELSE 1 + {k} * {SEG_LEVEL} + ({i} >> {k}) END"


def _bounds(t: pa.Table, seg: int, size_col: str | None) -> dict:
    import pyarrow.compute as pc

    def mm(c):
        r = pc.min_max(t.column(c))
        return r["min"].as_py(), r["max"].as_py()

    d, p = mm("depth"), mm("path")
    # The level sort's transitions have one stamp (`ts`): its bounds are the group's `vf_*` and `vt_*` both.
    vf, vt = (mm("vf"), mm("vt")) if "vf" in t.column_names else (mm("ts"),) * 2
    b = mm(size_col) if size_col else (0, 0)
    # A slice sort's owner range (NULL owners ignored, as the per-scan footers' `u_min`/`u_max`).
    u = mm("usr") if "usr" in t.column_names else (None, None)
    return {"seg": seg, "d_min": d[0], "d_max": d[1], "p_min": p[0], "p_max": p[1], "b_min": b[0], "b_max": b[1],
            "u_min": u[0], "u_max": u[1], "vf_min": vf[0], "vf_max": vf[1], "vt_min": vt[0], "vt_max": vt[1], "rows": t.num_rows}


#: A cut's spools (`write_served`: a sort's rows in order, `__seg` beside them) are written in row groups of at most this
#: many rows; a part's writer decodes one batch of `rg_rows` at a time from them.
CUT_SPOOL_RG = 65536
#: The fewest rows a parallel cut gives each writer process: its start-up (~1 s of imports) against about a million rows'
#: encoding.
CUT_PART_ROWS = 1 << 20
#: A merged sort's pieces (`piece_plan`) are at least this many rows, whatever the budget: each is one DuckDB query over
#: its spans of every run, whose fixed cost (the query's set-up, a spool group decoded at each span end) a smaller piece
#: would repeat for few rows.
PIECE_MIN_ROWS = 1 << 16
#: A merged sort's run spool records a fence (`_sort_run`: a row's merge key and position) every `stride` rows of each
#: segment, `stride = share // (FENCE_SLACK × (runs + 1))`: a piece's row bound (`piece_plan`) overstates its rows by
#: < 2 strides a run, and a piece one candidate too long overstates `share` by < `(runs + 1)` strides, so pieces fill
#: ≥ 1 − 3 / `FENCE_SLACK` of `share` and never pass it.
FENCE_SLACK = 8
#: The smallest row group of a merged sort's run spools: a piece reads each run's span by row-group index (`RunSpool`),
#: so a group is the waste at each end of a run's span; smaller groups would only grow the footers.
PIECE_MIN_RG = 1024
#: Bytes a spooled row carries past its range-file row (`write_served`'s `__seg`, a merged sort's computed merge
#: keys: `__b`, `__un`, `__u`, a copy of `usr` as a short string), charged on top of `interval_compact.row_bytes`
#: (`cut_gen`'s plan).
SPOOL_EXTRA = 4 * 8 + 16
#: Copies of a run's estimated in-memory input (`interval_compact.group_spans`) DuckDB holds while sorting it: the sort's
#: key + payload rows, and the blocks its merge phase writes.
SORT_COPIES = 2

#: The sorts whose order isn't led by the range key `(depth, path)`, so every key range's rows interleave in them: their
#: order as ascending, never-NULL key columns (strings in code-point order: DuckDB's, Python's and pyarrow's), each
#: `(name, SQL)` computed into the run spools or a column by name. A `DESC NULLS LAST` size bucket (≥ 0, `BUCKET`) is its
#: negation with NULL as −1 (so after every bucket); `usr NULLS FIRST` is `(usr IS NOT NULL, coalesce(usr, ''))`. The
#: key is the whole order, unique a row (a version is one `(path, vf)`, a slice's one `(path, usr, vf)`, a level row's
#: `interval_level.LEVEL_KEY`), so a key range's rows sorted alone are its rows of the one `ORDER BY`. Every other sort is led by `(depth, path)`: disjoint key
#: ranges in key order concatenate.
_NEG_BUCKET = f"-coalesce({BUCKET}, -1)"
_USR = [("__un", "(usr IS NOT NULL)::TINYINT"), ("__u", "coalesce(usr, '')")]
MERGE_KEYS: dict[str, list] = {
    "bysize": [("__b", _NEG_BUCKET), "path", "vf"],
    "slices-bytotal": [("__b", _NEG_BUCKET.replace("size", "tot")), "path", *_USR, "vf"],
    "slices-bysize-user": [*_USR, ("__b", _NEG_BUCKET), "path", "vf"],
    ATTRIBUTION: ["path", "vf"],
    LEVEL: ilv.LEVEL_KEY,
}
RANGE_LED = "depth, path"
if bad := [s for s, (_, o, _) in SORTS.items() if s not in MERGE_KEYS and not o.startswith(RANGE_LED)]:
    raise AssertionError(f"sorts {bad} aren't led by `{RANGE_LED}` and have no `MERGE_KEYS`")


def merge_key(sort: str) -> list[str]:
    """A merged sort's key column names (`MERGE_KEYS`)."""
    return [k if isinstance(k, str) else k[0] for k in MERGE_KEYS[sort]]


def cut_plan(segs: list[tuple[int, int]], rg_rows: int, k: int) -> list[list[tuple[int, int, int]]]:
    """A served sort's row groups — each segment (`(seg, rows)`, in order) cut in `rg_rows`-row groups, its last short,
    so no group mixes segments — as `(seg, first row, rows)`, split into at most `k` contiguous parts of about equal rows.
    Each part starts on a group boundary: a segment's start, or a multiple of `rg_rows` rows into one."""
    groups, start = [], 0
    for s, n in segs:
        groups += [(s, start + o, min(rg_rows, n - o)) for o in range(0, n, rg_rows)]
        start += n
    parts: list[list[tuple[int, int, int]]] = [[] for _ in range(max(1, k))]
    done = 0
    for g in groups:
        parts[done * len(parts) // start].append(g)
        done += g[2]
    return [p for p in parts if p] or [[]]


#: The served sorts' columns with statistics (`_served_writer`): their pages' bounds are the page index's.
STAT_COLS = ("depth", "path", "vf", "vt", "ts")


def writer_meta(files: list[str], rg_rows: int, page_rows: int = SERVED_PAGE_ROWS, cols: int | None = None) -> int:
    """What a served sort's writer keeps per row group it has written (`tree_mem.group_meta`), on average over the sort's
    groups, which is what the plan charges (all of them, at once): `cols`, the sort's columns (default `files`' widest, the
    range or delta files it's cut from), `rg_rows / page_rows` pages, and the string statistics columns' uncompressed bytes
    a row over all of `files`: a small file of long paths holds few of the sort's groups."""
    widest, size, rows = 1, 0, 0
    for f in files:
        if not Path(f).exists():
            continue
        md = pq.read_metadata(f)
        widest = max(widest, md.num_columns)
        sch = md.schema.to_arrow_schema()
        strs = {i for i, c in enumerate(sch) if c.name in STAT_COLS and (pa.types.is_string(c.type) or pa.types.is_large_string(c.type))}
        if not strs:
            continue
        for g in range(md.num_row_groups):
            rg = md.row_group(g)
            size += sum(rg.column(i).total_uncompressed_size for i in strs)
            rows += rg.num_rows
    return tm.group_meta(cols or widest, -(-rg_rows // page_rows), -(-size // rows) if rows else 0)


def _served_writer(out: Path, schema: pa.Schema, page: int) -> pq.ParquetWriter:
    """A served sort's writer (`write_served`): zstd, `page`-row pages with a page index, statistics on the planning
    columns, a dictionary on the low-cardinality strings only."""
    return pq.ParquetWriter(out, schema, compression=sn.CODEC, use_dictionary=[c for c in ("kind", "kind_prev", "us", "usr", "user", "source") if c in schema.names],
                            write_statistics=[c for c in STAT_COLS if c in schema.names],
                            write_page_index=True, write_batch_size=page, data_page_size=1)


def _file_rows(path: str, start: int, n: int, batch: int, columns: list[str]):
    """Rows `[start, start + n)` of parquet `path` (`columns`), as record batches of at most `batch` rows: only the row
    groups holding them are decoded, one at a time."""
    pf = pq.ParquetFile(path)
    md = pf.metadata
    rgs, pos, at = [], 0, 0
    for g in range(md.num_row_groups):
        m = md.row_group(g).num_rows
        if pos < start + n and pos + m > start:
            at = at if rgs else pos
            rgs.append(g)
        pos += m
    if not rgs:
        return
    end = start + n
    for b in pf.iter_batches(batch_size=batch, row_groups=rgs, columns=columns):
        a, z = max(start, at), min(end, at + b.num_rows)
        if a < z:
            yield b.slice(a - at, z - a)
        at += b.num_rows
        if at >= end:
            return


def _trim(pieces: list[tuple[str, int, int]], lo: int, hi: int) -> list[tuple[str, int, int]]:
    """The `(file, first row, rows)` pieces of a stream (in order) holding its rows `[lo, hi)`, cut to them."""
    out, pos = [], 0
    for f, a, n in pieces:
        x, z = max(lo, pos), min(hi, pos + n)
        if x < z:
            out.append((f, a + x - pos, z - x))
        pos += n
    return out


def cut_runs(src: str | list[str], budget: int | None) -> list[str]:
    """`write_served`'s runs, as relations in key order: `src` itself when a relation, else each of its parquet files
    (disjoint `(depth, path)` key ranges in key order: a generation's range files) cut into key sub-ranges of at most
    `budget` estimated in-memory bytes (`interval_compact.sub_ranges`, from the footer; a file it can't stat, e.g. a URL,
    is one run)."""
    if isinstance(src, str):
        return [src]
    from .interval_compact import sub_pieces, sub_ranges

    runs = []
    for f in src:
        rel = f"read_parquet({q(f)})"
        for lo, hi, _ in sub_ranges([f], budget):
            pieces = sub_pieces(lo, hi)
            runs.append(rel if pieces is None else "(" + " UNION ALL ".join(f"SELECT * FROM {rel} WHERE {p}" for p in pieces) + ")")
    return runs


def run_budget(con, at_once: int) -> int:
    """A run's estimated in-memory input bytes when `at_once` runs sort at once under `con`'s DuckDB memory limit:
    `mem × (1 − RESERVE) / (at_once × SORT_COPIES)` (`interval_compact.RESERVE`)."""
    from .interval_compact import RESERVE

    mem = sn.mem_bytes(con.execute("SELECT current_setting('memory_limit')").fetchone()[0])
    return int(mem * (1 - RESERVE) / (max(1, at_once) * SORT_COPIES))


def range_files(con, pattern: str) -> list[str]:
    """The files matching `pattern` (a dir's or URL's `r*.parquet`: a generation's range files), in name order, which is
    key order."""
    return [r[0] for r in con.execute(f"SELECT file FROM glob({q(pattern)}) ORDER BY file").fetchall()]


def _sort_run(con, sql: str, out: Path, rg: int, name: str, key: list[str] | None = None, stride: int = 0,
              params: list | None = None, timing: dict | None = None) -> tuple[dict[int, int], dict[int, list[tuple[tuple, int]]]]:
    """`sql` (ordered, with `__seg`) streamed into spool `out` (lz4, `rg`-row groups). Returns its rows per segment, and
    with `key` (a merged sort's run, ordered by `(__seg, key)`) its fences: per segment `(key, row)` of its first row and
    every `stride`-th after (a row's number in the spool, by which a piece reads a run's span: `RunSpool`). `timing`
    gets `first` (seconds from the query's start to its first batch: an `ORDER BY`'s whole sort) and `s` (to the
    spool's close)."""
    import numpy as np
    import pyarrow.compute as pc

    counts: dict[int, int] = {}
    fences: dict[int, list[tuple[tuple, int]]] = {}
    starts: dict[int, int] = {}
    t0 = monotonic()
    reader = con.execute(sql, params or []).to_arrow_reader(rg)
    schema = reader.schema
    opts = {"use_dictionary": list(schema.names), "write_statistics": False} if key else {"write_statistics": False}
    at = 0
    first = None
    with pq.ParquetWriter(out, schema, compression="lz4", **opts) as w:
        for b in reader:
            if first is None:
                first = monotonic() - t0
            if not b.num_rows:
                continue
            s = b.column("__seg")
            if s.null_count:
                raise ValueError(f"{name}: a closed version's vf/vt isn't one of the scans' stamps")
            for e in pc.value_counts(s).to_pylist():
                counts[e["values"]] = counts.get(e["values"], 0) + e["counts"]
            if key:
                segs = s.to_numpy()
                cuts = [0, *(np.flatnonzero(segs[1:] != segs[:-1]) + 1).tolist(), len(segs)]
                for x, z in zip(cuts, cuts[1:]):
                    first = starts.setdefault(int(segs[x]), at + x)
                    idx = list(range(first + -(-(at + x - first) // stride) * stride - at, z, stride))
                    if idx:
                        cols = [b.column(c).take(idx).to_pylist() for c in key]
                        fences.setdefault(int(segs[x]), []).extend(zip(zip(*cols), (at + i for i in idx)))
            w.write_table(pa.Table.from_batches([b]), row_group_size=rg)
            at += b.num_rows
    if timing is not None:
        timing["s"] = monotonic() - t0
        timing["first"] = timing["s"] if first is None else first
    return counts, fences


class RunSpool:
    """A merged sort's run spool (`_sort_run`'s, `write_served`'s pass 1) for its pieces' reads: its footer parsed once
    and its row groups' first rows, so a span's rows `[a, z)` are read by row-group index (`batches`), never touching
    the file's other groups. DuckDB's `read_parquet` costs a file's every row group on each query naming it (bind and
    scan set-up: ~5 ms per 1K groups measured, and it didn't scale across concurrent queries), and a piece names every
    run: at ~330 runs of ~1K groups, that fixed cost, not the rows, was the piece pass's time."""

    def __init__(self, path: str):
        import numpy as np

        self.path = path
        self.md = pq.read_metadata(path)
        self.starts = np.cumsum([0, *(self.md.row_group(g).num_rows for g in range(self.md.num_row_groups))])

    def batches(self, a: int, z: int, batch: int, columns: list[str], stat: dict) -> Iterator[pa.RecordBatch]:
        """Rows `[a, z)` (`columns`), decoding only the row groups holding them, about `batch` rows' worth of groups at
        a time (so ≤ `batch` + a group's rows held). Adds to `stat`'s `groups`, `decoded` (rows) and `read` (seconds)."""
        import numpy as np

        if a >= z:
            return
        starts = self.starts
        g = int(np.searchsorted(starts, a, "right")) - 1
        g1 = int(np.searchsorted(starts, z, "left"))
        pf = pq.ParquetFile(self.path, metadata=self.md)
        while g < g1:
            e = max(g + 1, min(g1, int(np.searchsorted(starts, starts[g] + batch, "left"))))
            t0 = monotonic()
            t = pf.read_row_groups(list(range(g, e)), columns=columns, use_threads=False)
            x, y = max(a, int(starts[g])), min(z, int(starts[e]))
            t = t.slice(x - int(starts[g]), y - x)
            stat["read"] += monotonic() - t0
            stat["groups"] += e - g
            stat["decoded"] += int(starts[e] - starts[g])
            yield from t.to_batches()
            g = e


def _quantile(xs: list, p: float) -> float:
    """The `p` quantile of `xs` (nearest rank; 0 when empty)."""
    ys = sorted(xs)
    return ys[min(len(ys) - 1, int(p * len(ys)))] if ys else 0


#: A piece's `write_served` stats, summed or quantiled by `piece_summary`.
PIECE_STATS = ("spans", "groups", "decoded", "rows", "read", "sort", "write", "s")


def piece_summary(stats: list[dict]) -> str:
    """One line over pieces' `write_served` stats: their count, sums, and p50/p99 of each phase."""
    tot = {k: sum(s[k] for s in stats) for k in PIECE_STATS}
    pq_ = {k: (_quantile([s[k] for s in stats], 0.5), _quantile([s[k] for s in stats], 0.99)) for k in PIECE_STATS}
    return (f"{len(stats)} pieces, {tot['rows']:,} rows (p50 {pq_['rows'][0]:,}, p99 {pq_['rows'][1]:,}) from {tot['spans']:,} spans, "
            f"{tot['groups']:,} spool groups ({tot['decoded']:,} rows decoded); seconds summed: read {tot['read']:.1f}, sort "
            f"{tot['sort']:.1f}, write {tot['write']:.1f}; a piece p50 {pq_['s'][0]:.2f}s p99 {pq_['s'][1]:.2f}s (read p99 "
            f"{pq_['read'][1]:.2f}, sort p99 {pq_['sort'][1]:.2f}, write p99 {pq_['write'][1]:.2f})")


def piece_plan(runs: list[tuple[int, int, list[tuple[tuple, int]]]], share: int) -> list[tuple[tuple | None, tuple | None, list[tuple[int, int, int]]]]:
    """One segment of a merged sort cut in key ranges ("pieces") of at most `share` rows each, from its rows' place in
    every run spool: `runs[j] = (start, end, fences)` — its rows `[start, end)` of run `j`, sorted by key, and fences
    `(key, row)` (`_sort_run`'s: its first row and every `stride`-th after, so ascending in both). Returns the pieces in
    key order as `(lo, hi, spans)`: the rows with `lo ≤ key < hi` (None: unbounded), which lie within `spans` —
    `(j, first, end)` rows of run `j`.

    Exact, never estimated: a run's rows with key ≥ `c` start after its last fence below `c` and by its first fence at
    or above `c`, so a piece's rows in a run lie within `[after its last fence < lo, its first fence ≥ hi)`, and the sum
    of those spans' lengths bounds the piece's rows. The splitters are fence keys, each piece's `hi` the furthest one
    whose bound ≤ `share` (the bound grows with `hi`). A piece passes `share` only when even the next fence key's
    bound does, which is < (runs + `lo`'s fences) × `stride` rows: for unique keys at `stride` ≤ `share / (runs + 1)`
    (`write_served`'s), never. Rows equal on the key fall in one piece (`[lo, hi)`), so the pieces concatenated are
    the segment's one order."""
    from bisect import bisect_left

    keys = [[k for k, _ in f] for _, _, f in runs]
    cands = sorted({k for ks in keys for k in ks})

    def low(lo: tuple | None) -> list[int]:
        if lo is None:
            return [a for a, _, _ in runs]
        return [f[j - 1][1] + 1 if (j := bisect_left(ks, lo)) else a for (a, _, f), ks in zip(runs, keys)]

    def high(hi: tuple | None) -> list[int]:
        if hi is None:
            return [z for _, z, _ in runs]
        return [f[j][1] if (j := bisect_left(ks, hi)) < len(f) else z for (_, z, f), ks in zip(runs, keys)]

    def bound(lows: list[int], hi: tuple | None) -> int:
        return sum(max(0, h - x) for h, x in zip(high(hi), lows))

    pieces = []
    lo, lows, i = None, low(None), 0
    while True:
        if i >= len(cands) or bound(lows, None) <= share:
            hi, t = None, len(cands)
        else:
            # The furthest candidate whose bound fits (bisected: the bound grows with `hi`); none fitting, the next.
            a, z = i, len(cands)
            while z - a > 1:
                m = (a + z) // 2
                a, z = (m, z) if bound(lows, cands[m]) <= share else (a, m)
            hi, t = cands[a], a
        pieces.append((lo, hi, [(j, x, h) for j, (x, h) in enumerate(zip(lows, high(hi))) if h > x]))
        if hi is None:
            return pieces
        lo, lows, i = hi, low(hi), t + 1


def _piece_sql(src: str, cols: list[str], key: list[str], lo: tuple | None, hi: tuple | None) -> tuple[str, list]:
    """A piece's (`piece_plan`) rows sorted by `key`: relation `src`'s (its spans of every run spool, `RunSpool`) with
    `lo ≤ key < hi` (lexicographic, strings by code point). Returns `(sql, params)`."""

    def lex(op: str, at: int) -> str:
        # `key op (${at}, …)`, spelled out: DuckDB rewrites two row comparisons to a BETWEEN, which rows don't support.
        expr = f"{ident(key[-1])} {op} ${at + len(key) - 1}"
        for i in range(len(key) - 2, -1, -1):
            expr = f"{ident(key[i])} {op[0]} ${at + i} OR ({ident(key[i])} = ${at + i} AND ({expr}))"
        return f"({expr})"

    params = [*(lo or ()), *(hi or ())]
    where = [*([lex(">=", 1)] if lo is not None else []), *([lex("<", 1 + len(lo or ()))] if hi is not None else [])]
    sql = f"SELECT {qcols(cols)} FROM {src}" + (f" WHERE {' AND '.join(where)}" if where else "") + f" ORDER BY {qcols(key)}"
    return sql, params


def cut_part(pieces: list[tuple[str, int, int]], groups: list[tuple[int, int, int]], out: str, schema: pa.Schema,
             size_col: str | None, kv: dict, page: int, rg_rows: int) -> int:
    """One part of a cut (`write_served`): its `groups` (`cut_plan`'s) read from the spool `pieces` holding exactly the
    part's rows (`(file, first row, rows)`, in order) and written to `out` as one writer of the whole sort writes them,
    then `kv`; each group's `_bounds` a JSON line of `<out>.bounds` (`read_bounds`). Returns its groups. Holds a spool
    batch and one group decoded at a time."""
    import pyarrow.compute as pc

    n_groups = 0
    with _served_writer(Path(out), schema, page) as w, open(f"{out}.bounds", "w") as fb:
        if groups:
            cols = [*schema.names, "__seg"]
            it = (pa.Table.from_batches([b]) for f, a, n in pieces for b in _file_rows(f, a, n, rg_rows, cols))
            pending: list[pa.Table] = []
            n = 0
            for s, _, k in groups:
                while n < k:
                    t = next(it)
                    pending.append(t)
                    n += t.num_rows
                t = pa.concat_tables(pending).combine_chunks()
                g, rest = t.slice(0, k), t.slice(k)
                pending, n = ([rest] if rest.num_rows else []), rest.num_rows
                mm = pc.min_max(g.column("__seg"))
                if (mm["min"].as_py(), mm["max"].as_py()) != (s, s):
                    raise RuntimeError(f"{out}: a group planned in segment {s} holds segments {mm['min']}..{mm['max']}")
                g = g.drop_columns(["__seg"])
                g = g.cast(schema) if g.schema != schema else g
                w.write_table(g, row_group_size=rg_rows)
                fb.write(json.dumps(_bounds(g, s, size_col), separators=(",", ":")) + "\n")
                n_groups += 1
        w.add_key_value_metadata(kv)
    return n_groups


def read_bounds(files: list[Path]) -> Iterator[dict]:
    """`cut_part`'s bounds files, in order, a group at a time."""
    for f in files:
        with open(f) as fb:
            for line in fb:
                yield json.loads(line)


def write_served(con, src: str | list[str], sort: str, out: Path, schema: pa.Schema, *, rg_rows: int = SERVED_RG,
                 stamps: list[int] | None = None, page_rows: int | None = None, procs: int | None = None,
                 part_rows: int = CUT_PART_ROWS, budget: int | None = None, piece_rows: int | None = None,
                 spool_rows: int = CUT_SPOOL_RG, opened: str = "true", key_bounds: list[list] | None = None) -> dict:
    """One served sort to `out`: its segments in order (`seg_sql`: the open versions, then the closed ones — one
    segment, or with `stamps` one per dyadic block of scans), each in the sort's order and cut in `rg_rows`-row groups (a
    segment's last may be short, so no group mixes segments), zstd. `src` is a relation of `schema` rows, or a
    generation's range files (`range_files`: disjoint `(depth, path)` key ranges, in key order). Writes `<out
    stem>.groups.parquet` beside it (`GROUPS_SCHEMA`: per group the exact bounds of its rows — never truncated
    statistics — and the compact metadata the Worker revives). Pages hold `page_rows` rows (`SERVED_PAGE_ROWS`; a group's
    last may be short), aligned across columns: pyarrow caps a page by bytes, not rows, so a 1-byte target closes one
    after every `write_batch_size` values (an all-null column, encoding no bytes, stays one page a group). The page index
    carries `path`'s bounds too (its statistics on), and `path` has no dictionary (it's almost all distinct: its
    dictionary page would be about the whole chunk, fetched by every page read).

    The level sort (`LEVEL`) is cut from the folded path versions' transitions (`interval_level.rows_sql` over each run, a
    path's versions never split across runs; `opened`: which versions opened in the source, a run's deltas'
    `interval_level.RUN_OPENED`), in segments of its parent key: one per key range whose `lo` is in `key_bounds` (a
    generation's `ranges.json`; `interval_level.seg_sql`), each cut in pieces; without, one segment.

    Bounded by construction, on every core (specs/diff-progressive.md §3b), in three passes over spools in a work dir
    beside `out`:
    1. Sort: each run (`cut_runs`: each range file in key sub-ranges of at most `budget` estimated bytes, default
       `run_budget` for `procs` runs at once) is sorted by `(__seg, <order>)` into its own lz4 spool — one DuckDB
       `ORDER BY` holding one run — `procs` runs at a time on cursors of `con`.
    2. Pieces: a sort led by `(depth, path)` needs none: a segment is its runs' rows of it, in key order. The others
       (`MERGE_KEYS`, whose every run spans the key space) cut each segment of more than one run into key ranges of at
       most `piece_rows` rows (default the run budget's rows, ≥ `PIECE_MIN_ROWS`; `piece_plan`, exact from the fences
       each run spool records every `stride` rows), and sort each piece — its spans of every run, read by row-group index
       (`RunSpool`, each spool's footer parsed once) and streamed to DuckDB as Arrow batches — into a piece spool: one
       DuckDB `ORDER BY` holding ≤ `piece_rows` rows, `procs` pieces at a time on cursors of `con`. Each piece's read,
       sort and write seconds are logged per segment and summed per sort (`piece_summary`).
       Every segment's pieces run at once, so no segment, however large, is serial; (segment, piece) order is the
       sort's order.
    3. Write: `cut_plan` splits the groups into at most `procs` (default: the machine's cores; at least `part_rows` rows
       each) contiguous parts, a process each writes its part's groups (`cut_part`) from the spools, and
       `parquet_pages.concat` joins the parts on the same processes: the bytes one writer of every group leaves, whatever
       the parts, runs, pieces or budget.

    Wall time: each pass is the sort's rows over `procs` cores at once, ≈ N / (`procs` × r) for N rows at a core's
    DuckDB sort rate r (~40K rows/s measured for the run phase on an n2-highmem), whatever any segment's size: a merged
    sort's pieces are a second sort of its rows, about the runs' time again (reading lz4 spools instead of the range
    files), plus each piece's decode past its key range (< 2 fence strides and 2 spool groups a run, a group ≈ a
    stride = `share / (FENCE_SLACK × (runs + 1))`: < ½ of its share, ~¼ on average; filtered before the sort). Peak
    memory: DuckDB's limit (`procs` runs or pieces at once, each sized to its share) and ≤
    `tree_mem.SORT_COPIES` × `procs` × `spool_rows` rows of run or piece batches, plus a merged sort's fences (≈
    `FENCE_SLACK` × (runs + 1) keys a piece) and its run spools' parsed footers (`RunSpool`, ~ a group's column
    chunks × runs: no rows); then per worker its imports and `tree_mem.worker_bytes` (writing or
    joining): never the sort's size (`tree_mem.Budget.cut` sizes `procs` to a budget). Peak disk: the run spools (lz4,
    the sort's rows once), plus a merged sort's piece spools (once more) until the parts are written, then the parts
    (≈ `out`) beside `out`."""
    from concurrent.futures import ProcessPoolExecutor
    from contextlib import nullcontext
    from multiprocessing import get_context

    from .parquet_pages import concat

    _, order, size_col = SORTS[sort]
    out.parent.mkdir(parents=True, exist_ok=True)
    page = page_rows or SERVED_PAGE_ROWS
    level = sort == LEVEL
    kv = {"store": "interval", "version": str(STORE_VERSION), "sort": sort, "order": order,
          "segments": "keys" if level else "open,dyadic" if stamps else "open,hist", "open": str(OPEN)}
    k = procs or os.cpu_count() or 1
    work = out.with_name(f".{out.name}.cut")
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    try:
        t0 = monotonic()
        hb.set_stage(f"cut {sort}: sort")
        budget = run_budget(con, k) if budget is None else budget
        # A level run's rows are up to `ROWS_PER_VERSION` its source's (any one key range; `rows_bound` is the whole's): its
        # sort holds them within the same budget.
        runs = cut_runs(src, max(1, budget // ilv.ROWS_PER_VERSION) if level else budget)
        pieced = sort in MERGE_KEYS and len(runs) > 1
        key = merge_key(sort) if sort in MERGE_KEYS else None
        if level:
            runs = [f"({ilv.rows_sql(r, opened)})" for r in runs]
        computed = [x for x in MERGE_KEYS.get(sort, []) if not isinstance(x, str)]
        share = stride = 0
        rg = spool_rows
        if pieced:
            from .interval_compact import row_bytes

            # `runs` > 1 only from range files (`cut_runs`), whose footers size a row.
            share = piece_rows or max(PIECE_MIN_ROWS, budget // (row_bytes(src) + SPOOL_EXTRA))
            stride = max(1, share // (FENCE_SLACK * (len(runs) + 1)))
            rg = min(spool_rows, max(stride, PIECE_MIN_RG))
        run_files = [str(work / f"run-{i:05d}.parquet") for i in range(len(runs))]
        seg = ilv.seg_sql(key_bounds) if level else seg_sql(stamps)
        sel = f"{qcols(schema.names)}, {seg} AS __seg" + "".join(f", {e} AS {n}" for n, e in computed)
        by = qcols(key) if key else order

        def sort_run(cur, i: int) -> tuple[dict[int, int], dict]:
            return _sort_run(cur, f"SELECT {sel} FROM {runs[i]} ORDER BY __seg, {by}", Path(run_files[i]), rg, out.name,
                             key if pieced else None, stride)

        got = [sort_run(con, 0)] if len(runs) == 1 else sn.each(con, sort_run, list(range(len(runs))), min(len(runs), k))
        counts = [c for c, _ in got]
        segs = [(s, sum(c.get(s, 0) for c in counts)) for s in sorted(set().union(*counts))]
        # Each segment's rows in each run spool: `(run, first row, end)`, in key order.
        at_run: dict[int, list[tuple[int, int, int]]] = {s: [] for s, _ in segs}
        for i, c in enumerate(counts):
            at = 0
            for s in sorted(c):
                at_run[s].append((i, at, at + c[s]))
                at += c[s]
        ins = {s: [(run_files[i], a, z - a) for i, a, z in at_run[s]] for s, _ in segs}

        t1 = monotonic()
        tasks = [(s, lo, hi, [(at_run[s][j][0], a, z) for j, a, z in spans])
                 for s, _ in segs if pieced and len(at_run[s]) > 1
                 for lo, hi, spans in piece_plan([(a, z, got[i][1].get(s, [])) for i, a, z in at_run[s]], share)]
        if tasks:
            hb.set_stage(f"cut {sort}: pieces")
            tf = monotonic()
            spools = sn.each(con, lambda _, f: RunSpool(f), run_files, min(len(run_files), k))
            footers_s = monotonic() - tf
            prog = hb.Progress(f"cut {sort} pieces", total=len(tasks), total_rows=sum(n for s, n in segs if any(t[0] == s for t in tasks)), unit="pieces")
            cols = [*schema.names, "__seg"]
            reads = [*cols, *(c for c in key if c not in cols)]
            stats: list[dict] = [{}] * len(tasks)

            def sort_piece(cur, j: int) -> int:
                s, lo, hi, spans = tasks[j]
                st = {"spans": len(spans), "groups": 0, "decoded": 0, "read": 0.0}
                rel = f"__piece_{j}"

                def rows() -> Iterator[pa.RecordBatch]:
                    for i, a, z in spans:
                        yield from spools[i].batches(a, z, spool_rows, reads, st)

                ab = spools[0].md.schema.to_arrow_schema()
                cur.register(rel, pa.RecordBatchReader.from_batches(pa.schema([ab.field(c) for c in reads]), rows()))
                try:
                    sql, params = _piece_sql(ident(rel), cols, key, lo, hi)
                    timing: dict = {}
                    c, _ = _sort_run(cur, sql, work / f"piece-{j:06d}.parquet", spool_rows, out.name, params=params, timing=timing)
                finally:
                    cur.unregister(rel)
                if set(c) - {s}:
                    raise RuntimeError(f"{out.name}: piece {j} of segment {s} holds segments {sorted(c)}")
                # The source is read inside the sort (before its first row): the rest of that is the sort's.
                stats[j] = {**st, "seg": s, "rows": c.get(s, 0), "sort": max(0.0, timing["first"] - st["read"]),
                            "write": timing["s"] - timing["first"], "s": timing["s"]}
                prog.tick(rows=c.get(s, 0))
                return c.get(s, 0)

            rows = sn.each(con, sort_piece, list(range(len(tasks))), min(len(tasks), k))
            prog.done()
            by_seg: dict[int, list[dict]] = {}
            for x in stats:
                by_seg.setdefault(x["seg"], []).append(x)
            for s in sorted(by_seg):
                err(f"  {sort} pieces, segment {s}: {piece_summary(by_seg[s])}")
            err(f"  {sort} pieces: {len(spools)} run footers in {footers_s:.1f}s; {piece_summary(stats)}")
            for s in {t[0] for t in tasks}:
                ins[s] = []
            for j, ((s, _, _, _), n) in enumerate(zip(tasks, rows)):
                ins[s].append((str(work / f"piece-{j:06d}.parquet"), 0, n))
            for s, n in segs:
                if (got_rows := sum(x[2] for x in ins[s])) != n:
                    raise RuntimeError(f"{out.name}: segment {s}'s pieces hold {got_rows} rows of {n}")
        pieces = [p for s, _ in segs for p in ins[s]]
        total = sum(c for _, c in segs)
        parts = cut_plan(segs, rg_rows, min(k, max(1, total // part_rows)) if part_rows else k)
        t2 = monotonic()

        with ProcessPoolExecutor(len(parts), mp_context=get_context("spawn"), initializer=tm.worker_init) if len(parts) > 1 else nullcontext() as ex:
            def args(i: int, groups: list) -> tuple:
                lo, hi = (groups[0][1], groups[-1][1] + groups[-1][2]) if groups else (0, 0)
                return _trim(pieces, lo, hi), groups, str(work / f"part-{i:04d}.parquet"), schema, size_col, kv, page, rg_rows

            hb.set_stage(f"cut {sort}: write")
            prog = hb.Progress(f"cut {sort}", total=len(parts), total_rows=total, unit="parts")
            files = [work / f"part-{i:04d}.parquet" for i in range(len(parts))]
            if len(parts) == 1:
                written = [cut_part(*args(0, parts[0]))]
                prog.tick(rows=total)
            else:
                futs = [ex.submit(cut_part, *args(i, p)) for i, p in enumerate(parts)]
                for f, p in zip(futs, parts):
                    f.add_done_callback(lambda _, r=sum(g[2] for g in p): prog.tick(rows=r))
                written = [f.result() for f in futs]
            prog.done()
            if sum(written) != sum(len(p) for p in parts):
                raise RuntimeError(f"{out.name}: parts wrote {sum(written)} groups of {sum(len(p) for p in parts)}")
            for p in [*work.glob("run-*.parquet"), *work.glob("piece-*.parquet")]:
                p.unlink()
            t3 = monotonic()
            concat(files, out, drop=True, **({"map_": ex.map} if ex else {}))
        hb.after(f"cut {sort}")
        seg_rows = dict(segs)
        err(f"  {sort}: {total:,} rows in {len(segs)} segments (open {seg_rows.get(0, 0):,}): {len(runs)} runs sorted in "
            f"{t1 - t0:.1f}s, {len(tasks)} pieces (≤ {share:,} rows) sorted in {t2 - t1:.1f}s, {len(parts)} parts written in "
            f"{t3 - t2:.1f}s, joined in {monotonic() - t3:.1f}s")
        return write_groups(out, read_bounds([work / f"part-{i:04d}.parquet.bounds" for i in range(len(parts))]), sort,
                            lambda rows: _seg_summary(rows, bool(stamps) and not level))
    finally:
        shutil.rmtree(work, ignore_errors=True)


#: The rows of `<sort>.groups.parquet` per row group, and per batch `write_groups` holds.
GROUPS_RG = 512


def write_groups(out: Path, bounds: Iterable[dict], sort: str, summary) -> dict:
    """`<out stem>.groups.parquet` beside a served sort just written (`GROUPS_SCHEMA`): per row group its exact `bounds`
    (as the writer saw them, in order) and the compact metadata the Worker revives; `summary(rows)` = the segments'
    summary (over each group's `seg`, `row_start`, `row_end`). Streamed: `GROUPS_RG` groups' rows at a time, the sort's
    page indexes a row group at a time (`iter_chunk_indexes`), the bounds as `bounds` yields them."""
    from .parquet_pages import iter_chunk_indexes

    md = pq.read_metadata(out)
    from disk_tree.find.groups import schema_json

    sj = schema_json(md)
    # Each chunk's ColumnIndex / OffsetIndex ranges ride along in its tuple, when it has them (2b.1's page reads).
    ixs = iter_chunk_indexes(out)
    bds = iter(bounds)
    gp = out.with_name(out.name.removesuffix(".parquet") + ".groups.parquet")
    segs_in: list[dict] = []
    start = 0
    with pq.ParquetWriter(gp, GROUPS_SCHEMA, compression="zstd", write_statistics=GROUPS_STAT_COLS, store_schema=False) as w:
        for lo in range(0, md.num_row_groups, GROUPS_RG):
            rows = []
            for g in range(lo, min(lo + GROUPS_RG, md.num_row_groups)):
                rg = md.row_group(g)
                chunks = [rg.column(c) for c in range(rg.num_columns)]
                codecs = {cc.compression for cc in chunks}
                cmeta = [[cc.data_page_offset, cc.total_compressed_size, cc.dictionary_page_offset or 0, *(list(ix) if ix.offset_index_length else [])]
                         for cc, ix in zip(chunks, next(ixs))]
                bd = next(bds, None)
                if bd is None or bd["rows"] != rg.num_rows:
                    raise RuntimeError(f"{out}: group {g} holds {rg.num_rows} rows, the writer saw {bd and bd['rows']}")
                rows.append({"rg": g, **{k: v for k, v in bd.items() if k != "rows"}, "row_start": start, "row_end": start + rg.num_rows,
                             "rg_json": json.dumps([rg.num_rows, codecs.pop(), cmeta], separators=(",", ":"))})
                segs_in.append({"seg": bd["seg"], "row_start": start, "row_end": start + rg.num_rows})
                start += rg.num_rows
            w.write_table(pa.table({c: [r[c] for r in rows] for c in GROUPS_SCHEMA.names}, schema=GROUPS_SCHEMA), row_group_size=GROUPS_RG)
        if not md.num_row_groups:
            # An empty sort's sidecar holds one empty row group, as a fresh build's always has (its bytes are the contract).
            w.write_table(GROUPS_SCHEMA.empty_table(), row_group_size=GROUPS_RG)
        if next(bds, None) is not None:
            raise RuntimeError(f"{out}: the writers saw more groups than its {md.num_row_groups}")
        segs = summary(segs_in)
        w.add_key_value_metadata({"groups_v": "1", "version": str(STORE_VERSION), "schema": json.dumps(sj["schema"], separators=(",", ":")),
                                  "sort": sort, "rows": str(start), "segments": json.dumps({k: v["groups"] for k, v in segs.items()})})
    return {"sort": sort, "rows": start, "groups": md.num_row_groups, "bytes": out.stat().st_size, "groups_bytes": gp.stat().st_size, "segments": segs}


@contextmanager
def plan_cut(con, tree: tm.Budget | None, files: list[str], *, at_once: int = 1, rg_rows: int = SERVED_RG,
             what: str = "cut", sort_rows: int | None = None, extra_rows: int = 0, sort_cols: int | None = None) -> Iterator[tuple[int, int]]:
    """The cut's `(procs, run budget)` for `at_once` sorts at once over `files` (local range or delta files: their
    footers size a row and count the rows), as every entry that cuts plans it. `tree`: the process tree's budget
    (`tree_mem.Budget`): the workers planned under it (`Budget.cut`, rows of the files' widest + `SPOOL_EXTRA`, no holder
    charged more rows than the files have, each sort's writers' metadata `writer_meta` of `sort_cols` columns (the widest
    sort's; default `files`') a group over `sort_rows` rows — the most one sort has, default all of `files`' and `extra_rows`:
    what a sort holds past its files', the level sort's, `interval_level.extra_rows`), DuckDB's limit set to the rest for the block, then the previous limit back; None: the
    machine's cores over `at_once`, DuckDB's limit as set. Each run and piece is a share of DuckDB's limit (`run_budget`
    over `at_once × procs`). The block is `TreeWatch`'s phase `what`."""
    with tm.phase(what):
        cores = os.cpu_count() or 1
        if tree is None:
            procs = max(1, cores // at_once)
            yield procs, run_budget(con, at_once * procs)
            return
        from .interval_compact import input_rows, row_bytes

        if remote := [f for f in files if not Path(f).is_file()]:
            raise SystemExit(f"{what}: the cut's plan reads its inputs' footers, which aren't local files ({remote[0]}…): mount the store")
        rb, n = row_bytes(files) + SPOOL_EXTRA, max(1, input_rows(files) + extra_rows)
        sort_rows = min(n, sort_rows or n)
        meta = writer_meta(files, rg_rows, cols=sort_cols)
        plan = tree.cut(at_once, cores, rg_rows, rb, CUT_SPOOL_RG, n, meta=meta, sort_rows=sort_rows)
        groups = -(-sort_rows // rg_rows)
        kept = at_once * plan.procs * -(-groups // plan.procs) * meta
        prev = con.execute("SELECT current_setting('memory_limit')").fetchone()[0]
        con.execute(tm.duck_sql(plan.duck))
        err(f"{what}: memory {tree.total / tm.MIB:,.0f} MiB = this process {tree.base / tm.MIB:,.0f} + {at_once} sorts × {plan.procs} workers × "
            f"{tree.proc_base / tm.MIB:,.0f} + Arrow {(plan.arrow - kept) / tm.MIB:,.0f} ({rb} B a row) + writers' metadata "
            f"{kept / tm.MIB:,.0f} ({at_once} × {groups:,} groups × {meta:,} B) + DuckDB {plan.duck / tm.MIB:,.0f}")
        try:
            yield plan.procs, run_budget(con, at_once * plan.procs)
        finally:
            con.execute(f"SET memory_limit='{prev}'")


def cut_gen(con, gen_dir: Path, scans: dict, *, rg_rows: int = SERVED_RG, jobs: int = 1, paths: dict | None = None,
            tree: tm.Budget | None = None, sorts: list[str] | None = None, served: Path | None = None) -> tuple[dict, dict | None]:
    """A generation's served sorts from its range files under `gen_dir` (every `served_sorts` entry of its owner axis,
    attribution table and level sort (`interval_level.leveled`, its segments the generation's key ranges), dyadic segments
    over `scans`' stamps): `<served>/<sort>.parquet` + `.groups.parquet` + `<sort>.json` (`served`: default
    `gen_dir/served`), then its churn (`stats.json`). What a base build (`interval_local.build_gen`), a compaction
    (`interval_compact`) and `interval-store cut -a` cut. `jobs` sorts at a time (`static_names.each`, the largest sources
    first), the cores split between the sorts: each its range files' runs sorted, cut in pieces and written in parallel
    (`write_served`), every run and piece sized so the ones sorting at once, across the sorts, fit DuckDB's memory limit
    (`run_budget`). `tree`: the process tree's budget, the workers and DuckDB's limit planned under it (`plan_cut`); None:
    every core, DuckDB's limit as set. `sorts`: only those (no `stats.json`; stats None). Returns `({sort: {rows, groups,
    bytes}}, stats)`."""
    stamps = [s["ts"] for s in scans["scans"]]
    served = gen_dir / "served" if served is None else served
    gen, churn = has_generation(scans), has_churn(scans)
    todo = sorts or served_sorts(scans["slices"], attributed(scans), leveled(scans))
    bounds = ilv.range_bounds(json.loads((gen_dir / "ranges.json").read_text())) if LEVEL in todo else None
    size = {sub: sum(p.stat().st_size for p in (gen_dir / sub).glob("r*.parquet")) for sub in {SORTS[s][0] for s in todo}}
    at_once = max(1, min(jobs, len(todo)))
    ins = [str(p) for sub in sorted(size) for p in sorted((gen_dir / sub).glob("r*.parquet"))]
    most = None
    extra = 0
    if tree is not None:
        from .interval_compact import input_rows

        rows = {sub: input_rows([str(p) for p in (gen_dir / sub).glob("r*.parquet")]) for sub in size}
        extra = ilv.extra_rows(con, [str(p) for p in sorted((gen_dir / SORTS[LEVEL][0]).glob("r*.parquet"))]) if LEVEL in todo else 0
        most = max(rows[SORTS[s][0]] + (extra if s == LEVEL else 0) for s in todo)
    cols = max(len(served_schema(s, gen, churn)) for s in todo)
    with plan_cut(con, tree, ins, at_once=at_once, rg_rows=rg_rows, sort_rows=most, extra_rows=extra, sort_cols=cols) as (procs, budget):
        def one(cur, sort: str) -> dict:
            sub, _, _ = SORTS[sort]
            t1 = monotonic()
            with tm.phase(f"sort {sort}"):
                sd = write_served(cur, [str(p) for p in sorted((gen_dir / sub).glob("r*.parquet"))], sort, served / f"{sort}.parquet",
                                  served_schema(sort, gen, churn), rg_rows=rg_rows, stamps=stamps, procs=procs, budget=budget, key_bounds=bounds)
            sd["s"] = round(monotonic() - t1, 1)
            (served / f"{sort}.json").write_text(json.dumps(sd, sort_keys=True) + "\n")
            return sd

        order = sorted(todo, key=lambda s: -size[SORTS[s][0]])
        got = dict(zip(order, sn.each(con, one, order, jobs)))
    cut = {sort: {"rows": got[sort]["rows"], "groups": got[sort]["groups"], "bytes": got[sort]["bytes"]} for sort in todo}
    if sorts:
        return cut, None
    stats = {**churn_stats(con, str(gen_dir), scans, paths), "slices": scans["slices"]}
    (served / "stats.json").write_text(json.dumps(stats, indent=1) + "\n")
    return cut, stats


def _seg_summary(rows: list[dict], dyadic: bool) -> dict:
    """Groups and rows per segment kind: `open`, then the closed ones — `hist` (one segment) or by
    dyadic level (`L<k>`, with its block count)."""
    out: dict[str, dict] = {}
    for r in rows:
        s = r["seg"]
        name = "open" if s == 0 else f"L{(s - 1) // SEG_LEVEL}" if dyadic else "hist"
        e = out.setdefault(name, {"groups": 0, "rows": 0, "segs": set()})
        e["groups"] += 1
        e["rows"] += r["row_end"] - r["row_start"]
        e["segs"].add(s)
    return {k: {"groups": v["groups"], "rows": v["rows"], **({"blocks": len(v["segs"])} if k.startswith("L") else {})} for k, v in out.items()}


# ── CLI ────────────────────────────────────────────────────────────────────


@group("interval-store")
def cli() -> None:
    """The change-interval path store (specs/interval-store.md)."""
    log_job_stamp("interval-store")


@cli.command("build-ranges")
@option("-b", "--bucket", help="The store: a bucket name (gs://) or URL, or a dir (default: the profile's)")
@option("-f", "--force", is_flag=True, help="Rebuild ranges whose digest is already uploaded")
@option("-g", "--gen", required=True, help="Generation: STORE/interval-store/GEN/ (its scans.json, ranges.json)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", help="Local mount of the data bucket")
@option("-M", "--mem", default="80GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task: task t builds [t·n, (t+1)·n)")
@option("-o", "--out", default="/stage/out", help="Local output dir (uploaded, then removed)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-r", "--range", "only", help="Comma-separated range indices (overrides -i/-n; a Batch job's tasks split them)")
@option("-S", "--slices", is_flag=True, help="Build the owner-slice versions (`sv/`, `sv-digest/`) instead of the path versions")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB database + spill dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
@option("-P", "--profile", help="Deployment profile JSON (default $INTERVAL_STORE_PROFILE)")
def build_ranges_cmd(bucket, profile, force, gen, index, mount, mem, per_task, out, threads, only, slices, tmp, no_upload) -> None:
    """Build key ranges' path and read versions over every scan of GEN's `scans.json`, verifying each
    scan's reconstruction (digest written last: it marks the range done)."""
    store = Store(_bucket(bucket, profile))
    prefix = f"{PREFIX}/{gen}"
    scans = store.read_json(f"{prefix}/scans.json")
    ranges = store.read_json(f"{prefix}/ranges.json")
    if only:
        todo = [int(x) for x in only.split(",")]
        # As a Batch job of several tasks, task t takes every count-th of the listed ranges.
        if os.environ.get("BATCH_TASK_COUNT"):
            todo = todo[sn._task(index)::int(os.environ["BATCH_TASK_COUNT"])]
    else:
        t = sn._task(index)
        todo = list(range(t * per_task, min((t + 1) * per_task, ranges["k"])))
    import duckdb

    Path(tmp).mkdir(parents=True, exist_ok=True)
    db = Path(tmp) / "build.duckdb"
    if db.exists():
        db.unlink()
    con = duckdb.connect(str(db))
    con.execute(f"SET threads={threads}; SET memory_limit='{mem}'; SET preserve_insertion_order=false; SET parquet_metadata_cache=true")
    con.execute(f"SET temp_directory={q(str(Path(tmp) / 'spill'))}")
    err(f"interval-store build: duckdb {duckdb.__version__}, pyarrow {pa.__version__}, {len(scans['scans'])} scans, ranges {todo}")
    dig = "sv-digest" if slices else "digest"
    if slices and not gen_sliced(store, prefix):
        err(f"interval-store build -S: {prefix} names no owner; no slices to build")
        return
    for i in todo:
        if not force and store.exists(f"{prefix}/{dig}/r{i:04d}.json"):
            err(f"range {i}: already built")
            continue
        outp = Path(out) / f"r{i}"
        doc = (build_slices_range if slices else build_range)(scans, ranges, i, outp, con, mount=mount)
        if not no_upload:
            # The digest marks the range done: uploaded last.
            digest = outp / dig
            moved = Path(out) / f"r{i}-digest"
            shutil.move(str(digest), moved)
            store.put_tree(outp, prefix)
            store.put_tree(moved, f"{prefix}/{dig}")
            shutil.rmtree(outp)
            shutil.rmtree(moved)
        print(json.dumps({k: v for k, v in doc.items() if k != "scans"}), flush=True)


def path_counts(con, files: list[str]) -> dict:
    """The distinct paths of a generation's `pv/` range files (`{"all": n, "kinds": {kind: n}}`): each file counted alone,
    one at a time, and summed, so a statement holds one range's paths however many the generation has. The sum is exact:
    a range file holds the rows whose own `(depth, path)` is in its key range (`static_names.range_preds`: `[lo, hi)` over
    that key, not over the objects under a directory), so no `(depth, path)` is in two files; and a path has one depth,
    its segments', so neither is a `path`."""
    out: dict = {"all": 0, "kinds": {}}
    for f in files:
        for kind, of_kind, of_all in con.execute(f"""SELECT kind, count(DISTINCT path), count(DISTINCT (depth, path)) FROM read_parquet({q(f)})
                GROUP BY GROUPING SETS ((kind), ())""").fetchall():
            if kind is None:  # the grand total's row (`kind` is never NULL)
                out["all"] += of_all
            else:
                out["kinds"][kind] = out["kinds"].get(kind, 0) + of_kind
    return out


def churn_stats(con, root: str, scans: dict, paths: dict | None = None) -> dict:
    """Per scan: path versions opened and closed there, read versions likewise, and live rows — the
    delta a per-scan append writes — plus totals by kind. `paths`: the distinct paths, already counted by whoever wrote
    the range files (`{"all": n, "kinds": {kind: n}}`, `interval_compact.compact_range`'s, summed over key-disjoint
    sub-ranges); None: counted here, a range file at a time (`path_counts`). No statement holds every path of the
    generation at once."""
    with tm.phase("churn_stats"):
        ids = {s["ts"]: s["id"] for s in scans["scans"]}
        pv, rd = f"read_parquet({q(root + '/pv/r*.parquet')})", f"read_parquet({q(root + '/rd/r*.parquet')})"
        per = {i: {"id": i, "opened": 0, "closed": 0, "reads_opened": 0, "reads_closed": 0} for i in ids.values()}
        for col, key, src in (("vf", "opened", pv), ("vt", "closed", pv), ("vf", "reads_opened", rd), ("vt", "reads_closed", rd)):
            for t, n in con.execute(f"SELECT {col}, count(*) FROM {src} WHERE {col} <> {OPEN} GROUP BY {col}").fetchall():
                per[ids[t]][key] = n
        paths = path_counts(con, range_files(con, root + "/pv/r*.parquet")) if paths is None else paths
        kinds = {k: {"versions": n, "open": o, "paths": paths["kinds"][k]} for k, n, o in con.execute(
            f"SELECT kind, count(*), count(*) FILTER (WHERE vt = {OPEN}) FROM {pv} GROUP BY kind ORDER BY kind").fetchall()}
        versions, live = con.execute(f"SELECT count(*), count(*) FILTER (WHERE vt = {OPEN}) FROM {pv}").fetchone()
        rtot = con.execute(f"SELECT count(*), count(*) FILTER (WHERE vt = {OPEN}) FROM {rd}").fetchone()
    return {"versions": versions, "open": live, "paths": paths["all"], "reads": rtot[0], "reads_open": rtot[1], "kinds": kinds,
            "scans": [per[s["id"]] for s in scans["scans"]]}


@cli.command("fold")
@option("-b", "--bucket", help="The store: a bucket name (gs://) or URL, or a dir (default: the profile's)")
@option("-g", "--gen", required=True, help="Generation (its pv/, rd/ range files)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", help="Local mount of the data bucket")
@option("-M", "--mem", default="64GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task: task t folds [t·n, (t+1)·n)")
@option("-o", "--out", default="/stage/out", help="Local output dir (uploaded, then removed)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-P", "--profile", help="Deployment profile JSON (default $INTERVAL_STORE_PROFILE)")
@option("-S", "--slice-totals", is_flag=True, help="Instead: each range's slice versions with their path's total (`svt/`, `slice_totals_range`)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
def fold_cmd(bucket, gen, index, mount, mem, per_task, out, threads, profile, slice_totals, tmp, no_upload) -> None:
    """Fold each range's read days into its path versions: `pvl/r####.parquet` (what `cut` serves); with
    `-S`, its slice versions with their path's total: `svt/r####.parquet`."""
    store = Store(_bucket(bucket, profile))
    prefix = f"{PREFIX}/{gen}"
    ranges = store.read_json(f"{prefix}/ranges.json")
    t = sn._task(index)
    con = connect(threads, mem, tmp)
    root = _root(store, prefix, mount)
    if slice_totals and not gen_sliced(store, prefix):
        err(f"interval-store fold -S: {prefix} names no owner; no slices to fold")
        return
    for i in range(t * per_task, min((t + 1) * per_task, ranges["k"])):
        sub = "svt" if slice_totals else "pvl"
        doc = (slice_totals_range if slice_totals else fold_range)(root, i, Path(out), con)
        if not no_upload:
            store.put_tree(Path(out) / sub, f"{prefix}/{sub}")
            shutil.rmtree(Path(out) / sub)
        print(json.dumps(doc), flush=True)


@cli.command("churn")
@option("-b", "--bucket", help="The store: a bucket name (gs://) or URL, or a dir (default: the profile's)")
@option("-g", "--gen", required=True, help="Generation (its folded `pvl/` range files)")
@option("-j", "--jobs", default=1, type=IntRange(min=1), help="Ranges at a time, on cursors of one DuckDB database")
@option("-M", "--mem", default="64GB", help="DuckDB memory limit")
@option("-o", "--out", default="/stage/out", help="Local work dir (the range files are copied here, uploaded, then removed)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-P", "--profile", help="Deployment profile JSON (default $INTERVAL_STORE_PROFILE)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
def churn_cmd(bucket, gen, jobs, mem, out, threads, profile, tmp, no_upload) -> None:
    """Set `churn_cum` on every range's folded path versions (`interval_churn.churn_gen`: a rollup across the ranges, one
    task, after `fold` and before `cut`), and flag it in `scans.json` (`"churn": true`)."""
    from .blobstore import mirror
    from .interval_churn import churn_from, churn_gen, record_churn

    store = Store(_bucket(bucket, profile))
    prefix = f"{PREFIX}/{gen}"
    scans = store.read_json(f"{prefix}/scans.json")
    local = Path(out) / "churn-gen"
    mirror(store, prefix, local, keep=lambda rel: rel.startswith("pvl/") or rel == "ranges.json")
    base = churn_from(scans["scans"])
    doc = churn_gen(connect(threads, mem, tmp), local, json.loads((local / "ranges.json").read_text())["k"], base["ts"], Path(tmp), jobs=jobs)
    if not no_upload:
        store.put_tree(local / "pvl", f"{prefix}/pvl")
        store.write_text(f"{prefix}/scans.json", json.dumps(record_churn(scans), indent=1) + "\n")
        shutil.rmtree(local)
    print(json.dumps({**doc, "base": base["id"]}), flush=True)


def _refuse_overwrite(store: Store, keys: list[str]) -> None:
    """Published served files are immutable: a cut into a generation that already has them is refused
    (cut into a new one, `--to-gen`)."""
    there = [k for k in keys if store.exists(k)]
    if there:
        raise SystemExit(f"refusing to overwrite published files: {', '.join(there)} (cut into a new generation: -O)")


@cli.command("cut")
@expect_sha_options("interval-store cut")
@option("-a", "--all-sorts", is_flag=True, help="Every served sort of the generation, -j at a time, then its churn (`stats.json`), as a build or compaction cuts them (`cut_gen`, one -M over all of them): the whole generation's cut, e.g. its real-scale memory check (needs -m)")
@option("-b", "--bucket", help="The store: a bucket name (gs://) or URL, or a dir (default: the profile's)")
@option("-D", "--no-dyadic", is_flag=True, help="One closed segment, not one per dyadic block of scans (`seg_sql`)")
@option("-g", "--gen", required=True, help="Generation (its pv/, rd/ range files)")
@option("-i", "--index", type=int, help="Task index → sort (path, bysize, reads, then `stats`: per-scan churn; default: $BATCH_TASK_INDEX)")
@option("-j", "--jobs", default=1, type=IntRange(min=1), help="With -a: sorts cut at once, the cores split between them (`cut_gen`'s `jobs`)")
@option("-J", "--as-jobs", default=1, type=IntRange(min=1), help="Size workers and runs as `cut_gen(jobs=N)` does for each of N sorts at once (a benchmark of one sort under a whole cut's split; default 1)")
@option("-m", "--mount", help="Local mount of the data bucket")
@option("-M", "--mem", default="100GB", help="Memory budget of the whole process tree (`tree_mem`): the cut's workers and DuckDB's limit planned under it (`plan_cut`); its peak is logged (`TreeWatch`; exit 1 past it with $DT_TREE_MEM_ASSERT)")
@option("-o", "--out", default="/stage/out", help="Local output dir (uploaded, then removed)")
@option("-O", "--to-gen", help="Write the served files (and a copy of scans.json) under this generation instead of GEN")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-r", "--rg-rows", default=SERVED_RG, type=int, help="Rows per served row group")
@option("-s", "--sort", "sorts", multiple=True, type=Choice(list(SORTS)), help="Sort(s) to cut (default: the task's)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
@option("-w", "--workers", type=IntRange(min=1), help="At most this many workers a sort's runs, pieces and parts use (default: as many as -M holds, ≤ the cores over -J)")
@option("-P", "--profile", help="Deployment profile JSON (default $INTERVAL_STORE_PROFILE)")
def cut_cmd(all_sorts, bucket, no_dyadic, profile, gen, index, jobs, as_jobs, mount, mem, out, to_gen, threads, rg_rows, sorts, tmp, no_upload, workers) -> None:
    """Cut the served sorts from the range files: `served/<sort>.parquet` + `.groups.parquet`. With `-U -s SORT` (and
    `-J`/`-w`), a benchmark of that one sort: each pass's seconds, and a merged sort's per-piece read / sort / write
    summaries (per segment and in total), on stderr. With `-a`, the whole generation's cut (`cut_gen`, `-j` sorts at
    once). Either way the process tree's peak against `-M` is logged at the end (`tree_mem.TreeWatch`)."""
    with tm.watch(mem, f"cut {gen}"):
        _cut(all_sorts, bucket, no_dyadic, profile, gen, index, jobs, as_jobs, mount, mem, out, to_gen, threads, rg_rows, sorts, tmp, no_upload, workers)


def _cut(all_sorts, bucket, no_dyadic, profile, gen, index, jobs, as_jobs, mount, mem, out, to_gen, threads, rg_rows, sorts, tmp, no_upload, workers) -> None:
    if all_sorts and (sorts or index is not None or as_jobs != 1 or workers or no_dyadic):
        raise SystemExit("cut -a: every sort as `cut_gen` cuts them; -s, -i, -J, -w and -D don't apply")
    if all_sorts and not mount:
        raise SystemExit("cut -a: the generation's range files are read from its mount (-m)")
    store = Store(_bucket(bucket, profile))
    prefix = f"{PREFIX}/{gen}"
    dst_prefix = f"{PREFIX}/{to_gen or gen}"
    todo = list(sorts) or ([] if all_sorts else [CUT_TASKS[sn._task(index)]])
    tree = tm.Budget.measure(sn.mem_bytes(mem))
    con = connect(threads, tm.stage_mem(tree, "the cut"), tmp)
    root = _root(store, prefix, mount)
    scans = store.read_json(f"{prefix}/scans.json")
    has_slices = gen_sliced(store, prefix)
    # The objects' native version: recorded, else whether the folded path versions carry it.
    has_gen = has_generation(scans) or source_has_generation(con, root + '/pvl/r*.parquet')
    out_dir = Path(out) / "served"
    if all_sorts:
        doc = record_generation({**scans, "slices": has_slices}, has_gen)
        cut = served_sorts(has_slices, attributed(doc))
        if not no_upload:
            _refuse_overwrite(store, [f"{dst_prefix}/served/{s}{x}" for s in cut for x in (".parquet", ".groups.parquet", ".json")])
        t0 = monotonic()
        got, stats = cut_gen(con, Path(root), doc, rg_rows=rg_rows, jobs=jobs, tree=tree, served=out_dir)
        err(f"cut {gen}: {len(got)} sorts, {sum(v['rows'] for v in got.values()):,} rows in {monotonic() - t0:.0f}s ({jobs} at a time)")
        if not no_upload:
            if to_gen and to_gen != gen:
                copy_base(store, prefix, dst_prefix)
            store.put_tree(out_dir, f"{dst_prefix}/served")
            shutil.rmtree(out_dir)
            record_slices(store, dst_prefix, scans, has_slices, has_gen)
        print(json.dumps({"sorts": got, "s": round(monotonic() - t0, 1)}), flush=True)
        return
    if todo == ["stats"]:
        doc = {**churn_stats(con, root, scans), "slices": has_slices}
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "stats.json").write_text(json.dumps(doc, indent=1) + "\n")
        if not no_upload:
            _refuse_overwrite(store, [f"{dst_prefix}/served/stats.json"])
            if to_gen and to_gen != gen:
                copy_base(store, prefix, dst_prefix)
            store.put_tree(out_dir, f"{dst_prefix}/served")
            record_slices(store, dst_prefix, scans, has_slices, has_gen)
        print(json.dumps({k: v for k, v in doc.items() if k != "scans"}), flush=True)
        return
    stamps = None if no_dyadic else [s["ts"] for s in scans["scans"]]
    for sort in todo:
        sub, _, _ = SORTS[sort]
        if sub in ATTRIBUTION_SUBS and not attributed(scans):
            err(f"cut {sort}: {prefix} has no attribution table")
            continue
        if sort == LEVEL and not leveled(scans):
            err(f"cut {sort}: {prefix} doesn't carry the level sort (`scans.json` `\"{ilv.LEVEL_FLAG}\": true`)")
            continue
        if sort not in served_sorts(has_slices, True):
            err(f"cut {sort}: {prefix} names no owner; no slice sorts")
            continue
        schema = served_schema(sort, has_gen, has_churn(scans))
        if not no_upload:
            _refuse_overwrite(store, [f"{dst_prefix}/served/{sort}{x}" for x in (".parquet", ".groups.parquet", ".json")])
        t0 = monotonic()
        dst = out_dir / f"{sort}.parquet"
        files = range_files(con, f"{root}/{sub}/r*.parquet")
        bounds = ilv.range_bounds(store.read_json(f"{prefix}/ranges.json")) if sort == LEVEL else None
        extra = ilv.extra_rows(con, files) if sort == LEVEL and tree is not None else 0
        # As `cut_gen` sizes each of `as_jobs` sorts at once: the cores and the budget split between them.
        with plan_cut(con, tree, files, at_once=as_jobs, rg_rows=rg_rows, what=f"cut {sort}", extra_rows=extra, sort_cols=len(schema)) as (procs, budget):
            procs = min(procs, workers or procs)
            err(f"cut {sort}: {procs} workers, runs of ≤ {budget:,} B (as {as_jobs} sorts at once)")
            doc = write_served(con, files, sort, dst, schema, rg_rows=rg_rows, stamps=stamps, procs=procs, budget=budget, key_bounds=bounds)
        doc["s"] = round(monotonic() - t0, 1)
        err(f"cut {sort}: {doc['rows']:,} rows, {doc['groups']:,} groups, {doc['bytes']:,} B in {doc['s']}s")
        (out_dir / f"{sort}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
        if not no_upload:
            store.put_tree(out_dir, f"{dst_prefix}/served")
            shutil.rmtree(out_dir)
        print(json.dumps(doc), flush=True)
    if not no_upload:
        # The generation's scans and owner axis (the reader's `scans.json`; under `-O`, the new generation's).
        record_slices(store, dst_prefix, scans, has_slices, has_gen)


def copy_base(store: Store, src: str, dst: str, *, workers: int = 16) -> int:
    """Generation `src`'s key ranges (`ranges.json`) and base range files (`SUB_SCHEMA`'s dirs) copied into `dst` (server
    side on a bucket), each unless `dst` has it at its size: a generation cut from another's ranges (`cut -O`) holds its
    own base, as every reader (append, compaction, backfill, retire's lineage) assumes. Returns how many it copied."""
    from concurrent.futures import ThreadPoolExecutor

    have = store.listing(dst)
    todo = [k for k, n in store.listing(src).items()
            if (k[len(src) + 1:] == "ranges.json" or k[len(src) + 1:].split("/")[0] in SUB_SCHEMA)
            and have.get(f"{dst}/{k[len(src) + 1:]}") != n]
    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(lambda k: store.copy(k, f"{dst}/{k[len(src) + 1:]}"), todo))
    err(f"cut: {len(todo)} base files ({src}'s ranges.json and range files) copied into {dst}")
    return len(todo)


def download_served(store: Store, prefix: str, dst: Path, *, workers: int = 16) -> None:
    """The generation's served sorts (+ `.groups.parquet`) to `dst`: from a whole GCS bucket chunked in parallel, else
    through the store."""
    from .blobstore import gs_bucket

    dst.mkdir(parents=True, exist_ok=True)
    bucket = gs_bucket(store.root)
    if bucket is None:
        for key, size in store.listing(f"{prefix}/served").items():
            out = dst / key.rsplit("/", 1)[-1]
            if out.name.endswith(".parquet") and not (out.exists() and out.stat().st_size == size):
                store.get(key, out)
        return
    from google.cloud import storage
    from google.cloud.storage import transfer_manager as tm

    b = storage.Client().bucket(bucket)
    for blob in storage.Client().list_blobs(bucket, prefix=f"{prefix}/served/"):
        name = blob.name.rsplit("/", 1)[-1]
        if not name.endswith(".parquet"):
            continue
        out = dst / name
        if out.exists() and out.stat().st_size == blob.size:
            continue
        t0 = monotonic()
        tm.download_chunks_concurrently(b.blob(blob.name), str(out), chunk_size=64 << 20, max_workers=workers)
        err(f"served {name}: {blob.size:,} B in {monotonic() - t0:.0f}s")


def verify_runs(store: Store, prefix: str, dst: Path, scans: dict) -> list[str]:
    """The newest manifest's runs, read beside the base as the Worker reads them: each run's served sorts downloaded under
    `dst`, and its scans' docs (`deltas/<scan>/scans.json`, a scan's own run dir) added to `scans` so they can be sampled.
    A scan whose own dir a carry folded and pruned (R2 keeps none of a folded run) stays unsampleable."""
    from .append_runner import latest_key

    newest = latest_key([k.removeprefix(f"{prefix}/") for k in store.keys(f"{prefix}/manifests/")])
    if newest is None:
        return []
    out = []
    for r in store.read_json(f"{prefix}/{newest}")["runs"]:
        d = dst / r["key"]
        download_served(store, f"{prefix}/{r['key']}", d)
        out.append(str(d))
        for sid in r["scans"]:
            if store.exists(f"{prefix}/deltas/{sid}/scans.json"):
                scans["scans"] += store.read_json(f"{prefix}/deltas/{sid}/scans.json")["scans"]
    err(f"verify: base + {len(out)} run(s) of {newest}")
    return out


@cli.command("verify")
@option("-b", "--bucket", help="The store: a bucket name (gs://) or URL, or a dir (default: the profile's)")
@option("-c", "--changes", is_flag=True, help="Also count each diff's changed versions (`Store.changes`: a cost statistic, never compared; reads every group with an open or close in the span)")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", help="Local mount of the data bucket (per-scan sorts are copied from it)")
@option("-n", "--tasks", default=1, type=IntRange(min=1), help="Tasks the sampled dates are split over")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-s", "--scans", "scan_ids", help="Comma-separated scan ids to sample (default: the profile's `verify_scans`)")
@option("-T", "--tmp", default="/stage/tmp", help="Local scratch (served store, per-scan copies, spill)")
@option("-P", "--profile", help="Deployment profile JSON (default $INTERVAL_STORE_PROFILE)")
def verify_cmd(bucket, changes, profile, gen, index, mount, tasks, out, scan_ids, tmp) -> None:
    """Parity against the per-scan path store over the profile's `verify_scans`: every
    view's tiles and every diff's rows, with both sides' read cost; `verify/t##.jsonl` + `.json`, and each sampled
    scan's parity marker `verify/<id>.json` (`interval_verify.marker`: `pass` when every view and diff is equal)."""
    store = Store(_bucket(bucket, profile))
    from . import interval_verify as iv

    prefix = f"{PREFIX}/{gen}"
    t = sn._task(index)
    tmp = Path(tmp)
    download_served(store, prefix, tmp / "served")
    scans = store.read_json(f"{prefix}/scans.json")
    runs = verify_runs(store, prefix, tmp / "runs", scans)
    outp = Path(out) / "verify"
    outp.mkdir(parents=True, exist_ok=True)
    sampled = scan_ids.split(",") if scan_ids else load_profile(profile)["verify_scans"]
    tag = f"t{t:02d}" if not scan_ids else f"t{t:02d}-{sampled[0]}"
    done: list[dict] = []

    def publish(s: dict) -> None:
        """Each scan's parity marker (`scan retire`'s gate) and the task's summaries so far, up as the scan completes:
        a task killed later (an OOM on a later scan's diffs) keeps every scan it finished."""
        done.append(s)
        store.write_text(f"{prefix}/verify/{s['date']}.json", json.dumps(iv.marker(s, gen)) + "\n")
        store.write_text(f"{prefix}/verify/{tag}.json", json.dumps(done, indent=1) + "\n")
        store.put(outp / f"{tag}.jsonl", f"{prefix}/verify/{tag}.jsonl")

    from .append_runner import latest_key
    from .interval_append import covered_scans

    newest = latest_key([k.removeprefix(f"{prefix}/") for k in store.keys(f"{prefix}/manifests/")])
    covered = covered_scans(store.read_json(f"{prefix}/scans.json"), store.read_json(f"{prefix}/{newest}") if newest else None)
    summaries = iv.verify_task(sampled, t, tasks, str(tmp / "served"), scans, outp / f"{tag}.jsonl", tmp, mount, runs, on_summary=publish,
                                 changes=changes, covered=covered)
    (outp / f"{tag}.json").write_text(json.dumps(summaries, indent=1) + "\n")
    print(json.dumps(summaries), flush=True)


#: What the Worker reads: the scans and the served sorts with their group indexes.
R2_SERVED = ("scans.json", "served/path.", "served/bysize.", "served/reads.", "served/slices.", "served/slices-bysize-user.", "served/slices-bytotal.",
             f"served/{ATTRIBUTION}.", f"served/{LEVEL}.")


@cli.command("r2-copy")
@option("-b", "--bucket", help="The store: a bucket name (gs://) or URL (default: the profile's)")
@option("-g", "--gen", required=True, help="Generation")
@option("-m", "--mount", help="Ignored (the Batch driver passes it)")
@option("-n", "--dry-run", is_flag=True, help="List what would be copied")
@option("-w", "--workers", default=8, type=int, help="Parallel copies")
@option("-P", "--profile", help="Deployment profile JSON (default $INTERVAL_STORE_PROFILE)")
def r2_copy_cmd(bucket, profile, gen, mount, dry_run, workers) -> None:
    """Copy the generation's files the Worker reads (`R2_SERVED`: `scans.json` and the served sorts; not
    `served/stats.json` or `ranges.json`, which only the jobs read, from GCS) GCS → R2 under the same keys
    (`static-names r2-copy`'s streaming copy: objects already there with the same size and md5 are skipped). R2 via `R2_ENDPOINT`,
    `R2_BUCKET` and AWS_* keys. A no-op when the store is the serving R2 bucket itself."""
    from .append_runner import r2_copy, r2_objects
    from .blobstore import gs_bucket, serves

    root = _bucket(bucket, profile)
    if serves(root, os.environ.get("R2_BUCKET")):
        err(f"r2-copy {gen}: the store {root} is the serving R2 bucket; nothing to copy")
        return
    if (bucket := gs_bucket(root)) is None:
        raise SystemExit(f"r2-copy: copies from a whole GCS bucket, not {root}")
    prefix = f"{PREFIX}/{gen}"
    objs = r2_objects(bucket, [prefix + "/"], lambda key: key.removeprefix(prefix + "/").startswith(R2_SERVED))
    doc = r2_copy(bucket, objs, workers=workers, dry_run=dry_run)
    err(f"r2-copy {gen}: {len(objs)} objects, {len(doc['keys']) if dry_run else doc['copied']} to copy ({doc['bytes']:,} B)")
    if dry_run:
        for k in doc["keys"]:
            print(k)
        return
    print(json.dumps({"gen": gen, **doc}))


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    cli()
