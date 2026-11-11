"""Reviewed cleanup lists cannot expand, shrink or silently change size."""
import json

import pytest

from dt_cloud.blobstore import Store
from dt_cloud.delete_review import validate
from dt_cloud import store_gen_gc as gc


def manifest(tmp_path, root, files, *, d1=False):
    path = tmp_path / 'review.json'
    path.write_text(json.dumps({'roots': [{'root': root, 'files': [{'key': k, 'bytes': n} for k, n in files.items()]}],
                                'd1_changes': d1}))
    return path


def test_exact_keys_and_sizes_pass(tmp_path):
    path = manifest(tmp_path, 'gs://derived', {'a/x': 3, 'b/y': 0})
    assert validate(path, 'gs://derived/', {'b/y': 0, 'a/x': 3}) is None


@pytest.mark.parametrize(('actual', 'counts'), [
    ({'a/x': 3, 'b/y': 4}, '1 extra, 0 missing, 0 size changes'),
    ({}, '0 extra, 1 missing, 0 size changes'),
    ({'a/x': 4}, '0 extra, 0 missing, 1 size changes'),
])
def test_changed_plan_refuses(tmp_path, actual, counts):
    path = manifest(tmp_path, 'gs://derived', {'a/x': 3})
    with pytest.raises(ValueError) as error:
        validate(path, 'gs://derived', actual)
    assert str(error.value) == f'deletion review mismatch: {counts}; nothing deleted'


@pytest.mark.parametrize(('root', 'd1', 'message'), [
    ('gs://other', False, 'deletion review: expected exactly one entry for gs://other'),
    ('gs://derived', True, 'deletion review: D1 changes were not reviewed'),
])
def test_other_root_or_unreviewed_d1_refuses(tmp_path, root, d1, message):
    path = manifest(tmp_path, 'gs://derived', {'a/x': 3})
    with pytest.raises(ValueError) as error:
        validate(path, root, {'a/x': 3}, d1=d1)
    assert str(error.value) == message


@pytest.mark.parametrize(('key', 'size'), [('../x', 1), ('/x', 1), ('a//x', 1), ('a/./x', 1), ('x', -1), ('x', True)])
def test_invalid_review_refuses(tmp_path, key, size):
    path = manifest(tmp_path, 'gs://derived', {key: size})
    with pytest.raises(ValueError) as error:
        validate(path, 'gs://derived', {key: size})
    assert str(error.value) == 'deletion review: invalid key or size'


def test_duplicate_root_and_key_refuse(tmp_path):
    path = manifest(tmp_path, 'gs://derived', {'x': 1})
    doc = json.loads(path.read_text())
    doc['roots'].append(doc['roots'][0])
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError) as error:
        validate(path, 'gs://derived', {'x': 1})
    assert str(error.value) == 'deletion review: expected exactly one entry for gs://derived'
    doc['roots'].pop()
    doc['roots'][0]['files'].append({'key': 'x', 'bytes': 1})
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError) as error:
        validate(path, 'gs://derived', {'x': 1})
    assert str(error.value) == 'deletion review: duplicate keys'


def test_gc_validates_all_generations_before_first_delete(tmp_path, monkeypatch):
    store = Store(str(tmp_path / 'store'))
    store.write_bytes('static-names/old/a', b'123')
    store.write_bytes('static-names/other/b', b'12')
    recs = [{'gen': g, 'action': 'delete', 'lineage': False, 'objects': 1, 'bytes': n, 'newest': '2026-01-01',
             '_files': [k]} for g, k, n in [('old', 'static-names/old/a', 3), ('other', 'static-names/other/b', 2)]]
    monkeypatch.setattr(gc, 'plan', lambda *a, **kw: recs)
    review = manifest(tmp_path, store.root, {'static-names/old/a': 3})
    with pytest.raises(ValueError) as error:
        gc.run(store, 'static-names', ['live'], now=0, execute=True, review=review)
    assert str(error.value) == 'deletion review mismatch: 1 extra, 0 missing, 0 size changes; nothing deleted'
    assert store.listing('') == {'static-names/old/a': 3, 'static-names/other/b': 2}
    review = manifest(tmp_path, store.root, store.listing(''))
    actual = gc.run(store, 'static-names', ['live'], now=0, execute=True, review=review)
    assert [r['deleted'] for r in actual] == [1, 1]
    assert store.listing('') == {}
