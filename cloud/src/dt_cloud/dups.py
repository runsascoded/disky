"""Duplicate and likely-duplicate data in one scan (specs/dups.md §1): `dt-cloud dups`.

Input: a scan's path index (the `path` or `bysize` sort: dirs + objects, one row per (path, label slice)). Output, in
`-o` (a dir or URL): `dups.parquet`, one row per (group, copy), and `dups.json`, the summary the page's header reads.

A **group** is a set of ≥2 paths judged to hold the same data. Tiers (no content hashes in the index, so no `exact`):

- `tree`: the same relative path (the path minus its bucket) with identical `size` and `n_files`, in ≥2 buckets.
- `moved`: identical basename, `size` and `n_files` under ≥2 distinct relative paths (any buckets: one bucket can
  hold the same dataset under several names).

Both over dirs and objects whose total size is ≥ the floor (`-f`, default 1 GiB). A path's size is its slices' sum: a
`bysize` sort's `tot` column prunes to the paths over the floor; without it, a labeled index is aggregated whole
before the floor applies (a per-slice filter would drop or undercount multi-owner paths).

Groups are **maximal**: a group is dropped when its copies' parents are exactly another group's copies (the parent
holds the same data, so the child adds nothing). A `moved` group with exactly a `tree` group's copies is that group.

Per copy:
- `nested`: the copy sits under another group's dir copy, so its bytes are already in that group's; `net_*` totals
  count only un-nested copies.
- `suspect` (the group's): ≥ `-s` copies (default 7), mostly same-size collisions (`part-00000.parquet` shards), not
  copies; `net_by_tier` leaves them out (they're totaled apart).
- `keep` (one per group): the suggested copy to keep, the most recently read (`last_read`), ties → the oldest
  (`created`), then path order.
- `check`, `check_n`, `check_eq`: with `-g N`, the top N groups (by reclaimable bytes, among those with an un-nested
  copy) are spot-checked on GCS: each copy's objects are listed (name, size, crc32c; at most `-n` per copy) and
  compared to the keep copy's. `equal`: every object matches and both listings are complete; `partial`: what both
  capped listings reached matches; `differs`: some object or name doesn't. The keep copy and unchecked groups: null.

Tiers overlap (a `moved` group can span two `tree` groups): their totals are never summed.

Columns: `group` (stable: a hash of the tier and its key), `tier`, `rel`, `path`, `bucket`, `kind`, `size`,
`n_files`, `mtime` / `created` (epoch seconds, newest / oldest), `last_read` (epoch days, newest), the index's class
pivots (`sum_storage_class_id_*`, as present), `copies`, `reclaimable` (the group's: size × (copies − 1)),
`n_buckets`, `nested`, `suspect`, `keep`, `check`, `check_n`, `check_eq`. Sorted by `reclaimable` desc, then group,
bucket, path (a group's copies are contiguous), in 8K-row groups.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import duckdb
from click import IntRange, argument, command, option

from .static_names import err

GIB = 1 << 30
#: A group of at least this many copies is flagged `suspect` (mostly same-size collisions).
SUSPECT_COPIES = 7
#: The label column of a sliced index (`index.LABEL_COL`): one row per (path, slice).
LABEL_COL = "usr"
CLASS_PREFIX = "sum_storage_class_id_"
ROW_GROUP = 8192
VERSION = 1
CHECKS = 'CREATE OR REPLACE TEMP TABLE checks ("group" VARCHAR, path VARCHAR, "check" VARCHAR, check_n BIGINT, check_eq BIGINT)'


@dataclass(frozen=True)
class Listing:
    """A copy's objects: relative name → (size, crc32c), in name order; `complete` unless capped."""
    objects: dict[str, tuple[int, str | None]]
    complete: bool


#: Lists a copy (`<bucket>/<rel>`, its kind): what `-g` compares.
Lister = Callable[[str, str], Listing]


