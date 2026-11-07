"""The path store's search sidecars (`disk_tree.find.search`, spec
`path-store-search.md` §2, layout v2): exact name-major rows, postings and
directory rows for a hand-built `path` sort spanning three 2048-row groups."""

import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from disk_tree.find.search import search_paths, tri_code, write_search

N_FILL = 4500
#: (path, size) in `(depth, path)` order — the `path` sort's order.
ROWS = [
    ('bk', 10000), ('zz', 300),
    ('bk/TTL', 2000), ('bk/a', 3000), ('bk/f', 4500), ('zz/ttl', 300),
    ('bk/TTL/ttl', 1500), ('bk/a/x.bin', 3000),
    *((f'bk/f/n{i:04d}', 1) for i in range(N_FILL)),
    ('zz/ttl/x.bin', 300),
]
RG = 2048


def _path_sort(path: Path) -> str:
    t = pa.table({
        'path': [p for p, _ in ROWS],
        'depth': pa.array([p.count('/') + 1 for p, _ in ROWS], pa.int32()),
        'size': pa.array([s for _, s in ROWS], pa.int64()),
    })
    pq.write_table(t, path, row_group_size=RG)
    return str(path)


def _rows(path: str, cols: str = '*') -> list[tuple]:
    return duckdb.connect().execute(f"SELECT {cols} FROM read_parquet('{path}')").fetchall()


def _expected_names() -> list[tuple[int, str]]:
    """(id, name), `Σ size desc, name`."""
    head = ['bk', 'f', 'x.bin', 'a', 'TTL', 'ttl', 'zz']  # x.bin: 3000 + 300; ttl: 300 + 1500
    return list(enumerate(head + [f'n{i:04d}' for i in range(N_FILL)]))


def _expected_rows() -> list[tuple[int, str, int, int]]:
    """(id, path, depth, size): each name's rows in `path`-sort order."""
    ids = {name: id for id, name in _expected_names()}
    rows = [(ids[p.rsplit('/', 1)[-1]], i, p, s) for i, (p, s) in enumerate(ROWS)]
    return [(id, p, p.count('/') + 1, s) for id, _, p, s in sorted(rows)]


def _expected_postings(names: list[tuple[int, str]]) -> list[tuple[int, int]]:
    out = set()
    for id, name in names:
        low = name.lower()
        for k in range(len(low) - 2):
            t = low[k:k + 3]
            if all(ord(c) < 128 for c in t):
                out.add((tri_code(t), id))
    return sorted(out)


