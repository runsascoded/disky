"""Group manifests beside index tiers (`find/groups.py`): the precomputed footer
a serverless range reader plans from, in mgu's `index_footer.py` wire format.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from disk_tree.find.aggregate_duckdb import aggregate_listing_to_parquet
from disk_tree.find.groups import (
    FOOTER_COLS, GROUP_FIELDS, GROUPS_VERSION, extract, group_rows, groups_from_json, groups_json, groups_parquet_bytes,
    groups_parquet_path, groups_path, read_groups_parquet, schema_json, write_groups, write_groups_parquet,
)
from disk_tree.find.tiers import write_tiers
from disk_tree.listing import prepare_listing
from test_tiers import _layer2

TS = dt.datetime(2026, 7, 28, tzinfo=dt.timezone.utc)


def _wide_layer2(tmp_path: Path, n_files: int, labels: str | None = None) -> str:
    """`n_files` one-byte files `d<k>/f<i>` spread over 4 dirs — enough rows for
    a tier to span several 2048-row groups."""
    names = [f'd{i % 4}/f{i:05d}' for i in range(n_files)]
    listing = tmp_path / 'wide.parquet'
    pd.DataFrame({
        'bucket': ['b1'] * n_files,
        'name': names,
        'size_bytes': [1] * n_files,
        'created': [TS] * n_files,
        'storage_class_id': [1] * n_files,
    }).to_parquet(listing)
    out = str(tmp_path / 'wide-layer2.parquet')
    con = duckdb.connect()
    aggregate_listing_to_parquet(
        prepare_listing(con, (str(listing),)), bucket='b1', scheme='gcs', out_parquet=out, con=con, label=labels,
    )
    return out


def _chunk_triples(path: str) -> list[list[list[int]]]:
    """Per row group, `[data_page_offset, total_compressed_size, dictionary_page_offset|0]`
    per column — the reference for `rg_json`'s third element."""
    md = pq.read_metadata(path)
    return [
        [
            [cc.data_page_offset, cc.total_compressed_size, cc.dictionary_page_offset or 0]
            for cc in (md.row_group(g).column(c) for c in range(md.num_columns))
        ]
        for g in range(md.num_row_groups)
    ]


def test_groups_path():
    assert groups_path('/x/gcs-b1.path.parquet') == '/x/gcs-b1.path.groups.json'
    assert groups_path('r2://bk/p/gcs-b1.bysize-by-usr.parquet') == 'r2://bk/p/gcs-b1.bysize-by-usr.groups.json'
    with pytest.raises(ValueError, match='not a parquet path'):
        groups_path('/x/gcs-b1.path.groups.json')


def test_schema_json_is_hyparquet_shaped(tmp_path: Path):
    layer2 = _layer2(tmp_path)
    stem = str(tmp_path / 'gcs-b1')
    write_tiers(layer2, stem, tiers=('path',))
    md = pq.read_metadata(f'{stem}.path.parquet')
    schema = schema_json(md)
    names = md.schema.names
    # One leaf per column, in file order, physical types as parquet-thrift names.
    assert [el['name'] for el in schema['schema'][1:]] == names
    assert schema['schema'][0] == {'repetition_type': 'REQUIRED', 'name': 'schema', 'num_children': len(names)}
    by_name = {el['name']: el for el in schema['schema'][1:]}
    assert by_name['path'] == {'type': 'BYTE_ARRAY', 'repetition_type': 'OPTIONAL', 'name': 'path', 'converted_type': 'UTF8'}
    assert by_name['depth'] == {'type': 'INT64', 'repetition_type': 'OPTIONAL', 'name': 'depth', 'converted_type': 'INT_64'}
    assert by_name['size'] == {'type': 'INT64', 'repetition_type': 'OPTIONAL', 'name': 'size', 'converted_type': 'INT_64'}
    assert schema['version'] == 1
    # The store's sorts have no floor (every byte floor is a prefix of `bysize`).
    assert 'floor_bytes' not in schema
    # A coarse tier's floor (mgu's `coarse_floor`, or `floor_bytes`) rides
    # along from the parquet key-value metadata.
    for key in ('floor_bytes', 'coarse_floor'):
        t = pa.table({'path': ['.', 'a'], 'depth': [0, 1], 'size': [7, 3]})
        pq.write_table(t.replace_schema_metadata({key: '512'}), tmp_path / f'{key}.parquet')
        assert schema_json(pq.read_metadata(tmp_path / f'{key}.parquet'))['floor_bytes'] == 512


