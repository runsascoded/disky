"""A release uses a clean commit, including nonignored untracked source. (A deployment's image build, `job/build.sh`, gates the same way on its own branch.)"""

import os
from pathlib import Path
import shutil
from subprocess import run

import pytest


ROOT = Path(__file__).resolve().parents[1]
REFUSAL = 'release: commit all source changes before deploying or building an image\n'


def git(repo: Path, *args: str) -> str:
    return run(['git', '-C', str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, 'init', '-q')
    git(tmp_path, 'config', 'user.name', 'Release Test')
    git(tmp_path, 'config', 'user.email', 'release@example.test')
    git(tmp_path, 'config', 'core.hooksPath', '/dev/null')
    git(tmp_path, 'config', 'core.excludesFile', '/dev/null')
    (tmp_path / '.git/info/exclude').write_text('scratch/\ntmp/\n')
    for name in ('scripts/require-clean', 'site/deploy', 'site/toml-check'):
        target = tmp_path / name
        target.parent.mkdir(exist_ok=True)
        shutil.copy(ROOT / name, target)
    (tmp_path / 'site/wrangler.toml').write_text('name = "release-test"\n[vars]\n')
    (tmp_path / 'source.txt').write_text('committed\n')
    git(tmp_path, 'add', 'scripts', 'site', 'source.txt')
    git(tmp_path, 'commit', '-qm', 'Fixture')
    return tmp_path


def invoke(repo: Path, script: str, *args: str, env: dict[str, str] | None = None):
    return run([str(repo / script), *args], cwd=repo, env={**os.environ, **(env or {})}, capture_output=True, text=True)


def test_clean_commit_and_ignored_scratch(repo: Path):
    (repo / 'scratch').mkdir()
    (repo / 'scratch/data').write_text('generated\n')
    result = invoke(repo, 'scripts/require-clean')
    assert (result.returncode, result.stdout, result.stderr) == (0, git(repo, 'rev-parse', 'HEAD') + '\n', '')


@pytest.mark.parametrize(('kind', 'status'), [('unstaged', ' M source.txt'), ('staged', 'M  source.txt'), ('untracked', '?? new.py')])
def test_refuse_every_kind_of_uncommitted_source(repo: Path, kind: str, status: str):
    if kind == 'untracked':
        (repo / 'new.py').write_text('pass\n')
    else:
        (repo / 'source.txt').write_text('edited\n')
        if kind == 'staged':
            git(repo, 'add', 'source.txt')
    for script, args in [('scripts/require-clean', ()), ('site/deploy', ('-P',))]:
        result = invoke(repo, script, *args, env={'ALLOW_DIRTY': '1'})
        assert (result.returncode, result.stdout, result.stderr) == (1, '', REFUSAL + status + '\n')


def test_head_must_stay_at_the_build_commit(repo: Path):
    previous = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'commit', '--allow-empty', '-qm', 'New revision')
    result = invoke(repo, 'scripts/require-clean', previous)
    assert (result.returncode, result.stdout, result.stderr) == (
        1, '', 'release: HEAD changed during the build; rebuild from the intended commit\n',
    )


def test_site_refuses_edits_during_build_before_publication(repo: Path):
    fake_bin = repo / 'scratch'
    fake_bin.mkdir()
    pnpm = fake_bin / 'pnpm'
    pnpm.write_text('#!/usr/bin/env bash\nset -eu\necho "$*" >> ../scratch/calls\necho edited > ../source.txt\n')
    pnpm.chmod(0o755)
    result = invoke(repo, 'site/deploy', '-P', env={
        'PATH': str(fake_bin) + os.pathsep + os.environ['PATH'],
        'CLOUDFLARE_ACCOUNT_ID': 'test-account',
    })
    assert (result.returncode, result.stdout, result.stderr) == (1, '', REFUSAL + ' M source.txt\n')
    assert (fake_bin / 'calls').read_text() == 'build\n'
    assert git(repo, 'branch', '--list', 'release-test-prod') == ''