def compare(ref: Listing, other: Listing) -> tuple[str, int, int]:
    """`other` against `ref`: (verdict, names compared, equal). Capped listings (both name-ordered) compare only the
    names both reached."""
    a, b = ref.objects, other.objects
    if ref.complete and other.complete:
        names_a, names_b = set(a), set(b)
    else:
        hi = min(max(a, default=""), max(b, default=""))
        names_a, names_b = {k for k in a if k <= hi}, {k for k in b if k <= hi}
    common = names_a & names_b
    eq = sum(a[k] == b[k] for k in common)
    if eq < len(common) or names_a != names_b:
        return "differs", len(common), eq
    return ("equal" if ref.complete and other.complete else "partial"), len(common), eq


def gcs_lister(max_objects: int) -> Lister:
    """Lists a copy on GCS (`<bucket>/<rel>`): an object's own metadata, a dir's objects under `<rel>/` (at most
    `max_objects`, name order)."""
    from google.cloud import storage

    client = storage.Client()

    def lister(path: str, kind: str) -> Listing:
        bucket, _, key = path.partition("/")
        if kind != "dir":
            b = client.bucket(bucket).get_blob(key)
            return Listing({"": (int(b.size), b.crc32c)} if b else {}, True)
        prefix = key.rstrip("/") + "/"
        out: dict[str, tuple[int, str | None]] = {}
        for b in client.list_blobs(bucket, prefix=prefix, fields="items(name,size,crc32c),nextPageToken", page_size=1000):
            if len(out) >= max_objects:
                return Listing(out, False)
            out[b.name[len(prefix):]] = (int(b.size), b.crc32c)
        return Listing(out, True)

    return lister


def _cols(con: duckdb.DuckDBPyConnection, src: str) -> list[str]:
    return [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{src}')").fetchall()]


def build(con: duckdb.DuckDBPyConnection, src: str, floor: int = GIB, suspect_copies: int = SUSPECT_COPIES) -> list[str]:
    """The `dups` table (no check columns yet) from the index at `src`; returns its class pivot columns."""
    cols = _cols(con, src)
    classes = sorted(c for c in cols if c.startswith(CLASS_PREFIX))
    if "tot" in cols:
        where = f"AND tot >= {floor}"   # the path's total across slices: prunes the size sort's tail
    elif LABEL_COL not in cols:
        where = f"AND size >= {floor}"  # one row per path
    else:
        where = ""                      # sliced, no totals: the floor applies after the slices are summed
    created = "min(created)::BIGINT" if "created" in cols else "NULL::BIGINT"
    last_read = "max(last_read)::INTEGER" if "last_read" in cols else "NULL::INTEGER"
    cls = "".join(f", sum({c})::BIGINT AS {c}" for c in classes)
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE nodes AS
    SELECT path, any_value(kind) AS kind, split_part(path, '/', 1) AS bucket,
           substr(path, strpos(path, '/') + 1) AS rel, regexp_replace(path, '/[^/]*$', '') AS parent,
           regexp_extract(path, '[^/]*$') AS base,
           sum(size)::BIGINT AS size, sum(n_files)::BIGINT AS n_files, max(mtime)::BIGINT AS mtime,
           {created} AS created, {last_read} AS last_read{cls}
    FROM read_parquet('{src}')
    WHERE depth >= 2 {where}
    GROUP BY path
    HAVING sum(size) >= {floor}
    """)
    con.execute("""
    CREATE OR REPLACE TEMP TABLE cand AS
    WITH tree AS (
      SELECT 'tree' AS tier, rel || '|' || size || '|' || n_files AS gkey, n.*
      FROM nodes n
      QUALIFY count(DISTINCT bucket) OVER (PARTITION BY rel, size, n_files) >= 2
    ), moved AS (
      SELECT 'moved' AS tier, base || '|' || size || '|' || n_files AS gkey, n.*
      FROM nodes n
      QUALIFY count(DISTINCT rel) OVER (PARTITION BY base, size, n_files) >= 2
    )
    SELECT * FROM tree UNION ALL SELECT * FROM moved
    """)
    con.execute("""
    CREATE OR REPLACE TEMP TABLE gsig AS
    SELECT tier, gkey, list_sort(list(path)) AS paths, list_sort(list(DISTINCT parent)) AS parents, count(*) AS copies
    FROM cand GROUP BY tier, gkey
    """)
    # Maximality: a group whose copies' parents are exactly another group's copies is dropped; several copies under
    # one parent (same-dir renames) never are.
    con.execute("""
    CREATE OR REPLACE TEMP TABLE kept AS
    SELECT g.tier, g.gkey, g.copies FROM gsig g
    WHERE len(g.parents) < g.copies OR NOT EXISTS (SELECT 1 FROM gsig p WHERE p.paths = g.parents)
    """)
    con.execute("""
    DELETE FROM kept WHERE tier = 'moved' AND gkey IN (
      SELECT m.gkey FROM gsig m JOIN gsig t ON m.tier = 'moved' AND t.tier = 'tree' AND t.paths = m.paths)
    """)
    sel = "".join(f", c.{c}" for c in classes)
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE dups AS
    SELECT substr(md5(k.tier || '|' || k.gkey), 1, 12) AS "group", k.tier, c.rel, c.path, c.bucket, c.kind,
           c.size, c.n_files, c.mtime, c.created, c.last_read{sel},
           k.copies, c.size * (k.copies - 1) AS reclaimable,
           count(DISTINCT c.bucket) OVER (PARTITION BY k.tier, k.gkey) AS n_buckets,
           false AS nested, k.copies >= {suspect_copies} AS suspect,
           row_number() OVER (PARTITION BY k.tier, k.gkey
                              ORDER BY c.last_read DESC NULLS LAST, c.created ASC NULLS LAST, c.path) = 1 AS keep
    FROM kept k JOIN cand c USING (tier, gkey)
    """)
    # `nested`: under another kept dir copy (any group, any tier): its bytes are in that copy's group already.
    con.execute("""
    UPDATE dups SET nested = true WHERE path IN (
      WITH anc AS (
        SELECT path, array_to_string(string_split(path, '/')[1:i], '/') AS a
        FROM (SELECT DISTINCT path FROM dups), range(2, len(string_split(path, '/'))) t(i)
      )
      SELECT DISTINCT anc.path FROM anc JOIN (SELECT DISTINCT path FROM dups WHERE kind = 'dir') d ON d.path = anc.a)
    """)
    return classes


