"""Rewrite a v1 layer-2 listing as v2 in place, lossless, verify-then-swap
(spec `listing-slim.md` phase 2).

A v1 listing (no format keys in its parquet metadata) carries a `uri` column
that is exactly `<scan_root>/<path>` (the root itself at `.`) and, from
`--pivot-sum`, a `sum_<col>_<v>` column per pivot value — which for a
single-class bucket equals `size` on every row. :func:`recompress` drops both
kinds of redundancy and re-encodes under the switch codec
(`$DISK_TREE_PARQUET_CODEC`), so the file reads back through
`blobfs.read_parquet` as the frame it held before, column for column.

Everything streams by row group, so a multi-GB file never lands in memory
(the bound is one decoded *source* row group — `iter_batches` reads a group at
a time, up to 1M rows on the oldest blobs — plus one ≤64K-row output batch):

1. **Analyze** (one projected pass over the old file): derive the scan root
   from `uri`/`path` and check it on every row — a row whose `uri` is not
   `root/path` makes the file *not* a listing this tool understands, and it is
   refused rather than rewritten; find the `sum_*` columns equal to `size` on
   every row (same type, no NULLs) — those become `implied`; and fold the
   :func:`digest` of `(path, size, mtime, kind)`.
2. **Write** the kept columns to a `.v2.tmp` sibling, ≤64K-row groups, the
   v2 metadata keys (the old keys stay; a pandas key forgets the dropped
   columns).
3. **Verify**: the new file's row count and digest equal the old file's, and
   its format keys are the planned ones. Otherwise the temp is deleted, the
   old file untouched, and :class:`VerifyError` raised.
4. **Swap**: an atomic rename locally; `fs.mv` (copy + delete) on a URL.
   `keep` first moves the old file to `<stem>.v1.parquet`.

A v2 file whose codec is not the switch codec takes the same path from step 2
(its root and implied columns come from its keys, so step 1 is the digest
alone): same columns, same keys, re-encoded — how a codec flip reaches the
files already slimmed. A v2 file under the switch codec is skipped.

The digest is order-insensitive — the sum, mod 2**64, of a per-row 64-bit hash
(`pd.util.hash_pandas_object`) — folded one batch at a time, so its memory is
one batch regardless of file size. Every column but the dropped ones is copied
verbatim (`child_scan_id`, `mtime_mean`, the written pivots, …); the digest
columns are the four every consumer keys on.
"""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator

from . import blobfs
from . import listing_format as lf
from .storage.base import BLOB_ROW_GROUP_SIZE

if TYPE_CHECKING:
    import pyarrow as pa
    import pyarrow.parquet as pq

#: The columns the verify digest covers (those present in the file).
DIGEST_COLUMNS = ('path', 'size', 'mtime', 'kind')
TMP_SUFFIX = '.v2.tmp'
KEEP_SUFFIX = '.v1.parquet'
#: Parquet metadata keys the writer regenerates or that describe dropped columns.
_ARROW_SCHEMA_KEY = b'ARROW:schema'
_PANDAS_KEY = b'pandas'


class RecompressError(RuntimeError):
    """The file can't be rewritten losslessly as v2 (nothing was changed)."""


class VerifyError(RecompressError):
    """The rewritten file did not verify against the original (temp deleted, original kept)."""


@dataclass(frozen=True)
class Info:
    """What `listing-format` prints: a footer read."""
    path: str
    version: int
    codec: str
    row_groups: int
    rows: int
    size: int
    columns: tuple[str, ...]
    scan_root: str | None = None
    implied: dict[str, str] = field(default_factory=dict)

    @property
    def listing(self) -> bool:
        """A layer-2 listing: v2 by its marker, v1 by its `uri` + `path` columns
        (any other parquet — a layer-1 listing, a sidecar — lacks the marker too)."""
        return self.version >= 2 or {'uri', 'path'} <= set(self.columns)

    def asdict(self) -> dict:
        return {
            'path': self.path, 'listing': self.listing, 'version': self.version, 'codec': self.codec,
            'row_groups': self.row_groups, 'rows': self.rows, 'size': self.size, 'columns': list(self.columns),
            'scan_root': self.scan_root, 'implied': dict(self.implied),
        }


