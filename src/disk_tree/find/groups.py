"""Group manifest beside a sorted-parquet tier: ``<tier>.groups.json``.

A serverless reader (mgu's Pages Functions today; DT's own Functions do the
same over the scan blob) plans an HTTP range read by row-group statistics —
but a fine tier's footer is thousands of thrift-encoded row groups, more than
a cold Worker isolate can parse under its memory cap. So the footer is
precomputed once, at write time, as one compact JSON document beside the
parquet, holding exactly what a read needs and nothing else:

- ``schema``: hyparquet's ``FileMetaData.schema`` — the root element plus one
  leaf per column (physical type, repetition, converted type, name);
- ``groups``: one compact array per row group, in this order::

    [rg, d_min, d_max, p_min, p_max, b_max, u_min, u_max, row_start, row_end, rg_json, b_min]

  — the pruning stats (``depth`` / ``path`` / size / user column min-max, the
  group's absolute row span), ``rg_json``, the stripped ``RowGroup`` the
  reader revives into a subset ``FileMetaData``, and ``b_min`` (appended:
  with ``b_max`` it is a ``bysize`` tier's bucket range per group, spec
  ``path-store.md`` §1.3; D1's ``index_row_groups`` has no column for it, so
  it is sidecar-only, and the positional readers ignore the tail)::

    rg_json = [num_rows, codec, [[data_page_offset, total_compressed_size, dictionary_page_offset|0], …]]

  one triple per leaf column in schema order (hyparquet reads nothing else
  from a column chunk);
- ``floor_bytes``: a coarse tier's floor, from the parquet key-value metadata
  (``floor_bytes``, or mgu's ``coarse_floor``), so a planner can pick tiers
  without touching a file; ``null`` for the path store's sorts, which have
  no floor (every byte floor is a prefix of ``bysize``).

Beside it, the **cold footer tier** ``<tier>.groups.parquet`` holds the same
rows as a small parquet (spec ``path-store.md`` §1.6): one row per tier row
group, columns :data:`FOOTER_COLS` typed (ints; strings; ``u_min`` /
``u_max`` nullable), in ``rg`` order — the tier's own key order, so each
footer group bounds a contiguous key range — in
:data:`FOOTER_ROW_GROUP_ROWS`-row groups, zstd, with column statistics on
the pruning columns (:data:`FOOTER_STAT_COLS`). A reader range-reads its
footer, prunes the footer's own row groups by those stats with the predicates
it sends D1, and decodes only the groups that can hold a match: how a
deployment serves a scan once retention retires its rows from D1
(``site/functions/_lib/index.ts`` ``pq`` mode), without parsing the whole
``.groups.json`` (86 MB on gcs) in a Worker. The key-value metadata carries
what ``index_schema`` holds for the tier (``groups_v``, ``version``,
``schema``, ``floor_bytes``), so the file is self-describing
(:func:`read_groups_parquet`).

This is the wire format of mgu's ``index_footer.py`` (``groups_blob``), owned
here so the serverless reader (``site/functions/_lib/index.ts`` ``openBlob``)
and the engine converge on one artifact (spec
``specs/done/mgu-engine-audit-2026-09-07.md`` §4). Column *names* differ between the two
producers (DT tiers carry ``size``, mgu's path index ``b``; the user slice is
``usr`` in both when present), so the size and user columns are resolved by
name with those defaults; the array layout and field order never change.
About 250 bytes per group.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from disk_tree import blobfs

if TYPE_CHECKING:
    import pyarrow as pa
    import pyarrow.parquet as pq

GROUPS_SUFFIX = '.groups.json'
GROUPS_VERSION = 1
GROUPS_PARQUET_SUFFIX = '.groups.parquet'
GROUPS_PARQUET_VERSION = 1
#: Rows per row group of a `.groups.parquet` — the reader's decode unit: 512
#: footer rows ≈ 4M (8K-row tier groups) to 16M (32K) tier rows of key range
#: per decode; a 95K-group gcs sort has ~186 footer groups.
FOOTER_ROW_GROUP_ROWS = 512
#: The `.groups.parquet` columns, in file order (readers name columns).
FOOTER_COLS = ('rg', 'd_min', 'd_max', 'p_min', 'p_max', 'b_min', 'b_max', 'u_min', 'u_max', 'row_start', 'row_end', 'rg_json')
#: The columns with statistics — what a reader prunes footer groups by.
FOOTER_STAT_COLS = ('d_min', 'd_max', 'p_min', 'p_max', 'b_min', 'b_max', 'u_min', 'u_max')
#: Field order of each ``groups`` array entry — mgu's ``index_row_groups`` column
#: order, then the appended ``b_min``.
GROUP_FIELDS = ('rg', 'd_min', 'd_max', 'p_min', 'p_max', 'b_max', 'u_min', 'u_max', 'row_start', 'row_end', 'rg_json', 'b_min')
#: Size column candidates, first present wins: DT layer-2 / tiers, then mgu's path index.
SIZE_COLS = ('size', 'b')
#: The user-slice column (item B's ``--label`` default), when the tier has one.
USER_COL = 'usr'
#: Key-value metadata keys a coarse tier's floor may be under (DT, then mgu).
FLOOR_KEYS = (b'floor_bytes', b'coarse_floor')


@dataclass(frozen=True)
class GroupsStats:
    path: str
    n_groups: int
    n_bytes: int


def groups_path(parquet_path: str) -> str:
    """``…/gcs-b1.dirs.parquet`` → ``…/gcs-b1.dirs.groups.json`` (local or URL)."""
    if not parquet_path.endswith('.parquet'):
        raise ValueError(f"not a parquet path: {parquet_path}")
    return parquet_path[: -len('.parquet')] + GROUPS_SUFFIX


def read_metadata(parquet_path: str) -> "pq.FileMetaData":
    """The footer of a local or URL parquet (one range read remotely)."""
    import pyarrow.parquet as pq
    if not blobfs.is_url(parquet_path):
        return pq.read_metadata(parquet_path)
    fs, p = blobfs.fs_for(parquet_path)
    with fs.open(p, 'rb') as f:
        return pq.ParquetFile(f).metadata


def schema_json(md: "pq.FileMetaData") -> dict:
    """hyparquet ``FileMetaData.schema``: a root element + one leaf per column.
    The root's name isn't load-bearing (reads key off each leaf's
    ``path_in_schema``); it is the stable placeholder ``schema``."""
    sch = md.schema
    leaves = []
    for i in range(md.num_columns):
        col = sch.column(i)
        el: dict = {
            'type': col.physical_type,
            'repetition_type': 'OPTIONAL' if col.max_definition_level else 'REQUIRED',
            'name': col.name,
        }
        ct = col.converted_type
        if ct and ct != 'NONE':
            el['converted_type'] = ct
        leaves.append(el)
    root = {'repetition_type': 'REQUIRED', 'name': 'schema', 'num_children': md.num_columns}
    out: dict = {'version': 1, 'schema': [root, *leaves]}
    kv = md.metadata or {}
    for k in FLOOR_KEYS:
        if k in kv:
            out['floor_bytes'] = int(kv[k])
            break
    return out


def _minmax(stats) -> tuple:
    """A column's row-group (min, max), or (None, None) when the group has no
    stats (all-NULL). Bytes decode to str for JSON."""
    if stats is None or not stats.has_min_max:
        return None, None

    def s(v):
        return v.decode() if isinstance(v, bytes) else v
    return s(stats.min), s(stats.max)


def group_rows(md: "pq.FileMetaData") -> list[dict]:
    """One dict per row group (keys = :data:`GROUP_FIELDS`): pruning stats +
    the stripped ``rg_json``. Requires ``depth`` and ``path`` columns and one
    of :data:`SIZE_COLS`; ``u_min``/``u_max`` are ``None`` without a
    :data:`USER_COL`."""
    names = md.schema.names
    for need in ('depth', 'path'):
        if need not in names:
            raise ValueError(f"no `{need}` column in {names}")
    size_col = next((c for c in SIZE_COLS if c in names), None)
    if size_col is None:
        raise ValueError(f"no size column ({'/'.join(SIZE_COLS)}) in {names}")
    di, pi, bi = names.index('depth'), names.index('path'), names.index(size_col)
    ui = names.index(USER_COL) if USER_COL in names else None

    rows: list[dict] = []
    row_start = 0
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        n = rg.num_rows
        chunks = [rg.column(c) for c in range(rg.num_columns)]
        codecs = {cc.compression for cc in chunks}
        if len(codecs) != 1:
            raise ValueError(f"row group {g}: mixed codecs {sorted(codecs)} (one codec per group assumed)")
        cols = [[cc.data_page_offset, cc.total_compressed_size, cc.dictionary_page_offset or 0] for cc in chunks]
        d_min, d_max = _minmax(chunks[di].statistics)
        p_min, p_max = _minmax(chunks[pi].statistics)
        b_min, b_max = _minmax(chunks[bi].statistics)
        u_min, u_max = _minmax(chunks[ui].statistics) if ui is not None else (None, None)
        rows.append({
            'rg': g,
            'd_min': int(d_min), 'd_max': int(d_max),
            'p_min': p_min, 'p_max': p_max,
            'b_max': int(b_max),
            'u_min': u_min, 'u_max': u_max,
            'row_start': row_start, 'row_end': row_start + n,
            'rg_json': json.dumps([n, codecs.pop(), cols], separators=(',', ':')),
            'b_min': int(b_min),
        })
        row_start += n
    return rows


def extract(parquet_path: str) -> tuple[dict, list[dict]]:
    """``(schema_json, group_rows)`` of a local or URL parquet."""
    md = read_metadata(parquet_path)
    return schema_json(md), group_rows(md)


def groups_json(schema: dict, rows: list[dict]) -> str:
    """The manifest document, compact: ``{v, version, schema, floor_bytes, groups}``
    with each group as an array in :data:`GROUP_FIELDS` order."""
    groups = [[r[k] for k in GROUP_FIELDS] for r in rows]
    body = {
        'v': GROUPS_VERSION,
        'version': schema['version'],
        'schema': schema['schema'],
        'floor_bytes': schema.get('floor_bytes'),
        'groups': groups,
    }
    return json.dumps(body, separators=(',', ':'))


def write_groups(parquet_path: str, footer_parquet: bool = False) -> GroupsStats:
    """Extract ``parquet_path``'s footer and write the manifest beside it
    (local or URL, via :mod:`disk_tree.blobfs`); with ``footer_parquet``, the
    cold footer tier (`.groups.parquet`) too, from the same rows."""
    schema, rows = extract(parquet_path)
    text = groups_json(schema, rows)
    out = groups_path(parquet_path)
    blobfs.write_text(out, text)
    if footer_parquet:
        write_groups_parquet(parquet_path, schema, rows)
    return GroupsStats(path=out, n_groups=len(rows), n_bytes=len(text.encode()))


def groups_parquet_path(parquet_path: str) -> str:
    """``…/path-index.parquet`` → ``…/path-index.groups.parquet`` (local or URL)."""
    if not parquet_path.endswith('.parquet') or parquet_path.endswith(GROUPS_PARQUET_SUFFIX):
        raise ValueError(f"not a tier parquet path: {parquet_path}")
    return parquet_path[: -len('.parquet')] + GROUPS_PARQUET_SUFFIX


def _footer_schema() -> "pa.Schema":
    import pyarrow as pa
    return pa.schema([
        pa.field('rg', pa.int32(), nullable=False),
        pa.field('d_min', pa.int32(), nullable=False),
        pa.field('d_max', pa.int32(), nullable=False),
        pa.field('p_min', pa.string(), nullable=False),
        pa.field('p_max', pa.string(), nullable=False),
        pa.field('b_min', pa.int64()),  # null in a pre-`b_min` .groups.json
        pa.field('b_max', pa.int64(), nullable=False),
        pa.field('u_min', pa.string()),
        pa.field('u_max', pa.string()),
        pa.field('row_start', pa.int64(), nullable=False),
        pa.field('row_end', pa.int64(), nullable=False),
        pa.field('rg_json', pa.string(), nullable=False),
    ])


def groups_parquet_table(schema: dict, rows: list[dict]) -> "pa.Table":
    """The footer rows as a typed table in ``rg`` order (they must be
    ``0..n-1``, each once), the tier's ``index_schema`` fields in the
    key-value metadata."""
    import pyarrow as pa
    rows = sorted(rows, key=lambda r: r['rg'])
    if [r['rg'] for r in rows] != list(range(len(rows))):
        raise ValueError("footer rows must be rg 0..n-1, each once")
    t = pa.table({c: [r[c] for r in rows] for c in FOOTER_COLS}, schema=_footer_schema())
    kv = {
        'groups_v': str(GROUPS_PARQUET_VERSION),
        'version': str(schema['version']),
        'schema': json.dumps(schema['schema'], separators=(',', ':')),
    }
    if schema.get('floor_bytes') is not None:
        kv['floor_bytes'] = str(int(schema['floor_bytes']))
    return t.replace_schema_metadata(kv)


def groups_parquet_bytes(schema: dict, rows: list[dict], row_group_rows: int = FOOTER_ROW_GROUP_ROWS) -> bytes:
    """The `.groups.parquet` file: zstd, ``row_group_rows`` rows per group,
    statistics on :data:`FOOTER_STAT_COLS` only (stats on ``rg_json`` would
    only bloat the footer a reader parses), no Arrow schema blob."""
    import io

    import pyarrow.parquet as pq
    t = groups_parquet_table(schema, rows)
    buf = io.BytesIO()
    # `store_schema=False` drops the schema's key-value metadata with the
    # Arrow blob; it goes back in as plain parquet key-values.
    with pq.ParquetWriter(buf, t.schema, compression='zstd', write_statistics=list(FOOTER_STAT_COLS), store_schema=False) as w:
        w.write_table(t, row_group_size=row_group_rows)
        w.add_key_value_metadata({k.decode(): v.decode() for k, v in t.schema.metadata.items()})
    return buf.getvalue()


def write_groups_parquet(parquet_path: str, schema: dict, rows: list[dict], row_group_rows: int = FOOTER_ROW_GROUP_ROWS) -> GroupsStats:
    """Write the cold footer tier beside ``parquet_path`` (local or URL)."""
    out = groups_parquet_path(parquet_path)
    data = groups_parquet_bytes(schema, rows, row_group_rows)
    blobfs.write_bytes(out, data)
    return GroupsStats(path=out, n_groups=len(rows), n_bytes=len(data))


def read_groups_parquet(path: str) -> tuple[dict, list[dict]]:
    """``(schema_json, group_rows)`` back from a `.groups.parquet` — the
    inverse of :func:`groups_parquet_table`."""
    import pyarrow.parquet as pq
    def read(f) -> tuple:
        pf = pq.ParquetFile(f)
        return pf.read(), pf.metadata.metadata or {}

    if blobfs.is_url(path):
        fs, p = blobfs.fs_for(path)
        with fs.open(p, 'rb') as f:
            t, raw = read(f)
    else:
        t, raw = read(path)
    kv = {k.decode(): v.decode() for k, v in raw.items()}
    if kv.get('groups_v') != str(GROUPS_PARQUET_VERSION):
        raise ValueError(f"{path}: not a v{GROUPS_PARQUET_VERSION} groups parquet (groups_v={kv.get('groups_v')!r})")
    schema: dict = {'version': int(kv['version']), 'schema': json.loads(kv['schema'])}
    if 'floor_bytes' in kv:
        schema['floor_bytes'] = int(kv['floor_bytes'])
    return schema, t.to_pylist()


def groups_from_json(text: str) -> tuple[dict, list[dict]]:
    """``(schema_json, group_rows)`` back from a `.groups.json` document — what
    a backfill builds a `.groups.parquet` from without the tier's footer."""
    body = json.loads(text)
    if body.get('v') != GROUPS_VERSION:
        raise ValueError(f"unknown groups.json version {body.get('v')!r}")
    schema: dict = {'version': body['version'], 'schema': body['schema']}
    if body.get('floor_bytes') is not None:
        schema['floor_bytes'] = int(body['floor_bytes'])
    return schema, [{'b_min': None, **dict(zip(GROUP_FIELDS, g))} for g in body['groups']]


def write_groups_sidecar(parquet_path: str) -> GroupsStats | None:
    """Emit the ``.groups.json`` footer beside a freshly-published blob, so the
    serverless reader (``site/functions/_lib/index.ts``) plans range reads
    without a cold thrift-footer parse. It is a pure *optimization*: the blob still reads through its own
    footer if the sidecar is absent, so a failure here (a blob without row-group
    stats, a transient write error) is non-fatal — warn and return ``None``
    rather than abort a publish whose scan blob + manifest are already valid."""
    try:
        return write_groups(parquet_path)
    except Exception as e:
        from utz import err
        err(f"groups.json sidecar skipped for {parquet_path}: {type(e).__name__}: {e}")
        return None
