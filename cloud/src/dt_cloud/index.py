"""The CoreWeave scan's served index (`dt-cloud index-write`): the path store's
two sorts, cut from the union of the scan's layer-2 parquets (specs/path-store.md
§4.2; `disk-tree import` writes one layer-2 per bucket).

The store is **one row per path, object or directory** (§1). This module
unions each bucket's layer-2 — every row, the bucket prefixed onto `path`
(`marin-us-east-02a/marin/…`, `depth + 1`, so depth 1 is the bucket and the
site's "one bucket" root opens inside it), the layer-2's own column names
(§1.1: `path, [usr,] size, depth, kind, n_files, n_children, n_desc, mtime,
mtime_mean, created, last_read, sum_storage_class_id_*`) — into one layer-2
shaped parquet (`.store.parquet`, deleted at the end), and hands it to the
engine's `write_tiers`, which cuts:

- **`path`** (`path-index.parquet`): every row sorted `(depth, path)`;
- **`bysize`** (`path-index-bysize.parquet`): the same rows sorted
  `(⌊log2 size⌋ desc, path)` — every byte-floor tier is a prefix of this file,
  which is why the coarse tiers this module used to write are gone;

8k-row groups, `tier` / `sort` in the parquet metadata, a `.groups.json`
footer sidecar beside each (`b_min`/`b_max` per group). `index-sync` publishes
them as variants `path` / `bysize`; `index_schema.version` 2 tells a reader
the generation has objects (§5 phase 1).

The columns are the layer-2's (spec §1.1): the site reader decodes a
version-2 generation by those names (phase 2) and a version-1 one by its own
short names — the version tells them apart, so the store carries no aliases.

The age pyramid reads the same union (`kind = 'file'` rows; §4.5), unchanged
in its output. No `by-user` sorts here: CoreWeave has no ownership signal
(`sort_variants` is the seam gcs's `path-index` uses).
"""
from __future__ import annotations

import math
import os
import sys
from functools import partial
from pathlib import Path

import duckdb

from .index_footer import STORE_SORTS, USER_SORTS

err = partial(print, file=sys.stderr)

ROW_GROUP_SIZE = 8192
#: The union's own file: the engine's layer-2 shape (64K-row groups), read once
#: by `write_tiers` and the age pyramid, then removed.
STORE_L2 = ".store.parquet"
STORE_L2_ROWS = 65536
#: Layer-2 columns every bucket source must carry (the engine's `_LAYER2_COLS`
#: plus `kind` / `depth`).
L2_REQUIRED = ("path", "size", "depth", "kind", "n_files", "n_children", "n_desc", "mtime")
#: Always in the store, NULL where a source lacks them (`mtime_mean` is
#: `import -m`; `created` / `last_read` are gcs's).
L2_OPTIONAL: dict[str, str] = {"mtime_mean": "DOUBLE", "created": "BIGINT", "last_read": "INTEGER"}
#: The label column (`import --label`), right after `path` when any source has it.
LABEL_COL = "usr"
PIVOT_PREFIX = "sum_storage_class_id_"

# The per-path created-day strata behind a path-aware `AgeChart` (specs/age-index.md).
# A distinct index (not a path-index tier): rows `(path, depth, day, b, o)` sorted
# `(depth, path, day)`, floored, served by prefix as a point lookup. Registered as
# variant `age` in `index_footer.INDEX_VARIANTS` too (what `index-sync` publishes).
AGE_INDEX = "age-index.parquet"
# The age index floors at F_24 of the fleet (the finest of the retired coarse
# tiers): every prefix the treemap can drill to as a page is covered; below it
# `/api/age` falls back to the nearest ancestor.
AGE_FLOOR_EXP = 24


def duckdb_codec() -> str:
    """The `COPY` codec clause for the served index parquet: zstd unless
    `$DISK_TREE_PARQUET_CODEC=snappy` (spec `listing-slim.md`). The engine import
    is lazy: the CLI must load without `disk_tree` (`test_cli_import.py`)."""
    from disk_tree.listing_format import duckdb_codec as codec
    return codec()


