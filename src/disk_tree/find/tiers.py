"""Layer-2 as the path store's sorts (spec path-store.md §1.2, §4.1).

Consumers that read layer-2 over HTTP range requests (mgu's Pages Functions;
DT's own Functions do the same over the scan blob) want *sorted parquet with
small row groups*. The store is one table — every row, object or directory —
and a tier is a **sort** of those rows, never a different row set:

======== ============================= ==================================== ==========================
tier     rows                          sort                                 serves
======== ============================= ==================================== ==========================
path     every row (× label slice)     (depth, path, …labels)               point lookups, children by
                                                                            name, small subtrees whole
bysize   every row (× label slice)     (⌊log2 size⌋ desc, path, …labels)    every thresholded read: a
                                       size 0 / NULL last                   subtree at threshold t is
                                                                            one run per bucket ≥ t
======== ============================= ==================================== ==========================

Both tiers hold the *same rows* with exact sums (each is the layer-2 row,
re-sorted, never re-aggregated), so a query reads one of them and gets one
result set. Every row keeps ``kind``; label columns (mgu-scale-unification
item B) sit right after ``path`` and close each sort. Sort variants are extra
sorted copies of either tier led by other columns — parquet has no secondary
index; a sorted copy is one.

The size bucket is ``⌊log2 size⌋`` computed in SQL (``length(bin(size)) − 1``,
exact for any int64), never stored: a reader recovers a group's bucket range
from its ``b_min``/``b_max`` stats (:mod:`disk_tree.find.groups`). The
parquet key-value metadata says ``bucket = log2`` beside ``tier`` and ``sort``.

Tiers are cut from the finished layer-2 parquet (one sorted COPY each,
spillable), so they are engine-agnostic: anything that leaves a layer-2 blob
on disk can call :func:`write_tiers`; :func:`cut_tiers` fronts it for the
``disk-tree tiers`` CLI (a local path or a URL in, local or URL outputs).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    import duckdb

TIERS = ('path', 'bysize')
#: DuckDB's memory budget for a cut when the caller brings no connection. The
#: sort is external (it spills to `temp_directory`), so this bounds peak RSS
#: rather than the input: an unbounded connection takes DuckDB's default, 80 %
#: of RAM — measured 28 GB peak for a 57M-row cut on a 30 GB Batch task.
DEFAULT_MEM = '8GB'
DEFAULT_ROW_GROUP_ROWS = 8192
# DuckDB's vector size: the granularity at which its parquet writer cuts row groups.
ROW_GROUP_STEP = 2048
#: The `bysize` sort's leading key, as it appears in the `sort` metadata.
BUCKET_KEY = 'size_bucket desc'
#: `⌊log2 size⌋` for `size > 0`, else NULL — exact (a bit length, not a float log).
BUCKET_SQL = 'CASE WHEN size > 0 THEN length(bin(size)) - 1 END'
#: The `sort` metadata of each tier, before labels / variants.
TIER_SORTS: dict[str, tuple[str, ...]] = {
    'path': ('depth', 'path'),
    'bysize': (BUCKET_KEY, 'path'),
}

# Layer-2 columns before/after the label block; anything between `path` and
# `size` is a label column.
_HEAD = 'path'
_AFTER_LABELS = 'size'


def size_bucket(size: int | None) -> int | None:
    """Python twin of :data:`BUCKET_SQL`: ``⌊log2 size⌋`` for ``size > 0``, else ``None``."""
    if size is None or size <= 0:
        return None
    return int(size).bit_length() - 1


def parse_tiers(spec: str) -> tuple[str, ...]:
    tiers = tuple(t for t in spec.split(',') if t)
    bad = [t for t in tiers if t not in TIERS]
    if bad:
        raise ValueError(f"unknown tier(s) {bad}; choose from {list(TIERS)}")
    if len(set(tiers)) != len(tiers):
        raise ValueError(f"tier repeated in {spec!r}")
    return tiers


def tier_path(stem: str, tier: str, variant: tuple[str, ...] = ()) -> str:
    suffix = f"-by-{'-'.join(variant)}" if variant else ''
    return f'{stem}.{tier}{suffix}.parquet'


def _order_sql(key: str) -> str:
    if key == BUCKET_KEY:
        return f'({BUCKET_SQL}) DESC NULLS LAST'
    return f'{key} NULLS FIRST'


def connect(mem: str = DEFAULT_MEM, threads: int | None = None, tmp_dir: str | None = None) -> "duckdb.DuckDBPyConnection":
    """A DuckDB connection bounded for an external sort: `memory_limit` = `mem`,
    `temp_directory` = `tmp_dir` (spill), `threads` when given."""
    import duckdb as _duckdb
    con = _duckdb.connect()
    con.execute(f"SET memory_limit='{mem}'")
    if threads is not None:
        con.execute(f"SET threads={int(threads)}")
    if tmp_dir is not None:
        os.makedirs(tmp_dir, exist_ok=True)
        con.execute(f"SET temp_directory='{tmp_dir}'")
    return con


def write_tiers(
    layer2: str,
    stem: str,
    tiers: tuple[str, ...] = TIERS,
    row_group_rows: int = DEFAULT_ROW_GROUP_ROWS,
    sort_variants: tuple[tuple[str, ...], ...] = (),
    con: "duckdb.DuckDBPyConnection | None" = None,
    groups: bool = False,
    mem: str = DEFAULT_MEM,
    threads: int | None = None,
    tmp_dir: str | None = None,
) -> dict[str, int]:
    """Cut `tiers` (+ `sort_variants` of each) from the local layer-2 parquet
    at `layer2` into `<stem>.<tier>[-by-<cols>].parquet`.

    Without `con`, the cut runs on :func:`connect` — `mem` (DuckDB's
    `memory_limit`), `threads`, and `tmp_dir` (its spill directory; default
    `<stem's dir>/.duckdb-tmp`, removed after) bound it; a caller's `con`
    brings its own settings.

    Returns `{output path: row count}` in write order. Each file carries
    `tier` / `sort` (and, for `bysize`, `bucket`) in its parquet key-value
    metadata. With `groups`, each tier also gets its group manifest
    `<tier>.groups.json` beside it (:mod:`disk_tree.find.groups`) — the
    precomputed footer a serverless reader plans range reads from.
    """
    own_tmp = None
    if con is None:
        if tmp_dir is None:
            own_tmp = tmp_dir = os.path.join(os.path.dirname(os.path.abspath(stem)) or '.', '.duckdb-tmp')
        con = connect(mem=mem, threads=threads, tmp_dir=tmp_dir)
    # DuckDB's parquet writer flushes a row group once the buffered rows reach
    # the target, in 2048-row vector steps — so "≤ N rows per group" holds
    # exactly for multiples of 2048 and silently overshoots otherwise
    # (measured: ROW_GROUP_SIZE 3000 wrote a 5000-row group).
    if row_group_rows < ROW_GROUP_STEP or row_group_rows % ROW_GROUP_STEP:
        raise ValueError(f"row_group_rows must be a positive multiple of {ROW_GROUP_STEP}; got {row_group_rows}")
    for tier in tiers:
        if tier not in TIERS:
            raise ValueError(f"unknown tier {tier!r}; choose from {list(TIERS)}")
    cols = [r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_parquet('{layer2}')").fetchall()]
    if cols[0] != _HEAD or _AFTER_LABELS not in cols:
        raise ValueError(f"{layer2}: not a layer-2 parquet (columns {cols})")
    labels = tuple(cols[1:cols.index(_AFTER_LABELS)])
    for variant in sort_variants:
        missing = [c for c in variant if c not in cols]
        if missing:
            raise ValueError(f"sort variant {variant}: column(s) {missing} not in {layer2} ({cols})")
        if not variant:
            raise ValueError("empty sort variant")

    def order(lead: tuple[str, ...], base: tuple[str, ...]) -> tuple[str, ...]:
        out: list[str] = []
        for c in (*lead, *base, *labels):
            if c not in out:
                out.append(c)
        return tuple(out)

    # A tier is the layer-2's rows, re-sorted: it inherits the source's listing
    # format (a v2 source's scan root / implied pivots / v1 column order ride
    # along, so `blobfs.read_parquet` restores tiers like blobs); the codec is
    # `listing_format.codec()` (spec `listing-slim.md`).
    from disk_tree import listing_format as lf
    src_kv = lf.format_of(layer2).kv()

    written: dict[str, int] = {}

    def copy(tier: str, sort: tuple[str, ...], variant: tuple[str, ...]) -> None:
        out = tier_path(stem, tier, variant)
        kv = {'tier': tier, 'sort': ','.join(sort)}
        if tier == 'bysize':
            kv['bucket'] = 'log2'
        kv.update(src_kv)
        kv_sql = ', '.join("'{}': '{}'".format(k, str(v).replace("'", "''")) for k, v in kv.items())
        order_by = ', '.join(_order_sql(c) for c in sort)
        tmp = out + '.tmp'
        con.execute(f"""
            COPY (
                SELECT * FROM read_parquet('{layer2}')
                ORDER BY {order_by}
            ) TO '{tmp}' (FORMAT PARQUET, {lf.duckdb_codec()},
                ROW_GROUP_SIZE {row_group_rows}, KV_METADATA {{{kv_sql}}})
        """)
        os.replace(tmp, out)
        written[out] = int(con.execute(f"SELECT COUNT(*) FROM read_parquet('{out}')").fetchone()[0])
        if groups:
            from disk_tree.find.groups import write_groups
            write_groups(out)

    for tier in tiers:
        base = TIER_SORTS[tier]
        copy(tier, order((), base), ())
        for variant in sort_variants:
            copy(tier, order(variant, base), variant)
    if own_tmp:
        shutil.rmtree(own_tmp, ignore_errors=True)
    return written


@dataclass(frozen=True)
class TierReport:
    """One cut tier, as `disk-tree tiers` reports it."""
    path: str
    rows: int
    groups: int
    bytes: int
    kv: dict[str, str]

    def asdict(self) -> dict:
        return {'path': self.path, 'rows': self.rows, 'groups': self.groups, 'bytes': self.bytes, 'kv': dict(self.kv)}


@contextmanager
def _local_copy(path: str) -> Iterator[str]:
    """`path` as a local file: itself, or (a URL) a bounded-memory copy under a
    temp dir, read through `blobfs.open_read` and closed with it."""
    from disk_tree import blobfs
    if not blobfs.is_url(path):
        yield path
        return
    d = tempfile.mkdtemp(prefix='disk-tree-tiers-')
    try:
        local = os.path.join(d, os.path.basename(path))
        with blobfs.open_read(path) as src, open(local, 'wb') as dst:
            shutil.copyfileobj(src, dst, 16 << 20)
        yield local
    finally:
        shutil.rmtree(d, ignore_errors=True)


def default_stem(layer2: str) -> str:
    """`<dir>/<name>` for a local `<dir>/<name>.parquet`; `./<name>` for a URL."""
    from disk_tree import blobfs
    name = os.path.basename(layer2)
    if name.endswith('.parquet'):
        name = name[: -len('.parquet')]
    if blobfs.is_url(layer2):
        return name
    return os.path.join(os.path.dirname(layer2), name)


def cut_tiers(
    layer2: str,
    stem: str | None = None,
    tiers: tuple[str, ...] = TIERS,
    row_group_rows: int = DEFAULT_ROW_GROUP_ROWS,
    sort_variants: tuple[tuple[str, ...], ...] = (),
    groups: bool = False,
    mem: str = DEFAULT_MEM,
    threads: int | None = None,
    tmp_dir: str | None = None,
) -> list[TierReport]:
    """:func:`write_tiers` over any layer-2 — a local path or a URL (read once
    into a temp copy; DuckDB sorts local files) — to `stem` (default
    :func:`default_stem`), which may itself be a URL: the tiers (+ sidecars)
    are cut locally, then uploaded beside each other."""
    import pyarrow.parquet as pq
    from disk_tree import blobfs
    from disk_tree.find.groups import groups_path
    if stem is None:
        stem = default_stem(layer2)
    remote_stem = blobfs.is_url(stem)
    with _local_copy(layer2) as local:
        out_dir = tempfile.mkdtemp(prefix='disk-tree-tiers-out-') if remote_stem else None
        try:
            local_stem = os.path.join(out_dir, os.path.basename(stem)) if out_dir else stem
            written = write_tiers(
                local, local_stem, tiers=tiers, row_group_rows=row_group_rows,
                sort_variants=sort_variants, groups=groups, mem=mem, threads=threads, tmp_dir=tmp_dir,
            )
            reports: list[TierReport] = []
            for out, n in written.items():
                md = pq.read_metadata(out)
                kv = {k.decode(): v.decode() for k, v in (md.metadata or {}).items() if k != b'ARROW:schema'}
                final = out
                if out_dir:
                    final = stem + out[len(local_stem):]
                    blobfs.put(out, final)
                    if groups:
                        blobfs.put(groups_path(out), groups_path(final))
                reports.append(TierReport(path=final, rows=n, groups=md.num_row_groups, bytes=os.path.getsize(out), kv=kv))
            return reports
        finally:
            if out_dir:
                shutil.rmtree(out_dir, ignore_errors=True)