def test_group_rows_span_the_sorted_path_tier(tmp_path: Path):
    n = 5000
    layer2 = _wide_layer2(tmp_path, n)
    stem = str(tmp_path / 'gcs-b1')
    write_tiers(layer2, stem, tiers=('path',), row_group_rows=2048)
    tier = f'{stem}.path.parquet'
    rows = group_rows(pq.read_metadata(tier))
    # The path tier is sorted `(depth, path)`: the root (5000 bytes, depth 0)
    # and the four 1250-byte dirs (depth 1) lead group 0, then the 1-byte
    # files (depth 2) in path order; group boundaries are the sorted paths at
    # 2043 / 2044 / 4091 / … — exact. Group 0's path stats span `.` to `d3`
    # (the dir row sorts after every `d0/…`, `d1/…` file it holds), a reminder
    # that a depth-spanning group's path range is not a prefix range.
    # `b_min`/`b_max` bound each group's sizes: group 0 spans the root down to
    # a file, the rest are all 1.
    paths = sorted(f'd{i % 4}/f{i:05d}' for i in range(n))
    expected = [
        {
            'rg': 0, 'd_min': 0, 'd_max': 2,
            'p_min': '.', 'p_max': 'd3',
            'b_max': 5000, 'u_min': None, 'u_max': None,
            'row_start': 0, 'row_end': 2048, 'b_min': 1,
        },
        {
            'rg': 1, 'd_min': 2, 'd_max': 2,
            'p_min': paths[2043], 'p_max': paths[4090],
            'b_max': 1, 'u_min': None, 'u_max': None,
            'row_start': 2048, 'row_end': 4096, 'b_min': 1,
        },
        {
            'rg': 2, 'd_min': 2, 'd_max': 2,
            'p_min': paths[4091], 'p_max': paths[4999],
            'b_max': 1, 'u_min': None, 'u_max': None,
            'row_start': 4096, 'row_end': 5005, 'b_min': 1,
        },
    ]
    assert [{k: r[k] for k in GROUP_FIELDS if k != 'rg_json'} for r in rows] == expected
    spans = [(0, 2048), (2048, 4096), (4096, 5005)]
    # `rg_json` = [num_rows, codec, per-column [data_page_offset, total_compressed_size, dictionary_page_offset|0]].
    triples = _chunk_triples(tier)
    assert [json.loads(r['rg_json']) for r in rows] == [
        [b - a, 'ZSTD', triples[g]] for g, (a, b) in enumerate(spans)
    ]
    assert all(len(t) == pq.read_metadata(tier).num_columns for t in triples)


def test_user_slice_bounds_and_size_column_fallback(tmp_path: Path):
    # A `usr`-labeled layer-2: the path-by-usr tier sorts on usr first, so each
    # group's u_min/u_max are that slice's bounds; the size column is `size`.
    labels = tmp_path / 'labels.parquet'
    pd.DataFrame({'prefix': ['d0', 'd1', 'd2'], 'usr': ['alice', 'bob', 'carol']}).to_parquet(labels)
    layer2 = _layer2(tmp_path, labels=str(labels))
    stem = str(tmp_path / 'gcs-b1')
    write_tiers(layer2, stem, tiers=('path',), sort_variants=(('usr',),))
    rows = group_rows(pq.read_metadata(f'{stem}.path-by-usr.parquet'))
    # One group (37 rows × slices ≤ 8192): NULL-labeled rows sort first, so
    # the string stats span the labeled slices only.
    assert [(r['rg'], r['u_min'], r['u_max'], r['row_start']) for r in rows] == [(0, 'alice', 'carol', 0)]
    sizes = pd.read_parquet(f'{stem}.path-by-usr.parquet')['size']
    assert (rows[0]['b_min'], rows[0]['b_max']) == (int(sizes.min()), int(sizes.max()))

    # mgu's path index names its size column `b`: the same extraction applies.
    df = pd.DataFrame({'path': ['.', 'a'], 'depth': [0, 1], 'b': [7, 3]})
    df.to_parquet(tmp_path / 'mgu.parquet')
    assert [(r['b_min'], r['b_max'], r['u_min']) for r in group_rows(pq.read_metadata(tmp_path / 'mgu.parquet'))] == [(3, 7, None)]
    # Without any size column the manifest is undefined, loudly.
    pd.DataFrame({'path': ['.'], 'depth': [0]}).to_parquet(tmp_path / 'nosize.parquet')
    with pytest.raises(ValueError, match=r'no size column \(size/b\)'):
        group_rows(pq.read_metadata(tmp_path / 'nosize.parquet'))


