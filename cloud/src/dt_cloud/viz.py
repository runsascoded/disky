"""Data extracts for the web viz (``dt-cloud path-index``).

DuckDB over the deduped listing parquet: the path store's sorts (dir rows
rolled up per owner slice + the listing's object rows; specs/path-store.md
§4.3), created-day age strata, and a small meta blob. The JSON outputs are
consumed by ``site/``; the sorts are what ``index-sync`` publishes.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import resource
import sys
from collections import defaultdict
from functools import partial
from pathlib import Path

import duckdb

from .index import AGE_COLS, STORE_L2, age_bucket_sql, duckdb_codec, store_columns

err = partial(print, file=sys.stderr)


def prefix_labels(
    con: "duckdb.DuckDBPyConnection",
    attributions: tuple[str, ...],
    identities_path: "str | Path | None",
    listing_src: str,
) -> "pd.DataFrame":
    """The attribution prefix map as rows ``(key, user, depth)`` — ``key`` is
    ``<bucket>[/<dir>…]`` (no ``gs://``, no trailing slash), deepest-prefix
    semantics applied by the caller (``path-index``'s per-depth joins, or DT's
    ``import --label``). Path-glob rules expand against ``listing_src``'s dirs
    (``(bucket, name)``); identities resolve to canonical users.

    Prefixes deeper than ``GCS_USAGE_ATTR_MAX_DEPTH`` (12) are dropped: the
    ancestor join is dirs × max depth, so a handful of ultra-deep prefixes
    inflate memory for everything (the 2026-08-26 wandb re-mine's 231
    depth-14+ config paths pushed it 13 → 16 and OOMed the 100GB REPROC); a
    truncated prefix would over-attribute whole parent dirs, so they go."""
    import pandas as pd

    from .identity import load_identities
    from .prefixes import load_prefix_map

    if identities_path is None:
        raise ValueError("attribution needs the deployment's identity map (-i / $DT_CLOUD_IDENTITIES)")
    identities = load_identities(identities_path)
    by_prefix = load_prefix_map(con, attributions, identities, listing_src)
    pfx_df = pd.DataFrame(
        [{"key": k.removeprefix("gs://").rstrip("/"), "user": u, "source": source} for k, (u, source) in by_prefix.items()],
        columns=["key", "user", "source"],
    )
    pfx_df["depth"] = pfx_df["key"].str.count("/") + 1
    attr_max_depth = int(os.environ.get("GCS_USAGE_ATTR_MAX_DEPTH", "12"))
    deep = pfx_df["depth"] > attr_max_depth
    if deep.any():
        err(f"dropping {int(deep.sum())} attribution prefixes deeper than {attr_max_depth}")
        pfx_df = pfx_df[~deep]
    return pfx_df


def write_labels(
    con: "duckdb.DuckDBPyConnection",
    listings: tuple[str, ...],
    attributions: tuple[str, ...],
    identities_path: "str | Path | None",
    out_dir: "Path",
) -> dict[str, int]:
    """DT's label tables (``import --label``, spec mgu-scale-unification.md
    §B) from mgu's attribution: one ``labels-<bucket>.parquet`` per bucket in
    the listings, rows ``(prefix, usr)`` with ``prefix`` relative to the
    bucket (``''`` = the bucket-wide rule). Returns bucket → row count."""
    from disk_tree.listing import prepare_listing

    src = prepare_listing(con, listings)
    fp_dir = "CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END"
    con.execute(f"CREATE OR REPLACE TEMP VIEW listing_dirs AS SELECT DISTINCT bucket, {fp_dir} AS name FROM {src}")
    pfx_df = prefix_labels(con, attributions, identities_path, "listing_dirs")
    buckets = [b for (b,) in con.execute("SELECT DISTINCT bucket FROM listing_dirs ORDER BY 1").fetchall()]
    out_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for bucket in buckets:
        rows = pfx_df[(pfx_df["key"] == bucket) | pfx_df["key"].str.startswith(bucket + "/")]
        table = rows.assign(prefix=rows["key"].str.slice(len(bucket) + 1)).rename(columns={"user": "usr"})[["prefix", "usr"]]
        table = table.sort_values("prefix").reset_index(drop=True)
        con.register("labels_out", table)
        con.execute(f"COPY labels_out TO '{out_dir / f'labels-{bucket}.parquet'}' (FORMAT parquet)")
        con.unregister("labels_out")
        counts[bucket] = len(table)
    return counts


def _rss(tag: str) -> None:
    """Log peak RSS so OOM autopsies can name the phase (linux: KB, mac: B)."""
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    err(f"[rss] {tag}: peak {peak / (1024**2 if sys.platform == 'linux' else 1024**3):.1f} GB")

# The gcs store's columns (specs/path-store.md §1.1) in the engine's layer-2
# order — `usr` (the owner slice) right after `path` as the label block, the
# storage-class pivots the listing carries, `created` / `last_read` (the access
# log's last-read day).
STORE_L2_SHAPE = (
    ["path", "usr", "size", "depth", "kind", "n_files", "n_children", "n_desc", "mtime", "mtime_mean", "created", "last_read",
     "sum_storage_class_id_2", "sum_storage_class_id_3", "sum_storage_class_id_4"],
    {},
)
# The per-dir rollup cache's columns (`dir-stats.parquet`): a cache written
# before the store (no `mtime_max` / `created_max`) is rebuilt, not read.
DIR_STATS_COLS = ("bucket", "dir", "fp", "b", "o", "wts", "wb", "c2", "c3", "c4", "mtime_max", "created_max")


def _write_store(
    con: "duckdb.DuckDBPyConnection",
    src: str,
    path_index: Path,
    *,
    attr: bool,
    fp_dir: str,
    maxseg: int,
    user_sorts: bool = True,
    row_group_rows: int | None = None,
    user_sort_tiers: tuple[str, ...] | None = None,
    asof_day: int = 0,
    age_cols: tuple[str, ...] = (),
) -> dict[str, dict]:
    """The store's sorts beside ``path_index`` (specs/path-store.md §4.3) from
    the rolled-up dir slices (``ptu``, ``dir_stats``, ``dir_attr`` when
    attributing) and the listing's object rows (``src``): one layer-2 shaped
    union (`index.STORE_L2`, streamed to disk, removed after) cut by the
    engine into `path` + `bysize` (+ `-by-user` when attributing). Returns
    `index.write_sorts`'s summary.

    Structural counts are per path, from the dir set itself (cheap: dirs, not
    objects × depth): `n_children` = the dir's own objects + its direct
    subdirs; `n_desc` = own objects + Σ over subdirs of (`n_desc` + 1), folded
    bottom-up one depth at a time."""
    from .index import write_sorts

    if path_index.name != "path-index.parquet":
        raise ValueError(f"the store's `path` sort is served as path-index.parquet; got {path_index}")
    path_index.parent.mkdir(parents=True, exist_ok=True)
    parent = "regexp_replace(path, '/[^/]*$', '')"
    con.execute("CREATE TEMP TABLE dirs AS SELECT DISTINCT path, depth FROM ptu")
    con.execute(
        f"""
        CREATE TEMP TABLE dstruct AS
        SELECT d.path, d.depth,
          coalesce(s.o, 0)::BIGINT AS own_o,
          coalesce(c.n, 0)::BIGINT AS n_subdirs
        FROM dirs d
        LEFT JOIN (SELECT fp, sum(o) AS o FROM dir_stats GROUP BY fp) s ON s.fp = d.path
        LEFT JOIN (SELECT {parent} AS parent, count(*) AS n FROM dirs WHERE depth > 1 GROUP BY 1) c ON c.parent = d.path
        """
    )
    con.execute("CREATE TEMP TABLE nd (path VARCHAR, n_desc BIGINT)")
    for k in range(maxseg, 0, -1):
        con.execute(
            f"""
            INSERT INTO nd
            SELECT d.path, d.own_o + coalesce(ch.s, 0)
            FROM dstruct d
            LEFT JOIN (
              SELECT {parent.replace('path', 'c.path')} AS parent, sum(n.n_desc + 1) AS s
              FROM dstruct c JOIN nd n USING (path) WHERE c.depth = {k + 1} GROUP BY 1
            ) ch ON ch.parent = d.path
            WHERE d.depth = {k}
            """
        )
    _rss("dstruct")
    columns = store_columns([([*STORE_L2_SHAPE[0], *age_cols], STORE_L2_SHAPE[1])])
    dir_exprs = {
        "path": "p.path", "usr": "p.usr", "size": "p.b::BIGINT", "depth": "p.depth::INTEGER", "kind": "'dir'",
        "n_files": "p.o::BIGINT", "n_children": "(d.own_o + d.n_subdirs)::BIGINT", "n_desc": "n.n_desc::BIGINT",
        "mtime": "coalesce(p.mtime, 0)::BIGINT",
        "mtime_mean": "(CASE WHEN p.wb > 0 THEN p.wts / p.wb END)::DOUBLE",
        "created": "p.created::BIGINT", "last_read": "p.a::INTEGER",
        "sum_storage_class_id_2": "coalesce(p.c2, 0)::BIGINT",
        "sum_storage_class_id_3": "coalesce(p.c3, 0)::BIGINT",
        "sum_storage_class_id_4": "coalesce(p.c4, 0)::BIGINT",
        **{c: f"coalesce(p.{c}, 0)::BIGINT" for c in age_cols},
    }
    # An object is attributed to its dir's owner (the same deepest-prefix join
    # `dir_attr` resolved per dir; a leaf needs no explosion). The access log is
    # per dir, so `last_read` is NULL on object rows.
    obj_dir = fp_dir.replace("name", "x.name")
    attr_join = f"LEFT JOIN dir_attr t ON t.bucket = x.bucket AND t.dir = ({obj_dir})" if attr else ""
    obj_exprs = {
        "path": "x.bucket || '/' || x.name", "usr": 't."user"' if attr else "NULL::VARCHAR",
        "size": "x.size_bytes::BIGINT", "depth": "(len(string_split(x.name, '/')) + 1)::INTEGER", "kind": "'file'",
        "n_files": "1::BIGINT", "n_children": "0::BIGINT", "n_desc": "0::BIGINT",
        "mtime": "coalesce(floor(epoch(coalesce(x.updated, x.created))), 0)::BIGINT",
        "mtime_mean": "floor(epoch(x.created))::DOUBLE",
        "created": "floor(epoch(x.created))::BIGINT", "last_read": "NULL::INTEGER",
        **{f"sum_storage_class_id_{c}": f"(CASE WHEN x.storage_class_id = {c} THEN x.size_bytes ELSE 0 END)::BIGINT" for c in (2, 3, 4)},
        **{c: f"(CASE WHEN x.created IS NOT NULL AND {age_bucket_sql('x.created', str(asof_day))} = {i} THEN x.size_bytes ELSE 0 END)::BIGINT" for i, c in enumerate(age_cols)},
    }
    sel = lambda exprs: ", ".join(f"{exprs[c]} AS {c}" for c in columns)  # noqa: E731
    store = path_index.with_name(STORE_L2)
    con.execute(
        f"""
        COPY (
          SELECT {sel(dir_exprs)} FROM ptu p JOIN dstruct d USING (path) JOIN nd n USING (path)
          UNION ALL
          SELECT {sel(obj_exprs)} FROM {src} x {attr_join}
        ) TO '{store}' (FORMAT parquet, {duckdb_codec()}, ROW_GROUP_SIZE 65536)
        """
    )
    for t in ("nd", "dstruct", "dirs"):
        con.execute(f"DROP TABLE {t}")
    _rss("store-l2")
    try:
        kw = {"row_group_rows": row_group_rows} if row_group_rows else {}
        return write_sorts(con, str(store), path_index.parent, sort_variants=((("usr",),) if attr and user_sorts else ()), variant_tiers=user_sort_tiers, **kw)
    finally:
        store.unlink()


def _cache_hit(con: "duckdb.DuckDBPyConnection", pq_path: Path, cols: tuple[str, ...]) -> bool:
    """Whether ``pq_path`` exists with (at least) ``cols`` — an older cache
    schema is a miss, so a re-attribution run after a writer change rebuilds
    the rollup instead of failing mid-query."""
    if not pq_path.exists():
        return False
    have = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{pq_path}') LIMIT 0").fetchall()}
    missing = [c for c in cols if c not in have]
    if missing:
        err(f"{pq_path.name}: cache predates the store (no {missing}); rebuilding")
        return False
    return True


def write_path_index(
    listings: tuple[str, ...],
    out_dir: Path,
    asof: str,
    attributions: tuple[str, ...] = (),
    identities_path: str | Path | None = None,
    access: tuple[str, ...] = (),
    dir_cache: Path | None = None,
    path_index: Path | None = None,
    user_sorts: bool = True,
    row_group_rows: int | None = None,
    age_strata: bool = False,
    user_sort_tiers: tuple[str, ...] | None = None,
) -> dict:
    """Write age.json / meta.json under ``out_dir`` (+ the path store's sorts
    beside ``path_index``); returns meta.

    ``user_sorts=False`` skips the ``-by-user`` copies; ``user_sort_tiers``
    keeps the copy for some sorts only. A lens view reads a user-first sort
    when one exists — on the mixed-user sorts a user's root view decodes the
    fleet's top rows (gcs 9/30: 413) — so gcs keeps ``("bysize",)``: one
    copy instead of two. ``row_group_rows`` overrides the 8K default.

    ``path_index`` (``<dir>/path-index.parquet``) writes the store
    (specs/path-store.md §4.3): every dir row — every ancestor path ×
    attribution slice, descendant-inclusive — and every object row from the
    listing, attributed to its dir's owner (the same deepest-prefix join;
    objects are leaves, so no ancestor explosion), with ``created`` / ``mtime``
    from the listing's ``created`` / ``updated``. The union is cut by the
    engine's ``write_tiers`` into the ``path`` and ``bysize`` sorts (+ the
    ``-by-user`` copies of both when attributing), under the served names
    (`index.write_sorts`). Structural counts (``n_children``, ``n_desc``) are
    per path and repeat on each owner slice of it — a slice partitions bytes
    and objects, not the tree — so sum ``size`` / ``n_files`` over slices,
    never those.

    ``dir_cache`` names a directory for the layer-2 rollups (``dir-stats`` /
    ``age-days`` parquet) — attribution-independent per-dir aggregates, cached
    write-through so re-attribution runs skip the 595M-row object scans
    entirely (specs/dir-agg-cache.md). Immutable per scan date, like the
    listing they derive from.

    With ``attributions``, every dir is attributed (deepest-prefix-wins, same
    join as ``report``) and every index row is an owner slice (``usr``).

    With ``access`` (layer-2a access-log agg parquet globs), every index row
    that has been read since logging began carries ``a`` — the epoch day of
    its most recent read (GET/HEAD/LIST anywhere under the prefix) — and meta
    gains the observation window (``meta.access = {from, to}``).
    """
    from disk_tree.listing import prepare_listing

    attr = bool(attributions)
    con = duckdb.connect()
    # attribution mode is node-scale (34M-dir python-side walk); plain mode
    # stays laptop-safe
    con.execute(f"SET memory_limit='{os.environ.get('DUCKDB_MEM', '24GB' if attr else '6GB')}'")
    # large hash aggregations stream instead of materializing ordered output;
    # matters in Cloud Run where DuckDB's disk spill is actually RAM-backed
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET threads={os.environ.get('DUCKDB_THREADS', '4')}")
    if tmp := os.environ.get("DUCKDB_TMP"):
        con.execute(f"SET temp_directory='{tmp}'")
    # A local capture's `bucket` is its scan root (`/Users/ryan`); drop the
    # leading slash so it tiles like a bucket name (`Users/ryan`). Kept, it
    # yields an empty first segment: a depth-1 node at path '' — the root's own
    # path — whose parent walk never terminates. The filesystem root (`/`)
    # strips to '' outright, so its rows re-split on their first segment
    # (`Applications`, `Users`, … become the roots); files directly under `/`
    # (`.file`, `.VolumeIcon.icns`) have no root to sit in and are dropped.
    src = f"""(
        SELECT * REPLACE (
          CASE WHEN bucket = '' THEN split_part(name, '/', 1) ELSE bucket END AS bucket,
          CASE WHEN bucket = '' THEN substr(name, strpos(name, '/') + 1) ELSE name END AS name)
        FROM (SELECT * REPLACE (ltrim(bucket, '/') AS bucket) FROM {prepare_listing(con, listings)})
        WHERE bucket <> '' OR strpos(name, '/') > 0
    )"""

    # --- layer-2 dir rollups (attribution-independent; cached when dir_cache) ---
    # Everything downstream needs objects only via these two aggregates:
    # `dir_stats` (per-dir sizes/objects/classes/weighted-mtimes) and
    # `age_days` (per-(day, dir) bytes/objects). Cache them as parquet next to
    # the listing (immutable per date) so re-attribution runs — REPROC, ledger
    # refreshes — do zero 595M-row object scans; cold runs also drop from four
    # object scans to two (attr dirs + storage classes now derive from these).
    fp_dir = "CASE WHEN name LIKE '%/%' THEN regexp_replace(name, '/[^/]*$', '') ELSE '' END"
    fp = f"CASE WHEN ({fp_dir}) = '' THEN bucket ELSE bucket || '/' || ({fp_dir}) END"
    asof_day = (dt.date.fromisoformat(asof[:10]) - dt.date(1970, 1, 1)).days
    # Bytes by age (`age_strata`, specs/done/row-age-strata.md): off by default,
    # so a store that hasn't opted in keeps its schema, cache and cost.
    age_cols = AGE_COLS if age_strata else ()
    age_sums = "".join(
        f"sum(CASE WHEN created IS NOT NULL AND {age_bucket_sql('created', str(asof_day))} = {i} THEN size_bytes END)::BIGINT AS {c}, "
        for i, c in enumerate(age_cols)
    )
    stats_pq = dir_cache / "dir-stats.parquet" if dir_cache else None
    age_pq = dir_cache / "age-days.parquet" if dir_cache else None
    if stats_pq is not None and _cache_hit(con, stats_pq, (*DIR_STATS_COLS, *age_cols)):
        con.execute(f"CREATE TEMP VIEW dir_stats AS SELECT * FROM read_parquet('{stats_pq}')")
        err(f"dir-stats: cache hit ({stats_pq})")
    else:
        con.execute(
            f"""
            CREATE TEMP TABLE dir_stats AS
            WITH obj AS (
              SELECT bucket, {fp_dir} AS dir, size_bytes, created, updated, storage_class_id, {fp} AS fp
              FROM {src}
            )
            SELECT bucket, dir, fp,
              sum(size_bytes)::BIGINT AS b, count(*)::BIGINT AS o,
              -- exact (DECIMAL, not DOUBLE): a float sum's last bits depend on the
              -- parallel aggregation order, which made re-runs differ in `wts`
              -- (verified 2026-09-06 on a 9/4 re-aggregation); seconds resolution
              -- is plenty for a byte-weighted mean written day.
              sum(CASE WHEN created IS NOT NULL THEN size_bytes::DECIMAL(38,0) * epoch(created)::BIGINT END)::DECIMAL(38,0) AS wts,
              sum(CASE WHEN created IS NOT NULL THEN size_bytes END)::BIGINT AS wb,
              sum(CASE WHEN storage_class_id = 2 THEN size_bytes END)::BIGINT AS c2,
              sum(CASE WHEN storage_class_id = 3 THEN size_bytes END)::BIGINT AS c3,
              sum(CASE WHEN storage_class_id = 4 THEN size_bytes END)::BIGINT AS c4,
              {age_sums}
              -- the store's two native stamps (path-store.md §1.1), max over the
              -- dir's own objects: `mtime` = `updated` where the platform has it
              -- (GCS), else `created` (S3/R2's LastModified lands there)
              max(floor(epoch(coalesce(updated, created))))::BIGINT AS mtime_max,
              max(floor(epoch(created)))::BIGINT AS created_max
            FROM obj GROUP BY bucket, dir, fp
            """
        )
        if stats_pq is not None:
            stats_pq.parent.mkdir(parents=True, exist_ok=True)
            con.execute(f"COPY dir_stats TO '{stats_pq}' (FORMAT parquet)")
            err(f"dir-stats: wrote cache ({stats_pq})")
    _rss("dir-stats")
    if age_pq is not None and age_pq.exists():
        con.execute(f"CREATE TEMP VIEW age_days AS SELECT * FROM read_parquet('{age_pq}')")
        err(f"age-days: cache hit ({age_pq})")
    else:
        con.execute(
            f"""
            CREATE TEMP TABLE age_days AS
            SELECT CAST(floor(epoch(created) / 86400) AS INTEGER) AS day, bucket, {fp_dir} AS dir,
              sum(size_bytes)::BIGINT AS bytes, count(*)::BIGINT AS objects
            FROM {src}
            WHERE created IS NOT NULL
            GROUP BY ALL
            """
        )
        if age_pq is not None:
            con.execute(f"COPY age_days TO '{age_pq}' (FORMAT parquet)")
            err(f"age-days: wrote cache ({age_pq})")
    _rss("age-days")
    # Dir-level stand-in for the raw listing where only dir paths matter
    # (bucket enumeration + path-glob prefix_owners expansion).
    con.execute("CREATE TEMP VIEW listing_dirs AS SELECT bucket, dir AS name FROM dir_stats")

    if attr:
        _rss("start")
        pfx_df = prefix_labels(con, attributions, identities_path, "listing_dirs")
        _rss("prefix-map")
        con.register("pfx", pfx_df)
        # Deepest-prefix-wins, one INSERT per prefix depth (deepest first):
        # inner hash join (build side = that depth's prefixes — thousands) plus
        # an anti-join against already-resolved dirs (build side ≤ resolved
        # set, a few GB at fleet scale). Keeps the proven-fast split+equi-join
        # machinery of the original explosion but drops its un-spillable
        # dirs×maxd arg_max aggregate (OOM-killed the daily's 128GB node when
        # the 2026-08-26 wandb re-mine grew the prefix map). A chained-LEFT-
        # JOIN single-pass variant planned pathologically (~100× slower);
        # per-depth INSERTs give the planner 12 trivial queries instead.
        depths = sorted({int(d) for d in pfx_df["depth"]}, reverse=True)
        con.execute('CREATE TEMP TABLE dir_attr (bucket VARCHAR, dir VARCHAR, "user" VARCHAR)')
        dk = "CASE WHEN s.dir = '' THEN s.bucket ELSE s.bucket || '/' || s.dir END"
        for k in depths:
            con.execute(
                f"""
                INSERT INTO dir_attr
                SELECT s.bucket, s.dir, p."user"
                FROM dir_stats s
                JOIN pfx p ON p.depth = {k}
                  AND p.key = array_to_string(str_split({dk}, '/')[1:{k}], '/')
                WHERE len(str_split({dk}, '/')) >= {k}
                  AND NOT EXISTS (
                    SELECT 1 FROM dir_attr d WHERE d.bucket = s.bucket AND d.dir = s.dir
                  )
                """
            )
        _rss("dir_attr")
        # per-user storage-class byte mixes (the site prices per-user roll-ups
        # with class-aware rates) + the per-user leaderboard meta.users needs —
        # both derived from `dir_agg` below, so no separate object scan.
        user_class: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
        user_bytes: dict[str, int] = defaultdict(int)

    # Access agg first (small): its per-dir last-read day joins into
    # `dir_agg` below so the path index carries a subtree-MAX `a`, and it
    # also decorates the age strata. Empty without logs.
    # As-of rule: a scan dated D sees reads through the end of D-1 UTC
    # (`day < D`), whatever shards exist when this runs — so a re-aggregation
    # of an old date reproduces it instead of leaking later reads into it.
    access_window: tuple[int, int] | None = None
    con.execute("CREATE TEMP TABLE access_agg (bucket VARCHAR, dir VARCHAR, aday INTEGER, ro BIGINT, rb BIGINT)")
    if access:
        globs = "[" + ", ".join(f"'{g}'" for g in access) + "]"
        # The aggregates' time grain moved from `day` to `hour` (engine
        # `aggregate_access`); a glob spans both shapes until the day-grain
        # parts age out, so read them by name and take the day from whichever
        # column a part has.
        acc = f"read_parquet({globs}, union_by_name = true)"
        have = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {acc}").fetchall()}
        day_of = [f"CAST({c} AS DATE)" for c in ("day", "hour") if c in have]
        if not day_of:
            raise ValueError(f"access aggregates have neither `day` nor `hour`: {sorted(have)}")
        day = day_of[0] if len(day_of) == 1 else f"COALESCE({', '.join(day_of)})"
        con.execute(
            f"""
            INSERT INTO access_agg
            SELECT bucket, CASE WHEN path = '.' THEN '' ELSE path END AS dir,
              CAST(floor(epoch(MAX(last_ts)) / 86400) AS INTEGER) AS aday,
              COALESCE(SUM(n_ops) FILTER (WHERE op IN ('GET', 'HEAD')), 0) AS ro,
              COALESCE(SUM(bytes_out) FILTER (WHERE op IN ('GET', 'HEAD')), 0) AS rb
            FROM {acc}
            WHERE op IN ('GET', 'HEAD', 'LIST') AND {day} < DATE '{asof}'
            GROUP BY 1, 2
            """
        )
        lo, hi = con.execute(
            f"SELECT CAST(floor(epoch(MIN(last_ts)) / 86400) AS INTEGER), "
            f"CAST(floor(epoch(MAX(last_ts)) / 86400) AS INTEGER) FROM {acc} "
            f"WHERE {day} < DATE '{asof}'"
        ).fetchone()
        if lo is not None:
            access_window = (int(lo), int(hi))
        _rss("access")

    # --- the path index: every ancestor path, every depth (specs/view-serving.md) ---
    # Roll every object up to *all* its ancestor prefixes (descendant-inclusive
    # totals at every depth), attribute per dir, keep only prefixes clearing the
    # fold floor so the Python side stays small regardless of object count, and
    # link them parent->child. No d1..d4 cap — the tree is as deep as the data.

    attr_join_s = "LEFT JOIN dir_attr t ON t.bucket = s.bucket AND t.dir = s.dir" if attr else ""
    ages = "".join(f", {c}" for c in age_cols)
    ages_s = "".join(f", s.{c}" for c in age_cols)
    age_rollup = "".join(f", sum({c})::BIGINT AS {c}" for c in age_cols)
    user_sel = 't."user"' if attr else "CAST(NULL AS VARCHAR)"
    # Attribution is a join over the cached per-dir rollups — a few million
    # rows — never over objects. dir_agg also feeds the class-mix and
    # leaderboard, so those need no separate scan either.
    con.execute(
        f"""
        CREATE TEMP TABLE dir_agg AS
        SELECT s.fp, {user_sel} AS usr,
          s.b, s.o, s.wts, s.wb, s.c2, s.c3, s.c4, xa.aday AS a, s.mtime_max, s.created_max{ages_s}
        FROM dir_stats s {attr_join_s}
        LEFT JOIN access_agg xa ON xa.bucket = s.bucket AND xa.dir = s.dir
        """
    )
    total_b, total_o = con.execute("SELECT coalesce(sum(b), 0)::BIGINT, coalesce(sum(o), 0)::BIGINT FROM dir_agg").fetchone()
    total_b, total_o = int(total_b), int(total_o)
    maxseg = int(con.execute("SELECT coalesce(max(len(string_split(fp, '/'))), 1) FROM dir_agg").fetchone()[0])
    _rss("dir-agg")

    if attr:
        # class-mix + leaderboard straight off dir_agg (class 1/STANDARD =
        # total minus the non-standard classes we track explicitly).
        for usr, b, c2, c3, c4 in con.execute(
            "SELECT usr, sum(b), sum(c2), sum(c3), sum(c4) FROM dir_agg WHERE usr IS NOT NULL GROUP BY usr"
        ).fetchall():
            c1 = int(b) - int(c2 or 0) - int(c3 or 0) - int(c4 or 0)
            for cid, cv in ((1, c1), (2, c2), (3, c3), (4, c4)):
                if cv:
                    user_class[usr][cid] += int(cv)
            user_bytes[usr] += int(b)

    # The full rolled-up (path, user) relation — every ancestor path,
    # descendant-inclusive, attributed, NO floor: the store's dir rows, one per
    # owner slice (specs/path-store.md §1.1 `usr`).
    con.execute(
        f"""
        CREATE TEMP TABLE ptu AS
        WITH da AS (
          -- split computed here (streamed), not materialized into dir_agg —
          -- a LIST column on 150M rows is a ~40GB temp table by itself
          SELECT *, string_split(fp, '/') AS segs FROM dir_agg
        ),
        exploded AS (
          SELECT array_to_string(segs[1:r.k], '/') AS path, r.k AS depth,
            b, o, wts, wb, c2, c3, c4, a, usr, mtime_max, created_max{ages}
          FROM da, range(1, {maxseg} + 1) r(k)
          WHERE len(segs) >= r.k
        )
        SELECT path, depth, usr,
          sum(b)::BIGINT AS b, sum(o)::BIGINT AS o,
          sum(wts)::DECIMAL(38,0) AS wts, sum(wb)::BIGINT AS wb,
          sum(c2)::BIGINT AS c2, sum(c3)::BIGINT AS c3, sum(c4)::BIGINT AS c4,
          max(a) AS a,  -- subtree-max last-read epoch day (NULL = never read)
          max(mtime_max)::BIGINT AS mtime, max(created_max)::BIGINT AS created{age_rollup}
        FROM exploded GROUP BY path, depth, usr
        """
    )
    _rss("ptu")
    sorts: dict[str, dict] = {}
    if path_index is not None:
        sorts = _write_store(con, src, path_index, attr=attr, fp_dir=fp_dir, maxseg=maxseg, user_sorts=user_sorts, row_group_rows=row_group_rows, user_sort_tiers=user_sort_tiers, asof_day=asof_day, age_cols=age_cols)
        _rss("store")
        # Provenance sidecar: the attributing prefixes' user/source/evidence.
        from .extras import write_extras
        write_extras(pfx_df if attr else None, path_index.parent)
        _rss("extras")
    con.execute("DROP TABLE ptu")

    # Age strata also carry `a` — the dir's last-read epoch day from the access
    # agg (subtree MAX, the same semantics as the tree's `a`) — so the site can
    # color a vintage by whether anyone has touched it since logging began.
    # Absent = no read observed. Multiplies rows by at most the number of
    # distinct read days (a few weeks of logs), not by dirs.
    if attr:
        # (day, d1, user, a) strata: the cached per-(day, dir) rollup
        # joined to the same dir attribution. Day keys are epoch days; the site
        # aggregates to day/week/month.
        age = con.execute(
            """
            SELECT d.day,
              CASE WHEN d.dir = '' THEN '(files)' ELSE regexp_extract(d.dir, '^([^/]+)', 1) END AS d1,
              t."user" AS user, x.aday AS a,
              sum(d.bytes)::BIGINT AS bytes, sum(d.objects)::BIGINT AS objects
            FROM age_days d
            LEFT JOIN dir_attr t ON t.bucket = d.bucket AND t.dir = d.dir
            LEFT JOIN access_agg x ON x.bucket = d.bucket AND x.dir = d.dir
            GROUP BY ALL ORDER BY ALL  -- fully deterministic output order (byte-identical reruns)
            """
        ).fetchall()
        age_rows = [
            {"d": day, "d1": d1, **({"u": u} if u else {}), **({"a": a} if a is not None else {}), "b": b, "o": o}
            for day, d1, u, a, b, o in age
        ]
        _rss("age")
    else:
        age = con.execute(
            """
            SELECT d.day,
              CASE WHEN d.dir = '' THEN NULL ELSE regexp_extract(d.dir, '^([^/]+)', 1) END AS d1,
              x.aday AS a,
              sum(d.bytes)::BIGINT AS bytes, sum(d.objects)::BIGINT AS objects
            FROM age_days d LEFT JOIN access_agg x ON x.bucket = d.bucket AND x.dir = d.dir
            GROUP BY ALL ORDER BY ALL
            """
        ).fetchall()
        age_rows = [
            {"d": day, "d1": d1 or "(files)", **({"a": a} if a is not None else {}), "b": b, "o": o}
            for day, d1, a, b, o in age
        ]

    # Storage-class mix from the dir rollups (class 1/STANDARD = total minus
    # the explicitly-tracked classes — same derivation the per-user mix uses).
    s_b, s_c2, s_c3, s_c4 = con.execute(
        "SELECT coalesce(sum(b), 0)::BIGINT, coalesce(sum(c2), 0)::BIGINT,"
        " coalesce(sum(c3), 0)::BIGINT, coalesce(sum(c4), 0)::BIGINT FROM dir_stats"
    ).fetchone()
    classes = [(cid, cb) for cid, cb in ((1, int(s_b) - int(s_c2) - int(s_c3) - int(s_c4)), (2, int(s_c2)), (3, int(s_c3)), (4, int(s_c4))) if cb]

    meta = {
        "asof": asof,
        "generated": dt.date.today().isoformat(),
        # When this scan was published, as data (the site used to splice the
        # store object's mtime in, which stops being the publish time once the
        # served copy lives in R2 — specs/done/r2-serving-migration.md step 6).
        "published": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "total_bytes": total_b,
        "total_objects": total_o,
        "class_bytes": {int(c): int(b) for c, b in classes},
    }
    if sorts:
        meta["index"] = {"rows": sorts["path"]["rows"], "sorts": {v: {"rows": s["rows"], "groups": s["groups"]} for v, s in sorts.items()}}
    if access_window:
        # Epoch days the access logs cover — the UI's "no reads since <from>"
        # is only meaningful relative to when logging began.
        meta["access"] = {"from": access_window[0], "to": access_window[1]}
    if attr:
        meta["users"] = [{"u": u, "b": b} for u, b in sorted(user_bytes.items(), key=lambda kv: -kv[1])]
        meta["user_class_bytes"] = {u: dict(sorted(c.items())) for u, c in sorted(user_class.items())}

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "age.json").write_text(json.dumps(age_rows, separators=(",", ":")) + "\n")
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta
