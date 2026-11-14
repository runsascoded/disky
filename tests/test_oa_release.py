"""The release controller fails closed on wrong source, stale CI and unproven production promotions."""
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from json import dumps, loads
from pathlib import Path

from click import ClickException
from click.testing import CliRunner
import pytest


ROOT = Path(__file__).resolve().parents[1]
loader = SourceFileLoader('oa_release', str(ROOT / 'scripts/oa-release'))
spec = spec_from_loader(loader.name, loader)
release = module_from_spec(spec)
loader.exec_module(release)
SHA = 'a' * 40
REPO = 'Open-Athena/disky'


def ci(branch='hf'):
    return {'id': 42, 'head_branch': branch, 'head_sha': SHA, 'head_repository': {'full_name': REPO},
            'event': 'push', 'conclusion': 'success', 'path': '.github/workflows/ci.yml'}


def setup_api(monkeypatch, *, tip=SHA, proof=None, states=None, runs=None, comparison='ahead', tip_recipes=None):
    """`tip_recipes`: the `.sql` recipes at the branch's tip; None: no `site/migrations` there."""
    calls = []

    def api(path, body=None):
        calls.append((path, body))
        if path == f'repos/{REPO}/contents/site/migrations?ref={tip}':
            return [{'name': 'README.md', 'type': 'file'}, *({'name': n, 'type': 'file'} for n in tip_recipes)]
        if path == f'repos/{REPO}/contents/site?ref={tip}':
            return [{'name': 'src', 'type': 'dir'}, *([{'name': 'migrations', 'type': 'dir'}] if tip_recipes is not None else [])]
        if '/git/ref/heads/' in path:
            return {'object': {'sha': tip}}
        if '/compare/' in path:
            return {'status': comparison}
        if path.endswith('/deployments/91'):
            return proof
        if '/statuses?' in path:
            return [{'state': s} for s in (states or ['success'])]
        if '/actions/workflows/' in path:
            return {'workflow_runs': [ci()] if runs is None else runs}
        raise AssertionError((path, body))

    monkeypatch.setattr(release, 'api', api)
    return calls


@pytest.mark.parametrize(('branch', 'project', 'pages'), [
    ('gcs', 'oa-gcs-usage', 'dev'), ('cw', 'oa-cw-s3-usage', 'dev'),
    ('hf', 'oa-hf-usage', 'dev'), ('r2', 'oa-r2-usage', 'r2'),
])
def test_exact_dev_plan(monkeypatch, branch, project, pages):
    calls = setup_api(monkeypatch)
    assert release.plan_release({'workflow_run': ci(branch)}, 'workflow_run', REPO) == {
        'protocol': 'disky-pages-v1', 'repository': REPO, 'source_branch': branch, 'sha': SHA,
        'mode': 'dev', 'environment': f'{branch}-dev', 'project': project, 'pages_branch': pages,
        'host': f'dev.{branch}.oa.dev', 'ci_run': 42, 'dev_deployment': None, 'tip_recipes': None,
    }
    assert calls == [(f'repos/{REPO}/git/ref/heads/{branch}', None)]


@pytest.mark.parametrize(('field', 'value', 'message'), [
    ('head_repository', {'full_name': 'someone/fork'}, 'Only successful in-repository integration push CI can deploy dev'),
    ('event', 'pull_request', 'Only successful in-repository integration push CI can deploy dev'),
    ('conclusion', 'failure', 'Only successful in-repository integration push CI can deploy dev'),
    ('path', '.github/workflows/unrelated.yml', 'Only successful in-repository integration push CI can deploy dev'),
    ('head_branch', 'cloud', 'Unknown OA deployment'),
    ('head_sha', 'a' * 10, 'A full lowercase source SHA is required'),
])
def test_refuse_untrusted_ci(monkeypatch, field, value, message):
    calls = setup_api(monkeypatch)
    event = {**ci(), field: value}
    with pytest.raises(ClickException) as error:
        release.plan_release({'workflow_run': event}, 'workflow_run', REPO)
    assert str(error.value) == message
    assert calls == []