def test_write_search(tmp_path: Path):
    src = _path_sort(tmp_path / 'path-index.parquet')
    st = write_search(src, rows_rg_rows=1000, postings_rg_rows=2048, dir_rg_rows=4)
    paths = search_paths(src)
    assert paths == {
        'rows': str(tmp_path / 'path-index.rows.parquet'),
        'trigrams': str(tmp_path / 'path-index.trigrams.parquet'),
        'search': str(tmp_path / 'path-index.rows-search.parquet'),
    }
    names = _expected_names()
    rows = _expected_rows()
    postings = _expected_postings(names)
    assert (st.names, st.rows, st.postings, st.path_groups, st.files) == (len(names), len(ROWS), len(postings), 3, paths)

    # The rows, name-major: a name's rows together (`x.bin`'s two, `ttl`'s
    # `zz/ttl` then `bk/TTL/ttl` as the `path` sort has them), every column
    # of the sort after `id`, in exact `rows_rg_rows` groups.
    assert _rows(paths['rows']) == rows
    assert rows[:9] == [
        (0, 'bk', 1, 10000), (1, 'bk/f', 2, 4500), (2, 'bk/a/x.bin', 3, 3000), (2, 'zz/ttl/x.bin', 3, 300),
        (3, 'bk/a', 2, 3000), (4, 'bk/TTL', 2, 2000), (5, 'zz/ttl', 2, 300), (5, 'bk/TTL/ttl', 3, 1500), (6, 'zz', 1, 300),
    ]
    rmd = pq.read_metadata(paths['rows'])
    assert [rmd.row_group(g).num_rows for g in range(rmd.num_row_groups)] == [1000, 1000, 1000, 1000, 509]
    # Statistics only on `id` (what the directory's key range comes from).
    rg0 = rmd.row_group(0)
    assert {rg0.column(c).path_in_schema: rg0.column(c).statistics is not None for c in range(rg0.num_columns)} == {
        'id': True, 'path': False, 'depth': False, 'size': False,
    }
    # Postings: every ASCII trigram of the lowercased name, `(tri, id)` order.
    assert _rows(paths['trigrams']) == postings
    ttl = tri_code('ttl')
    assert [id for t, id in postings if t == ttl] == [4, 5]  # `TTL` and `ttl` alike

    # The directory: one row per data row group, key ranges = ids / trigrams.
    d = pq.read_table(paths['search']).to_pylist()
    for r in d:
        assert json.loads(r.pop('rg_json'))[0] == r['row_end'] - r['row_start']
    n_post = len(postings)
    post_groups = [(i * RG, min((i + 1) * RG, n_post)) for i in range((n_post + RG - 1) // RG)]
    row_groups = [(i * 1000, min((i + 1) * 1000, len(rows))) for i in range(5)]
    assert d == [
        *({'file': 0, 'rg': g, 'row_start': a, 'row_end': b, 'k_min': rows[a][0], 'k_max': rows[b - 1][0]} for g, (a, b) in enumerate(row_groups)),
        *({'file': 1, 'rg': g, 'row_start': a, 'row_end': b, 'k_min': postings[a][0], 'k_max': postings[b - 1][0]} for g, (a, b) in enumerate(post_groups)),
    ]
    md = pq.read_metadata(paths['search'])
    assert md.num_row_groups == (len(d) + 3) // 4
    kv = {k.decode(): v.decode() for k, v in md.metadata.items()}
    schemas = {k: [e['name'] for e in json.loads(kv.pop(k))] for k in ('rows_schema', 'trigrams_schema')}
    assert schemas == {'rows_schema': ['schema', 'id', 'path', 'depth', 'size'], 'trigrams_schema': ['schema', 'tri', 'id']}
    assert kv == {'search_v': '2', 'names': str(len(names)), 'rows': str(len(ROWS)), 'postings': str(n_post), 'path_groups': '3', 'path_rows': str(len(ROWS))}
    # Statistics only on what a reader prunes by.
    rg0 = md.row_group(0)
    assert {rg0.column(c).path_in_schema: rg0.column(c).statistics is not None for c in range(rg0.num_columns)} == {
        'file': True, 'rg': True, 'row_start': False, 'row_end': False, 'k_min': True, 'k_max': True, 'rg_json': False,
    }


def test_owner_slices(tmp_path: Path):
    """A path's owner slices (one row per `usr`) stay together and in the
    `path` sort's order; `usr` is dictionary-encoded, `path` plain."""
    p = tmp_path / 'path-index.parquet'
    pq.write_table(pa.table({
        'path': ['bk', 'bk', 'bk/run', 'bk/run', 'bk/x/run'],
        'usr': ['al', 'bo', 'bo', 'al', None],
        'size': pa.array([5, 4, 3, 2, 1], pa.int64()),
    }), p)
    write_search(str(p))
    rows = search_paths(str(p))['rows']
    # `bk` (Σ 9) is id 0, `run` (Σ 6) 1; `x` has no row of its own.
    assert _rows(rows) == [(0, 'bk', 'al', 5), (0, 'bk', 'bo', 4), (1, 'bk/run', 'bo', 3), (1, 'bk/run', 'al', 2), (1, 'bk/x/run', None, 1)]
    enc = {c: pq.read_metadata(rows).row_group(0).column(c).encodings for c in (1, 2)}
    assert ['RLE_DICTIONARY' in enc[1], 'RLE_DICTIONARY' in enc[2]] == [False, True]


def test_unicode_names(tmp_path: Path):
    """Trigrams come from DuckDB `lower()`, ASCII ones only: the Kelvin sign
    lowercases to `k` (as in JS), and a non-ASCII trigram is never stored."""
    p = tmp_path / 'path-index.parquet'
    pq.write_table(pa.table({'path': ['bk', 'bk/Key', 'bk/cafés'], 'size': pa.array([3, 2, 1], pa.int64())}), p)
    write_search(str(p))
    tri = {v: k for k, v in {t: tri_code(t) for t in ('key', 'caf')}.items()}
    assert [(tri.get(t, t), id) for t, id in _rows(search_paths(str(p))['trigrams'])] == [('caf', 2), ('key', 1)]


def test_rejects(tmp_path: Path):
    p = tmp_path / 'x.parquet'
    pq.write_table(pa.table({'path': ['a']}), p)
    with pytest.raises(ValueError, match='no `size` column'):
        write_search(str(p))
    with pytest.raises(ValueError, match='multiple of 2048'):
        write_search(str(p), postings_rg_rows=1000)
    q = tmp_path / 'y.parquet'
    pq.write_table(pa.table({'path': ['a'], 'size': [1], 'id': [0]}), q)
    with pytest.raises(ValueError, match='has an `id` column'):
        write_search(str(q))
