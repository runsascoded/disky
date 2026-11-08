from dt_cloud.blobstore import Store
from dt_cloud.store_audit import audit


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