def test_stale_ci_cannot_overwrite_newer_dev(monkeypatch):
    setup_api(monkeypatch, tip='b' * 40)
    with pytest.raises(ClickException) as error:
        release.plan_release({'workflow_run': ci()}, 'workflow_run', REPO)
    assert str(error.value) == 'Obsolete CI completion: source branch has moved'


def proof():
    return {'sha': SHA, 'environment': 'hf-dev', 'creator': {'login': 'github-actions[bot]'},
            'payload': {'protocol': 'disky-pages-v1', 'source_branch': 'hf'}}


def promote():
    return release.plan_release({'inputs': {'deployment': 'hf', 'sha': SHA, 'dev_deployment': '91'}}, 'workflow_dispatch', REPO)


def test_production_uses_explicit_dev_sha_even_after_branch_advances(monkeypatch):
    setup_api(monkeypatch, tip='b' * 40, proof=proof(), states=['inactive', 'success'], tip_recipes=['c-new.sql'])
    assert promote() == {
        'protocol': 'disky-pages-v1', 'repository': REPO, 'source_branch': 'hf', 'sha': SHA,
        'mode': 'prod', 'environment': 'hf-prod', 'project': 'oa-hf-usage', 'pages_branch': 'main',
        'host': 'hf.oa.dev', 'ci_run': 42, 'dev_deployment': '91', 'tip_recipes': ['c-new.sql'],
    }


def test_production_plan_without_recipes_at_the_tip(monkeypatch):
    setup_api(monkeypatch, proof=proof())
    assert promote()['tip_recipes'] == []


@pytest.mark.parametrize(('change', 'states'), [
    ({'sha': 'b' * 40}, ['success']), ({'environment': 'cw-dev'}, ['success']),
    ({'creator': {'login': 'some-user'}}, ['success']), ({'payload': {}}, ['success']),
    ({}, ['failure']), ({}, ['in_progress']),
])
def test_production_rejects_false_dev_proof(monkeypatch, change, states):
    setup_api(monkeypatch, proof={**proof(), **change}, states=states)
    with pytest.raises(ClickException) as error:
        promote()
    assert str(error.value) == 'Production requires a successful exact-SHA dev release from this controller'


@pytest.mark.parametrize('runs', [[], [{**ci(), 'conclusion': 'failure'}]])
def test_production_requires_current_ci_receipt(monkeypatch, runs):
    setup_api(monkeypatch, proof=proof(), runs=runs)
    with pytest.raises(ClickException) as error:
        promote()
    assert str(error.value) == ('No CI receipt for this source SHA' if not runs else 'The latest CI attempt for this revision must succeed')


def test_reject_production_sha_off_deployment_branch(monkeypatch):
    setup_api(monkeypatch, comparison='diverged')
    with pytest.raises(ClickException) as error:
        promote()
    assert str(error.value) == 'Production SHA is not on the deployment branch'


def test_artifact_seal_and_tamper_refusal(tmp_path, monkeypatch):
    monkeypatch.setattr(release, 'validate_source', lambda source, sha: None)
    source = tmp_path / 'source'
    site = source / 'site'
    (site / 'dist/assets').mkdir(parents=True)
    (site / 'dist/_worker.js').mkdir()
    (site / 'dist/_worker.js/index.js').write_text('export default {};\n')
    (site / 'wrangler.toml').write_text('name="oa-hf-usage"\n[vars]\nDEV_HOST="dev.hf.oa.dev"\n')
    (site / 'dist/index.html').write_text('<script src="/assets/app.js"></script>\n')
    (site / 'dist/assets/app.js').write_text('const answer=42;\n')
    setup_api(monkeypatch)
    plan = release.plan_release({'workflow_run': ci()}, 'workflow_run', REPO)
    plan_path, seal_path, receipt = [tmp_path / n for n in ['plan.json', 'seal.json', 'receipt.json']]
    plan_path.write_text(dumps(plan))
    runner = CliRunner()
    result = runner.invoke(release.main, ['seal', '-p', str(plan_path), '-o', str(seal_path), str(source)])
    assert (result.exit_code, result.output) == (0, '')
    sealed = loads(seal_path.read_text())
    assert sorted(sealed) == sorted([*plan, 'config_sha256', 'artifact_sha256', 'entry_assets'])
    assert sealed['entry_assets'] == {'assets/app.js': release.sha256(b'const answer=42;\n').hexdigest()}
    (site / 'dist/assets/app.js').write_text('changed\n')
    result = runner.invoke(release.main, ['begin', '-p', str(plan_path), '-s', str(seal_path), '-o', str(receipt), str(source)])
    assert (result.exit_code, result.output, receipt.exists()) == (1, 'Error: Upload artifact/config changed\n', False)