def test_write_tiers_groups_writes_the_manifest_beside_each_tier(tmp_path: Path):
    layer2 = _layer2(tmp_path)
    stem = str(tmp_path / 'gcs-b1')
    written = write_tiers(layer2, stem, groups=True)
    assert sorted(p.name for p in tmp_path.glob('gcs-b1.*')) == [
        'gcs-b1.bysize.groups.json', 'gcs-b1.bysize.groups.parquet', 'gcs-b1.bysize.parquet',
        'gcs-b1.path.groups.json', 'gcs-b1.path.groups.parquet', 'gcs-b1.path.parquet',
    ]
    for tier_path, n_rows in written.items():
        doc = json.loads(Path(groups_path(tier_path)).read_text())
        schema, rows = extract(tier_path)
        # The cold footer tier: the same rows, typed, and the same schema.
        assert read_groups_parquet(groups_parquet_path(tier_path)) == (schema, rows)
        # The document is exactly `groups_json(extract(tier))`: envelope + arrays in GROUP_FIELDS order.
        assert doc == json.loads(groups_json(schema, rows))
        assert list(doc) == ['v', 'version', 'schema', 'floor_bytes', 'groups']
        assert doc['v'] == GROUPS_VERSION
        assert doc['groups'] == [[r[k] for k in GROUP_FIELDS] for r in rows]
        assert doc['groups'][-1][9] == n_rows  # row_end of the last group = the tier's rows
        # `b_min` is the appended 12th field; the reader's positional
        # destructure (`index.ts` `openBlob`) reads the first 11 and ignores it.
        assert [len(g) for g in doc['groups']] == [12] * len(rows)
        assert doc['floor_bytes'] is None


def test_write_groups_over_a_url(tmp_path: Path):
    # The blob seam handles URLs; `file://` exercises the fsspec branch end to end.
    layer2 = _layer2(tmp_path)
    stem = str(tmp_path / 'gcs-b1')
    write_tiers(layer2, stem, tiers=('path',))
    st = write_groups(f'file://{stem}.path.parquet')
    assert (st.path, st.n_groups) == (f'file://{stem}.path.groups.json', 1)
    assert Path(f'{stem}.path.groups.json').read_text() == groups_json(*extract(f'{stem}.path.parquet'))
    assert st.n_bytes == len(Path(f'{stem}.path.groups.json').read_bytes())
    # The cold footer tier over the same seam.
    schema, rows = extract(f'{stem}.path.parquet')
    pst = write_groups_parquet(f'file://{stem}.path.parquet', schema, rows)
    assert (pst.path, pst.n_groups) == (f'file://{stem}.path.groups.parquet', 1)
    assert pst.n_bytes == len(Path(f'{stem}.path.groups.parquet').read_bytes())
    assert read_groups_parquet(pst.path) == (schema, rows)


def test_groups_parquet_spans_many_footer_groups_and_round_trips_json(tmp_path: Path):
    """A 5000-row `path` tier at 2048-row groups has 3 tier groups; at 2 footer
    rows per footer group, the `.groups.parquet` has 2 footer groups whose
    stats bound their rows' stats. A `.groups.json` (incl. one written before
    `b_min`) rebuilds it."""
    layer2 = _wide_layer2(tmp_path, 5000)
    stem = str(tmp_path / 'gcs-b1')
    write_tiers(layer2, stem, tiers=('path',), row_group_rows=2048)
    tier = f'{stem}.path.parquet'
    schema, rows = extract(tier)
    assert len(rows) == 3
    data = groups_parquet_bytes(schema, rows, row_group_rows=2)
    Path(groups_parquet_path(tier)).write_bytes(data)
    md = pq.read_metadata(groups_parquet_path(tier))
    assert [md.row_group(g).num_rows for g in range(md.num_row_groups)] == [2, 1]
    names = md.schema.names
    assert names == list(FOOTER_COLS)

    def mm(g: int, col: str) -> tuple:
        s = md.row_group(g).column(names.index(col)).statistics
        return (s.min, s.max) if s.has_min_max else None

    assert [(mm(g, 'd_min'), mm(g, 'd_max'), mm(g, 'b_max'), mm(g, 'p_min'), mm(g, 'p_max'), mm(g, 'u_min')) for g in range(2)] == [
        ((rows[0]['d_min'], rows[1]['d_min']), (rows[0]['d_max'], rows[1]['d_max']), (rows[1]['b_max'], rows[0]['b_max']),
         (rows[0]['p_min'], rows[1]['p_min']), (rows[0]['p_max'], rows[1]['p_max']), None),
        ((rows[2]['d_min'],) * 2, (rows[2]['d_max'],) * 2, (rows[2]['b_max'],) * 2, (rows[2]['p_min'],) * 2, (rows[2]['p_max'],) * 2, None),
    ]
    assert md.row_group(0).column(names.index('rg_json')).statistics is None
    # From the JSON: the same file, byte for byte.
    assert groups_from_json(groups_json(schema, rows)) == (schema, rows)
    assert groups_parquet_bytes(*groups_from_json(groups_json(schema, rows)), row_group_rows=2) == data
    # A pre-`b_min` document (11 fields) loads with `b_min` null.
    old = json.loads(groups_json(schema, rows))
    old['groups'] = [g[:11] for g in old['groups']]
    assert groups_from_json(json.dumps(old)) == (schema, [{**r, 'b_min': None} for r in rows])
    assert groups_parquet_path('/x/a.parquet') == '/x/a.groups.parquet'
    with pytest.raises(ValueError, match='not a tier parquet path'):
        groups_parquet_path('/x/a.groups.parquet')
    with pytest.raises(ValueError, match='rg 0..n-1'):
        groups_parquet_bytes(schema, rows[1:])
