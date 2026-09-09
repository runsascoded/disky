"""`backend_for`: scheme → backend, including `r2://` through the S3 backend
with the bucket's endpoint, and a loud refusal for `gcs://` (which used to
fall through to the *local* backend and "succeed" with an empty scan).
"""

import sys
from io import StringIO
from os.path import dirname, exists, join
from unittest.mock import MagicMock, patch

import pytest

from disk_tree import find
from disk_tree.backends import LocalBackend, S3Backend, UnsupportedBackend, backend_for
from disk_tree.blobfs import R2_ENDPOINT_VAR

TESTDATA = join(dirname(__file__), 'data')
EP = 'https://acct.r2.cloudflarestorage.com'

# The native walker binary (built by `cargo build --release` in apps/tauri).
DT_WALKER = join(dirname(dirname(__file__)), 'apps', 'tauri', 'target', 'release', 'dt-walker')


def test_dispatch_by_scheme(monkeypatch):
    monkeypatch.setenv(R2_ENDPOINT_VAR, EP)
    assert type(backend_for('/Users/x')) is LocalBackend
    s3 = backend_for('s3://bk/p')
    assert (type(s3), s3.scheme, s3.endpoint_url) == (S3Backend, 's3', None)
    r2 = backend_for('r2://bk/p')
    assert (type(r2), r2.scheme, r2.endpoint_url) == (S3Backend, 'r2', EP)
    gcs = backend_for('gcs://bk')
    assert (type(gcs), gcs.scheme, gcs.is_local) == (UnsupportedBackend, 'gcs', False)


def test_r2_lists_through_the_endpoint_with_r2_uris(monkeypatch):
    monkeypatch.setenv(R2_ENDPOINT_VAR, EP)
    with open(join(TESTDATA, 's3.txt')) as f:
        listing = f.read()
    with patch('subprocess.Popen') as popen:
        proc = MagicMock()
        proc.stdout = StringIO(listing)
        popen.return_value = proc
        result = find.index('r2://runsascoded/gopro')
    # The aws CLI is invoked against the endpoint with an `s3://` URL…
    assert popen.call_args.args[0] == ['aws', '--endpoint-url', EP, 's3', 'ls', '--recursive', 's3://runsascoded/gopro']
    # …and the scan is the same shape as the s3 fixture's, with `r2://` uris.
    with patch('subprocess.Popen') as popen2:
        proc2 = MagicMock()
        proc2.stdout = StringIO(listing)
        popen2.return_value = proc2
        expected = find.index('s3://runsascoded/gopro').df
    expected['uri'] = expected['uri'].str.replace('^s3://', 'r2://', regex=True)
    assert result.df.equals(expected)
    assert result.df['uri'].str.startswith('r2://').all()


def test_r2_without_an_endpoint_refuses_at_list_time(monkeypatch, tmp_path):
    monkeypatch.delenv(R2_ENDPOINT_VAR, raising=False)
    # No buckets.yml entry either (DISK_TREE_ROOT points at a fresh dir).
    monkeypatch.setenv('DISK_TREE_ROOT', str(tmp_path / 'root'))
    r2 = backend_for('r2://bk')
    assert r2.endpoint_url is None
    with pytest.raises(RuntimeError, match=f'r2:// needs an S3 endpoint — set {R2_ENDPOINT_VAR}'):
        list(r2.list('r2://bk'))


@pytest.mark.skipif(sys.platform != 'darwin', reason='dt-walker is a macOS getattrlistbulk walker')
@pytest.mark.skipif(not exists(DT_WALKER), reason='dt-walker not built (cargo build --release in apps/tauri)')
def test_dt_walker_seam_matches_gfind(tmp_path, monkeypatch):
    """`DISK_TREE_WALKER` swaps the scan source from `gfind` to the native walker
    and produces a byte-identical aggregated scan — the drop-in guarantee."""
    tree = tmp_path / 'tree'
    (tree / 'sub').mkdir(parents=True)
    (tree / 'a.txt').write_text('hello world\n')
    (tree / 'big.bin').write_bytes(b'\0' * 100_000)
    (tree / 'sub' / 'b.txt').write_text('x\n')

    monkeypatch.delenv('DISK_TREE_WALKER', raising=False)
    via_gfind = find.index(str(tree)).df

    monkeypatch.setenv('DISK_TREE_WALKER', DT_WALKER)
    via_walker = find.index(str(tree)).df

    # Same rows, same order, same every column (path/size/mtime/kind/parent/uri/
    # n_desc/n_children/depth). mtime is int-truncated identically by both paths.
    assert via_walker.equals(via_gfind)


def test_gcs_refuses_live_operations():
    gcs = backend_for('gcs://bk/p')
    msg = r"live scanning of gcs:// isn't implemented; import a listing instead \(`disk-tree pull`"
    with pytest.raises(NotImplementedError, match=msg):
        list(gcs.list('gcs://bk/p'))
    with pytest.raises(NotImplementedError, match=r'delete of gcs://'):
        gcs.delete('gcs://bk/p')
    with pytest.raises(NotImplementedError, match=r'existence check of gcs://'):
        gcs.exists('gcs://bk/p')