def test_seal_holds_a_release_while_a_d1_recipe_is_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(release, 'validate_source', lambda source, sha: None)
    source = tmp_path / 'source'
    site = source / 'site'
    (site / 'migrations').mkdir(parents=True)
    (site / 'migrations/b-tables.sql').write_text('ALTER TABLE t ADD COLUMN c TEXT;\n')
    (site / 'migrations/a-index.sql').write_text('CREATE INDEX i ON t (c);\n')
    (site / 'migrations/README.md').write_text('recipes\n')
    (site / 'wrangler.toml').write_text('name="oa-hf-usage"\n[vars]\nDEV_HOST="dev.hf.oa.dev"\n')
    setup_api(monkeypatch)
    plan_path, seal_path = tmp_path / 'plan.json', tmp_path / 'seal.json'
    plan_path.write_text(dumps(release.plan_release({'workflow_run': ci()}, 'workflow_run', REPO)))
    result = CliRunner().invoke(release.main, ['seal', '-p', str(plan_path), '-o', str(seal_path), str(source)])
    assert (result.exit_code, result.output, seal_path.exists()) == (
        1,
        "Error: Pending D1 recipes: a-index.sql, b-tables.sql. Apply each to this deployment's databases (owner, with Ryan's go), `git rm` it, then release\n",
        False,
    )


PENDING_HELD = "Pending D1 recipes: {}. Apply each to this deployment's databases (owner, with Ryan's go), `git rm` it, then release"


@pytest.mark.parametrize(('tip_recipes', 'exit_code', 'output'), [
    # rolling back past both recipes' `git rm`: neither holds
    (None, 0, "Not held (gone at hf's tip: applied or dropped): a-index.sql, b-tables.sql\n"),
    # one still pending at the tip holds the rollback; the other was applied since
    (['b-tables.sql', 'c-new.sql'], 1,
     "Not held (gone at hf's tip: applied or dropped): a-index.sql\nError: " + PENDING_HELD.format('b-tables.sql') + '\n'),
    (['a-index.sql', 'b-tables.sql'], 1, 'Error: ' + PENDING_HELD.format('a-index.sql, b-tables.sql') + '\n'),
])
def test_production_seal_holds_only_recipes_still_at_the_tip(tmp_path, monkeypatch, tip_recipes, exit_code, output):
    monkeypatch.setattr(release, 'validate_source', lambda source, sha: None)
    source = tmp_path / 'source'
    site = source / 'site'
    (site / 'dist/assets').mkdir(parents=True)
    (site / 'dist/_worker.js').mkdir()
    (site / 'dist/_worker.js/index.js').write_text('export default {};\n')
    (site / 'dist/index.html').write_text('<script src="/assets/app.js"></script>\n')
    (site / 'dist/assets/app.js').write_text('const answer=42;\n')
    (site / 'wrangler.toml').write_text('name="oa-hf-usage"\n[vars]\nPROD_HOST="hf.oa.dev"\n')
    (site / 'migrations').mkdir()
    (site / 'migrations/b-tables.sql').write_text('ALTER TABLE t ADD COLUMN c TEXT;\n')
    (site / 'migrations/a-index.sql').write_text('CREATE INDEX i ON t (c);\n')
    setup_api(monkeypatch, tip='b' * 40, proof=proof(), tip_recipes=tip_recipes)
    plan_path, seal_path = tmp_path / 'plan.json', tmp_path / 'seal.json'
    plan_path.write_text(dumps(promote()))
    result = CliRunner().invoke(release.main, ['seal', '-p', str(plan_path), '-o', str(seal_path), str(source)])
    assert (result.exit_code, result.output, seal_path.exists()) == (exit_code, output, exit_code == 0)