def coarse_floor(fleet: int, e: int) -> int:
    """F_E = 2^(round(log2 fleet) − E); 1 for an empty fleet. The age pyramid's
    per-path floor (`AGE_FLOOR_EXP`); the retired coarse tiers used the same."""
    return 2 ** (round(math.log2(fleet)) - e) if fleet > 0 else 1


def _q(s: str) -> str:
    return s.replace("'", "''")


def _l2_shape(con: "duckdb.DuckDBPyConnection", l2: str) -> tuple[list[str], dict[str, str]]:
    """A layer-2's columns and its implied pivots (`{dropped column: the column
    it equals}`, a v2 listing's `sum_*` that equalled `size` on every row)."""
    from disk_tree.listing_format import format_of

    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{_q(l2)}')").fetchall()]
    missing = [c for c in L2_REQUIRED if c not in cols]
    if missing:
        raise ValueError(f"{l2}: not a layer-2 parquet — missing {missing} (columns {cols})")
    return cols, dict(format_of(l2).implied)


def store_columns(shapes: list[tuple[list[str], dict[str, str]]]) -> list[str]:
    """The store's column order for a scan whose sources have `shapes`: the
    layer-2's, label first (so `write_tiers` reads `usr` as the label block),
    then the pivots present in any source (implied ones included)."""
    pivots = sorted(
        {c for cols, implied in shapes for c in (*cols, *implied) if c.startswith(PIVOT_PREFIX)},
        key=lambda c: int(c[len(PIVOT_PREFIX):]),
    )
    label = [LABEL_COL] if any(LABEL_COL in cols for cols, _ in shapes) else []
    return ["path", *label, "size", "depth", "kind", "n_files", "n_children", "n_desc", "mtime", *L2_OPTIONAL, *pivots]


def store_rows_sql(l2: str, bucket: str, shape: tuple[list[str], dict[str, str]], columns: list[str]) -> str:
    """One bucket's rows of the store, as a SELECT over its layer-2 (`l2` a
    DuckDB `getvariable(...)` or a literal path expression): every row, the
    bucket prefixed onto `path` (the `.` root becomes the bucket row),
    `depth + 1`, projected to `columns` (NULL / 0 for what this source lacks)."""
    cols, implied = shape
    b = _q(bucket)

    def pivot(c: str) -> str:
        if c in cols:
            return f"{c}::BIGINT"
        if c in implied:
            return f"{implied[c]}::BIGINT"
        return "0::BIGINT"

    exprs: dict[str, str] = {
        "path": f"CASE WHEN path = '.' THEN '{b}' ELSE '{b}/' || path END",
        LABEL_COL: f"{LABEL_COL}::VARCHAR" if LABEL_COL in cols else "NULL::VARCHAR",
        "size": "size::BIGINT",
        "depth": "(depth + 1)::INTEGER",
        "kind": "kind::VARCHAR",
        "n_files": "n_files::BIGINT",
        "n_children": "n_children::BIGINT",
        "n_desc": "n_desc::BIGINT",
        "mtime": "mtime::BIGINT",
        **{c: (f"{c}::{t}" if c in cols else f"NULL::{t}") for c, t in L2_OPTIONAL.items()},
    }
    sel = ",\n          ".join(f"{exprs[c] if c in exprs else pivot(c)} AS {c}" for c in columns)
    return f"""
        SELECT
          {sel}
        FROM read_parquet({l2})
    """


