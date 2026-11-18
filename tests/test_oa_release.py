"""The release controller fails closed on wrong source, stale CI and unproven production promotions."""
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from json import dumps, loads
from pathlib import Path
from re import findall
from shlex import split

from click import Argument, ClickException, Option
from click.testing import CliRunner
import pytest


ROOT = Path(__file__).resolve().parents[1]
loader = SourceFileLoader('oa_release', str(ROOT / 'scripts/oa-release'))
spec = spec_from_loader(loader.name, loader)
release = module_from_spec(spec)
loader.exec_module(release)
notify_loader = SourceFileLoader('deploy_notify', str(ROOT / 'site/deploy-notify'))
notify = module_from_spec(spec_from_loader(notify_loader.name, notify_loader))
notify_loader.exec_module(notify)
SHA = 'a' * 40
REPO = 'Open-Athena/disky'


def ci(branch='hf'):
    return {'id': 42, 'head_branch': branch, 'head_sha': SHA, 'head_repository': {'full_name': REPO},
            'event': 'push', 'conclusion': 'success', 'path': '.github/workflows/ci.yml'}


PREV = 'c' * 40


def setup_api(monkeypatch, *, tip=SHA, pointer=SHA, dev=None, states=None, runs=None, comparison='ahead', tip_recipes=None,
              prod=None, back='ahead'):
    """`tip_recipes`: the `.sql` recipes at the branch's tip; None: no `site/migrations` there. `dev` / `prod`: the
    GitHub deployments listed for `hf-dev` at the sha / for `hf-prod` (newest first); `states`: each one's statuses by id
    (default `success`). `comparison`: the sha against the branch; `back`: the sha against the previous prod release."""
    calls = []
    states = states or {}

    def api(path, body=None):
        calls.append((path, body))
        if path == f'repos/{REPO}/contents/site/migrations?ref={tip}':
            return [{'name': 'README.md', 'type': 'file'}, *({'name': n, 'type': 'file'} for n in tip_recipes)]
        if path == f'repos/{REPO}/contents/site?ref={tip}':
            return [{'name': 'src', 'type': 'dir'}, *([{'name': 'migrations', 'type': 'dir'}] if tip_recipes is not None else [])]
        if path.endswith('/git/ref/heads/hf-prod'):
            return {'object': {'sha': pointer}}
        if '/git/ref/heads/' in path:
            return {'object': {'sha': tip}}
        if path.startswith(f'repos/{REPO}/compare/{SHA}...{PREV}'):
            return {'status': back}
        if '/compare/' in path:
            return {'status': comparison}
        if '/deployments?environment=hf-dev' in path:
            return [proof()] if dev is None else dev
        if '/deployments?environment=hf-prod' in path:
            return prod or []
        if '/statuses?' in path:
            return [{'state': s} for s in states.get(int(path.split('/deployments/')[1].split('/')[0]), ['success'])]
        if '/actions/workflows/' in path:
            return {'workflow_runs': [ci()] if runs is None else runs}
        raise AssertionError((path, body))

    monkeypatch.setattr(release, 'api', api)
    return calls