def test_credential_inventory_never_returns_values_or_folds_source_up_into_next_line(tmp_path):
    path = tmp_path / '.envrc'
    path.write_text('source_up\nexport SLACK_BOT_TOKEN=do-not-output-one\nexport CF_ANALYTICS_TOKEN="do-not-output-two"\nsource_env other-secret-file\nexport OTHER_THING=do-not-output-three\n')
    result = CliRunner().invoke(release.main, ['credential-names', str(path)])
    assert result.exit_code == 0
    assert loads(result.output) == [{
        'path': str(path), 'names': ['CF_ANALYTICS_TOKEN', 'SLACK_BOT_TOKEN'], 'inherits': True,
    }]


def test_begin_records_source_sha_and_exact_artifact_not_controller_sha(tmp_path, monkeypatch):
    monkeypatch.setattr(release, 'validate_source', lambda source, sha: None)
    source = tmp_path / 'source'
    site = source / 'site'
    (site / 'dist/assets').mkdir(parents=True)
    (site / 'dist/_worker.js').mkdir()
    (site / 'dist/_worker.js/index.js').write_text('export default {};\n')
    (site / 'dist/index.html').write_text('<script src="/assets/app.js"></script>\n')
    (site / 'dist/assets/app.js').write_text('42\n')
    (site / 'wrangler.toml').write_text('name="oa-hf-usage"\n[vars]\nDEV_HOST="dev.hf.oa.dev"\n')
    setup_api(monkeypatch)
    planned = release.plan_release({'workflow_run': ci()}, 'workflow_run', REPO)
    plan_path, seal_path, receipt_path = [tmp_path / n for n in ['plan.json', 'seal.json', 'receipt.json']]
    plan_path.write_text(dumps(planned))
    runner = CliRunner()
    result = runner.invoke(release.main, ['seal', '-p', str(plan_path), '-o', str(seal_path), str(source)])
    assert (result.exit_code, result.output) == (0, '')
    calls = []

    def api(path, body=None):
        calls.append((path, body))
        if body is None:
            return []
        return {'id': 73} if path == f'repos/{REPO}/deployments' else {}

    monkeypatch.setattr(release, 'api', api)
    monkeypatch.setattr(release, 'pages', lambda path: {'production_branch': 'main'})
    result = runner.invoke(release.main, ['begin', '-p', str(plan_path), '-s', str(seal_path), '-o', str(receipt_path), str(source)], env={
        'GITHUB_RUN_ID': '123', 'GITHUB_SHA': 'b' * 40,
    })
    assert (result.exit_code, result.output) == (0, '')
    run_url = f'https://github.com/{REPO}/actions/runs/123'
    payload = {**loads(seal_path.read_text()), 'run_url': run_url, 'previous_deployment': None, 'production_cloudflare_before_upload': None}
    assert loads(receipt_path.read_text()) == {**payload, 'id': 73}
    assert calls == [
        (f'repos/{REPO}/deployments?environment=hf-dev&per_page=100&page=1', None),
        (f'repos/{REPO}/deployments', {
            'ref': SHA, 'environment': 'hf-dev', 'auto_merge': False, 'required_contexts': [],
            'description': f'hf {SHA[:12]} via OA release', 'production_environment': False, 'payload': payload,
        }),
        (f'repos/{REPO}/deployments/73/statuses', {
            'state': 'in_progress', 'log_url': run_url, 'environment_url': '', 'auto_inactive': False,
        }),
    ]


