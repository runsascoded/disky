"""The change-interval path store (specs/interval-store.md): every scan's path-store rows as versions of
one row per `(depth, path)` — objects and dirs, owner slices folded into `us` — with `[vf, vt)` validity,
instead of a full per-scan copy. The build reuses the static name index's key ranges and pyrmts'
gaps-and-islands kernel (`static_names`); `cut` lays the versions out as the served sorts (`path`,
`bysize`, `reads`) with a `.groups.parquet` beside each, which a reader plans its range reads from.

    dt-cloud interval-store build -g GEN [-i TASK -n PER_TASK | -r RANGES]   # per range: pv/, rd/, digest/
    dt-cloud interval-store cut -g GEN [-s SORT]                             # served/{path,bysize,reads}.parquet + .groups.parquet
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from functools import partial
from pathlib import Path
from time import monotonic

import pyarrow as pa
import pyarrow.parquet as pq
from click import Choice, IntRange, group, option

from . import static_names as sn
from .static_names import OPEN, U64, connect, q, range_preds, read_json, upload_tree, write_sorted

err = partial(print, file=sys.stderr, flush=True)

PREFIX = "interval-store"
PROFILES = Path(__file__).parent / "interval_profiles"


def load_profile(name: str | None) -> dict:
    """A deployment profile (`interval_profiles/<name>.json`, or a path): its bucket and the scans the
    parity check samples. `$INTERVAL_STORE_PROFILE` names one when `-P` doesn't."""
    name = name or os.environ.get("INTERVAL_STORE_PROFILE")
    if not name:
        raise SystemExit("no profile: pass -P NAME|PATH or set $INTERVAL_STORE_PROFILE (e.g. `gcs`)")
    p = Path(name) if name.endswith(".json") else PROFILES / f"{name}.json"
    return json.loads(p.read_text())


def _bucket(bucket: str | None, profile: str | None) -> str:
    return bucket or load_profile(profile)["bucket"]
#: A served sort's row-group size: the per-scan path store's (decode cost per group is the same).
SERVED_RG = 8192
#: The range files' row-group size (an intermediate: only `cut` and the verifier read them).
RANGE_RG = 65536

KEY_COLS = ["depth", "path"]
#: The values whose change opens a version. `dr` is the size-weighted mean stamp rounded to the second
#: (the change test; the exact `wts` is carried from the version's first scan), `us` the owner slices.
#: `last_read` is not here: it moves from scan to scan on read paths and lives in its own intervals (`rd/`).
CHANGE_COLS = ["kind", "size", "n_files", "n_children", "n_desc", "mtime", "dr", "wb", "c2", "c3", "c4", "us"]
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
    pa.field("c2", pa.int64(), nullable=False),
    pa.field("c3", pa.int64(), nullable=False),
    pa.field("c4", pa.int64(), nullable=False),
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
SV_HASH = "hash(depth, path, usr, kind, size, n_files, n_children, n_desc, mtime, dr, wb, c2, c3, c4, last_read)"

# ── Per-scan rows → one row per path ───────────────────────────────────────

#: A scan's raw rows in the reader's terms (`index.ts` `toRowV1` / `toRowV2`): `mw` is the bytes the
#: mean stamp weighs and `wt` = `mtime_mean · mw` (`view.ts` `merge` adds both only where `mw > 0`).
V2_ROW = """depth::UTINYINT AS depth, path, coalesce(usr, '') AS usr, kind = 'file' AS is_file, size::BIGINT AS size,
    n_files::BIGINT AS n_files, coalesce({n_children}, -1)::BIGINT AS n_children, coalesce({n_desc}, -1)::BIGINT AS n_desc,
    coalesce({mtime}, -1)::BIGINT AS mtime, CASE WHEN {mtime_mean} IS NULL THEN 0 ELSE size END::BIGINT AS mw,
    CASE WHEN {mtime_mean} IS NULL THEN 0 ELSE {mtime_mean}::DOUBLE * size::DOUBLE END AS wt,
    coalesce({last_read}, -1)::INTEGER AS last_read, coalesce({c2}, 0)::BIGINT AS c2, coalesce({c3}, 0)::BIGINT AS c3, coalesce({c4}, 0)::BIGINT AS c4"""