@pytest.mark.parametrize(('branch', 'project', 'pages', 'channel'), [
    ('gcs', 'oa-gcs-usage', 'dev', 'C0C579HNRFB'), ('cw', 'oa-cw-s3-usage', 'dev', 'C0C53CPM886'),
    ('hf', 'oa-hf-usage', 'dev', 'C0C9J9VDPV5'), ('r2', 'oa-r2-usage', 'r2', 'C0C9U47KTM2'),
])
def test_exact_dev_plan(monkeypatch, branch, project, pages, channel):
    calls = setup_api(monkeypatch)
    assert release.plan_release({'workflow_run': ci(branch)}, 'workflow_run', REPO) == {
        'protocol': 'disky-pages-v1', 'repository': REPO, 'source_branch': branch, 'sha': SHA,
        'mode': 'dev', 'environment': f'{branch}-dev', 'project': project, 'pages_branch': pages,
        'host': f'dev.{branch}.oa.dev', 'admin_channel': channel, 'ci_run': 42, 'dev_deployment': None, 'tip_recipes': None,
        'previous': None, 'rollback': False,
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


def proof(id=91, **change):
    return {'id': id, 'sha': SHA, 'environment': 'hf-dev', 'creator': {'login': 'github-actions[bot]'},
            'payload': {'protocol': 'disky-pages-v1', 'source_branch': 'hf'}, **change}


def live(id, sha=PREV):
    """A production release this controller recorded."""
    return {**proof(id), 'sha': sha, 'environment': 'hf-prod'}


def promote():
    """A push to `hf-prod` at `SHA`, as `oa-prod-push.yml` dispatches it."""
    return release.plan_release({'inputs': {'deployment': 'hf', 'sha': SHA}}, 'workflow_dispatch', REPO)


def test_production_finds_its_dev_proof_even_after_branch_advances(monkeypatch):
    setup_api(monkeypatch, tip='b' * 40, states={91: ['inactive', 'success']}, tip_recipes=['c-new.sql'], prod=[live(95)], back='behind')
    assert promote() == {
        'protocol': 'disky-pages-v1', 'repository': REPO, 'source_branch': 'hf', 'sha': SHA,
        'mode': 'prod', 'environment': 'hf-prod', 'project': 'oa-hf-usage', 'pages_branch': 'main',
        'host': 'hf.oa.dev', 'admin_channel': 'C0C9J9VDPV5', 'ci_run': 42, 'dev_deployment': '91', 'tip_recipes': ['c-new.sql'],
        'previous': PREV, 'rollback': False,
    }


def test_production_plan_without_recipes_at_the_tip(monkeypatch):
    setup_api(monkeypatch)
    assert promote()['tip_recipes'] == []


@pytest.mark.parametrize(('back', 'rollback'), [('ahead', True), ('behind', False), ('diverged', False)])
def test_a_push_behind_what_prod_served_is_a_rollback(monkeypatch, back, rollback):
    """`back`: the previous prod release's sha against this one (`ahead`: this sha is its ancestor)."""
    setup_api(monkeypatch, prod=[live(97, 'd' * 40), live(96)], states={97: ['failure']}, back=back)
    assert (promote()['previous'], promote()['rollback']) == (PREV, rollback)


def test_production_with_no_earlier_release(monkeypatch):
    setup_api(monkeypatch)
    assert (promote()['previous'], promote()['rollback']) == (None, False)


def test_production_skips_failed_and_foreign_dev_releases_for_the_newest_success(monkeypatch):
    dev = [proof(94), proof(93, creator={'login': 'some-user'}), proof(92, payload={}), proof(91)]
    setup_api(monkeypatch, dev=dev, states={94: ['failure']})
    assert promote()['dev_deployment'] == '91'


@pytest.mark.parametrize(('dev', 'states', 'message'), [
    ([], {}, f'{SHA[:12]} has no successful dev release: push it to hf first'),
    ([proof(91, sha='b' * 40)], {}, f'{SHA[:12]} has no successful dev release: push it to hf first'),
    ([proof(91, environment='cw-dev')], {}, f'{SHA[:12]} has no successful dev release: push it to hf first'),
    ([proof(91, creator={'login': 'some-user'})], {}, f'{SHA[:12]} has no successful dev release: push it to hf first'),
    ([proof(91, payload={})], {}, f'{SHA[:12]} has no successful dev release: push it to hf first'),
    ([proof(91)], {91: ['failure']}, f'{SHA[:12]} has no successful dev release (its newest: failure): push it to hf first'),
    ([proof(91)], {91: ['in_progress']}, f'{SHA[:12]} has no successful dev release (its newest: in_progress): push it to hf first'),
    ([proof(92), proof(91)], {92: ['inactive', 'failure'], 91: ['error']},
     f'{SHA[:12]} has no successful dev release (its newest: inactive): push it to hf first'),
])
def test_production_refuses_without_a_successful_dev_release(monkeypatch, dev, states, message):
    setup_api(monkeypatch, dev=dev, states=states)
    with pytest.raises(ClickException) as error:
        promote()
    assert str(error.value) == message


def test_production_releases_only_what_the_pointer_names(monkeypatch):
    calls = setup_api(monkeypatch, pointer='e' * 40)
    with pytest.raises(ClickException) as error:
        promote()
    assert str(error.value) == f'hf-prod points at {"e" * 12}, not {SHA[:12]}: production releases the pointer (git push o <sha>:hf-prod)'
    assert calls == [(f'repos/{REPO}/git/ref/heads/hf-prod', None)]


@pytest.mark.parametrize('runs', [[], [{**ci(), 'conclusion': 'failure'}]])
def test_production_requires_current_ci_receipt(monkeypatch, runs):
    setup_api(monkeypatch, runs=runs)
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
    setup_api(monkeypatch, tip='b' * 40, tip_recipes=tip_recipes)
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
        ('Deploy notice', "always() && needs.plan.outputs.admin_channel != ''", 'true'),
    ]


