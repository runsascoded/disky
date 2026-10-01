"""URL-aware blob IO — the one seam between a local scans dir and a remote one.

A scans dir on the search path (`config.scan_read_dirs()`) may be a plain
directory *or* an fsspec URL (`r2://bucket/prefix`, `s3://…`, `gs://…`,
`file://…`; `memory://…` in tests). `Scan.blob` stays a basename either way;
these helpers take the resolved path and dispatch on `is_url`, so the storage
backends and the diff/index readers never branch on it themselves.

`fsspec` (and the per-scheme driver) is an optional extra — every import here
is lazy, so a local-only install never pays for it, and a remote entry on the
search path fails with a pointed message instead of an ImportError deep in
pandas. `r2://` is not a registered fsspec protocol: it rides `s3fs` with the
bucket's Cloudflare endpoint, resolved by `r2_endpoint` (env, then the same
per-bucket `endpoint_url` that `buckets.yml` already carries for `sync`).

Remote `exists` hits are remembered (`_known`): blobs are immutable UUID-named
files, so a positive answer stays true until this module removes the blob.
Writes seed it; nothing here caches a miss.
"""
from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from os.path import exists as _local_exists, getmtime, getsize, join as _local_join
from typing import TYPE_CHECKING
from urllib.parse import urlparse

if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa

R2_ENDPOINT_VAR = 'DISK_TREE_R2_ENDPOINT_URL'

# gcsfs ≥ 2026.8.1 reads through an "adaptive prefetcher" by default
# (`USE_EXPERIMENTAL_ADAPTIVE_PREFETCHING`, formerly opt-in): every read handle
# gets a producer task on fsspec's event loop, and a handle that pyarrow still
# holds at interpreter exit is finalized after that loop is gone — `close()`
# then blocks forever in `fsspec.asyn.sync` (`recompress gs://…` printed its
# report and never exited; on Batch, killed at `maxRunDuration`). Every reader
# here seeks the footer, then streams row groups, which a plain readahead cache
# serves as well; so the process opts out unless the environment already chose,
# and handles this module opens itself name their cache (`open_read`).
os.environ.setdefault('USE_EXPERIMENTAL_ADAPTIVE_PREFETCHING', 'false')

#: Remote paths known to exist (see module docstring).
_known: set[str] = set()


def is_url(path: str) -> bool:
    return '://' in path


def join(d: str, name: str) -> str:
    """`os.path.join` for a local dir; a textual join for a URL (no normalization)."""
    return f"{d.rstrip('/')}/{name}" if is_url(d) else _local_join(d, name)


def _fsspec():
    try:
        import fsspec
    except ImportError:
        raise RuntimeError(
            "remote scans dirs need the `fsspec` extra — install `disk-tree[r2]` (or `[s3]` / `[gcs]`)"
        ) from None
    return fsspec


def _buckets_yml() -> dict:
    """The parsed `~/.config/disk-tree/buckets.yml` (or `{}` if absent). Read
    fresh each call — reads are cheap and `_s3fs` memoizes the filesystem."""
    from . import config as _config
    cfg_path = _local_join(_config.ROOT_DIR, 'buckets.yml')
    if not _local_exists(cfg_path):
        return {}
    import yaml
    with open(cfg_path) as f:
        return yaml.safe_load(f) or {}


def _bucket_field(bucket: str, field: str) -> str | None:
    """A per-bucket field from `buckets.yml` — the matching `buckets[…].<field>`,
    else `defaults.<field>`. Only a truthy per-bucket value wins; otherwise the
    lookup falls through to `defaults`."""
    raw = _buckets_yml()
    for e in raw.get('buckets') or []:
        if isinstance(e, dict) and urlparse(e.get('uri', '')).netloc == bucket and e.get(field):
            return e[field]
    return (raw.get('defaults') or {}).get(field)


def r2_endpoint(bucket: str) -> str | None:
    """Cloudflare R2's S3 endpoint for `bucket`: `DISK_TREE_R2_ENDPOINT_URL`, else
    the bucket's (or `defaults`) `endpoint_url` in `~/.config/disk-tree/buckets.yml`."""
    ep = os.environ.get(R2_ENDPOINT_VAR)
    if ep:
        return ep
    return _bucket_field(bucket, 'endpoint_url')


def bucket_profile(bucket: str) -> str | None:
    """The AWS credential profile a bucket authenticates with, from `buckets.yml`
    (per-bucket, else `defaults`). This is how a source and a target in *different*
    accounts each pick their own key within one `index --to` run — every S3/R2
    seam (this module's `s3fs`, the `aws` CLI lister, the `boto3` bulk lister)
    honors a named profile. `None` → ambient credentials (env / default profile),
    the single-account default."""
    return _bucket_field(bucket, 'profile')


