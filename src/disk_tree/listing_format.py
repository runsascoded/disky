"""Layer-2 listing format versions (spec `listing-slim.md`).

A layer-2 listing (the scan blob `import -e duckdb|stream` writes, and the
tiers cut from it) comes in two shapes:

- **v1** (no marker): every row carries `uri` (`<scan_root>/<path>`, the
  scan root itself at `.`), and `--pivot-sum` writes one `sum_<col>_<v>`
  column per value present. Snappy.
- **v2** (`disk_tree.listing_format = '2'` in the parquet key-value
  metadata): no `uri` column — the scan root is in the metadata and `uri` is
  derived at read time. A pivot column that equals `size` on every row (the
  pivot column held exactly one value and no NULLs, e.g. a single-storage-class
  bucket) is not written; the metadata's `implied` map names it and the column
  it equals, so readers restore it exactly.

The codec is a separate switch (:func:`codec`, env `DISK_TREE_PARQUET_CODEC`,
default zstd since every reader decodes it — the site's `/files` viewer,
`@rdub/file-tree`, was the last; `snappy` opts back out) governing the layer-2
listing, the engine tiers and the overlay's served indexes (spec
`listing-slim.md`). A reader's decoder must ship before its writer flips: a
site deploy before the job image that writes zstd.

Readers see the v1 shape through :func:`restore` (`blobfs.read_parquet` does
this for every blob read), so nothing downstream needs to know which one it
got. `columns` records the v1 column order so the restored frame is
column-for-column the frame a v1 writer would have produced.

Writers: the duckdb engine COPYs with :func:`duckdb_copy_options`, the stream
engine's finalize stamps :func:`with_kv` on its schema, and every in-memory
writer (local `index`, `import -e pandas`, the hybrid backend's chunk and
delete rewrites) goes through
:func:`write_listing`, which slims a v1 frame (:func:`slim_table`) and
writes it with the switch codec.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd
    import pyarrow as pa

FORMAT_KEY = 'disk_tree.listing_format'
SCAN_ROOT_KEY = 'disk_tree.scan_root'
IMPLIED_KEY = 'disk_tree.implied'
COLUMNS_KEY = 'disk_tree.columns'

LISTING_FORMAT = 2

#: The one codec switch (see module doc): `zstd` (default) | `snappy` (opt-out).
CODEC_VAR = 'DISK_TREE_PARQUET_CODEC'
CODECS = ('snappy', 'zstd')
ZSTD_LEVEL = 3


def codec() -> str:
    """The parquet codec the listing/tier/index writers use (`$DISK_TREE_PARQUET_CODEC`)."""
    c = os.environ.get(CODEC_VAR, 'zstd').strip().lower() or 'zstd'
    if c not in CODECS:
        raise ValueError(f"${CODEC_VAR}={c!r}: expected one of {CODECS}")
    return c


def duckdb_codec() -> str:
    """The codec clause for a DuckDB `COPY … (FORMAT PARQUET, <this>, …)`."""
    c = codec()
    return f'COMPRESSION {c}' + (f', COMPRESSION_LEVEL {ZSTD_LEVEL}' if c == 'zstd' else '')


def pyarrow_codec() -> dict:
    """`pq.ParquetWriter` / `write_table` kwargs for :func:`codec`."""
    c = codec()
    return {'compression': c, **({'compression_level': ZSTD_LEVEL} if c == 'zstd' else {})}


@dataclass(frozen=True)
class ListingFormat:
    version: int = 1
    scan_root: str | None = None
    #: dropped column → the column it equals on every row
    implied: dict[str, str] = field(default_factory=dict)
    #: the v1 column order (None: unknown / v1 file)
    columns: tuple[str, ...] | None = None

    def kv(self) -> dict[str, str]:
        """This format's parquet key-value metadata."""
        if self.version < 2:
            return {}
        if self.scan_root is None:
            raise ValueError("a v2 listing needs its scan root")
        kv = {FORMAT_KEY: str(self.version), SCAN_ROOT_KEY: self.scan_root}
        if self.implied:
            kv[IMPLIED_KEY] = json.dumps(self.implied, sort_keys=True, separators=(',', ':'))
        if self.columns is not None:
            kv[COLUMNS_KEY] = json.dumps(list(self.columns), separators=(',', ':'))
        return kv


V1 = ListingFormat()


def slim(scan_root: str, v1_columns: list[str], implied: dict[str, str] | None = None) -> ListingFormat:
    """The v2 format for a listing whose v1 columns would be `v1_columns`."""
    return ListingFormat(LISTING_FORMAT, scan_root, dict(implied or {}), tuple(v1_columns))