@pytest.mark.parametrize('wrong_asset', [False, True])
def test_success_requires_live_build_and_exact_entry_asset(tmp_path, monkeypatch, wrong_asset):
    from io import BytesIO
    receipt = {'repository': REPO, 'id': 73, 'run_url': f'https://github.com/{REPO}/actions/runs/123',
               'mode': 'prod', 'sha': SHA, 'project': 'oa-hf-usage', 'host': 'hf.oa.dev',
               'entry_assets': {'assets/app.js': release.sha256(b'42').hexdigest()}}
    path = tmp_path / 'receipt.json'
    path.write_text(dumps(receipt))
    monkeypatch.setattr(release, 'pages', lambda path: {'canonical_deployment': {
        'id': 'cf73', 'url': 'https://cf73.oa-hf-usage.pages.dev', 'latest_stage': {'status': 'success'},
        'deployment_trigger': {'metadata': {'commit_hash': SHA, 'commit_dirty': False}},
    }})
    calls = []
    monkeypatch.setattr(release, 'api', lambda path, body=None: calls.append((path, body)))

    agents = []

    def urlopen(request, timeout):
        agents.append(request.get_header('User-agent'))
        response = BytesIO(b'wrong' if wrong_asset else b'42')
        response.headers = {'x-disky-build': f'{SHA} 2026-11-10T12:34:56Z'}
        return response

    monkeypatch.setattr(release, 'urlopen', urlopen)
    result = CliRunner().invoke(release.main, ['finish', str(path)], env={'GITHUB_RUN_ID': '123'})
    if wrong_asset:
        assert (result.exit_code, result.output, calls) == (1, 'Error: Live entry asset differs: assets/app.js\n', [])
        assert loads(path.read_text()) == receipt
    else:
        assert result.exit_code == 0
        assert agents == ['disky-oa-release/1', 'disky-oa-release/1']
        expected = {**receipt, 'cloudflare_deployment': 'cf73', 'cloudflare_url': 'https://cf73.oa-hf-usage.pages.dev'}
        assert loads(result.output) == expected
        assert loads(path.read_text()) == expected
        assert calls == [(f'repos/{REPO}/deployments/73/statuses', {
            'state': 'success', 'log_url': receipt['run_url'], 'environment_url': 'https://hf.oa.dev', 'auto_inactive': True,
        })]


def test_wrong_cloudflare_account_refused_before_any_request(monkeypatch):
    monkeypatch.setenv('CLOUDFLARE_ACCOUNT_ID', 'different-account')
    with pytest.raises(ClickException) as error:
        release.pages('oa-hf-usage')
    assert str(error.value) == 'Cloudflare account differs from the controller allowlist'


def test_workflow_guards_and_secret_boundaries():
    from yaml import BaseLoader, load
    workflow = load((ROOT / '.github/workflows/oa-release.yml').read_text(), Loader=BaseLoader)
    assert workflow['on']['workflow_run'] == {
        'workflows': ['CI'], 'types': ['completed'], 'branches': ['gcs', 'cw', 'hf', 'r2'],
    }
    assert workflow['permissions'] == {'contents': 'read', 'actions': 'read'}
    jobs = workflow['jobs']
    assert sorted(jobs) == ['build', 'plan', 'release']
    assert [(key, step['name']) for key in ['plan', 'build'] for step in jobs[key]['steps'] if 'CF_PAGES_DEPLOY_TOKEN' in str(step)] == []
    assert jobs['release']['environment'] == {'name': '${{ needs.plan.outputs.environment }}', 'deployment': 'false'}
    assert jobs['release']['permissions'] == {'contents': 'read', 'actions': 'read', 'deployments': 'write'}
    assert jobs['release']['concurrency'] == {'group': 'oa-site-${{ needs.plan.outputs.environment }}', 'cancel-in-progress': 'false'}
    assert [step['name'] for step in jobs['release']['steps'] if 'CLOUDFLARE_API_TOKEN' in step.get('env', {})] == [
        'Recheck approved revision and start exact-SHA deployment record',
        'Upload the sealed build to Cloudflare',
        'Verify CF readback and live build; record success',
    ]
    notice = [step for key in jobs for step in jobs[key]['steps'] if 'SLACK_DEPLOY_BOT_TOKEN' in str(step)]
    assert [(step['name'], step['if'], step['continue-on-error']) for step in notice] == [
        ('Deploy notice', "always() && vars.DEPLOY_NOTIFICATIONS_ENABLED == '1'", 'true'),
    ]


@pytest.mark.parametrize(('head', 'dirty', 'message'), [
    ('b' * 40, False, 'Source checkout differs from the approved SHA'),
    (SHA, True, 'Source checkout has tracked changes'),
    (SHA, False, None),
])
def test_source_checkout_must_be_clean_and_exact(monkeypatch, head, dirty, message):
    from subprocess import CompletedProcess
    monkeypatch.setattr(release, 'run', lambda args, **kwargs: CompletedProcess(
        args, int(dirty) if 'diff' in args else 0, stdout=head + '\n',
    ))
    if message:
        with pytest.raises(ClickException) as error:
            release.validate_source(Path('source'), SHA)
        assert str(error.value) == message
    else:
        assert release.validate_source(Path('source'), SHA) is None


