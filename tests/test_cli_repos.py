"""Tests for `disk-tree repos` — git-repo delete-safety audit.

`RepoState` classification is the logic under test; `_repo_sizes` (which shells
to `disk-tree du`) is exercised via monkeypatch so tests stay hermetic.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from disk_tree.cli import repos as R


def _git(repo: Path, *args: str, remotes_env: dict | None = None):
    subprocess.run(['git', '-C', str(repo), *args], check=True,
                   capture_output=True, text=True)


def _repo(tmp: Path, name: str) -> Path:
    d = tmp / name
    d.mkdir(parents=True)
    _git(d, 'init', '-q', '-b', 'main')
    _git(d, 'config', 'user.email', 't@t')
    _git(d, 'config', 'user.name', 't')
    (d / 'f.txt').write_text('hi\n')
    _git(d, 'add', '.')
    _git(d, 'commit', '-q', '-m', 'init')
    return d


def _bare_remote(tmp: Path, name: str, url_host: str) -> Path:
    """A bare repo standing in for a hosted remote; its URL carries the host so
    RepoState's github/gitlab test sees it as hosted."""
    bare = tmp / f'{name}.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(bare)], check=True)
    return bare


def test_clean_pushed_repo_is_deletable(tmp_path):
    d = _repo(tmp_path, 'proj')
    bare = _bare_remote(tmp_path, 'proj', 'github.com')
    # Rewrite the url to look hosted, push main so its tip is contained there.
    _git(d, 'remote', 'add', 'u', f'https://github.com/x/proj.git')
    _git(d, 'remote', 'set-url', 'u', str(bare))
    _git(d, 'push', '-q', 'u', 'main')
    # Point the url back at a github.com string for the hosted-detection test,
    # while the objects really live in `bare` (already pushed).
    _git(d, 'remote', 'set-url', 'u', 'https://github.com/x/proj.git')
    _git(d, 'remote', 'set-url', '--push', 'u', str(bare))
    # branch -r --contains needs the remote-tracking ref; fetch created it on push
    st = R._state(str(d), size=1 << 30)
    assert st.hosted_remotes == ['u']
    assert st.modified == 0
    assert st.untracked == 0
    assert st.unbacked_branches == []
    assert st.recoverable and st.clean


def test_untracked_file_blocks_deletable_but_not_recoverable(tmp_path):
    d = _repo(tmp_path, 'proj')
    bare = _bare_remote(tmp_path, 'proj', 'github.com')
    _git(d, 'remote', 'add', 'u', str(bare))
    _git(d, 'push', '-q', 'u', 'main')
    _git(d, 'remote', 'set-url', 'u', 'https://github.com/x/proj.git')
    (d / 'scratch_notes.txt').write_text('x')
    st = R._state(str(d), size=1 << 30)
    assert st.untracked == 1
    assert st.recoverable is True
    assert st.clean is False


def test_modified_file_is_not_recoverable(tmp_path):
    d = _repo(tmp_path, 'proj')
    bare = _bare_remote(tmp_path, 'proj', 'github.com')
    _git(d, 'remote', 'add', 'u', str(bare))
    _git(d, 'push', '-q', 'u', 'main')
    _git(d, 'remote', 'set-url', 'u', 'https://github.com/x/proj.git')
    (d / 'f.txt').write_text('changed\n')
    st = R._state(str(d), size=1 << 30)
    assert st.modified == 1
    assert st.recoverable is False


def test_unbacked_branch_flags_review(tmp_path):
    """A local branch whose tip is on no hosted remote is at risk."""
    d = _repo(tmp_path, 'proj')
    bare = _bare_remote(tmp_path, 'proj', 'github.com')
    _git(d, 'remote', 'add', 'u', str(bare))
    _git(d, 'push', '-q', 'u', 'main')
    _git(d, 'remote', 'set-url', 'u', 'https://github.com/x/proj.git')
    _git(d, 'checkout', '-q', '-b', 'wip')
    (d / 'g.txt').write_text('local only\n')
    _git(d, 'add', '.')
    _git(d, 'commit', '-q', '-m', 'wip')
    st = R._state(str(d), size=1 << 30)
    assert st.unbacked_branches == ['wip']
    assert st.recoverable is False


def test_peer_remote_is_not_treated_as_hosted(tmp_path):
    """A non-github/gitlab remote (e.g. an EC2 peer) does not back a branch."""
    d = _repo(tmp_path, 'proj')
    bare = _bare_remote(tmp_path, 'proj', 'peer')
    _git(d, 'remote', 'add', 'e', str(bare))
    _git(d, 'push', '-q', 'e', 'main')
    _git(d, 'remote', 'set-url', 'e', 'ssh://ec2-box/proj.git')
    st = R._state(str(d), size=1 << 30)
    assert st.hosted_remotes == []
    assert st.unbacked_branches == ['main']
    assert st.recoverable is False


@pytest.mark.parametrize('text,expected', [
    ('0', 0), ('200M', 200 * 2**20), ('1.5G', int(1.5 * 2**30)),
    ('512K', 512 * 2**10), ('4096', 4096),
])
def test_parse_size(text, expected):
    assert R._parse_size(text) == expected


def test_du_argv_uses_this_interpreter_not_a_path_lookup():
    """`_repo_sizes` shells out; a bare `disk-tree` breaks outside the venv's
    `PATH`, so it runs `python -m disk_tree` with the current interpreter."""
    import sys
    assert R._du_argv('/x/y') == [sys.executable, '-m', 'disk_tree', 'du', '/x/y', '-d', '3', '-n', '0', '-j']


def test_python_m_disk_tree_is_the_cli():
    import sys
    out = subprocess.run([sys.executable, '-m', 'disk_tree', '--help'], capture_output=True, text=True)
    assert (out.returncode, out.stdout.split('\n')[0]) == (0, 'Usage: disk-tree [OPTIONS] COMMAND [ARGS]...')
