import json

import pytest
from click.testing import CliRunner
from disk_tree.blobfs import read_text

from dt_cloud.blobstore import Store
from dt_cloud.store_audit import audit, store_audit_cmd


def test_inventory_reports_exact_keys_sizes_and_prefix_groups(tmp_path):
    store = Store(str(tmp_path))
    store.write_bytes('listing/a/index/x.parquet', b'123')
    store.write_bytes('listing/b/index/y.parquet', b'12')
    store.write_bytes('history/age/c.parquet', b'1')
    assert audit(store, '', 1) == {
        'root': str(tmp_path), 'prefix': '', 'objects': 3, 'bytes': 6,
        'groups': {'history': {'objects': 1, 'bytes': 1}, 'listing': {'objects': 2, 'bytes': 5}},
        'files': {'history/age/c.parquet': 1, 'listing/a/index/x.parquet': 3, 'listing/b/index/y.parquet': 2},
    }
    assert audit(store, 'listing', 2) == {
        'root': str(tmp_path), 'prefix': 'listing', 'objects': 2, 'bytes': 5,
        'groups': {'listing/a': {'objects': 1, 'bytes': 3}, 'listing/b': {'objects': 1, 'bytes': 2}},
        'files': {'listing/a/index/x.parquet': 3, 'listing/b/index/y.parquet': 2},
    }


def test_inventory_of_empty_prefix_is_empty(tmp_path):
    assert audit(Store(str(tmp_path)), 'missing', 1) == {
        'root': str(tmp_path), 'prefix': 'missing', 'objects': 0, 'bytes': 0, 'groups': {}, 'files': {},
    }


@pytest.mark.parametrize('remote', [False, True])
def test_cli_saves_exact_keys_locally_or_remotely_and_prints_only_summary(tmp_path, remote):
    source = Store(str(tmp_path / 'source'))
    source.write_bytes('a/x', b'123')
    out = f'memory://{tmp_path.name}/inventory.json' if remote else str(tmp_path / 'out/inventory.json')
    result = CliRunner().invoke(store_audit_cmd, ['-o', out, source.root], catch_exceptions=False)
    assert result.exit_code == 0
    expected = {'root': source.root, 'prefix': '', 'bytes': 3, 'objects': 1, 'groups': {'a': {'bytes': 3, 'objects': 1}}}
    assert json.loads(result.stdout) == expected
    assert json.loads(read_text(out)) == {**expected, 'files': {'a/x': 3}}