def setup_expected(target: str, enabled: str, targets: str) -> dict:
    return {
        'repository': REPO,
        'operator_policy': 'Existing repository write/maintain/admin; explicit production dispatch, self-approval allowed; no added merge-review requirement',
        'environments': [{
            'name': f'{target}-{mode}',
            'settings': {'wait_timer': 0, 'prevent_self_review': False, 'reviewers': [],
                         'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True}},
            'branch_policy': {'name': 'front-door', 'type': 'branch'},
            'variables': {'CLOUDFLARE_ACCOUNT_ID': '74981a43be0de7712369306c7b19133d'},
            'secret_names': ['CF_PAGES_DEPLOY_TOKEN'],
        } for mode in ['dev', 'prod']],
        'repository_variables': {'OA_SITE_RELEASE_ENABLED': enabled, 'OA_SITE_RELEASE_TARGETS': targets},
        'production_action': 'Manual promotion of a successful exact-SHA dev deployment',
        'preview_data_policy': 'Unchanged; per-PR preview activation is separate',
    }


def test_setup_plan_adds_to_live_targets_and_keeps_live_enabled(tmp_path):
    output = tmp_path / 'setup.json'
    result = CliRunner().invoke(release.main, ['setup-plan', '-e', 'true', '-l', '["hf"]', '-t', 'r2', '-o', str(output)])
    assert result.exit_code == 0
    assert loads(result.output) == loads(output.read_text()) == setup_expected('r2', 'true', '["hf","r2"]')


def test_setup_plan_for_a_live_target_changes_no_repository_variable(tmp_path):
    output = tmp_path / 'setup.json'
    result = CliRunner().invoke(release.main, ['setup-plan', '-e', 'true', '-l', '["hf","r2"]', '-t', 'hf', '-o', str(output)])
    assert result.exit_code == 0
    assert loads(output.read_text())['repository_variables'] == {'OA_SITE_RELEASE_ENABLED': 'true', 'OA_SITE_RELEASE_TARGETS': '["hf","r2"]'}


def test_setup_plan_refuses_unknown_live_targets(tmp_path):
    result = CliRunner().invoke(release.main, ['setup-plan', '-e', 'false', '-l', '["hf","nope"]', '-t', 'r2', '-o', str(tmp_path / 'setup.json')])
    assert (result.exit_code, result.output.splitlines()[-1]) == (1, 'Error: Unknown OA deployment')


def test_setup_plan_is_scoped_and_disabled_without_extra_approval_clicks(tmp_path):
    output = tmp_path / 'setup.json'
    result = CliRunner().invoke(release.main, ['setup-plan', '-e', 'false', '-l', '[]', '-t', 'hf', '-o', str(output)])
    assert result.exit_code == 0
    expected = {
        'repository': REPO,
        'operator_policy': 'Existing repository write/maintain/admin; explicit production dispatch, self-approval allowed; no added merge-review requirement',
        'environments': [{
            'name': f'hf-{mode}',
            'settings': {'wait_timer': 0, 'prevent_self_review': False, 'reviewers': [],
                         'deployment_branch_policy': {'protected_branches': False, 'custom_branch_policies': True}},
            'branch_policy': {'name': 'front-door', 'type': 'branch'},
            'variables': {'CLOUDFLARE_ACCOUNT_ID': '74981a43be0de7712369306c7b19133d'},
            'secret_names': ['CF_PAGES_DEPLOY_TOKEN'],
        } for mode in ['dev', 'prod']],
        'repository_variables': {'OA_SITE_RELEASE_ENABLED': 'false', 'OA_SITE_RELEASE_TARGETS': '["hf"]'},
        'production_action': 'Manual promotion of a successful exact-SHA dev deployment',
        'preview_data_policy': 'Unchanged; per-PR preview activation is separate',
    }
    assert loads(result.output) == loads(output.read_text()) == expected
