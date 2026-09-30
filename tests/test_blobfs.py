"""`disk_tree.blobfs` — the local-vs-URL seam (spec `remote-scan-targets.md`).

`memory://` exercises every URL branch with no network and no extra drivers.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pandas as pd
import pytest

from disk_tree import blobfs, config

pytest.importorskip('fsspec')


def test_is_url():
    assert [blobfs.is_url(p) for p in ['/a/b', 'r2://b/p', 'memory://x', 'file:///tmp']] == [False, True, True, True]


def test_join_is_textual_for_urls_and_os_join_locally():
    assert blobfs.join('r2://b/p/', 'x.parquet') == 'r2://b/p/x.parquet'
    assert blobfs.join('r2://b/p', 'x.parquet') == 'r2://b/p/x.parquet'
    assert blobfs.join('/d', 'x.parquet') == '/d/x.parquet'


def test_fs_for_memory_url():
    fs, path = blobfs.fs_for('memory://blobs/a')
    assert type(fs).__name__ == 'MemoryFileSystem'
    assert path == '/blobs/a'


def test_fs_for_unknown_scheme_raises():
    with pytest.raises(ValueError, match='Protocol not known: bogus'):
        blobfs.fs_for('bogus://x')


def test_r2_endpoint_env_wins(monkeypatch):
    monkeypatch.setenv(blobfs.R2_ENDPOINT_VAR, 'https://env.example')
    assert blobfs.r2_endpoint('any') == 'https://env.example'


def test_r2_endpoint_from_buckets_yml(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(blobfs.R2_ENDPOINT_VAR, raising=False)
    monkeypatch.setattr(config, 'ROOT_DIR', str(tmp_path))
    (tmp_path / 'buckets.yml').write_text(
        'defaults:\n  endpoint_url: https://default.example\n'
        'buckets:\n  - uri: r2://ctbk\n    endpoint_url: https://ctbk.example\n  - r2://other\n'
    )
    assert blobfs.r2_endpoint('ctbk') == 'https://ctbk.example'
    assert blobfs.r2_endpoint('other') == 'https://default.example'


def test_r2_endpoint_none_without_config(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(blobfs.R2_ENDPOINT_VAR, raising=False)
    monkeypatch.setattr(config, 'ROOT_DIR', str(tmp_path))
    assert blobfs.r2_endpoint('ctbk') is None


def test_bucket_profile_per_bucket_then_defaults(monkeypatch, tmp_path: Path):
    """A bucket's own `profile` wins; a bucket without one inherits `defaults`;
    a bucket absent from the config resolves to `defaults` too."""
    monkeypatch.setattr(config, 'ROOT_DIR', str(tmp_path))
    (tmp_path / 'buckets.yml').write_text(
        'defaults:\n  profile: rac\n'
        'buckets:\n'
        '  - uri: r2://ctbk\n    profile: hccs\n'
        '  - uri: r2://disk-tree-demo\n'
    )
    assert blobfs.bucket_profile('ctbk') == 'hccs'          # per-bucket wins
    assert blobfs.bucket_profile('disk-tree-demo') == 'rac'  # falls to defaults
    assert blobfs.bucket_profile('never-configured') == 'rac'


def test_bucket_profile_none_without_config(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config, 'ROOT_DIR', str(tmp_path))
    assert blobfs.bucket_profile('ctbk') is None


def test_s3fs_threads_profile_when_set(monkeypatch):
    """A per-bucket profile reaches `s3fs` as `profile=`; without one the kwarg
    is absent (ambient credential resolution)."""
    import s3fs
    captured: dict = {}

    class Fake:
        def __init__(self, **kw):
            captured.clear()
            captured.update(kw)

    monkeypatch.setattr(s3fs, 'S3FileSystem', Fake)
    ep = f'https://ep-{uuid4()}.example'  # fresh endpoint dodges the lru_cache
    blobfs._s3fs(ep, 'hccs')
    assert captured == {'client_kwargs': {'endpoint_url': ep}, 'fixed_upload_size': True, 'profile': 'hccs'}
    blobfs._s3fs(f'https://ep-{uuid4()}.example')  # no profile → no `profile` kwarg
    assert 'profile' not in captured


def test_fs_for_r2_without_endpoint_is_a_pointed_error(monkeypatch, tmp_path: Path):
    monkeypatch.delenv(blobfs.R2_ENDPOINT_VAR, raising=False)
    monkeypatch.setattr(config, 'ROOT_DIR', str(tmp_path))
    with pytest.raises(RuntimeError) as e:
        blobfs.fs_for('r2://ctbk/scans')
    assert str(e.value) == (
        'r2://ctbk: no endpoint — set DISK_TREE_R2_ENDPOINT_URL, or give the bucket an `endpoint_url` in buckets.yml'
    )


def test_s3fs_sets_fixed_upload_size_for_r2(monkeypatch):
    """R2 rejects varying-length multipart parts; s3fs only guarantees fixed
    lengths under `fixed_upload_size=True` (spec `r2-scan-target.md`)."""
    import s3fs
    captured: dict = {}

    class Fake:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setattr(s3fs, 'S3FileSystem', Fake)
    ep = f'https://ep-{uuid4()}.example'  # fresh endpoint dodges the lru_cache
    blobfs._s3fs(ep)
    assert captured == {'client_kwargs': {'endpoint_url': ep}, 'fixed_upload_size': True}


def test_remote_write_read_list_put_remove(tmp_path: Path):
    d = f'memory://{uuid4()}'
    df = pd.DataFrame({'path': ['.', 'a'], 'depth': [0, 1], 'size': [3, 2]})
    p = blobfs.join(d, 'x.parquet')
    assert blobfs.exists(p) is False

    blobfs.write_parquet(df, p, 1)
    assert blobfs.exists(p) is True
    assert blobfs.read_schema(p).names == ['path', 'depth', 'size']
    assert blobfs.read_table(p, columns=['path'])['path'].to_pylist() == ['.', 'a']
    assert blobfs.read_parquet(p, filters=[('depth', '<=', 0)])['path'].tolist() == ['.']
    fs, inner = blobfs.fs_for(p)
    assert blobfs.size(p) == len(fs.cat(inner))
    assert blobfs.mtime(p) > 0

    local = tmp_path / 'y.parquet'
    df.to_parquet(local, index=False)
    blobfs.put(str(local), blobfs.join(d, 'y.parquet'))
    assert blobfs.list_parquets(d) == ['x.parquet', 'y.parquet']

    blobfs.remove(p)
    assert blobfs.exists(p) is False
    assert blobfs.list_parquets(d) == ['y.parquet']