def workflow() -> dict:
    from yaml import BaseLoader, load
    return load((ROOT / '.github/workflows/oa-release.yml').read_text(), Loader=BaseLoader)


def test_a_prod_pointer_push_only_dispatches_front_doors_release():
    """`oa-prod-push.yml`: every OA site's `<d>-prod` and nothing else, in OA's repo only, not on a pointer's deletion,
    with no permission but dispatching; the release itself (and its gate) is `front-door`'s `oa-release.yml`."""
    from yaml import BaseLoader, load
    workflow = load((ROOT / '.github/workflows/oa-prod-push.yml').read_text(), Loader=BaseLoader)
    sites = sorted(loads((ROOT / 'deploy/oa/sites.json').read_text())['sites'])
    assert sorted(workflow['on']['push']['branches']) == [f'{site}-prod' for site in sites]
    assert workflow['permissions'] == {'actions': 'write'}
    job = workflow['jobs']['dispatch']
    assert job['if'] == "github.repository == 'Open-Athena/disky' && !github.event.deleted"
    release_inputs = load((ROOT / '.github/workflows/oa-release.yml').read_text(), Loader=BaseLoader)['on']['workflow_dispatch']['inputs']
    assert sorted(release_inputs) == ['deployment', 'sha']
    assert sorted(release_inputs['deployment']['options']) == sites
    [step] = job['steps']
    assert [line for line in step['run'].splitlines() if line.startswith('gh ')] == [
        'gh workflow run oa-release.yml -R "$GITHUB_REPOSITORY" --ref front-door -f deployment="$deployment" -f sha="$SHA"',
    ]


def test_controller_is_cloud_pinned_once_and_source_is_the_release_sha():
    """The default branch's stub runs `cloud`'s scripts: `plan` checks out `cloud` and outputs its sha, the later jobs
    check out exactly that sha (never a `cloud` that moved meanwhile), and the source checkout stays the planned sha."""
    jobs = workflow()['jobs']
    checkouts = {
        key: [(step['with'].get('path', '.'), step['with']['ref'], step['with']['persist-credentials'])
              for step in jobs[key]['steps'] if step.get('uses') == 'actions/checkout@v4']
        for key in jobs
    }
    pinned = ('control', '${{ needs.plan.outputs.control_sha }}', 'false')
    source = ('source', '${{ needs.plan.outputs.sha }}', 'false')
    assert checkouts == {
        'plan': [('control', 'refs/heads/cloud', 'false')],
        'build': [pinned, source],
        'release': [pinned, source],
    }
    assert jobs['plan']['outputs']['control_sha'] == '${{ steps.control.outputs.sha }}'
    steps = [(step.get('id'), step.get('uses'), step.get('name')) for step in jobs['plan']['steps']]
    assert steps == [
        (None, 'actions/checkout@v4', None),
        ('control', None, "Pin the controller to `cloud`'s sha; this stub must be `cloud`'s copy"),
        (None, 'astral-sh/setup-uv@v6', None),
        ('plan', None, None),
    ]
    control = jobs['plan']['steps'][1]['run']
    assert [line.strip() for line in control.splitlines() if 'cmp ' in line or 'GITHUB_OUTPUT' in line] == [
        'if ! cmp "$RUNNER_TEMP/stub.yml" control/.github/workflows/oa-release.yml; then',
        'echo "sha=$sha" >> "$GITHUB_OUTPUT"',
    ]


#: The stub's array expansions, as the flags they can hold (`verified=(-v)`, or none)
EXPANSIONS = {'${verified[@]}': ['-v']}


def invocations(script: str) -> list[list[str]]:
    """Each `control/<script> …` call in the stub's `run:` blocks, as argv (continuations joined, cut at `;`)."""
    calls = []
    for job in workflow()['jobs'].values():
        for step in job['steps']:
            for line in step.get('run', '').replace('\\\n', ' ').splitlines():
                for call in findall(rf'control/{script} ([^;]*)', line):
                    calls.append([token for raw in split(call) for token in EXPANSIONS.get(raw, [raw])])
    return calls