def written_columns(fmt: ListingFormat) -> list[str]:
    """The columns a writer of `fmt` emits (v1 order minus `uri` and the implied ones)."""
    if fmt.columns is None:
        raise ValueError("format has no column list")
    if fmt.version < 2:
        return list(fmt.columns)
    return [c for c in fmt.columns if c != 'uri' and c not in fmt.implied]


def parse(metadata: dict | None) -> ListingFormat:
    """The format from a parquet/Arrow schema's key-value metadata (bytes or str keys)."""
    if not metadata:
        return V1
    md = {(k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v) for k, v in metadata.items()}
    if FORMAT_KEY not in md:
        return V1
    version = int(md[FORMAT_KEY])
    if version > LISTING_FORMAT:
        raise ValueError(f"listing format {version} is newer than this reader ({LISTING_FORMAT})")
    return ListingFormat(
        version=version,
        scan_root=md.get(SCAN_ROOT_KEY),
        implied=json.loads(md[IMPLIED_KEY]) if IMPLIED_KEY in md else {},
        columns=tuple(json.loads(md[COLUMNS_KEY])) if COLUMNS_KEY in md else None,
    )


def format_of(path: "str | os.PathLike") -> ListingFormat:
    """The format of the parquet at `path` (local or URL) — a footer read."""
    from . import blobfs
    return parse(blobfs.read_schema(os.fspath(path)).metadata)


def readable_columns(schema) -> list[str]:
    """The columns `blobfs.read_parquet` can return for a file with this
    (Arrow) schema — its own plus, for a v2 listing, the derived ones (`uri`,
    the implied pivots), in the v1 order. A reader that intersects a wanted
    column list with a file's columns must use this, not `schema.names`, or a
    v2 file loses `uri` from the projection."""
    fmt = parse(schema.metadata)
    names = list(schema.names)
    if fmt.version < 2:
        return names
    derived = ['uri'] if 'uri' not in names and 'path' in names else []
    derived += [c for c, src in fmt.implied.items() if c not in names and src in names]
    if fmt.columns is None:
        return names + derived
    have = set(names) | set(derived)
    order = [c for c in fmt.columns if c in have]
    return order + [c for c in names if c not in order]


def uri_prefix(scan_root: str) -> str:
    """What a non-root row's `path` is appended to for its `uri`: `<root>/`, or the
    root itself when it already ends with `/` (the filesystem root: `/` + `foo`
    is `/foo`, not `//foo`). Bucket roots and `abspath`ed local roots never end
    with `/`."""
    return scan_root if scan_root.endswith('/') else f'{scan_root}/'


def uri_of(scan_root: str, path: str) -> str:
    """A row's v1 `uri`: the root at `.`, else `root/path` (see :func:`uri_prefix`)."""
    return scan_root if path == '.' else uri_prefix(scan_root) + path


def uri_sql(scan_root: str, col: str = 'path') -> str:
    """DuckDB expression deriving a row's `uri` from its path (`.` = the root)."""
    esc = lambda s: s.replace("'", "''")
    return f"CASE WHEN {col} = '.' THEN '{esc(scan_root)}' ELSE '{esc(uri_prefix(scan_root))}' || {col} END"


def restore(df: "pd.DataFrame", fmt: ListingFormat) -> "pd.DataFrame":
    """The v1 view of a frame read from a `fmt` listing: add `uri` and the implied
    columns (only those missing — a projection that left them out stays out when
    their source column is absent too), in the recorded v1 order. v1 frames pass
    through untouched."""
    if fmt.version < 2:
        return df
    add = {}
    if 'uri' not in df.columns and 'path' in df.columns:
        root = fmt.scan_root
        path = df['path']
        add['uri'] = (uri_prefix(root) + path).where(path != '.', root)
    for col, src in fmt.implied.items():
        if col not in df.columns and src in df.columns:
            add[col] = df[src]
    if not add:
        return df
    df = df.assign(**add)
    if fmt.columns is not None:
        order = [c for c in fmt.columns if c in df.columns]
        rest = [c for c in df.columns if c not in order]
        df = df[order + rest]
    return df


