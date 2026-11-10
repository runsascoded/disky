from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest

S = ModuleType('cf_status')
S.__file__ = str(Path(__file__).parent.parent / 'site/cf-status')
SourceFileLoader(S.__name__, S.__file__).exec_module(S)
SHA = 'a' * 40


def dep(branch='main', environment='production', status='success', **metadata):
    return {'environment': environment, 'latest_stage': {'status': status}, 'deployment_trigger': {'metadata': {'branch': branch, 'commit_hash': SHA, **metadata}}, 'created_on': '2026-11-09', 'short_id': 'release'}


def test_production_uses_canonical_even_when_latest_is_preview():
    calls = []
    expected = dep()
    def fetch(path):
        calls.append(path)
        return {'result': {'canonical_deployment': expected, 'latest_deployment': dep('dev', 'preview')}}
    assert S.deployment(fetch, 'demo', None) == expected
    assert calls == ['demo']


def test_preview_paginates_and_rejects_other_branches_and_failed_builds():
    calls = []
    expected = dep('r2', 'preview')
    def fetch(path):
        calls.append(path)
        return {'result': [dep('other', 'preview'), dep('r2', 'preview', 'failure')], 'result_info': {'total_pages': 2}} if len(calls) == 1 else {'result': [expected], 'result_info': {'total_pages': 2}}
    assert S.deployment(fetch, 'demo', 'r2') == expected
    assert calls == ['demo/deployments?env=preview&per_page=25&page=1', 'demo/deployments?env=preview&per_page=25&page=2']


@pytest.mark.parametrize('canonical', [None, dep(environment='preview'), dep(status='failure')])
def test_missing_or_invalid_production_does_not_fall_back(canonical):
    with pytest.raises(ValueError) as exc:
        S.deployment(lambda _: {'result': {'canonical_deployment': canonical}}, 'demo', None)
    assert str(exc.value) == 'no successful canonical production deployment found'


def test_missing_preview_is_explicit():
    with pytest.raises(ValueError) as exc:
        S.deployment(lambda _: {'result': []}, 'demo', 'dev')
    assert str(exc.value) == 'no successful preview deployment for branch dev'


@pytest.mark.parametrize('local_dirty,remote_dirty,expected', [
    ('', False, '✓ HEAD is deployed'),
    (' M src/App.tsx', False, '? local tracked changes differ from HEAD; commit equality cannot verify this checkout'),
    ('', True, '? deployed from a dirty checkout; its commit does not identify the shipped tree'),
])
def test_check_uses_script_checkout_and_reports_dirty_trees(tmp_path, monkeypatch, capsys, local_dirty, remote_dirty, expected):
    (tmp_path / 'wrangler.toml').write_text('name = "demo" # inline comment\n')
    monkeypatch.setenv('CLOUDFLARE_ACCOUNT_ID', 'account')
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN', 'test-token')
    monkeypatch.setattr(S, 'get_json', lambda *args: {'result': {'canonical_deployment': dep(commit_dirty=remote_dirty)}})
    def git(site, *args):
        assert site == tmp_path
        return SHA if args == ('rev-parse', 'HEAD') else local_dirty
    monkeypatch.setattr(S, 'git', git)
    assert S.check(tmp_path, False) == int(bool(local_dirty or remote_dirty))
    assert capsys.readouterr().out.splitlines() == ['environment: production (canonical)', 'deployed: aaaaaaaaa  (2026-11-09)  [release]', 'HEAD:     aaaaaaaaa', expected]


def test_preview_uses_configured_branch(tmp_path, monkeypatch, capsys):
    (tmp_path / 'wrangler.toml').write_text('name = "demo"\n')
    (tmp_path / 'deploy.env').write_text('DEV_BRANCH=r2\n')
    monkeypatch.setenv('CLOUDFLARE_ACCOUNT_ID', 'account')
    monkeypatch.setenv('CLOUDFLARE_API_TOKEN', 'test-token')
    monkeypatch.setattr(S, 'get_json', lambda *args: {'result': [dep('r2', 'preview')]})
    monkeypatch.setattr(S, 'git', lambda site, *args: SHA if args == ('rev-parse', 'HEAD') else '')
    assert S.check(tmp_path, True) == 0
    assert capsys.readouterr().out.splitlines() == ['environment: preview (r2)', 'deployed: aaaaaaaaa  (2026-11-09)  [release]', 'HEAD:     aaaaaaaaa', '✓ HEAD is deployed']
