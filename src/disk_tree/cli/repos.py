"""`disk-tree repos` — a cleanup-oriented view of the git repos under a root.

Joins each repo's size (from the freshest covering scan) to its git state, so
"what can I delete to free space" is answerable in one place: size, whether the
tree is dirty, whether every local branch is safe on a hosted remote, and — the
part disk-tree is uniquely positioned to add — whether the size is real or
mostly reflinked (`--reclaim`).

`recoverable` means: no modified tracked files, no unpushed commits, and every
local branch tip contained in a github.com/gitlab.com remote. Untracked files
are reported separately — they are never in git, so they gate a safe delete
even for an otherwise-clean repo.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

from click import argument, option
from humanize import naturalsize

from disk_tree.cli.base import cli

HOSTED = ('github.com', 'gitlab.com')


def _git(repo: str, *args: str) -> str:
    r = subprocess.run(['git', '-C', repo, *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ''


@dataclass
class RepoState:
    path: str
    size: int
    modified: int
    untracked: int
    hosted_remotes: list[str] = field(default_factory=list)
    unbacked_branches: list[str] = field(default_factory=list)
    ncommits_at_risk: int = 0

    @property
    def recoverable(self) -> bool:
        return self.modified == 0 and not self.unbacked_branches

    @property
    def clean(self) -> bool:
        return self.recoverable and self.untracked == 0


def _du_argv(root: str) -> list[str]:
    """The `du` subprocess: this interpreter's `python -m disk_tree`, so it works
    wherever the package is importable (a bare `disk-tree` needs the venv's bin
    on `PATH`, which a LaunchAgent or another project's shell may not have)."""
    return [sys.executable, '-m', 'disk_tree', 'du', root, '-d', '3', '-n', '0', '-j']


def _repo_sizes(root: str) -> dict[str, int]:
    """path (abs) → size, for every dir under `root` in the freshest scan."""
    out = subprocess.run(_du_argv(root), capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(out.stderr.strip() or 'no scan covers that root')
    sizes: dict[str, int] = {}

    def walk(rows):
        for r in rows:
            if r['kind'] == 'dir':
                sizes[os.path.join(root, r['path'])] = r['size']
            walk(r.get('children', []))

    walk(json.loads(out.stdout)['rows'])
    return sizes


def _state(repo: str, size: int) -> RepoState:
    remotes = {}
    for line in _git(repo, 'remote', '-v').split('\n'):
        if '\t' in line and '(fetch)' in line:
            name, rest = line.split('\t', 1)
            remotes[name] = rest.split(' ')[0]
    hosted = sorted(n for n, u in remotes.items() if any(h in u for h in HOSTED))

    status = [ln for ln in _git(repo, 'status', '--porcelain').split('\n') if ln]
    modified = sum(1 for ln in status if not ln.startswith('??'))
    untracked = sum(1 for ln in status if ln.startswith('??'))

    unbacked, at_risk = [], 0
    for b in [x for x in _git(repo, 'for-each-ref', '--format=%(refname:short)', 'refs/heads').split('\n') if x]:
        contained = [x.strip() for x in _git(repo, 'branch', '-r', '--contains', b).split('\n') if x.strip()]
        if not any(rb.split('/')[0] in hosted for rb in contained):
            unbacked.append(b)
            if hosted:
                cnt = _git(repo, 'rev-list', '--count', b, '--not', *[f'{r}/HEAD' for r in hosted])
                at_risk += int(cnt) if cnt.isdigit() else 0

    return RepoState(repo, size, modified, untracked, hosted, unbacked, at_risk)


@cli.command('repos')
@option('-H', '--no-human', is_flag=True, help='Print raw bytes instead of human-readable sizes')
@option('-j', '--json', 'as_json', is_flag=True, help='Emit JSON')
@option('-m', '--min-size', default='200M', help='Skip repos smaller than this (e.g. 500M, 1G; 0 for all)')
@option('-r', '--recoverable', is_flag=True, help='Only repos safe to delete (clean + fully pushed)')
@argument('root')
def repos_cmd(no_human: bool, as_json: bool, min_size: str, recoverable: bool, root: str):
    """Audit git repos under ROOT for size and delete-safety."""
    root = os.path.abspath(os.path.expanduser(root)).rstrip('/')
    floor = _parse_size(min_size)
    sizes = _repo_sizes(root)
    repos = [_state(p, s) for p, s in sizes.items()
             if s >= floor and os.path.isdir(os.path.join(p, '.git'))]
    repos.sort(key=lambda r: -r.size)
    if recoverable:
        repos = [r for r in repos if r.recoverable]

    if as_json:
        print(json.dumps([{
            'path': r.path, 'size': r.size, 'modified': r.modified,
            'untracked': r.untracked, 'hosted_remotes': r.hosted_remotes,
            'unbacked_branches': r.unbacked_branches, 'recoverable': r.recoverable,
            'clean': r.clean,
        } for r in repos], indent=2))
        return

    human = not no_human
    def fmt(n: int) -> str:
        return naturalsize(n, binary=True, format='%.3g') if human else str(n)
    print(f"{'size':>9}  {'verdict':<11}  {'dirty':>5} {'untrk':>5}  path")
    for r in repos:
        verdict = 'DELETABLE' if r.clean else 'recoverable' if r.recoverable else 'REVIEW'
        rel = os.path.relpath(r.path, root)
        print(f"{fmt(r.size):>9}  {verdict:<11}  {r.modified:>5} {r.untracked:>5}  {rel}")
        for b in r.unbacked_branches:
            print(f"{'':>9}  {'':11}  → unbacked branch: {b}")
    deletable = [r for r in repos if r.clean]
    if deletable:
        print(f"\n{len(deletable)} DELETABLE (clean + pushed, no untracked): "
              f"{fmt(sum(r.size for r in deletable))}")


def _parse_size(s: str) -> int:
    s = s.strip().upper()
    if s in ('0', ''):
        return 0
    mult = {'K': 2**10, 'M': 2**20, 'G': 2**30, 'T': 2**40}
    if s[-1] in mult:
        return int(float(s[:-1]) * mult[s[-1]])
    return int(s)