def check_groups(con: duckdb.DuckDBPyConnection, lister: Lister, top: int, workers: int = 16) -> int:
    """Spot-check the top `top` groups with an un-nested copy (by reclaimable bytes): fills `check`, `check_n`,
    `check_eq` on each non-keep copy. Returns the groups checked."""
    rows = con.execute("""
    WITH g AS (
      SELECT "group", any_value(reclaimable) AS r FROM dups GROUP BY "group" HAVING bool_or(NOT nested)
      ORDER BY r DESC, "group" LIMIT ?
    )
    SELECT d."group", d.path, d.kind, d.keep FROM dups d JOIN g USING ("group") ORDER BY d."group", d.keep DESC, d.path
    """, [top]).fetchall()
    groups: dict[str, list[tuple[str, str, bool]]] = {}
    for g, path, kind, keep in rows:
        groups.setdefault(g, []).append((path, kind, keep))

    def one(item: tuple[str, list[tuple[str, str, bool]]]) -> list[tuple[str, str, str, int, int]]:
        g, ((ref_path, ref_kind, _), *others) = item
        ref = lister(ref_path, ref_kind)
        return [(g, path, *compare(ref, lister(path, kind))) for path, kind, _ in others]

    with ThreadPoolExecutor(workers) as ex:
        results = [r for rs in ex.map(one, groups.items()) for r in rs]
    con.execute(CHECKS)
    if results:
        con.executemany("INSERT INTO checks VALUES (?, ?, ?, ?, ?)", results)
    return len(groups)