def duckdb_copy_options(fmt: ListingFormat, row_group_size: int | None = None, extra_kv: dict | None = None) -> str:
    """`COPY … TO` options writing `fmt` (the :func:`codec` + its key-value metadata)."""
    opts = ['FORMAT PARQUET', duckdb_codec()]
    if row_group_size:
        opts.append(f'ROW_GROUP_SIZE {row_group_size}')
    kv = {**fmt.kv(), **(extra_kv or {})}
    if kv:
        esc = lambda s: str(s).replace("'", "''")
        opts.append('KV_METADATA {' + ', '.join(f"'{esc(k)}': '{esc(v)}'" for k, v in kv.items()) + '}')
    return f"({', '.join(opts)})"


def with_kv(schema, fmt: ListingFormat):
    """`schema` carrying `fmt`'s key-value metadata (existing keys kept)."""
    kv = fmt.kv()
    if not kv:
        return schema
    return schema.with_metadata({**(schema.metadata or {}), **{k.encode(): v.encode() for k, v in kv.items()}})


def _pandas_metadata_without(metadata: dict | None, dropped: set[str]) -> dict:
    """`metadata` with the dropped columns taken out of its `pandas` entry (what
    `Table.from_pandas` would have recorded for the slimmed frame), so the file
    describes the columns it holds."""
    md = dict(metadata or {})
    if b'pandas' in md:
        pm = json.loads(md[b'pandas'])
        pm['columns'] = [c for c in pm['columns'] if c.get('field_name', c.get('name')) not in dropped]
        md[b'pandas'] = json.dumps(pm).encode()
    return md


def slim_table(table: "pa.Table", scan_root: str | None = None, where: str = '') -> "tuple[pa.Table, ListingFormat]":
    """The v2 table for an in-memory v1 listing `table` (what the pandas engines
    build: every row carries `uri`), and its format.

    The scan root is `scan_root`, else the `uri` of the `.` row. Slimming is
    lossless by construction only when every row's `uri` is
    :func:`uri_of` `(root, path)`, so that is checked, vectorized, on every row;
    a table that fails it (or has no `uri`, or no root row to name the root)
    comes back untouched with :data:`V1` — a v1 blob is always a correct
    fallback, since readers accept both — and the check failure is reported on
    stderr (`where` names the destination). A `sum_*` column equal to `size`
    on every row (a single-valued `--pivot-sum` class) is left implied.
    """
    import pyarrow as pa
    import pyarrow.compute as pc
    names = table.column_names
    if 'uri' not in names or 'path' not in names:
        return table, V1
    path, uri = table['path'], table['uri']
    if scan_root is None:
        i = pc.index(path, pa.scalar('.', path.type)).as_py()
        if i < 0:
            return table, V1
        scan_root = uri[i].as_py()
        if scan_root is None:
            return table, V1
    t = path.type
    expect = pc.if_else(
        pc.equal(path, pa.scalar('.', t)),
        pa.scalar(scan_root, t),
        pc.binary_join_element_wise(pa.scalar(uri_prefix(scan_root), t), path, pa.scalar('', t)),
    )
    if not pc.all(pc.equal(expect.cast(uri.type), uri), skip_nulls=False).as_py():
        print(
            f"{where + ': ' if where else ''}`uri` is not `<scan root>/<path>` on every row "
            f"(root {scan_root!r}); writing the v1 listing format", file=sys.stderr,
        )
        return table, V1
    implied: dict[str, str] = {}
    if 'size' in names:
        size = table['size']
        for c in names:
            if c.startswith('sum_') and table[c].type == size.type and pc.all(pc.equal(table[c], size), skip_nulls=False).as_py():
                implied[c] = 'size'
    fmt = slim(scan_root, names, implied)
    keep = written_columns(fmt)
    out = table.select(keep)
    md = _pandas_metadata_without(out.schema.metadata, set(names) - set(keep))
    return out.replace_schema_metadata({**md, **{k.encode(): v.encode() for k, v in fmt.kv().items()}}), fmt


def write_listing(
    data: "pd.DataFrame | pa.Table",
    path: str,
    row_group_size: int | None = None,
    scan_root: str | None = None,
) -> ListingFormat:
    """Write an in-memory listing (a v1 frame or Arrow table) to `path` (local or
    URL) as v2 (:func:`slim_table`) with the switch :func:`codec`. Returns the
    format written — :data:`V1` when the table could not be slimmed."""
    import pyarrow as pa
    from . import blobfs
    table = data if isinstance(data, pa.Table) else pa.Table.from_pandas(data, preserve_index=False)
    table, fmt = slim_table(table, scan_root, where=path)
    blobfs.write_table(table, path, row_group_size, **pyarrow_codec())
    return fmt