@dataclass(frozen=True)
class Result:
    path: str
    #: `rewritten` | `skipped` (already v2, under the switch codec) | `planned` (dry run)
    status: str
    old_size: int
    new_size: int | None = None
    rows: int = 0
    scan_root: str | None = None
    implied: dict[str, str] = field(default_factory=dict)
    #: where the old file went under `keep`
    kept: str | None = None
    #: the file's codec after this call (skipped: as found; planned: as it is now)
    codec: str | None = None
    #: a v2 file re-encoded (or, planned, to be re-encoded) under the switch codec: its old codec
    recoded: str | None = None

    @property
    def ratio(self) -> float | None:
        return None if self.new_size is None or not self.old_size else self.new_size / self.old_size

    def asdict(self) -> dict:
        return {
            'path': self.path, 'status': self.status, 'old_size': self.old_size, 'new_size': self.new_size,
            'ratio': self.ratio, 'rows': self.rows, 'scan_root': self.scan_root, 'implied': dict(self.implied),
            'kept': self.kept, 'codec': self.codec, 'recoded': self.recoded,
        }


@dataclass(frozen=True)
class Analysis:
    scan_root: str
    implied: dict[str, str]
    rows: int
    digest: int


def _open(path: str) -> tuple["pq.ParquetFile", object | None, str]:
    """`(ParquetFile, fs | None, path-within-fs)` for a local path or URL. A
    URL is read through `blobfs.open_read` (its cache named, no prefetcher);
    read it under `_opened`, which closes that handle — `ParquetFile.close()`
    alone leaves a caller-supplied source open."""
    import pyarrow.parquet as pq
    if not blobfs.is_url(path):
        return pq.ParquetFile(path), None, path
    fs, p = blobfs.fs_for(path)
    return pq.ParquetFile(blobfs.open_read(path)), fs, p


@contextmanager
def _opened(path: str) -> Iterator[tuple["pq.ParquetFile", object | None, str]]:
    pf, fs, p = _open(path)
    try:
        yield pf, fs, p
    finally:
        pf.close(force=True)


def _codec_name(md: "pq.FileMetaData") -> str:
    if not md.num_row_groups or not md.num_columns:
        return '-'
    return md.row_group(0).column(0).compression.lower()


def info(path: str) -> Info:
    """A file's format version, codec, row groups, rows and size — the footer only."""
    with _opened(path) as (pf, _, _):
        md = pf.metadata
        fmt = lf.parse(pf.schema_arrow.metadata)
        return Info(
            path=path, version=fmt.version, codec=_codec_name(md), row_groups=md.num_row_groups, rows=md.num_rows,
            size=blobfs.size(path), columns=tuple(pf.schema_arrow.names), scan_root=fmt.scan_root,
            implied=dict(fmt.implied),
        )


def _is_blob(path: str) -> bool:
    return path.endswith('.parquet') and not path.endswith(blobfs.SIDECAR_SUFFIXES) and not path.endswith(KEEP_SUFFIX)


def expand(paths: list[str]) -> list[str]:
    """Each argument as itself (a file) or its `*.parquet` files (a dir, recursive;
    sidecars, kept `.v1.parquet` files and temps excluded), local or URL, sorted."""
    out: list[str] = []
    for arg in paths:
        if not blobfs.is_url(arg):
            if os.path.isdir(arg):
                names = [os.path.join(root, f) for root, _, files in os.walk(arg) for f in files]
                out += sorted(n for n in names if _is_blob(n))
            elif os.path.exists(arg):
                out.append(arg)
            else:
                raise FileNotFoundError(arg)
            continue
        fs, p = blobfs.fs_for(arg)
        if arg.endswith('/') or fs.isdir(p):
            base, prefix = arg.rstrip('/'), fs._strip_protocol(p).rstrip('/')
            for f in sorted(fs.glob(f"{prefix}/**/*.parquet")):
                rel = f[len(prefix):].lstrip('/') if f.startswith(prefix) else f.rsplit('/', 1)[-1]
                if _is_blob(rel):
                    out.append(f'{base}/{rel}')
        elif fs.exists(p):
            out.append(arg)
        else:
            raise FileNotFoundError(arg)
    return out