def summary(con: duckdb.DuckDBPyConnection) -> dict:
    """`dups.json`'s totals: by tier (every copy), net by tier (un-nested, non-suspect copies), the suspect groups,
    by bucket pair (net: a group counts its size once in each pair of buckets its un-nested copies span; a diagonal
    pair, ≥2 copies in one bucket), the copies histogram, and the checks' verdicts."""
    def rows(sql: str) -> list[dict]:
        r = con.execute(sql)
        names = [d[0] for d in r.description]
        return [dict(zip(names, v)) for v in r.fetchall()]

    def totals(where: str, by: str) -> list[dict]:
        # Per group, u of its k copies pass `where`, each holding a (k − 1)/k share of its size: an exact integer per
        # group (⌊size · (k − 1) · u / k⌋), so the sum is deterministic (a float sum isn't, across threads).
        return rows(f"""
          WITH g AS (
            SELECT {by}, "group", any_value(size)::HUGEINT AS size, any_value(copies) AS k, count(*) AS u
            FROM out WHERE {where} GROUP BY {by}, "group"
          )
          SELECT {by}, count(*) AS groups, sum(u)::BIGINT AS copies, sum(size * u)::BIGINT AS bytes,
                 sum(size * (k - 1) * u // k)::BIGINT AS reclaimable
          FROM g GROUP BY {by} ORDER BY reclaimable DESC, {by}""")

    return {
        "by_tier": totals("true", "tier, kind"),
        "net_by_tier": totals("NOT nested AND NOT suspect", "tier"),
        "suspect": totals("NOT nested AND suspect", "tier"),
        "by_pair": rows("""
          WITH c AS (
            SELECT "group", tier, any_value(size) AS size, bucket, count(*) AS n
            FROM out WHERE NOT nested AND NOT suspect GROUP BY "group", tier, bucket
          )
          SELECT a.tier, a.bucket AS b1, b.bucket AS b2, count(*) AS groups, sum(a.size)::BIGINT AS bytes
          FROM c a JOIN c b ON a."group" = b."group" AND (a.bucket < b.bucket OR (a.bucket = b.bucket AND a.n >= 2))
          GROUP BY ALL ORDER BY bytes DESC, a.tier, b1, b2"""),
        "copies_hist": rows("""
          SELECT tier, copies, count(DISTINCT "group") AS groups FROM out GROUP BY ALL ORDER BY tier, copies"""),
        "checks": rows("""
          SELECT tier, "check", count(DISTINCT "group") AS groups, count(*) AS copies
          FROM out WHERE "check" IS NOT NULL GROUP BY ALL ORDER BY tier, "check" """),
    }


def find_dups(
    src: str,
    out_dir: Path,
    *,
    floor: int = GIB,
    suspect_copies: int = SUSPECT_COPIES,
    check: int = 0,
    lister: Lister | None = None,
    workers: int = 16,
    con: duckdb.DuckDBPyConnection | None = None,
    scan: str | None = None,
) -> dict:
    """Write `out_dir/dups.parquet` and `out_dir/dups.json` (a local dir) from the index at `src`; returns the
    summary."""
    con = con or duckdb.connect()
    t0 = time.monotonic()
    classes = build(con, src, floor, suspect_copies)
    n_groups, n_rows = con.execute('SELECT count(DISTINCT "group"), count(*) FROM dups').fetchone()
    err(f"dups: {n_groups:,} groups, {n_rows:,} copies ({time.monotonic() - t0:.0f}s)")
    checked = 0
    if check:
        if lister is None:
            raise ValueError("check needs a lister")
        t1 = time.monotonic()
        checked = check_groups(con, lister, check, workers)
        err(f"dups: spot-checked {checked:,} groups ({time.monotonic() - t1:.0f}s)")
    else:
        con.execute(CHECKS)
    con.execute("""
    CREATE OR REPLACE TEMP TABLE out AS
    SELECT d.*, c."check", c.check_n, c.check_eq FROM dups d LEFT JOIN checks c USING ("group", path)
    """)
    out_dir.mkdir(parents=True, exist_ok=True)
    con.execute(f"""COPY (SELECT * FROM out ORDER BY reclaimable DESC, "group", bucket, path)
                    TO '{out_dir / 'dups.parquet'}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {ROW_GROUP})""")
    s = {"v": VERSION, **({"scan": scan} if scan else {}), "floor": floor, "suspect_copies": suspect_copies,
         "classes": classes, "checked_groups": checked,
         "groups": con.execute('SELECT count(DISTINCT "group") FROM out').fetchone()[0],
         "rows": con.execute("SELECT count(*) FROM out").fetchone()[0], **summary(con)}
    (out_dir / "dups.json").write_text(json.dumps(s, indent=1, default=str) + "\n")
    return s