V1_ROW = """depth::UTINYINT AS depth, path, coalesce(usr, '') AS usr, false AS is_file, b::BIGINT AS size, o::BIGINT AS n_files,
    -1::BIGINT AS n_children, -1::BIGINT AS n_desc, -1::BIGINT AS mtime, coalesce(wb, 0)::BIGINT AS mw,
    CASE WHEN coalesce(wb, 0) > 0 THEN (coalesce(wts, 0)::DOUBLE / wb::DOUBLE) * wb::DOUBLE ELSE 0 END AS wt,
    coalesce(a, -1)::INTEGER AS last_read, coalesce(c2, 0)::BIGINT AS c2, coalesce(c3, 0)::BIGINT AS c3, coalesce(c4, 0)::BIGINT AS c4"""


def path_rows_sql(con, src: str, preds: list[str], version: int | None = None) -> str:
    """One scan's rows in the range, one per `(depth, path)`: the sums and maxima the reader's `merge`
    takes over a path's rows (owner slices, a v1 index's duplicate rows), `kind` = `dir` if any row is,
    `us` = the owner slices, `dr` = the mean stamp to the second."""
    version = version or sn.source_version(con, src)
    if version == 2:
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({q(src)})").fetchall()}
        sel = V2_ROW.format(**{k: (c if c in cols else "NULL") for k, c in sn.V2_OPTIONAL.items()})
    else:
        sel = V1_ROW
    raw = " UNION ALL ".join(f"SELECT {sel} FROM read_parquet({q(src)}) WHERE {p}" for p in preds)
    one = """SELECT depth, path, CASE WHEN bool_and(is_file) THEN 'file' ELSE 'dir' END AS kind, sum(size)::BIGINT AS size,
        sum(n_files)::BIGINT AS n_files, max(n_children) AS n_children, max(n_desc) AS n_desc, max(mtime) AS mtime,
        sum(wt)::DOUBLE AS wts, sum(mw)::BIGINT AS wb, max(last_read) AS last_read,
        sum(c2)::BIGINT AS c2, sum(c3)::BIGINT AS c3, sum(c4)::BIGINT AS c4
        FROM raw GROUP BY depth, path"""
    return f"""WITH raw AS ({raw}), one AS ({one}), sl AS ({us_sql_from_raw()})
        SELECT one.depth, one.path, kind, size, n_files, n_children, n_desc, mtime,
            CASE WHEN wb > 0 THEN round(wts / wb, 0) ELSE 0 END::DOUBLE AS dr, wb, c2, c3, c4, sl.us, wts, last_read
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
    version = version or sn.source_version(con, src)
    if version == 2:
        cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet({q(src)})").fetchall()}
        sel = V2_ROW.format(**{k: (c if c in cols else "NULL") for k, c in sn.V2_OPTIONAL.items()})
    else:
        sel = V1_ROW
    raw = " UNION ALL ".join(f"SELECT {sel} FROM read_parquet({q(src)}) WHERE {p}" for p in preds)
    return f"""WITH raw AS ({raw})
        SELECT depth, path, usr, CASE WHEN bool_and(is_file) THEN 'file' ELSE 'dir' END AS kind, sum(size)::BIGINT AS size,
            sum(n_files)::BIGINT AS n_files, max(n_children) AS n_children, max(n_desc) AS n_desc, max(mtime) AS mtime,
            CASE WHEN sum(mw) > 0 THEN round(sum(wt) / sum(mw), 0) ELSE 0 END::DOUBLE AS dr, sum(mw)::BIGINT AS wb,
            sum(c2)::BIGINT AS c2, sum(c3)::BIGINT AS c3, sum(c4)::BIGINT AS c4, sum(wt)::DOUBLE AS wts, max(last_read) AS last_read
        FROM raw GROUP BY depth, path, usr"""


#: Per-version hash of the change columns: what a version and a scan's row are compared by.
ROW_HASH = "hash(depth, path, kind, size, n_files, n_children, n_desc, mtime, dr, wb, c2, c3, c4, us)"
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
    those of the versions live at it — equal iff the versions reconstruct every scan exactly."""
    from pyrmts.intervals import islands_sql, stamped_sql

    t0 = monotonic()
    r = ranges["ranges"][i]
    preds = range_preds(r)
    srcs = [(sn._src(scans["bucket"], s["src"], mount), s["ts"], s.get("version")) for s in scans["scans"]]
    stamps = [ts for _, ts, _ in srcs]
    for t in ("lng", "pv", "rd"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    # One scan at a time: a single 71-way `UNION ALL` runs every scan's per-path aggregation at once and
    # ran out of memory on the widest ranges (`failed to pin block`, 59.6 GiB of 60).
    for j, (p, _, v) in enumerate(srcs):
        sql = f"SELECT {j}::BIGINT AS __scan, * FROM ({path_rows_sql(con, p, preds, v)})"
        con.execute(f"CREATE TABLE lng AS {sql}" if j == 0 else f"INSERT INTO lng {sql}")
    t_long = monotonic() - t0
    src_dig = {j: [n, int(s) % U64] for j, n, s in con.execute(f"SELECT __scan, count(*), sum({ROW_HASH}::HUGEINT) FROM lng GROUP BY __scan").fetchall()}
    rd_src = {j: [n, int(s) % U64] for j, n, s in con.execute(f"SELECT __scan, count(*), sum({RD_HASH}::HUGEINT) FROM lng WHERE last_read >= 0 GROUP BY __scan").fetchall()}
    runs = islands_sql("SELECT * FROM lng", KEY_COLS, STATE_COLS, carried=CARRIED)
    con.execute(f"CREATE TABLE pv AS {stamped_sql(runs, KEY_COLS, STATE_COLS, stamps, OPEN)}")
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
    cols = ", ".join(PV_COLS)
    n_pv = write_sorted(sn._batches(con, f"SELECT {cols} FROM pv ORDER BY depth, path, vf"), out / "pv" / f"{name}.parquet",
                        PV_SCHEMA, RANGE_RG, dictionary=["kind", "us"])
    n_rd = write_sorted(sn._batches(con, "SELECT depth, path, vf, vt, last_read FROM rd ORDER BY depth, path, vf"), out / "rd" / f"{name}.parquet",
                        RD_SCHEMA, RANGE_RG)
    kinds = {k: {"versions": n, "open": o, "paths": p} for k, n, o, p in con.execute(
        f"SELECT kind, count(*), count(*) FILTER (WHERE vt = {OPEN}), count(DISTINCT path) FROM pv GROUP BY kind ORDER BY kind").fetchall()}
    doc = {
        "range": r, "i": i, "versions": n_pv, "reads": n_rd,
        "open": con.execute(f"SELECT count(*) FROM pv WHERE vt = {OPEN}").fetchone()[0],
        "reads_open": con.execute(f"SELECT count(*) FROM rd WHERE vt = {OPEN}").fetchone()[0],
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


# ── Read days folded into the path versions ──────────────────────────────


def fold_sql(pv: str, rd: str) -> str:
    """`pv`'s versions split where `rd` (the read-day versions of the same paths) changes, each piece
    carrying its `last_read` (-1 where none is live), adjacent pieces with the same path version and
    read day merged: `PVL_SCHEMA` rows, unsorted. Equal to versioning the scans with `last_read` as one
    more change column (`test_fold_equals_versioning_with_read_days`). Paths without read versions pass
    through untouched."""
    cols = ", ".join(f"p.{c}" for c in PV_COLS if c not in ("vf", "vt"))
    keep = ", ".join(c for c in PV_COLS if c not in ("depth", "path", "vf", "vt"))
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
        SELECT depth, path, min(lo) AS vf, max(hi) AS vt, {", ".join(f"any_value({c}) AS {c}" for c in PV_COLS if c not in ("depth", "path", "vf", "vt"))}, any_value(last_read) AS last_read
        FROM g GROUP BY depth, path, pvf, run
        UNION ALL
        SELECT p.*, -1 AS last_read FROM {pv} p ANTI JOIN rp USING (depth, path)"""


def fold_range(root: str, i: int, out: Path, con) -> dict:
    """Range `i`'s `pvl/r####.parquet` from its `pv/` and `rd/` (sorted `(depth, path, vf)`)."""
    t0 = monotonic()
    name = f"r{i:04d}"
    pv, rd = f"read_parquet({q(f'{root}/pv/{name}.parquet')})", f"read_parquet({q(f'{root}/rd/{name}.parquet')})"
    con.execute("DROP TABLE IF EXISTS pvl")
    con.execute(f"CREATE TABLE pvl AS {fold_sql(pv, rd)}")
    n = write_sorted(sn._batches(con, f"SELECT {', '.join(PVL_SCHEMA.names)} FROM pvl ORDER BY depth, path, vf"), out / "pvl" / f"{name}.parquet",
                     PVL_SCHEMA, RANGE_RG, dictionary=["kind", "us"])
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
}
#: Each range-file dir's schema.
SUB_SCHEMA = {"pv": PV_SCHEMA, "pvl": PVL_SCHEMA, "rd": RD_SCHEMA, "sv": SV_SCHEMA, "svt": SVT_SCHEMA}
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

    d, p, vf, vt = mm("depth"), mm("path"), mm("vf"), mm("vt")
    b = mm(size_col) if size_col else (0, 0)
    # A slice sort's owner range (NULL owners ignored, as the per-scan footers' `u_min`/`u_max`).
    u = mm("usr") if "usr" in t.column_names else (None, None)
    return {"seg": seg, "d_min": d[0], "d_max": d[1], "p_min": p[0], "p_max": p[1], "b_min": b[0], "b_max": b[1],
            "u_min": u[0], "u_max": u[1], "vf_min": vf[0], "vf_max": vf[1], "vt_min": vt[0], "vt_max": vt[1], "rows": t.num_rows}


def write_served(con, src: str, sort: str, out: Path, schema: pa.Schema, *, rg_rows: int = SERVED_RG,
                 stamps: list[int] | None = None) -> dict:
    """One served sort of `src` (a relation of `schema` rows) to `out`: its segments in order (`seg_sql`:
    the open versions, then the closed ones — one segment, or with `stamps` one per dyadic block of
    scans), each in the sort's order and cut in `rg_rows`-row groups (a segment's last may be short, so
    no group mixes segments), zstd. Writes `<out stem>.groups.parquet` beside it (`GROUPS_SCHEMA`: per
    group the exact bounds of its rows — never truncated statistics — and the compact metadata the
    Worker revives)."""
    import pyarrow.compute as pc

    _, order, size_col = SORTS[sort]
    cols = ", ".join(schema.names)
    bounds: list[dict] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    dictionary = [c for c in ("kind", "us", "usr") if c in schema.names]
    seg_rows: dict[int, int] = {}
    with pq.ParquetWriter(out, schema, compression=sn.CODEC, use_dictionary=dictionary, write_statistics=["depth", "vf", "vt"]) as w:
        pending: list[pa.Table] = []
        n = 0
        cur: int | None = None

        def flush(final: bool) -> None:
            nonlocal pending, n
            if not pending:
                return
            t = pa.concat_tables(pending).combine_chunks()
            off = 0
            while t.num_rows - off >= rg_rows or (final and off < t.num_rows):
                g = t.slice(off, min(rg_rows, t.num_rows - off))
                w.write_table(g, row_group_size=rg_rows)
                bounds.append(_bounds(g, cur, size_col))
                off += g.num_rows
            rest = t.slice(off)
            pending, n = ([rest] if rest.num_rows else []), rest.num_rows

        sql = f"SELECT {cols}, {seg_sql(stamps)} AS __seg FROM {src} ORDER BY __seg, {order}"
        for b in sn._batches(con, sql):
            if not b.num_rows:
                continue
            segs = b.column("__seg")
            if segs.null_count:
                raise ValueError(f"{out.name}: a closed version's vf/vt isn't one of the scans' stamps")
            t = pa.Table.from_batches([b]).drop_columns(["__seg"])
            t = t.cast(schema) if t.schema != schema else t
            # The batch's runs of one segment (it's sorted by segment first).
            vals = segs.to_pylist()
            cuts = [0] + [x for x in range(1, len(vals)) if vals[x] != vals[x - 1]] + [len(vals)]
            for a, z in zip(cuts, cuts[1:]):
                s = vals[a]
                if s != cur:
                    flush(True)
                    cur = s
                pending.append(t.slice(a, z - a))
                n += z - a
                seg_rows[s] = seg_rows.get(s, 0) + z - a
                if n >= rg_rows:
                    flush(False)
        flush(True)
        err(f"  {sort}: {sum(seg_rows.values()):,} rows in {len(seg_rows)} segments (open {seg_rows.get(0, 0):,})")
        w.add_key_value_metadata({"store": "interval", "version": str(STORE_VERSION), "sort": sort, "order": order,
                                  "segments": "open,dyadic" if stamps else "open,hist", "open": str(OPEN)})
    md = pq.read_metadata(out)
    rows, start = [], 0
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        chunks = [rg.column(c) for c in range(rg.num_columns)]
        codecs = {cc.compression for cc in chunks}
        cmeta = [[cc.data_page_offset, cc.total_compressed_size, cc.dictionary_page_offset or 0] for cc in chunks]
        bd = bounds[g]
        if bd["rows"] != rg.num_rows:
            raise RuntimeError(f"{out}: group {g} holds {rg.num_rows} rows, the writer saw {bd['rows']}")
        rows.append({"rg": g, **{k: v for k, v in bd.items() if k != "rows"}, "row_start": start, "row_end": start + rg.num_rows,
                     "rg_json": json.dumps([rg.num_rows, codecs.pop(), cmeta], separators=(",", ":"))})
        start += rg.num_rows
    from disk_tree.find.groups import schema_json

    sj = schema_json(md)
    t = pa.table({c: [r[c] for r in rows] for c in GROUPS_SCHEMA.names}, schema=GROUPS_SCHEMA)
    segs = _seg_summary(rows, bool(stamps))
    kv = {"groups_v": "1", "version": str(STORE_VERSION), "schema": json.dumps(sj["schema"], separators=(",", ":")), "sort": sort,
          "rows": str(start), "segments": json.dumps({k: v["groups"] for k, v in segs.items()})}
    gp = out.with_name(out.name.removesuffix(".parquet") + ".groups.parquet")
    with pq.ParquetWriter(gp, GROUPS_SCHEMA, compression="zstd", write_statistics=GROUPS_STAT_COLS, store_schema=False) as w:
        w.write_table(t, row_group_size=512)
        w.add_key_value_metadata(kv)
    return {"sort": sort, "rows": start, "groups": len(rows), "bytes": out.stat().st_size, "groups_bytes": gp.stat().st_size, "segments": segs}


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


@cli.command("build")
@option("-b", "--bucket", help="Data bucket (default: the profile's)")
@option("-f", "--force", is_flag=True, help="Rebuild ranges whose digest is already uploaded")
@option("-g", "--gen", required=True, help="Generation: gs://BUCKET/interval-store/GEN/ (its scans.json, ranges.json)")
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
@option("-P", "--profile", help="Deployment profile (`interval_profiles/<name>.json` or a path; default $INTERVAL_STORE_PROFILE)")
def build_cmd(bucket, profile, force, gen, index, mount, mem, per_task, out, threads, only, slices, tmp, no_upload) -> None:
    """Build key ranges' path and read versions over every scan of GEN's `scans.json`, verifying each
    scan's reconstruction (digest written last: it marks the range done)."""
    bucket = _bucket(bucket, profile)
    from google.cloud import storage

    prefix = f"{PREFIX}/{gen}"
    scans = read_json(f"gs://{bucket}/{prefix}/scans.json")
    ranges = read_json(f"gs://{bucket}/{prefix}/ranges.json")
    if only:
        todo = [int(x) for x in only.split(",")]
        # As a Batch job of several tasks, task t takes every count-th of the listed ranges.
        if os.environ.get("BATCH_TASK_COUNT"):
            todo = todo[sn._task(index)::int(os.environ["BATCH_TASK_COUNT"])]
    else:
        t = sn._task(index)
        todo = list(range(t * per_task, min((t + 1) * per_task, ranges["k"])))
    b = storage.Client().bucket(bucket)
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
    for i in todo:
        if not force and b.blob(f"{prefix}/{dig}/r{i:04d}.json").exists():
            err(f"range {i}: already built")
            continue
        outp = Path(out) / f"r{i}"
        doc = (build_slices_range if slices else build_range)(scans, ranges, i, outp, con, mount=mount)
        if not no_upload:
            # The digest marks the range done: uploaded last.
            digest = outp / dig
            moved = Path(out) / f"r{i}-digest"
            shutil.move(str(digest), moved)
            upload_tree(outp, bucket, prefix)
            upload_tree(moved, bucket, f"{prefix}/{dig}")
            shutil.rmtree(outp)
            shutil.rmtree(moved)
        print(json.dumps({k: v for k, v in doc.items() if k != "scans"}), flush=True)


def churn_stats(con, root: str, scans: dict) -> dict:
    """Per scan: path versions opened and closed there, read versions likewise, and live rows — the
    delta a per-scan append writes — plus totals by kind."""
    ids = {s["ts"]: s["id"] for s in scans["scans"]}
    pv, rd = f"read_parquet({q(root + '/pv/r*.parquet')})", f"read_parquet({q(root + '/rd/r*.parquet')})"
    per = {i: {"id": i, "opened": 0, "closed": 0, "reads_opened": 0, "reads_closed": 0} for i in ids.values()}
    for col, key, src in (("vf", "opened", pv), ("vt", "closed", pv), ("vf", "reads_opened", rd), ("vt", "reads_closed", rd)):
        for t, n in con.execute(f"SELECT {col}, count(*) FROM {src} WHERE {col} <> {OPEN} GROUP BY {col}").fetchall():
            per[ids[t]][key] = n
    kinds = {k: {"versions": n, "open": o, "paths": p} for k, n, o, p in con.execute(
        f"SELECT kind, count(*), count(*) FILTER (WHERE vt = {OPEN}), count(DISTINCT path) FROM {pv} GROUP BY kind ORDER BY kind").fetchall()}
    tot = con.execute(f"SELECT count(*), count(*) FILTER (WHERE vt = {OPEN}), count(DISTINCT (depth, path)) FROM {pv}").fetchone()
    rtot = con.execute(f"SELECT count(*), count(*) FILTER (WHERE vt = {OPEN}) FROM {rd}").fetchone()
    return {"versions": tot[0], "open": tot[1], "paths": tot[2], "reads": rtot[0], "reads_open": rtot[1], "kinds": kinds,
            "scans": [per[s["id"]] for s in scans["scans"]]}


@cli.command("fold")
@option("-b", "--bucket", help="Data bucket (default: the profile's)")
@option("-g", "--gen", required=True, help="Generation (its pv/, rd/ range files)")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", help="Local mount of the data bucket")
@option("-M", "--mem", default="64GB", help="DuckDB memory limit")
@option("-n", "--per-task", default=1, type=IntRange(min=1), help="Ranges per task: task t folds [t·n, (t+1)·n)")
@option("-o", "--out", default="/stage/out", help="Local output dir (uploaded, then removed)")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-P", "--profile", help="Deployment profile (`interval_profiles/<name>.json` or a path; default $INTERVAL_STORE_PROFILE)")
@option("-S", "--slice-totals", is_flag=True, help="Instead: each range's slice versions with their path's total (`svt/`, `slice_totals_range`)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
def fold_cmd(bucket, gen, index, mount, mem, per_task, out, threads, profile, slice_totals, tmp, no_upload) -> None:
    """Fold each range's read days into its path versions: `pvl/r####.parquet` (what `cut` serves); with
    `-S`, its slice versions with their path's total: `svt/r####.parquet`."""
    bucket = _bucket(bucket, profile)
    prefix = f"{PREFIX}/{gen}"
    ranges = read_json(f"gs://{bucket}/{prefix}/ranges.json")
    t = sn._task(index)
    con = connect(threads, mem, tmp)
    root = f"{mount}/{prefix}" if mount else f"gs://{bucket}/{prefix}"
    for i in range(t * per_task, min((t + 1) * per_task, ranges["k"])):
        sub = "svt" if slice_totals else "pvl"
        doc = (slice_totals_range if slice_totals else fold_range)(root, i, Path(out), con)
        if not no_upload:
            upload_tree(Path(out) / sub, bucket, f"{prefix}/{sub}")
            shutil.rmtree(Path(out) / sub)
        print(json.dumps(doc), flush=True)


def _refuse_overwrite(bucket: str, keys: list[str]) -> None:
    """Published served files are immutable: a cut into a generation that already has them is refused
    (cut into a new one, `--to-gen`)."""
    from google.cloud import storage

    b = storage.Client().bucket(bucket)
    there = [k for k in keys if b.blob(k).exists()]
    if there:
        raise SystemExit(f"refusing to overwrite published files: {', '.join(there)} (cut into a new generation: -O)")


@cli.command("cut")
@option("-b", "--bucket", help="Data bucket (default: the profile's)")
@option("-D", "--no-dyadic", is_flag=True, help="One closed segment, not one per dyadic block of scans (`seg_sql`)")
@option("-g", "--gen", required=True, help="Generation (its pv/, rd/ range files)")
@option("-i", "--index", type=int, help="Task index → sort (path, bysize, reads, then `stats`: per-scan churn; default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", help="Local mount of the data bucket")
@option("-M", "--mem", default="80GB", help="DuckDB memory limit")
@option("-o", "--out", default="/stage/out", help="Local output dir (uploaded, then removed)")
@option("-O", "--to-gen", help="Write the served files (and a copy of scans.json) under this generation instead of GEN")
@option("-p", "--threads", default=16, type=int, help="DuckDB threads")
@option("-r", "--rg-rows", default=SERVED_RG, type=int, help="Rows per served row group")
@option("-s", "--sort", "sorts", multiple=True, type=Choice(list(SORTS)), help="Sort(s) to cut (default: the task's)")
@option("-T", "--tmp", default="/stage/tmp", help="DuckDB spill dir")
@option("-U", "--no-upload", is_flag=True, help="Keep the outputs local")
@option("-P", "--profile", help="Deployment profile (`interval_profiles/<name>.json` or a path; default $INTERVAL_STORE_PROFILE)")
def cut_cmd(bucket, no_dyadic, profile, gen, index, mount, mem, out, to_gen, threads, rg_rows, sorts, tmp, no_upload) -> None:
    """Cut the served sorts from the range files: `served/<sort>.parquet` + `.groups.parquet`."""
    bucket = _bucket(bucket, profile)
    prefix = f"{PREFIX}/{gen}"
    dst_prefix = f"{PREFIX}/{to_gen or gen}"
    todo = list(sorts) or [[*SORTS, "stats"][sn._task(index)]]
    con = connect(threads, mem, tmp)
    root = f"{mount}/{prefix}" if mount else f"gs://{bucket}/{prefix}"
    scans = read_json(f"gs://{bucket}/{prefix}/scans.json")
    out_dir = Path(out) / "served"
    if todo == ["stats"]:
        doc = churn_stats(con, root, scans)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "stats.json").write_text(json.dumps(doc, indent=1) + "\n")
        if not no_upload:
            _refuse_overwrite(bucket, [f"{dst_prefix}/served/stats.json"])
            upload_tree(out_dir, bucket, f"{dst_prefix}/served")
        print(json.dumps({k: v for k, v in doc.items() if k != "scans"}), flush=True)
        return
    stamps = None if no_dyadic else [s["ts"] for s in scans["scans"]]
    for sort in todo:
        sub, _, _ = SORTS[sort]
        schema = SUB_SCHEMA[sub]
        if not no_upload:
            _refuse_overwrite(bucket, [f"{dst_prefix}/served/{sort}{x}" for x in (".parquet", ".groups.parquet", ".json")])
        t0 = monotonic()
        dst = out_dir / f"{sort}.parquet"
        doc = write_served(con, f"read_parquet({q(root + '/' + sub + '/r*.parquet')})", sort, dst, schema, rg_rows=rg_rows, stamps=stamps)
        doc["s"] = round(monotonic() - t0, 1)
        err(f"cut {sort}: {doc['rows']:,} rows, {doc['groups']:,} groups, {doc['bytes']:,} B in {doc['s']}s")
        (out_dir / f"{sort}.json").write_text(json.dumps(doc, sort_keys=True) + "\n")
        if not no_upload:
            upload_tree(out_dir, bucket, f"{dst_prefix}/served")
            shutil.rmtree(out_dir)
        print(json.dumps(doc), flush=True)
    if to_gen and not no_upload:
        # The new generation's scans (the reader's `scans.json`), once.
        from google.cloud import storage

        blob = storage.Client().bucket(bucket).blob(f"{dst_prefix}/scans.json")
        if not blob.exists():
            blob.upload_from_string(json.dumps(scans, indent=1) + "\n", content_type="application/json")


def download_served(bucket: str, prefix: str, dst: Path, *, workers: int = 16) -> None:
    """The generation's served sorts (+ `.groups.parquet`) to `dst`, chunked in parallel."""
    from google.cloud import storage
    from google.cloud.storage import transfer_manager as tm

    b = storage.Client().bucket(bucket)
    dst.mkdir(parents=True, exist_ok=True)
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


@cli.command("verify")
@option("-b", "--bucket", help="Data bucket (default: the profile's)")
@option("-g", "--gen", required=True, help="Generation")
@option("-i", "--index", type=int, help="Task index (default: $BATCH_TASK_INDEX)")
@option("-m", "--mount", help="Local mount of the data bucket (per-scan sorts are copied from it)")
@option("-n", "--tasks", default=1, type=IntRange(min=1), help="Tasks the sampled dates are split over")
@option("-o", "--out", default="/stage/out", help="Local output dir")
@option("-s", "--scans", "scan_ids", help="Comma-separated scan ids to sample (default: the profile's `verify_scans`)")
@option("-T", "--tmp", default="/stage/tmp", help="Local scratch (served store, per-scan copies, spill)")
@option("-P", "--profile", help="Deployment profile (`interval_profiles/<name>.json` or a path; default $INTERVAL_STORE_PROFILE)")
def verify_cmd(bucket, profile, gen, index, mount, tasks, out, scan_ids, tmp) -> None:
    """Parity against the per-scan path store over the profile's `verify_scans`: every
    view's tiles and every diff's rows, with both sides' read cost; `verify/t##.jsonl` + `.json`."""
    bucket = _bucket(bucket, profile)
    from . import interval_verify as iv

    prefix = f"{PREFIX}/{gen}"
    t = sn._task(index)
    tmp = Path(tmp)
    download_served(bucket, prefix, tmp / "served")
    scans = read_json(f"gs://{bucket}/{prefix}/scans.json")
    outp = Path(out) / "verify"
    outp.mkdir(parents=True, exist_ok=True)
    sampled = scan_ids.split(",") if scan_ids else load_profile(profile)["verify_scans"]
    tag = f"t{t:02d}" if not scan_ids else f"t{t:02d}-{sampled[0]}"
    summaries = iv.verify_task(sampled, t, tasks, str(tmp / "served"), scans, outp / f"{tag}.jsonl", tmp, mount)
    (outp / f"{tag}.json").write_text(json.dumps(summaries, indent=1) + "\n")
    upload_tree(outp, bucket, f"{prefix}/verify")
    print(json.dumps(summaries), flush=True)


#: What the Worker reads: the scans and the served sorts with their group indexes.
R2_SERVED = ("scans.json", "served/path.", "served/bysize.", "served/reads.", "served/slices.", "served/slices-bysize-user.", "served/slices-bytotal.")


@cli.command("r2-copy")
@option("-b", "--bucket", help="Data bucket (default: the profile's)")
@option("-g", "--gen", required=True, help="Generation")
@option("-m", "--mount", help="Ignored (the Batch driver passes it)")
@option("-n", "--dry-run", is_flag=True, help="List what would be copied")
@option("-w", "--workers", default=8, type=int, help="Parallel copies")
@option("-P", "--profile", help="Deployment profile (`interval_profiles/<name>.json` or a path; default $INTERVAL_STORE_PROFILE)")
def r2_copy_cmd(bucket, profile, gen, mount, dry_run, workers) -> None:
    """Copy the generation's served files GCS → R2 under the same keys (`static-names r2-copy`'s streaming
    copy: objects already there with the same size and md5 are skipped). R2 via `R2_ENDPOINT`,
    `R2_BUCKET` and AWS_* keys."""
    from .append_runner import r2_copy, r2_objects

    bucket = _bucket(bucket, profile)
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
