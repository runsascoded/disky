"""The path store's search index (spec ``path-store-search.md``): what the
site's filter view (``q=``) reads to find match roots without scanning rows.

Built from a generation's ``path`` sort (``path-index.parquet``), three files
beside it (layout v2, spec §2):

- ``<stem>.rows.parquet`` — the store's rows **name-major**: ``id`` (the
  row's last path segment's vocabulary id) then every column of the ``path``
  sort, sorted ``(id, path-sort order)`` in small row groups. Ids are an
  impact order (names by ``Σ size desc, name``), so a name's rows are
  contiguous and a candidate name is verified and its rows read in one place;
- ``<stem>.trigrams.parquet`` — postings ``(tri, id)`` sorted ``(tri, id)``:
  every all-ASCII trigram of DuckDB ``lower(name)``, packed ``c0<<16 | c1<<8 |
  c2``;
- ``<stem>.rows-search.parquet`` — the directory: one row per row group of
  both data files (``file`` 0 = rows / 1 = trigrams, ``rg``, ``row_start``,
  ``row_end``, key range ``k_min``/``k_max`` = ``id`` / ``tri``, the compact
  ``rg_json`` a reader revives), statistics on the key columns, the data
  files' flat schemas and the build's counts in the key-value metadata.

A reader opens the directory's footer, prunes its groups by those stats,
then range-reads only the postings / rows groups a query needs — never a
data file's own footer (a gcs-sized postings footer is ~10 MB of thrift).

Layout v1 (``<stem>.names.parquet`` + ``<stem>.search.parquet``: the
vocabulary with each name's ``path``-sort row groups) is no longer written;
the site still reads generations that have it. The v2 directory has its own
name so a v1-only reader finds no directory (and reads as if unindexed)
instead of failing on a layout it doesn't know.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

SEARCH_V = 2
ROWS_SUFFIX = '.rows.parquet'
TRIGRAMS_SUFFIX = '.trigrams.parquet'
SEARCH_SUFFIX = '.rows-search.parquet'
#: Rows per rows-file row group — the reader's decode unit. Candidates are
#: scattered in impact order, so a query decodes about one group per
#: candidate name: smaller groups decode less (r2, `2019`: 154 groups / 158K
#: rows at 1024 vs 124 / 254K at 2048) at the cost of directory rows.
ROWS_RG_ROWS = 1024
#: Rows per postings row group (~40 KB of sorted ids).
POSTINGS_RG_ROWS = 16384
#: Rows per directory row group (the `.groups.parquet` default).
DIR_RG_ROWS = 512
ROW_GROUP_STEP = 2048
#: Directory columns with statistics (what a reader prunes by).
DIR_STAT_COLS = ('file', 'rg', 'k_min', 'k_max')
#: Rows-file columns written with a dictionary (few distinct values); the
#: rest (`path` above all: unique strings) are plain.
ROWS_DICT_COLS = ('usr', 'kind')
FILE_ROWS = 0
FILE_TRIGRAMS = 1


@dataclass
class SearchStats:
    names: int
    rows: int
    postings: int
    path_groups: int
    files: dict[str, str]


def search_paths(path_sort: str) -> dict[str, str]:
    """``…/path-index.parquet`` → its three sidecars, by role."""
    if not path_sort.endswith('.parquet'):
        raise ValueError(f"not a parquet path: {path_sort}")
    stem = path_sort[: -len('.parquet')]
    return {'rows': stem + ROWS_SUFFIX, 'trigrams': stem + TRIGRAMS_SUFFIX, 'search': stem + SEARCH_SUFFIX}


def tri_code(t: str) -> int:
    """A 3-character ASCII trigram as the int the postings store."""
    if len(t) != 3 or any(ord(c) > 127 for c in t):
        raise ValueError(f"not an ASCII trigram: {t!r}")
    return (ord(t[0]) << 16) | (ord(t[1]) << 8) | ord(t[2])


def _q(s: str) -> str:
    return s.replace("'", "''")


def _rg_dir_rows(md: "pq.FileMetaData", file: int, key: str) -> list[dict]:
    """One directory row per row group of a data file: rows, key range, compact `rg_json`."""
    ki = md.schema.names.index(key)
    out = []
    start = 0
    for g in range(md.num_row_groups):
        rg = md.row_group(g)
        chunks = [rg.column(c) for c in range(rg.num_columns)]
        codecs = {cc.compression for cc in chunks}
        if len(codecs) != 1:
            raise ValueError(f"row group {g}: mixed codecs {sorted(codecs)}")
        st = chunks[ki].statistics
        if st is None or not st.has_min_max:
            raise ValueError(f"row group {g}: no `{key}` statistics")
        n = rg.num_rows
        cols = [[cc.data_page_offset, cc.total_compressed_size, cc.dictionary_page_offset or 0] for cc in chunks]
        out.append({
            'file': file, 'rg': g, 'row_start': start, 'row_end': start + n,
            'k_min': int(st.min), 'k_max': int(st.max),
            'rg_json': json.dumps([n, codecs.pop(), cols], separators=(',', ':')),
        })
        start += n
    return out


def _dir_schema() -> "pa.Schema":
    import pyarrow as pa
    return pa.schema([
        pa.field('file', pa.int8(), nullable=False),
        pa.field('rg', pa.int32(), nullable=False),
        pa.field('row_start', pa.int64(), nullable=False),
        pa.field('row_end', pa.int64(), nullable=False),
        pa.field('k_min', pa.int64(), nullable=False),
        pa.field('k_max', pa.int64(), nullable=False),
        pa.field('rg_json', pa.string(), nullable=False),
    ])


def write_search(
    path_sort: str,
    *,
    con: "duckdb.DuckDBPyConnection | None" = None,
    rows_rg_rows: int = ROWS_RG_ROWS,
    postings_rg_rows: int = POSTINGS_RG_ROWS,
    dir_rg_rows: int = DIR_RG_ROWS,
) -> SearchStats:
    """Write the search sidecars beside a local ``path`` sort (any store sort
    with ``path`` and ``size`` works; the rows file carries its columns)."""
    import duckdb as _duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    from disk_tree import listing_format as lf
    from disk_tree.find.groups import schema_json

    if postings_rg_rows < ROW_GROUP_STEP or postings_rg_rows % ROW_GROUP_STEP:
        raise ValueError(f"postings_rg_rows must be a positive multiple of {ROW_GROUP_STEP}; got {postings_rg_rows}")
    if rows_rg_rows < 1:
        raise ValueError(f"rows_rg_rows must be positive; got {rows_rg_rows}")
    md = pq.read_metadata(path_sort)
    cols = md.schema.names
    for need in ('path', 'size'):
        if need not in cols:
            raise ValueError(f"{path_sort}: no `{need}` column ({cols})")
    if 'id' in cols:
        raise ValueError(f"{path_sort}: has an `id` column (the rows file's key)")
    paths = search_paths(path_sort)
    own = con is None
    if own:
        con = _duckdb.connect()
    prev_order = con.execute("SELECT current_setting('preserve_insertion_order')").fetchone()[0]
    # The data files' row order IS the index ((id, path order), (tri, id)):
    # a parallel COPY without insertion order may write batches out of order.
    con.execute("SET preserve_insertion_order = true")
    codec = lf.duckdb_codec()
    src = f"read_parquet('{_q(path_sort)}', file_row_number = true)"
    try:
        con.execute(f"""
            CREATE OR REPLACE TEMP TABLE _search_names AS
            SELECT (row_number() OVER (ORDER BY b DESC, name) - 1)::INTEGER AS id, name
            FROM (
              SELECT regexp_extract(path, '[^/]*$') AS name, coalesce(sum(size), 0)::BIGINT AS b
              FROM read_parquet('{_q(path_sort)}') GROUP BY 1
            )
        """)
        n_names = con.execute("SELECT count(*) FROM _search_names").fetchone()[0]
        tri_tmp = paths['trigrams'] + '.tmp'
        con.execute(f"""
            COPY (
              WITH k AS (
                SELECT id, l, unnest(range(1, length(l) - 1)) AS k
                FROM (SELECT id, lower(name) AS l FROM _search_names)
              ),
              t AS (
                SELECT DISTINCT id,
                  (ascii(substr(l, k, 1)) * 65536 + ascii(substr(l, k + 1, 1)) * 256 + ascii(substr(l, k + 2, 1)))::INTEGER AS tri
                FROM k WHERE strlen(substr(l, k, 3)) = 3
              )
              SELECT tri, id FROM t ORDER BY tri, id
            ) TO '{_q(tri_tmp)}' (FORMAT PARQUET, {codec}, ROW_GROUP_SIZE {postings_rg_rows})
        """)
        os.replace(tri_tmp, paths['trigrams'])
        # The rows, name-major: streamed through pyarrow, which writes exact
        # `rows_rg_rows` groups (DuckDB's COPY flushes in 2048-row steps).
        reader = con.execute(f"""
            SELECT nm.id, {', '.join(f'r."{c}"' for c in cols)}
            FROM {src} r JOIN _search_names nm ON nm.name = regexp_extract(r.path, '[^/]*$')
            ORDER BY nm.id, r.file_row_number
        """).to_arrow_reader(rows_rg_rows * 256)
        rows_tmp = paths['rows'] + '.tmp'
        dict_cols = [c for c in ROWS_DICT_COLS if c in cols]
        with pq.ParquetWriter(rows_tmp, reader.schema, use_dictionary=dict_cols or False, write_statistics=['id'], store_schema=False, **lf.pyarrow_codec()) as w:
            # Batches may come short: carry the remainder so every group but
            # the last is exactly `rows_rg_rows`.
            pending: list[pa.RecordBatch] = []
            held = 0
            for batch in reader:
                pending.append(batch)
                held += batch.num_rows
                if held >= rows_rg_rows:
                    t = pa.Table.from_batches(pending)
                    full = held - held % rows_rg_rows
                    w.write_table(t.slice(0, full), row_group_size=rows_rg_rows)
                    pending = t.slice(full).to_batches()
                    held -= full
            if held:
                w.write_table(pa.Table.from_batches(pending, schema=reader.schema), row_group_size=rows_rg_rows)
        os.replace(rows_tmp, paths['rows'])
    finally:
        con.execute("DROP TABLE IF EXISTS _search_names")
        con.execute(f"SET preserve_insertion_order = {str(prev_order).lower()}")
        if own:
            con.close()
    rmd = pq.read_metadata(paths['rows'])
    tmd = pq.read_metadata(paths['trigrams'])
    if rmd.num_rows != md.num_rows:
        raise RuntimeError(f"rows file has {rmd.num_rows} rows, the `path` sort {md.num_rows}")
    rows = _rg_dir_rows(rmd, FILE_ROWS, 'id') if rmd.num_rows else []
    rows += _rg_dir_rows(tmd, FILE_TRIGRAMS, 'tri') if tmd.num_rows else []
    t = pa.table({c: [r[c] for r in rows] for c in _dir_schema().names}, schema=_dir_schema())
    kv = {
        'search_v': str(SEARCH_V),
        'rows_schema': json.dumps(schema_json(rmd)['schema'], separators=(',', ':')),
        'trigrams_schema': json.dumps(schema_json(tmd)['schema'], separators=(',', ':')),
        'names': str(n_names),
        'rows': str(rmd.num_rows),
        'postings': str(tmd.num_rows),
        'path_groups': str(md.num_row_groups),
        'path_rows': str(md.num_rows),
    }
    tmp = paths['search'] + '.tmp'
    with pq.ParquetWriter(tmp, t.schema, compression='zstd', write_statistics=list(DIR_STAT_COLS), store_schema=False) as w:
        w.write_table(t, row_group_size=dir_rg_rows)
        w.add_key_value_metadata(kv)
    os.replace(tmp, paths['search'])
    return SearchStats(names=n_names, rows=rmd.num_rows, postings=tmd.num_rows, path_groups=md.num_row_groups, files=paths)