@contextmanager
def _readable(con: duckdb.DuckDBPyConnection, src: str) -> Iterator[str]:
    """`src` as DuckDB reads it: a local path as is; a URL through its fsspec filesystem (range reads, so a `tot`
    filter fetches only the size sort's head). The fsspec path is slow on a big index: gcs's 10-18 `bysize` sort
    (645M rows) took 38 min through it vs 8 through a gcsfuse mount on the same e2-standard-4, so the step reads the
    mounted path."""
    from disk_tree import blobfs

    if not blobfs.is_url(src):
        yield src
        return
    fs, p = blobfs.fs_for(src)
    con.register_filesystem(fs)
    proto = fs.protocol[0] if isinstance(fs.protocol, (tuple, list)) else fs.protocol
    yield f"{proto}://{p}"


def _upload(local: Path, url: str) -> None:
    """`local`'s parquet, then its JSON (the summary last: what a skip check keys on), to the dir at `url`."""
    from disk_tree import blobfs

    for name in ("dups.parquet", "dups.json"):
        fs, p = blobfs.fs_for(f"{url.rstrip('/')}/{name}")
        fs.put_file(str(local / name), p)


@command("dups")
@option("-d", "--scan", help="The scan's id, recorded in the summary")
@option("-f", "--floor", type=int, default=GIB, show_default=True, help="Size floor (bytes) for dirs and objects")
@option("-g", "--gcs-check", "check", type=IntRange(0), default=0, help="Spot-check the top N groups' copies on GCS (list each copy's objects: name, size, crc32c)")
@option("-m", "--mem", help="DuckDB memory limit (e.g. 8GB)")
@option("-n", "--check-objects", type=IntRange(1), default=20000, show_default=True, help="Objects listed per copy, at most, by -g")
@option("-o", "--out", required=True, help="Output dir or URL: dups.parquet, dups.json")
@option("-s", "--suspect-copies", type=IntRange(2), default=SUSPECT_COPIES, show_default=True, help="Flag groups of at least this many copies as suspect")
@option("-t", "--threads", type=int, help="DuckDB threads")
@option("-T", "--tmp-dir", help="DuckDB spill dir")
@option("-w", "--workers", type=IntRange(1), default=16, show_default=True, help="Concurrent listings for -g")
@argument("index")
def dups_cmd(scan, floor, check, mem, check_objects, out, suspect_copies, threads, tmp_dir, workers, index) -> None:
    """Duplicate-candidate groups in one scan's path index (INDEX: its `path` or `bysize` sort, a local path or URL;
    the `bysize` sort's `tot` prunes the read to paths over the floor): `dups.parquet` + `dups.json` in OUT
    (specs/dups.md). Prints the summary as JSON."""
    from disk_tree import blobfs

    con = duckdb.connect()
    con.execute("SET preserve_insertion_order = false")
    if mem:
        con.execute(f"SET memory_limit = '{mem}'")
    if threads:
        con.execute(f"SET threads = {int(threads)}")
    if tmp_dir:
        con.execute(f"SET temp_directory = '{tmp_dir}'")
    lister = gcs_lister(check_objects) if check else None
    with _readable(con, index) as src:
        if blobfs.is_url(out):
            d = Path(tempfile.mkdtemp(prefix="dt-dups-"))
            try:
                s = find_dups(src, d, floor=floor, suspect_copies=suspect_copies, check=check, lister=lister,
                              workers=workers, con=con, scan=scan)
                _upload(d, out)
            finally:
                shutil.rmtree(d, ignore_errors=True)
        else:
            s = find_dups(src, Path(out), floor=floor, suspect_copies=suspect_copies, check=check, lister=lister,
                          workers=workers, con=con, scan=scan)
    for r in s["net_by_tier"]:
        err(f"dups {r['tier']}: {r['groups']:,} groups, {r['copies']:,} copies, {r['reclaimable']:,} B reclaimable (net)")
    print(json.dumps(s, default=str))