def write_store(
    con: "duckdb.DuckDBPyConnection",
    sources: list[tuple[str, str]],
    out_dir: str | Path,
) -> tuple[str, list[str]]:
    """Write the store's table — the union of ``sources``' rows, one
    ``(bucket, layer-2 parquet)`` per bucket of the scan — as
    ``<out_dir>/.store.parquet`` in the engine's layer-2 shape, streamed
    (never materialized as a table). Returns ``(path, columns)``."""
    if not sources:
        raise ValueError("write_store: no (bucket, layer-2) sources")
    buckets = [b for b, _ in sources]
    if len(set(buckets)) != len(buckets):
        raise ValueError(f"write_store: duplicate bucket in {buckets}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    shapes = [_l2_shape(con, str(l2)) for _, l2 in sources]
    columns = store_columns(shapes)
    selects = []
    for i, ((bucket, l2), shape) in enumerate(zip(sources, shapes)):
        con.execute(f"SET VARIABLE STORE_L2_{i} = ?", [str(l2)])
        selects.append(store_rows_sql(f"getvariable('STORE_L2_{i}')", bucket, shape, columns))
    store = out / STORE_L2
    con.execute(
        f"COPY ({' UNION ALL '.join(f'({s})' for s in selects)}) TO '{_q(str(store))}' "
        f"(FORMAT parquet, {duckdb_codec()}, ROW_GROUP_SIZE {STORE_L2_ROWS})"
    )
    return str(store), columns


def variant_file(tier: str, variant: tuple[str, ...]) -> str:
    """The served file name of a cut sort: `path-index[-bysize][-by-user].parquet`."""
    if not variant:
        return STORE_SORTS[tier]
    if variant == (LABEL_COL,):
        return USER_SORTS[f"{tier}-user" if tier == "bysize" else "user"]
    raise ValueError(f"no served name for sort variant {variant} of tier {tier!r}")


def variant_name(tier: str, variant: tuple[str, ...]) -> str:
    """The D1 variant of a cut sort: `path` / `bysize` / `user` / `bysize-user`."""
    return next(v for v, f in {**STORE_SORTS, **USER_SORTS}.items() if f == variant_file(tier, variant))


def write_sorts(
    con: "duckdb.DuckDBPyConnection",
    store: str,
    out_dir: str | Path,
    *,
    sort_variants: tuple[tuple[str, ...], ...] = (),
    groups: bool = True,
    row_group_rows: int = ROW_GROUP_SIZE,
) -> dict[str, dict]:
    """Cut the store's two sorts (+ ``sort_variants`` of each) from the union
    at ``store`` into ``out_dir`` under their served names, with the
    `.groups.json` footer sidecars. Returns ``{variant: {file, rows, groups}}``
    in write order.

    ``row_group_rows``: the HTTP range-read unit — and the D1 footer's row
    count per sort. A fleet the size of gcs (777M rows) needs 32K to keep
    two sorts × 120 scans under D1's 10 GB (specs/path-store.md §1.6)."""
    import pyarrow.parquet as pq
    from disk_tree.find.groups import groups_path
    from disk_tree.find.tiers import TIERS, tier_path, write_tiers

    out = Path(out_dir)
    stem = str(out / "path-index")
    written = write_tiers(
        store, stem, tiers=TIERS, row_group_rows=row_group_rows,
        sort_variants=sort_variants, con=con, groups=groups,
    )
    result: dict[str, dict] = {}
    for tier in TIERS:
        for variant in ((), *sort_variants):
            src = tier_path(stem, tier, variant)
            dst = str(out / variant_file(tier, variant))
            os.replace(src, dst)
            if groups:
                os.replace(groups_path(src), groups_path(dst))
            name = variant_name(tier, variant)
            result[name] = {"file": dst, "rows": written[src], "groups": pq.read_metadata(dst).num_row_groups}
            err(f"{name}: {written[src]:,} rows, {result[name]['groups']:,} row groups → {dst}")
    return result


def _age_explode_sql(src: str, bin_sql: str, bin_col: str) -> str:
    """Per-`(prefix, time-bin)` bytes/objects from the store's **file** rows,
    descendant-inclusive: each object's bin-bucketed size/count rolled up to
    every ancestor prefix, the fleet root `''` (depth 0) included. Two-stage —
    aggregate objects to `(parent-dir, bin)` first, then explode that to
    ancestors — so the intermediate is distinct-dirs × bins, not objects ×
    depth. ``bin_sql`` is the bin expression over `mtime` (epoch seconds),
    emitted as ``bin_col``."""
    return f"""
        WITH files AS (
          SELECT
            regexp_replace(path, '/[^/]*$', '') AS pdir,
            {bin_sql} AS {bin_col},
            size::BIGINT AS b
          FROM {src}
          WHERE kind = 'file' AND mtime > 0
        ),
        leaf AS (
          SELECT pdir, {bin_col}, sum(b) AS b, count(*) AS o FROM files GROUP BY pdir, {bin_col}
        ),
        comps AS (
          SELECT {bin_col}, b, o, string_split(pdir, '/') AS parts FROM leaf
        ),
        expl AS (
          SELECT {bin_col}, b, o, parts, unnest(range(0, len(parts) + 1)) AS i FROM comps
        )
        SELECT
          CASE WHEN i = 0 THEN '' ELSE array_to_string(parts[1:i], '/') END AS path,
          i::INTEGER AS depth,
          {bin_col}::BIGINT AS {bin_col},
          b::BIGINT AS b,
          o::BIGINT AS o
        FROM expl
    """


def write_age_index(
    con: "duckdb.DuckDBPyConnection",
    store: str,
    out_dir: Path,
) -> dict:
    """Write `age-index.parquet` under ``out_dir`` from the store's file rows at
    ``store`` (the union `write_store` leaves), on the caller's ``con``. Rows
    are the `(prefix, created-day)` strata, grouped, floored to prefixes
    clearing `AGE_FLOOR_EXP`'s byte floor, sorted `(depth, path, day)` — the
    prefix-range + row-group-prune contract of the path index, so
    `/api/age?path=P` is a point lookup on P's own day rows. Returns a summary
    (rows, floor, kept paths, file)."""
    out = Path(out_dir)
    con.execute(
        "CREATE TEMP TABLE age_agg AS SELECT path, depth, sum(b)::BIGINT AS b, sum(o)::BIGINT AS o, day FROM ("
        + _age_explode_sql(f"read_parquet('{_q(store)}')", "CAST(mtime / 86400 AS BIGINT)", "day")
        + ") GROUP BY path, depth, day"
    )
    fleet = int(con.execute("SELECT coalesce(sum(b), 0) FROM age_agg WHERE depth = 1").fetchone()[0])
    floor = max(1, int(coarse_floor(fleet, AGE_FLOOR_EXP)))
    # Per-path total bytes (over all days) decides what clears the floor.
    con.execute(f"CREATE TEMP TABLE age_keep AS SELECT path FROM age_agg GROUP BY path HAVING sum(b) >= {floor}")
    kept = con.execute("SELECT count(*) FROM age_keep").fetchone()[0]
    all_paths = con.execute("SELECT count(DISTINCT path) FROM age_agg").fetchone()[0]
    out_path = out / AGE_INDEX
    kv = f"(FORMAT parquet, {duckdb_codec()}, ROW_GROUP_SIZE {ROW_GROUP_SIZE}, KV_METADATA {{coarse_floor: '{floor}'}})"
    con.execute(
        f"COPY (SELECT a.path, a.depth, a.day, a.b, a.o FROM age_agg a JOIN age_keep USING (path) "
        f"ORDER BY a.depth, a.path, a.day) TO '{out_path}' {kv}"
    )
    n_rows = con.execute("SELECT count(*) FROM age_agg a JOIN age_keep USING (path)").fetchone()[0]
    con.execute("DROP TABLE age_agg")
    con.execute("DROP TABLE age_keep")
    err(f"age-index: {n_rows:,} rows, {kept:,} of {all_paths:,} paths ≥ floor {floor:,} B → {out_path}")
    return {"rows": int(n_rows), "floor": floor, "paths": int(kept), "file": str(out_path)}


# --- Phase B: multi-scale time pyramid (specs/age-index.md) ------------------
# Path-major tiers (one parquet per bin), reusing pyrmts's planner + our
# D1-footer reader at serve time (DIY sum-combine, so the producer stays our own
# DuckDB — no pyrmts Python dep). Bins finest→coarsest; the base (first) is
# exploded once from the store's `mtime` (seconds), coarser bins re-bin from it
# (pyrmts `cascade_tiers` in SQL). A dense, all-fixed-width ladder (no calendar
# `mo`/`y` — pyrmts forbids mixing fixed-width and calendar in one ladder):
# powers-of-2 rungs let any output bin compose from ≤popcount(N) atoms (7d =
# 4+2+1), which keeps the served bins few. `1h` base gives sub-day created-time
# resolution (`mtime` is second-precision). The serve tier list in
# `site/functions/_lib/agePyramid.ts` must match.
AGE_PYRAMID_BINS = ("1h", "3h", "6h", "12h", "1d", "2d", "4d", "8d")
# Variant name per pyramid bin (what `index-sync`/`indexKey` resolve).
AGE_PYRAMID_VARIANTS = {b: f"age-pyramid-{b}" for b in AGE_PYRAMID_BINS}


def _binstart_ms_sql(bin: str, secs: str) -> str:
    """Epoch-**ms** bucket start (pyrmts `binCol` convention) for the epoch-**seconds**
    int expression ``secs`` at ``bin`` (fixed-width `Nmin|Nh|Nd`, or calendar
    `1mo`/`1y` via `date_trunc`)."""
    if bin.endswith("min"):
        n = int(bin[:-3]) * 60
    elif bin.endswith("mo"):
        return f"epoch_ms(date_trunc('month', to_timestamp({secs})))"
    elif bin.endswith("h"):
        n = int(bin[:-1]) * 3600
    elif bin.endswith("d"):
        n = int(bin[:-1]) * 86400
    elif bin.endswith("y"):
        return f"epoch_ms(date_trunc('year', to_timestamp({secs})))"
    else:
        raise ValueError(f"bad bin {bin!r}")
    return f"((({secs}) // {n}) * {n * 1000})"


def write_age_pyramid(
    con: "duckdb.DuckDBPyConnection",
    store: str,
    out_dir: str | Path,
    bins: tuple[str, ...] = AGE_PYRAMID_BINS,
) -> dict:
    """Write one path-major tier `age-pyramid-<bin>.parquet` per bin under
    ``out_dir`` from the store's file rows at ``store``. Explode once at the
    base (finest) bin; re-bin coarser tiers from it. Rows `(path, depth,
    binstart, b, o)` sorted `(depth, path, binstart)` — a path's whole history
    contiguous, RG-pruned by `path` (the drilled-path query is a prefix range).
    Floored per-path (total bytes ≥ `AGE_FLOOR_EXP`'s floor); a depth-0
    fleet-root row per bin. Returns `{floor, bins: {bin: {rows, file}}}`."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    base = bins[0]
    con.execute(
        "CREATE TEMP TABLE pyr_base AS SELECT path, depth, sum(b)::BIGINT AS b, sum(o)::BIGINT AS o, binstart FROM ("
        + _age_explode_sql(f"read_parquet('{_q(store)}')", _binstart_ms_sql(base, "mtime"), "binstart")
        + ") GROUP BY path, depth, binstart"
    )
    fleet = int(con.execute("SELECT coalesce(sum(b), 0) FROM pyr_base WHERE depth = 1").fetchone()[0])
    floor = max(1, int(coarse_floor(fleet, AGE_FLOOR_EXP)))
    con.execute(f"CREATE TEMP TABLE pyr_keep AS SELECT path FROM pyr_base GROUP BY path HAVING sum(b) >= {floor}")
    summ: dict[str, dict] = {}
    for bin in bins:
        out_path = out / f"age-pyramid-{bin}.parquet"
        if bin == base:
            sel = "SELECT b.path, b.depth, b.binstart, b.b, b.o FROM pyr_base b JOIN pyr_keep USING (path)"
        else:
            # re-bin the base's ms bucket start (ms // 1000 = exact seconds) up.
            rb = _binstart_ms_sql(bin, "(b.binstart // 1000)")
            sel = f"SELECT b.path, b.depth, {rb} AS binstart, sum(b.b)::BIGINT AS b, sum(b.o)::BIGINT AS o FROM pyr_base b JOIN pyr_keep USING (path) GROUP BY b.path, b.depth, {rb}"
        kv = f"(FORMAT parquet, {duckdb_codec()}, ROW_GROUP_SIZE {ROW_GROUP_SIZE}, KV_METADATA {{coarse_floor: '{floor}', bin: '{bin}'}})"
        con.execute(f"COPY ({sel} ORDER BY depth, path, binstart) TO '{out_path}' {kv}")
        n = con.execute(f"SELECT count(*) FROM ({sel})").fetchone()[0]
        summ[bin] = {"rows": int(n), "file": str(out_path)}
        err(f"age-pyramid[{bin}]: {n:,} rows → {out_path}")
    con.execute("DROP TABLE pyr_base")
    con.execute("DROP TABLE pyr_keep")
    return {"floor": floor, "bins": summ}


def write_index(
    sources: list[tuple[str, str]],
    out_dir: str | Path,
    *,
    mem: str = "8GB",
    threads: int = 8,
    tmp_dir: str | Path | None = None,
    age_only: bool = False,
    sort_variants: tuple[tuple[str, ...], ...] = (),
    row_group_rows: int = ROW_GROUP_SIZE,
) -> dict:
    """Write the store's sorts (`path-index.parquet`, `path-index-bysize.parquet`,
    + `sort_variants` copies) and the age pyramid under ``out_dir`` from
    ``sources`` — one ``(bucket, layer-2 parquet)`` per bucket of the scan
    (specs/cw-multi-bucket.md §2): the rows are the UNION of each bucket's
    rows, so depth 1 holds every bucket. Returns a summary (rows, buckets,
    columns, per-sort rows/groups, pyramid, files).

    ``age_only``: write *only* the age pyramid (skip the sorts). For a
    ladder-only backfill, where the layer-2s are unchanged so the sorts would
    come out byte-identical — sync just the `age-pyramid-*` variants
    (`index-sync -A`) and the sort pointers keep their generation."""
    out = Path(out_dir)
    con = duckdb.connect()
    con.execute(f"SET memory_limit='{mem}'; SET threads={threads}")
    con.execute(f"SET temp_directory='{tmp_dir or out / '.duckdb-tmp'}'")
    store, columns = write_store(con, sources, out)
    buckets = [b for b, _ in sources]
    try:
        if age_only:
            pyramid = write_age_pyramid(con, store, out)
            return {
                "buckets": buckets,
                "pyramid": pyramid,
                "files": {AGE_PYRAMID_VARIANTS[b]: s["file"] for b, s in pyramid["bins"].items()},
            }
        sorts = write_sorts(con, store, out, sort_variants=sort_variants, row_group_rows=row_group_rows)
        n = sorts["path"]["rows"]
        err(f"store: {n:,} rows ({', '.join(columns)}) over {buckets}")
        pyramid = write_age_pyramid(con, store, out)
    finally:
        os.remove(store)
    return {
        "rows": int(n),
        "buckets": buckets,
        "columns": columns,
        "sorts": {v: {"rows": s["rows"], "groups": s["groups"]} for v, s in sorts.items()},
        "pyramid": pyramid,
        "files": {
            **{v: s["file"] for v, s in sorts.items()},
            **{AGE_PYRAMID_VARIANTS[b]: s["file"] for b, s in pyramid["bins"].items()},
        },
    }