def parse(cli, argv: list[str]) -> tuple[str, list[str], list[str]]:
    """ARGV against CLI's own declaration: a known subcommand, known options (each with its value), every required
    option, and exactly its positional arguments. Returns (subcommand, the options given, the positionals)."""
    name, *rest = argv
    command = cli.commands[name]
    options = {flag: param for param in command.params if isinstance(param, Option) for flag in param.opts}
    given, positional, tokens = [], [], iter(rest)
    for token in tokens:
        if token.startswith('-'):
            param = options[token]
            given.append(param.name)
            if not param.is_flag:
                next(tokens)
        else:
            positional.append(token)
    required = sorted(param.name for param in command.params if isinstance(param, Option) and param.required)
    assert sorted(set(given) & set(required)) == required, argv
    assert len(positional) == len([param for param in command.params if isinstance(param, Argument)]), argv
    return name, sorted(given), positional


def test_stub_calls_parse_against_the_scripts():
    """The contract between `front-door`'s stub and `cloud`'s scripts: every call the stub makes parses against that
    script's own CLI, so a script change that breaks one fails here, on `cloud`, before a release can run it."""
    assert [parse(release.main, argv) for argv in invocations('scripts/oa-release')] == [
        ('plan', ['event', 'event_name', 'output', 'repository'], []),
        ('plan', ['event', 'event_name', 'output', 'repository'], []),
        ('seal', ['output', 'plan_path'], ['source']),
        ('plan', ['event', 'event_name', 'output', 'repository'], []),
        ('begin', ['output', 'plan_path', 'seal_path'], ['source']),
        ('finish', [], ['receipt.json']),
        ('finish', ['failed'], ['receipt.json']),
    ]
    assert [parse(notify.main, argv) for argv in invocations('site/deploy-notify')] == [
        ('record', ['config_path', 'dirty', 'mode', 'output', 'sha', 'stage', 'status', 'upload_log', 'verified'], []),
        ('send', ['execute'], ['notice.json']),
    ]


def test_stub_reads_exactly_the_outputs_plan_writes(tmp_path, monkeypatch):
    """`plan`'s `$GITHUB_OUTPUT` keys are exactly the `steps.plan.outputs.*` the stub reads."""
    planned = {'sha': SHA, 'source_branch': 'hf', 'mode': 'dev', 'environment': 'hf-dev', 'project': 'oa-hf-usage',
               'pages_branch': 'dev', 'host': 'dev.hf.oa.dev', 'admin_channel': 'C0C9J9VDPV5', 'repository': REPO}
    monkeypatch.setattr(release, 'plan_release', lambda event, event_name, repository: planned)
    event, out = tmp_path / 'event.json', tmp_path / 'github_output'
    event.write_text('{}')
    monkeypatch.setenv('GITHUB_OUTPUT', str(out))
    result = CliRunner().invoke(release.main, ['plan', '-e', str(event), '-n', 'workflow_run', '-r', REPO, '-o', str(tmp_path / 'plan.json')])
    assert result.exit_code == 0, result.output
    written = sorted(line.split('=', 1)[0] for line in out.read_text().splitlines())
    read = sorted(set(findall(r'steps\.plan\.outputs\.(\w+)', (ROOT / '.github/workflows/oa-release.yml').read_text())))
    assert read == written == ['admin_channel', 'environment', 'host', 'mode', 'ok', 'pages_branch', 'project', 'sha', 'source_branch']


@pytest.mark.parametrize(('event_name', 'event', 'message'), [
    ('workflow_run', {'workflow_run': ci()}, 'Obsolete CI completion: source branch has moved'),
    ('workflow_dispatch', {'inputs': {'deployment': 'hf', 'sha': SHA}},
     f'hf-prod points at {"b" * 12}, not {SHA[:12]}: production releases the pointer (git push o <sha>:hf-prod)'),
])
def test_a_superseded_release_is_skipped_not_failed(tmp_path, monkeypatch, event_name, event, message):
    """A newer push overtook it (the branch, or the prod pointer, moved): `plan` exits 0 with a notice and `ok=false`, so
    the stub skips build and release and the run isn't red; nothing is planned."""
    setup_api(monkeypatch, tip='b' * 40, pointer='b' * 40)
    paths = {name: tmp_path / name for name in ('event.json', 'github_output', 'summary', 'plan.json')}
    paths['event.json'].write_text(dumps(event))
    monkeypatch.setenv('GITHUB_OUTPUT', str(paths['github_output']))
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(paths['summary']))
    result = CliRunner().invoke(release.main, ['plan', '-e', str(paths['event.json']), '-n', event_name, '-r', REPO, '-o', str(paths['plan.json'])])
    assert (result.exit_code, result.output) == (0, f'::notice::{message}\n')
    assert (paths['github_output'].read_text(), paths['summary'].read_text()) == ('ok=false\n', f'Skipped: {message}\n')
    assert not paths['plan.json'].exists()


