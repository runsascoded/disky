"""`dt-index` (the Rust path store, apps/tauri/crates/dt-index) writes what
`dt-cloud path-index -g` writes from the same capture: the `path` and `bysize`
sorts row for row (same order, same row groups, same schema and key-value
metadata), and the same `meta.json` (bar its timestamps) and `age.json`."""

import json
import subprocess
import sys
from os.path import dirname, exists, join
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

DT_INDEX = join(dirname(dirname(__file__)), 'apps', 'tauri', 'target', 'release', 'dt-index')
DT_CLOUD = join(dirname(sys.executable), 'dt-cloud')

pytestmark = [
    pytest.mark.skipif(not exists(DT_INDEX), reason='dt-index not built (cargo build --release -p dt-index in apps/tauri)'),
    pytest.mark.skipif(not exists(DT_CLOUD), reason='dt-cloud not installed (uv sync --all-packages)'),
]

ASOF = '2026-10-02'
DAY_MS = 86_400_000
NOW_MS = 20728 * DAY_MS + 12 * 3_600_000


def _capture(dir: Path, root: str, names: list[str], sizes: list[int], created_ms: list[int]) -> Path:
    """A layer-1 capture dir (`capture.py`'s shard schema) of `root`."""
    dir.mkdir(parents=True)
    table = pa.table({
        'bucket': pa.array([root] * len(names), pa.large_string()),
        'name': pa.array(names, pa.large_string()),
        'size_bytes': pa.array(sizes, pa.int64()),
        'created': pa.array(created_ms, pa.timestamp('ms', tz='UTC')),
        'storage_class_id': pa.array([0] * len(names), pa.int64()),
    })
    pq.write_table(table, dir / 'shard-00000.parquet', compression='snappy')
    return dir


def _listing(n_bulk: int) -> tuple[list[str], list[int], list[int]]:
    """Files at every age bucket (and a future stamp), half-second stamps (the
    rounding of `wts`), a zero-size file, non-ASCII names, `/`-sorting traps
    (`a-b` vs `a/b`), and `n_bulk` small files spanning several row groups."""
    rows = [
        ('a/x.txt', 4096, NOW_MS),
        ('a/b/y.bin', 53248, NOW_MS - 3 * DAY_MS + 500),
        ('a/b/c/z', 12288, NOW_MS - 20 * DAY_MS + 1500),
        ('a-b/q', 8192, NOW_MS - 60 * DAY_MS),
        ('a/b-c/r', 8192, NOW_MS - 200 * DAY_MS + 999),
        ('d/old', 0, NOW_MS - 2000 * DAY_MS),
        ('d/ünï cødé.txt', 5000, NOW_MS - 700 * DAY_MS),
        ('future', 1, NOW_MS + 3 * DAY_MS),
        ('top', 4096, NOW_MS - 1),
    ]
    rows += [(f'bulk/{i % 37}/f{i:05}', 512 * (i % 50), NOW_MS - (i % 1500) * DAY_MS - i) for i in range(n_bulk)]
    names, sizes, created = zip(*rows)
    return list(names), list(sizes), list(created)


def _kv(path: Path) -> dict[str, str]:
    return {k.decode(): v.decode() for k, v in (pq.read_metadata(path).metadata or {}).items() if k != b'ARROW:schema'}


def _groups(path: Path) -> list[int]:
    md = pq.read_metadata(path)
    return [md.row_group(i).num_rows for i in range(md.num_row_groups)]


@pytest.mark.parametrize('root', ['/', '/Users/ryan'], ids=['fs-root', 'home'])
def test_dt_index_matches_path_index(tmp_path: Path, root: str):
    names, sizes, created = _listing(20_000)
    cap = _capture(tmp_path / 'cap', root, names, sizes, created)
    py, rs = tmp_path / 'py', tmp_path / 'rs'
    subprocess.run([DT_CLOUD, 'path-index', '-g', '-d', ASOF, '-l', f'{cap}/*.parquet', '-P', str(py / 'index' / 'path-index.parquet'), '-o', str(py / 'snap')], check=True, capture_output=True)
    subprocess.run([DT_INDEX, '-d', ASOF, '-P', str(rs / 'index'), '-o', str(rs / 'snap'), str(cap)], check=True, capture_output=True)
    for name in ('path-index.parquet', 'path-index-bysize.parquet'):
        p, r = py / 'index' / name, rs / 'index' / name
        assert pq.read_schema(r).remove_metadata() == pq.read_schema(p).remove_metadata()
        assert _kv(r) == _kv(p)
        assert _groups(r) == _groups(p)
        assert len(_groups(p)) > 2
        pd.testing.assert_frame_equal(pd.read_parquet(r), pd.read_parquet(p))
    strip = lambda m: {k: v for k, v in m.items() if k not in ('generated', 'published')}
    mp, mr = (json.loads((d / 'snap' / 'meta.json').read_text()) for d in (py, rs))
    assert strip(mr) == strip(mp)
    assert (rs / 'snap' / 'age.json').read_text() == (py / 'snap' / 'age.json').read_text()