@lru_cache(maxsize=None)
def _s3fs(endpoint_url: str, profile: str | None = None):
    import s3fs
    # Cloudflare R2 requires all non-trailing multipart parts to be the same
    # length; s3fs only guarantees that under `fixed_upload_size=True`. Without
    # it, a blob large enough to go multipart fails `CompleteMultipartUpload`
    # with `InvalidPart` (leaking the in-flight upload). Harmless for real S3.
    kw = {'client_kwargs': {'endpoint_url': endpoint_url}, 'fixed_upload_size': True}
    if profile:
        kw['profile'] = profile
    return s3fs.S3FileSystem(**kw)


def fs_for(url: str):
    """`(fs, path)` for a URL — the filesystem plus the path *within* it."""
    p = urlparse(url)
    if p.scheme == 'r2':
        ep = r2_endpoint(p.netloc)
        if not ep:
            raise RuntimeError(
                f"r2://{p.netloc}: no endpoint — set {R2_ENDPOINT_VAR}, or give the bucket an "
                "`endpoint_url` in buckets.yml"
            )
        return _s3fs(ep, bucket_profile(p.netloc)), f"{p.netloc}{p.path}"
    return _fsspec().core.url_to_fs(url)


def open_read(path: str):
    """A binary read handle on a URL, with its cache named (see the prefetch note
    at the top): the caller closes it. Local paths are not accepted — pyarrow
    reads those natively."""
    if not is_url(path):
        raise ValueError(f"open_read: not a URL: {path}")
    fs, p = fs_for(path)
    return fs.open(p, 'rb', cache_type='readahead')


def _ensure_parent(fs, p: str) -> None:
    """Object stores have no directories, but the local driver does — and pyarrow
    swaps fsspec's `LocalFileSystem` for its native one on write, so the
    driver's own `auto_mkdir` never gets a say. Make the parent explicitly."""
    from fsspec.implementations.local import LocalFileSystem
    if isinstance(fs, LocalFileSystem):
        fs.makedirs(p.rsplit('/', 1)[0], exist_ok=True)


def exists(path: str) -> bool:
    if not is_url(path):
        return _local_exists(path)
    if path in _known:
        return True
    fs, p = fs_for(path)
    if fs.exists(p):
        _known.add(path)
        return True
    return False


def _info(path: str) -> dict:
    fs, p = fs_for(path)
    return fs.info(p)


def size(path: str) -> int:
    return getsize(path) if not is_url(path) else int(_info(path)['size'])


def stat(path: str) -> tuple[float, int] | None:
    """``(mtime, size)`` in one round-trip (one `stat` / one HEAD), or `None`
    when the path doesn't exist — a cache key for an immutable-unless-rewritten
    blob (`shallow.py`)."""
    if not is_url(path):
        try:
            st = os.stat(path)
        except FileNotFoundError:
            return None
        return st.st_mtime, st.st_size
    try:
        info = _info(path)
    except FileNotFoundError:
        return None
    return _info_mtime(info), int(info['size'])


def _info_mtime(info: dict) -> float:
    v = info.get('mtime') or info.get('LastModified') or info.get('created')
    if isinstance(v, datetime):
        return v.timestamp()
    return float(v) if v is not None else 0.0


def mtime(path: str) -> float:
    """Modification time as an epoch float — `getmtime` locally; whatever the
    remote driver reports (`mtime` / `LastModified` / `created`, float or datetime)."""
    if not is_url(path):
        return getmtime(path)
    return _info_mtime(_info(path))


def read_schema(path: str):
    import pyarrow.parquet as pq
    if not is_url(path):
        return pq.read_schema(path)
    fs, p = fs_for(path)
    return pq.read_schema(p, filesystem=fs)


def read_table(path: str, columns: list[str] | None = None, filters=None) -> pa.Table:
    import pyarrow.parquet as pq
    if not is_url(path):
        return pq.read_table(path, columns=columns, filters=filters)
    fs, p = fs_for(path)
    return pq.read_table(p, columns=columns, filters=filters, filesystem=fs)


def read_parquet(path: str, filters=None, columns: list[str] | None = None) -> pd.DataFrame:
    """`pd.read_parquet`, URL-aware. Predicate pushdown works the same remotely:
    row-group min/max stats are read from the footer and only overlapping groups
    are fetched (range GETs).

    A v2 layer-2 listing (spec `listing-slim.md`) comes back in its v1 shape:
    `uri` and the implied pivot columns are derived (:func:`listing_format.restore`),
    so every blob reader sees one format. Asking for `uri` (or an implied
    column) by name works on either format."""
    from . import listing_format as lf
    path = os.fspath(path)
    fmt = lf.parse(read_schema(path).metadata)
    if columns is not None and fmt.version >= 2:
        want = list(columns)
        derived = {'uri': 'path', **fmt.implied}
        read = [c for c in want if c not in derived]
        for c in want:
            if c in derived and derived[c] not in read:
                read.append(derived[c])
        df = _read_parquet(path, filters, read)
        return lf.restore(df, fmt)[want]
    return lf.restore(_read_parquet(path, filters, columns), fmt)