def test_build_and_so_release_run_only_on_a_plan_that_is_ok():
    from yaml import BaseLoader, load
    jobs = load((ROOT / '.github/workflows/oa-release.yml').read_text(), Loader=BaseLoader)['jobs']
    assert jobs['plan']['outputs']['ok'] == '${{ steps.plan.outputs.ok }}'
    assert (jobs['build']['needs'], jobs['build']['if']) == ('plan', "needs.plan.outputs.ok == 'true'")
    assert (jobs['release']['needs'], 'if' in jobs['release']) == (['plan', 'build'], False)


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


def test_live_get_retries_a_transient_404_5xx_or_reset_but_not_another_4xx(monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError
    answers = {'https://h/a.js': [404, 500, 'reset', 502, b'42'], 'https://h/b.js': [403, b'never']}
    asked = []

    def urlopen(request, timeout):
        asked.append(request.full_url)
        answer = answers[request.full_url].pop(0)
        if isinstance(answer, int):
            raise HTTPError(request.full_url, answer, 'status', {}, None)
        if answer == 'reset':  # mid-read, after `urlopen` returned: a bare `ConnectionResetError`
            class Reset(BytesIO):
                def read(self, *a):
                    raise ConnectionResetError(104, 'Connection reset by peer')
            return Reset(b'')
        return BytesIO(answer)

    monkeypatch.setattr(release, 'urlopen', urlopen)
    monkeypatch.setattr(release, 'sleep', lambda s: None)
    assert release.live_get('https://h/a.js') == b'42'
    with pytest.raises(HTTPError) as error:
        release.live_get('https://h/b.js')
    assert (error.value.code, asked) == (403, ['https://h/a.js'] * 5 + ['https://h/b.js'])


def test_build_header_poll_waits_out_a_5xx_and_a_reset(tmp_path, monkeypatch):
    from io import BytesIO
    from urllib.error import HTTPError
    receipt = {'repository': REPO, 'id': 73, 'run_url': f'https://github.com/{REPO}/actions/runs/123',
               'mode': 'prod', 'sha': SHA, 'project': 'oa-hf-usage', 'host': 'hf.oa.dev',
               'entry_assets': {'assets/app.js': release.sha256(b'42').hexdigest()}}
    path = tmp_path / 'receipt.json'
    path.write_text(dumps(receipt))
    monkeypatch.setattr(release, 'pages', lambda path: {'canonical_deployment': {
        'id': 'cf73', 'url': 'https://cf73.oa-hf-usage.pages.dev', 'latest_stage': {'status': 'success'},
        'deployment_trigger': {'metadata': {'commit_hash': SHA, 'commit_dirty': False}},
    }})
    monkeypatch.setattr(release, 'api', lambda path, body=None: None)
    monkeypatch.setattr(release, 'sleep', lambda s: None)
    health = [503, 'reset']
    asked = []

    def urlopen(request, timeout):
        url = request.full_url.split('?')[0]
        asked.append(url)
        if url.endswith('/api/health') and health:
            answer = health.pop(0)
            if answer == 'reset':
                raise ConnectionResetError(104, 'Connection reset by peer')
            raise HTTPError(request.full_url, answer, 'status', {}, None)
        response = BytesIO(b'42')
        response.headers = {'x-disky-build': f'{SHA} 2026-11-10T12:34:56Z'}
        return response

    monkeypatch.setattr(release, 'urlopen', urlopen)
    result = CliRunner().invoke(release.main, ['finish', str(path)], env={'GITHUB_RUN_ID': '123'})
    assert (result.exit_code, asked) == (0, ['https://hf.oa.dev/api/health'] * 3 + ['https://hf.oa.dev/assets/app.js'])
