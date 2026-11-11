"""Existing SSD outputs must match the GCS publication before R2 is allowed to expose them."""
import hashlib

import pytest

from dt_cloud import append_runner as ar
from dt_cloud import publish as pub
from dt_cloud import r2_copy_probe as probe


@pytest.mark.parametrize('mode', ['local', 'stream', 'corrupt', 'wrong-size', 'missing-md5'])
def test_copy_uses_only_verified_local_outputs(tmp_path, monkeypatch, mode):
    key, content = 'p/g/data', b'abc'
    path = tmp_path / key
    path.parent.mkdir(parents=True)
    path.write_bytes(b'bad' if mode == 'corrupt' else b'abcd' if mode == 'wrong-size' else content)
    obj = pub.Obj(key, 3, None if mode == 'missing-md5' else hashlib.md5(content).hexdigest())
    calls = []
    monkeypatch.setattr(pub, 'r2_client', lambda: 'client')
    monkeypatch.setattr(pub, 'r2_bucket', lambda: 'dest')
    monkeypatch.setattr(pub, 'head_dest', lambda *a: None)
    monkeypatch.setattr(pub, 'copy_retrying', lambda src, s3, bucket, o, **kw: calls.append((src, s3, bucket, o)))
    kw = {'local_root': None if mode == 'stream' else tmp_path, 'log': lambda m: None}
    if mode in ('corrupt', 'wrong-size', 'missing-md5'):
        with pytest.raises(ValueError) as e:
            ar.r2_copy('gcs', [obj], **kw)
        assert str(e.value) == f'{path}: local output differs from its published source object'
        assert calls == []
    else:
        result = ar.r2_copy('gcs', [obj], **kw)
        assert {k: v for k, v in result.items() if k != 's'} == {'objects': 1, 'copied': 1, 'bytes': 3}
        assert calls == [('gcs' if mode == 'stream' else str(tmp_path), 'client', 'dest', obj)]


def test_local_generation_uploads_markers_last_and_never_promotes_failed_verification(tmp_path, monkeypatch):
    objs = [pub.Obj(f'p/g2/{k}', 3, 'hash') for k in ('data', 'compaction.json', 'scans.json')]
    calls = []
    monkeypatch.setattr(ar, 'r2_objects', lambda *a: objs)
    monkeypatch.setattr(ar, 'r2_copy', lambda bucket, os, **kw: calls.append(([o.key for o in os], kw['local_root'])) or {'copied': len(os), 'bytes': len(os) * 3})
    monkeypatch.setattr(ar, 'r2_missing', lambda *a, **kw: ['p/g2/data'])
    monkeypatch.setattr(ar, 'r2_promote', lambda *a, **kw: pytest.fail('unverified generation promoted'))
    with pytest.raises(SystemExit) as e:
        ar.r2_publish_gen('gcs', 'p', 'g2', 'g1', local_root=tmp_path)
    assert str(e.value) == 'r2 p/g2: 1 of 3 files not on R2 after the copy (e.g. p/g2/data): pointer not moved'
    assert calls == [(['p/g2/data'], tmp_path), (['p/g2/scans.json'], tmp_path), (['p/g2/compaction.json'], tmp_path)]


def test_probe_sample_is_bounded_and_rejects_unknown_hashes():
    objs = [pub.Obj(k, mib * 2**20, md5) for k, mib, md5 in [('huge', 300, 'h'), ('a', 128, 'h'), ('b', 64, 'h'), ('c', 16, None), ('tiny', 1, 'h')]]
    assert probe.sample(objs, 192 * 2**20) == objs[1:3]
    assert probe.sample(objs, 64 * 2**20) == [objs[2]]
    with pytest.raises(ValueError, match='^no objects with md5 and sizes 8–256 MiB fit the probe budget$'):
        probe.sample(objs, 8 * 2**20)


@pytest.mark.parametrize('fail', [False, True])
def test_probe_alternates_paths_verifies_uploads_and_cleans_only_owned_keys(tmp_path, monkeypatch, fail):
    from types import SimpleNamespace

    obj = pub.Obj('static-names/g/data', 3, hashlib.md5(b'abc').hexdigest())
    calls, deleted, uploaded = [], [], {}
    monkeypatch.setattr(probe, 'sample', lambda objs, budget: [obj])
    monkeypatch.setattr(pub, 'list_source', lambda *a: [obj])
    monkeypatch.setattr(probe, 'uuid4', lambda: SimpleNamespace(hex='owned'))
    monkeypatch.setattr(probe.Store, 'get', lambda self, key, dst: (dst.parent.mkdir(parents=True, exist_ok=True), dst.write_bytes(b'abc')))

    def delete_objects(**kw):
        deleted.extend(k['Key'] for k in kw['Delete']['Objects'])
        return {}

    monkeypatch.setattr(pub, 'r2_client', lambda: SimpleNamespace(delete_objects=delete_objects))
    monkeypatch.setattr(pub, 'r2_bucket', lambda: 'r2')

    def copy(src, s3, bucket, o, *, dest_key):
        calls.append((src, bucket, dest_key))
        if fail:
            raise RuntimeError('upload failed')
        uploaded[dest_key] = pub.Dest(size=o.size, md5=o.md5)

    monkeypatch.setattr(pub, 'copy_one', copy)
    monkeypatch.setattr(pub, 'head_dest', lambda s3, bucket, key: uploaded[key])
    if fail:
        with pytest.raises(RuntimeError, match='^upload failed$'):
            probe.probe('gcs', 'static-names/g', tmp_path, budget=3, workers=1, rounds=2)
        assert calls == [('gcs', 'r2', 'tmp/r2-copy-probe/owned/0/stream/static-names/g/data')]
    else:
        result = probe.probe('gcs', 'static-names/g', tmp_path, budget=3, workers=1, rounds=2)
        expected = [('gcs', 0, 'stream'), (str(tmp_path / 'source'), 0, 'local'),
                    (str(tmp_path / 'source'), 1, 'local'), ('gcs', 1, 'stream')]
        assert calls == [(src, 'r2', f'tmp/r2-copy-probe/owned/{r}/{mode}/static-names/g/data') for src, r, mode in expected]
        assert [(m['round'], m['mode']) for m in result['measurements']] == [(0, 'stream'), (0, 'local'), (1, 'local'), (1, 'stream')]
        assert (result['objects'], result['bytes_per_round'], result['temporary_objects_deleted']) == (1, 3, 4)
    assert deleted == [key for _, _, key in calls]