def _read_parquet(path: str, filters, columns: list[str] | None) -> pd.DataFrame:
    import pandas as pd
    if not is_url(path):
        return pd.read_parquet(path, filters=filters, columns=columns)
    fs, p = fs_for(path)
    return pd.read_parquet(p, filesystem=fs, filters=filters, columns=columns)


def write_table(table: pa.Table, path: str, row_group_size: int | None = None, **kw) -> None:
    """`pq.write_table`, URL-aware; `kw` (e.g. `compression`) passes through."""
    import pyarrow.parquet as pq
    if row_group_size:
        kw['row_group_size'] = row_group_size
    if not is_url(path):
        pq.write_table(table, path, **kw)
        return
    fs, p = fs_for(path)
    _ensure_parent(fs, p)
    pq.write_table(table, p, filesystem=fs, **kw)
    _known.add(path)


def row_group_sizes(path: str) -> list[int]:
    """Rows per row group, from the footer only (local or URL)."""
    import pyarrow.parquet as pq
    if not is_url(path):
        md = pq.read_metadata(path)
    else:
        fs, p = fs_for(path)
        md = pq.ParquetFile(p, filesystem=fs).metadata
    return [md.row_group(i).num_rows for i in range(md.num_row_groups)]


def _codec_of(pf) -> dict:
    """`ParquetWriter` kwargs reproducing an existing file's codec (first column
    chunk; our writers use one codec per file). Empty file → the writer default."""
    md = pf.metadata
    if not md.num_row_groups or not md.num_columns:
        return {}
    c = md.row_group(0).column(0).compression.lower()
    return {'compression': 'none' if c == 'uncompressed' else c}


def rewrite_row_groups(path: str, rows: int) -> None:
    """Rewrite a parquet file in place into ≤``rows``-row groups, streaming
    (one batch resident at a time, so a 130 MiB remote chunk never lands in
    memory whole) via a `.rg.tmp` sibling moved into place at the end."""
    import pyarrow.parquet as pq
    tmp = path + '.rg.tmp'
    if not is_url(path):
        src = pq.ParquetFile(path)
        # `schema_arrow` carries the key-value metadata; the codec stays the file's own.
        kw = _codec_of(src)
        with pq.ParquetWriter(tmp, src.schema_arrow, **kw) as w:
            for batch in src.iter_batches(batch_size=rows):
                w.write_batch(batch, row_group_size=rows)
        os.replace(tmp, path)
        return
    fs, p = fs_for(path)
    src = pq.ParquetFile(p, filesystem=fs)
    kw = _codec_of(src)
    with pq.ParquetWriter(p + '.rg.tmp', src.schema_arrow, filesystem=fs, **kw) as w:
        for batch in src.iter_batches(batch_size=rows):
            w.write_batch(batch, row_group_size=rows)
    fs.mv(p + '.rg.tmp', p)
    fs.invalidate_cache()


def write_parquet(df: pd.DataFrame, path: str, row_group_size: int) -> None:
    """`df.to_parquet(path, index=False, row_group_size=…)`, URL-aware."""
    if not is_url(path):
        df.to_parquet(path, index=False, row_group_size=row_group_size)
        return
    import pyarrow as pa
    write_table(pa.Table.from_pandas(df, preserve_index=False), path, row_group_size)


def read_text(path: str) -> str:
    if not is_url(path):
        with open(path) as f:
            return f.read()
    fs, p = fs_for(path)
    return fs.cat(p).decode()


def write_text(path: str, text: str) -> None:
    if not is_url(path):
        with open(path, 'w') as f:
            f.write(text)
        return
    write_bytes(path, text.encode())


def write_bytes(path: str, data: bytes) -> None:
    if not is_url(path):
        with open(path, 'wb') as f:
            f.write(data)
        return
    fs, p = fs_for(path)
    _ensure_parent(fs, p)
    fs.pipe(p, data)
    _known.add(path)


def remove(path: str) -> None:
    if not is_url(path):
        os.remove(path)
        return
    fs, p = fs_for(path)
    fs.rm(p)
    _known.discard(path)


def put(local_path: str, path: str) -> None:
    """Upload a local file to a URL path (an `adopt_parquet` into a remote dir)."""
    fs, p = fs_for(path)
    _ensure_parent(fs, p)
    fs.put(local_path, p)
    _known.add(path)


#: Parquet files beside a blob that annotate it (`sidecar.py`, `extents.py`,
#: `shallow.py`) — not scans, so never listed as blobs; kept/moved with it.
SIDECAR_SUFFIXES = ('.vocab.parquet', '.reclaim.parquet', '.shallow.parquet')


def list_parquets(d: str) -> list[str]:
    """Basenames of the `*.parquet` blobs in a scans dir (sidecars excluded)."""
    if not is_url(d):
        from glob import glob
        names = (os.path.basename(p) for p in glob(_local_join(d, '*.parquet')))
    else:
        fs, p = fs_for(d)
        names = (x.rsplit('/', 1)[-1] for x in fs.glob(f"{p.rstrip('/')}/*.parquet"))
    return sorted(n for n in names if not n.endswith(SIDECAR_SUFFIXES))