def digest(batches: Iterator["pa.RecordBatch"], columns: tuple[str, ...] = DIGEST_COLUMNS) -> tuple[int, int]:
    """`(rows, sum mod 2**64 of the per-row hash of `columns`)` over `batches`
    (those of `columns` present, in this order), one batch resident at a time."""
    import numpy as np
    import pandas as pd
    import pyarrow as pa
    n = 0
    acc = np.uint64(0)
    cols: list[str] | None = None
    for batch in batches:
        if cols is None:
            cols = [c for c in columns if c in batch.schema.names]
        n += batch.num_rows
        if not batch.num_rows:
            continue
        # Hash from the Arrow values, not the file's pandas metadata (which the
        # rewrite edits): both sides then see the same dtypes.
        df = pa.Table.from_batches([batch.select(cols)]).replace_schema_metadata(None).to_pandas()
        with np.errstate(over='ignore'):
            acc = acc + pd.util.hash_pandas_object(df, index=False).to_numpy().sum(dtype=np.uint64)
    return n, int(acc)


def analyze(pf: "pq.ParquetFile", batch_rows: int = BLOB_ROW_GROUP_SIZE) -> Analysis:
    """One projected pass over a v1 file: the scan root (checked on every row),
    the `sum_*` columns equal to `size` on every row, and the digest."""
    import pyarrow as pa
    import pyarrow.compute as pc
    schema = pf.schema_arrow
    names = schema.names
    if 'uri' not in names or 'path' not in names:
        raise RecompressError("not a v1 layer-2 listing: no `uri` + `path` columns")
    has_size = 'size' in names
    candidates = [c for c in names if c.startswith('sum_') and has_size and schema.field(c).type == schema.field('size').type]
    read = [c for c in dict.fromkeys(['path', 'uri', 'size', *candidates, *DIGEST_COLUMNS]) if c in names]
    root: str | None = None
    implied = set(candidates)
    all_rows = lambda mask: pc.all(mask, skip_nulls=False).as_py() is True

    def batches():
        nonlocal root
        for batch in pf.iter_batches(batch_size=batch_rows, columns=read):
            if not batch.num_rows:
                continue
            path, uri = batch.column('path'), batch.column('uri')
            if path.null_count or uri.null_count:
                raise RecompressError("a NULL `path`/`uri`: not a layer-2 listing")
            if root is None:
                p0, u0 = path[0].as_py(), uri[0].as_py()
                if p0 == '.':
                    root = u0
                elif u0.endswith('/' + p0):
                    root = u0[: -len(p0) - 1]
                else:
                    raise RecompressError(f"row 0: uri {u0!r} is not `<root>/{p0}`")
            expected = pc.if_else(
                pc.equal(path, '.'), pa.scalar(root, type=path.type),
                pc.binary_join_element_wise(pa.scalar(root + '/', type=path.type), path, pa.scalar('', type=path.type)),
            )
            ok = pc.equal(uri, expected)
            if not all_rows(ok):
                bad = batch.filter(pc.invert(ok))
                raise RecompressError(
                    f"uri {bad.column('uri')[0].as_py()!r} is not `{root}/{bad.column('path')[0].as_py()}`: "
                    "not a listing under one scan root"
                )
            for c in list(implied):
                col = batch.column(c)
                if col.null_count or not all_rows(pc.equal(col, batch.column('size'))):
                    implied.discard(c)
            yield batch

    rows, dg = digest(batches())
    if root is None:
        raise RecompressError("empty file: no row to derive the scan root from")
    return Analysis(scan_root=root, implied={c: 'size' for c in sorted(implied)}, rows=rows, digest=dg)


def analyze_v2(pf: "pq.ParquetFile", fmt: lf.ListingFormat, batch_rows: int = BLOB_ROW_GROUP_SIZE) -> Analysis:
    """The digest pass over a v2 file (its root and implied columns are in its keys):
    what a re-encode under another codec verifies against."""
    dcols = tuple(c for c in DIGEST_COLUMNS if c in pf.schema_arrow.names)
    rows, dg = digest(pf.iter_batches(batch_size=batch_rows, columns=list(dcols)), dcols)
    return Analysis(scan_root=fmt.scan_root, implied=dict(fmt.implied), rows=rows, digest=dg)


