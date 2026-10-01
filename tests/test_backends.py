"""`backend_for`: scheme → backend, including `r2://` through the S3 backend
with the bucket's endpoint, and a loud refusal for `gcs://` (which used to
fall through to the *local* backend and "succeed" with an empty scan).
"""

import re
import sys
from io import StringIO
from os.path import dirname, exists, join
from unittest.mock import MagicMock, patch

import pytest

from disk_tree import find
from disk_tree.backends import LocalBackend, S3Backend, UnsupportedBackend, backend_for
from disk_tree.blobfs import R2_ENDPOINT_VAR

#: The Tauri app's native walker (`apps/tauri`, on the app's branch); its seam test skips where it isn't built.
DT_WALKER = join(dirname(dirname(__file__)), 'apps', 'tauri', 'target', 'release', 'dt-walker')

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
    # No buckets.yml entry either: point the config root at a fresh dir. (The
    # `DISK_TREE_ROOT` env var alone isn't enough: `config.ROOT_DIR` is read at
    # import, so a real `~/.config/disk-tree/buckets.yml` with a `defaults`
    # endpoint leaked in.)
    from disk_tree import config
    monkeypatch.setenv('DISK_TREE_ROOT', str(tmp_path / 'root'))
    monkeypatch.setattr(config, 'ROOT_DIR', str(tmp_path / 'root'))
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


def test_backend_for_threads_bucket_profile(monkeypatch, tmp_path):
    """A per-bucket `profile` in buckets.yml reaches the lister backend, so a
    cross-account source authenticates with its own key (`s3://` and `r2://`);
    an unconfigured bucket gets no profile (ambient credentials)."""
    from disk_tree import config
    monkeypatch.setenv(R2_ENDPOINT_VAR, EP)
    monkeypatch.setattr(config, 'ROOT_DIR', str(tmp_path))
    (tmp_path / 'buckets.yml').write_text(
        'buckets:\n  - uri: r2://ctbk\n    profile: hccs\n  - uri: s3://raw\n    profile: aws-src\n'
    )
    assert backend_for('r2://ctbk/p').profile == 'hccs'
    assert backend_for('s3://raw/p').profile == 'aws-src'
    assert backend_for('s3://unconfigured/p').profile is None


def test_gcs_refuses_live_operations():
    gcs = backend_for('gcs://bk/p')
    msg = "live scanning of gcs:// isn't implemented; import a listing instead (`disk-tree bulk-list` + `disk-tree import -l <listing>`)"
    with pytest.raises(NotImplementedError, match=re.escape(msg)):
        list(gcs.list('gcs://bk/p'))
    with pytest.raises(NotImplementedError, match=r'delete of gcs://'):
        gcs.delete('gcs://bk/p')
    with pytest.raises(NotImplementedError, match=r'existence check of gcs://'):
        gcs.exists('gcs://bk/p')


MACOS_MOUNT = """\
/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)
devfs on /dev (devfs, local, nobrowse)
/dev/disk3s6 on /System/Volumes/VM (apfs, local, noexec, journaled, noatime, nobrowse)
/dev/disk3s5 on /System/Volumes/Data (apfs, local, journaled, nobrowse, protect, root data)
/dev/disk3s3 on /Volumes/Recovery (apfs, local, journaled, nobrowse)
/dev/disk5s1 on /Volumes/crucial x6 (apfs, local, nodev, nosuid, journaled, noowners)
map auto_home on /System/Volumes/Data/home (autofs, automounted, nobrowse)
"""
LINUX_MOUNT = """\
/dev/nvme0n1p1 on / type ext4 (rw,relatime,discard)
proc on /proc type proc (rw,nosuid,nodev,noexec,relatime)
/dev/nvme1n1 on /home/ubuntu/data type xfs (rw,relatime)
"""


def test_mount_points_parses_macos_and_linux(monkeypatch):
    from disk_tree.backends import local
    for out, expected in [
        (MACOS_MOUNT, ['/', '/dev', '/System/Volumes/VM', '/System/Volumes/Data', '/Volumes/Recovery', '/Volumes/crucial x6', '/System/Volumes/Data/home']),
        (LINUX_MOUNT, ['/', '/proc', '/home/ubuntu/data']),
    ]:
        monkeypatch.setattr(local.subprocess, 'run', lambda *a, out=out, **kw: MagicMock(stdout=out))
        assert local.mount_points() == expected


def test_mounts_below():
    from disk_tree.backends.local import mounts_below
    mounts = ['/', '/dev', '/System/Volumes/Data', '/System/Volumes/Data/home', '/Volumes/crucial x6', '/Users/x/mnt/fuse']
    assert mounts_below('/', mounts) == ['/System/Volumes/Data', '/System/Volumes/Data/home', '/Users/x/mnt/fuse', '/Volumes/crucial x6', '/dev']
    assert mounts_below('/Users/x', mounts) == ['/Users/x/mnt/fuse']
    assert mounts_below('/Users/x/mnt/fuse', mounts) == []


def test_one_fs_prunes_mounts_below_the_root_in_the_gfind_cmd(monkeypatch):
    """`one_fs` on the gfind path: every mount strictly below the root becomes a
    `-prune` (CloudStorage stays pruned too); without it, only CloudStorage."""
    from disk_tree.backends import local
    monkeypatch.delenv('DISK_TREE_WALKER', raising=False)
    monkeypatch.setattr(local, 'mount_points', lambda: ['/', '/dev', '/System/Volumes/Data', '/Volumes/ext'])
    cmds = []
    monkeypatch.setattr(local, 'run_gfind', lambda cmd, *a, **kw: cmds.append(cmd) or iter(()))
    list(LocalBackend().list('/', one_fs=True))
    list(LocalBackend().list('/'))
    printf = ['-printf', r'%y %b %T@ %p\0']
    prune = lambda p: ['-path', p, *printf, '-prune', '-o']
    assert cmds == [
        [local.FIND, '/', *prune(local.CLOUDSTORAGE_PATHS[0]), *prune(local.CLOUDSTORAGE_PATHS[1]),
         *prune('/System/Volumes/Data'), *prune('/Volumes/ext'), *prune('/dev'), *printf],
        [local.FIND, '/', *prune(local.CLOUDSTORAGE_PATHS[0]), *prune(local.CLOUDSTORAGE_PATHS[1]), *printf],
    ]


def test_one_fs_passes_through_to_the_walker(monkeypatch):
    from disk_tree.backends import local
    monkeypatch.setenv('DISK_TREE_WALKER', '/bin/dt-walker')
    monkeypatch.setattr(local, 'mount_points', lambda: pytest.fail('the walker checks mounts itself'))
    cmds = []
    monkeypatch.setattr(local, 'run_gfind', lambda cmd, *a, **kw: cmds.append(cmd) or iter(()))
    list(LocalBackend().list('/Users/x', one_fs=True))
    assert cmds == [['/bin/dt-walker', '--no-default-excludes', '--one-fs', '/Users/x']]