def _v2_schema(schema: "pa.Schema", fmt: lf.ListingFormat, dropped: list[str]) -> "pa.Schema":
    """`schema` minus `dropped`, carrying `fmt`'s keys beside the old ones (less
    the writer-regenerated Arrow schema; a pandas key forgets the dropped columns)."""
    import pyarrow as pa
    md = dict(schema.metadata or {})
    md.pop(_ARROW_SCHEMA_KEY, None)
    if _PANDAS_KEY in md:
        pmd = json.loads(md[_PANDAS_KEY])
        pmd['columns'] = [c for c in pmd.get('columns', []) if c.get('name') not in dropped]
        md[_PANDAS_KEY] = json.dumps(pmd).encode()
    return lf.with_kv(pa.schema([f for f in schema if f.name not in dropped], metadata=md), fmt)


def _keep_name(s: str) -> str:
    return s[: -len('.parquet')] + KEEP_SUFFIX if s.endswith('.parquet') else s + KEEP_SUFFIX


def recompress(
    path: str,
    keep: bool = False,
    dry_run: bool = False,
    batch_rows: int = BLOB_ROW_GROUP_SIZE,
) -> Result:
    """Rewrite the v1 listing at `path` (local or URL) as v2 in place, or a v2
    listing under another codec as the switch codec; see the module doc."""
    import pyarrow.parquet as pq
    with _opened(path) as (pf, fs, p):
        old_size = blobfs.size(path)
        fmt0 = lf.parse(pf.schema_arrow.metadata)
        old_codec, codec = _codec_name(pf.metadata), lf.codec()
        if fmt0.version >= 2:
            if old_codec == codec:
                return Result(
                    path, 'skipped', old_size, rows=pf.metadata.num_rows, scan_root=fmt0.scan_root, implied=dict(fmt0.implied),
                    codec=old_codec,
                )
            # already slim: same columns and keys, re-encoded under the switch codec
            a, fmt, dropped, kept, recoded = analyze_v2(pf, fmt0, batch_rows), fmt0, [], list(pf.schema_arrow.names), old_codec
        else:
            a = analyze(pf, batch_rows)
            fmt = lf.slim(a.scan_root, pf.schema_arrow.names, a.implied)
            dropped, kept, recoded = ['uri', *a.implied], lf.written_columns(fmt), None
        if dry_run:
            return Result(path, 'planned', old_size, rows=a.rows, scan_root=a.scan_root, implied=a.implied, codec=old_codec, recoded=recoded)
        dcols = tuple(c for c in DIGEST_COLUMNS if c in kept)
        schema = _v2_schema(pf.schema_arrow, fmt, dropped)
        tmp, tmp_p = path + TMP_SUFFIX, p + TMP_SUFFIX
        kw = {'filesystem': fs} if fs is not None else {}
        with pq.ParquetWriter(tmp_p, schema, **kw, **lf.pyarrow_codec()) as w:
            for batch in pf.iter_batches(batch_size=batch_rows, columns=kept):
                w.write_batch(batch.select(kept), row_group_size=batch_rows)
    try:
        with _opened(tmp) as (new, _, _):
            rows, dg = digest(new.iter_batches(batch_size=batch_rows, columns=list(dcols)), dcols)
            got = lf.parse(new.schema_arrow.metadata)
        if rows != a.rows or dg != a.digest:
            raise VerifyError(
                f"{path}: rewritten file does not verify (rows {rows} vs {a.rows}, "
                f"digest {dg:#x} vs {a.digest:#x}); original kept"
            )
        if got != fmt:
            raise VerifyError(f"{path}: rewritten file's format keys {got} != {fmt}; original kept")
        new_size = blobfs.size(tmp)
    except BaseException:
        blobfs.remove(tmp)
        raise
    kept_path = _keep_name(path) if keep else None
    if fs is None:
        if keep:
            os.replace(path, kept_path)
        os.replace(tmp, path)
    else:
        if keep:
            fs.mv(p, _keep_name(p))
        fs.mv(tmp_p, p)
        fs.invalidate_cache()
    from .find.groups import groups_path, write_groups_sidecar
    if path.endswith('.parquet') and blobfs.exists(groups_path(path)):
        # the edge reader's precomputed footer describes the old row groups
        write_groups_sidecar(path)
    return Result(
        path, 'rewritten', old_size, new_size, rows=a.rows, scan_root=a.scan_root, implied=a.implied, kept=kept_path,
        codec=codec, recoded=recoded,
    )
