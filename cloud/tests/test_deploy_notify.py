from importlib.machinery import SourceFileLoader
from json import dumps, loads
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType
from urllib.error import HTTPError, URLError
from uuid import UUID

from click import ClickException
from click.testing import CliRunner
import pytest


ROOT = Path(__file__).resolve().parents[2]
N = ModuleType('deploy_notify')
N.__file__ = str(ROOT / 'site/deploy-notify')
SourceFileLoader(N.__name__, N.__file__).exec_module(N)
SHA = 'b' * 40
HOOK = 'https://hooks.slack.com/services/TTEST/BTEST/dummy'


def doc(**changes):
    return {
        'notice_version': 1, 'deployment': 'hf', 'mode': 'dev', 'status': 'success', 'stage': 'finish',
        'sha': SHA, 'source_branch': 'hf', 'repository': 'Open-Athena/disky', 'host': 'dev.hf.oa.dev',
        'verified': True, 'dirty': False, 'actor': 'louisa', 'actor_source': 'github', 'attempt': '123-1',
        'cloudflare_deployment': 'abcdef12', 'cloudflare_url': 'https://abcdef12.oa-hf-usage.pages.dev',
        'run_url': 'https://github.com/Open-Athena/disky/actions/runs/123', **changes,
    }


SUCCESS = f'''✅ **HF dev deployment succeeded**

Source: [hf](https://github.com/Open-Athena/disky/tree/hf) · [{SHA}](https://github.com/Open-Athena/disky/commit/{SHA}) · clean

GitHub actor: `louisa` · attempt `123-1`

Serving verification: passed. CF deployment: abcdef12.

[Site](https://dev.hf.oa.dev) · [CF deployment](https://abcdef12.oa-hf-usage.pages.dev) · [Run logs](https://github.com/Open-Athena/disky/actions/runs/123)
'''


def test_render_exact_success_omits_private_controller_fields():
    assert N.render(doc(account_id='private', credentials={'secret': 'private'}, error='private')) == SUCCESS


@pytest.mark.parametrize('site,label', [('gcs', 'GCS'), ('cw', 'CW'), ('hf', 'HF'), ('r2', 'R2')])
@pytest.mark.parametrize('mode', ['dev', 'prod'])
def test_each_site_and_environment_uses_its_own_identity(site, label, mode):
    host = f'{"dev." if mode == "dev" else ""}{site}.oa.dev'
    value = doc(deployment=site, source_branch=site, mode=mode, host=host)
    expected = (SUCCESS.replace('HF dev deployment succeeded', f'{label} {mode} deployment succeeded')
                .replace('[hf](', f'[{site}](').replace('/tree/hf)', f'/tree/{site})')
                .replace('https://dev.hf.oa.dev', f'https://{host}'))
    assert N.render(value) == expected


def test_render_build_failure_has_no_fictitious_cf_receipt_or_verification():
    assert N.render(doc(status='failure', stage='build', verified=False, cloudflare_deployment=None,
                        cloudflare_url=None, actor=None, actor_source=None, sha=None, source_branch=None)) == '''❌ **HF dev deployment failed during build**

Source: ref not recorded · revision not recorded · clean

Initiator: not recorded · attempt `123-1`

Serving verification: not recorded. CF deployment: not recorded.

[Site](https://dev.hf.oa.dev) · [Run logs](https://github.com/Open-Athena/disky/actions/runs/123)
'''


def test_render_unverified_dirty_manual_upload_and_cancellation():
    value = doc(mode='prod', host='hf.oa.dev', verified=False, dirty=True, actor_source='local', run_url=None)
    expected = f'''✅ **HF prod deployment succeeded**

Source: [hf](https://github.com/Open-Athena/disky/tree/hf) · `{SHA}` (base commit) · tracked changes present

Local user: `louisa` · attempt `123-1`

Serving verification: not recorded. CF deployment: abcdef12.

[Site](https://hf.oa.dev) · [CF deployment](https://abcdef12.oa-hf-usage.pages.dev)
'''
    assert N.render(value) == expected
    assert N.render({**value, 'status': 'cancelled'}) == expected.replace('✅ **HF prod deployment succeeded**', '⏹️ **HF prod deployment cancelled**')


@pytest.mark.parametrize('changes,message', [
    ({'notice_version': 2}, 'notice_version must be 1'),
    ({'sha': 'b' * 10}, 'sha must be a full 40-character revision or null'),
    ({'host': 'bad.example/@channel'}, 'host must be a DNS name'),
    ({'source_branch': '<!channel>'}, 'source_branch must be a plain source ref or null'),
    ({'actor': '<@USER>'}, 'actor must be a plain user identifier or null'),
    ({'run_url': 'https://secret@github.com/example'}, 'run_url: expected an HTTPS URL without credentials, port, query or fragment'),
    ({'status': 'failure'}, 'verified must be a boolean and requires success'),
])
def test_invalid_or_injectable_receipt_is_refused(changes, message):
    with pytest.raises(ClickException) as error:
        N.render(doc(**changes))
    assert str(error.value) == message


def test_cli_defaults_to_review_even_with_credentials_and_enabled_delivery(tmp_path, monkeypatch):
    path = tmp_path / 'receipt.json'
    path.write_text(dumps(doc()))
    def unexpected(*args):
        pytest.fail('review must not open a connection')
    monkeypatch.setattr(N, 'build_opener', unexpected)
    result = CliRunner().invoke(N.main, ['send', str(path)], env={'DEPLOY_NOTIFICATIONS_ENABLED': '1', 'SLACK_DEPLOY_WEBHOOK': HOOK})
    assert (result.exit_code, result.output) == (0, SUCCESS)
    result = CliRunner().invoke(N.main, ['send', '-x', str(path)], env={'DEPLOY_NOTIFICATIONS_ENABLED': '0', 'SLACK_DEPLOY_WEBHOOK': HOOK})
    assert (result.exit_code, result.output) == (1, 'Error: Deploy notifications are disabled pending template/transport review\n')


def test_webhook_payload_uses_thrds_formatting_and_cannot_select_a_channel(monkeypatch):
    calls = []
    class Response:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, size):
            assert size == 1024
            return b'ok'
    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, request.method, timeout, loads(request.data)))
            return Response()
    def opener(handler):
        assert type(handler) is N.NoRedirect
        return Opener()
    monkeypatch.setattr(N, 'build_opener', opener)
    N.send(doc(), {'DEPLOY_NOTIFICATIONS_ENABLED': '1', 'SLACK_DEPLOY_WEBHOOK': HOOK})
    assert calls == [(HOOK, 'POST', 15, {
        'text': f'''✅ *HF dev deployment succeeded*

Source: <https://github.com/Open-Athena/disky/tree/hf|hf> · <https://github.com/Open-Athena/disky/commit/{SHA}|{SHA}> · clean

GitHub actor: `louisa` · attempt `123-1`

Serving verification: passed. CF deployment: abcdef12.

<https://dev.hf.oa.dev|Site> · <https://abcdef12.oa-hf-usage.pages.dev|CF deployment> · <https://github.com/Open-Athena/disky/actions/runs/123|Run logs>
''',
        'unfurl_links': False, 'unfurl_media': False,
    })]


@pytest.mark.parametrize('failure,message', [
    (HTTPError(HOOK, 429, 'private', {}, None), 'Slack notification HTTP 429; not retried'),
    (HTTPError(HOOK, 302, 'private', {}, None), 'Slack notification HTTP 302; not retried'),
    (URLError(HOOK), 'Slack notification transport failed; acceptance unknown, not retried'),
])
def test_uncertain_delivery_and_redirects_are_not_retried_or_leaked(monkeypatch, failure, message):
    calls = []
    class Opener:
        def open(self, *args, **kwargs):
            calls.append(1)
            raise failure
    monkeypatch.setattr(N, 'build_opener', lambda _: Opener())
    with pytest.raises(ClickException) as error:
        N.send(doc(), {'DEPLOY_NOTIFICATIONS_ENABLED': '1', 'SLACK_DEPLOY_WEBHOOK': HOOK})
    assert (calls, str(error.value)) == ([1], message)


def executable(path: Path, body: str):
    path.write_text('#!' + sys.executable + '\n' + body)
    path.chmod(0o755)


@pytest.mark.parametrize('configured_store,expected_deployment', [(None, 'demo-site'), ('hf', 'hf')])
@pytest.mark.parametrize('build_rc,upload_rc,enabled,expected', [
    (7, 0, False, (7, 'failure', 'build', None)),
    (130, 0, False, (130, 'cancelled', 'build', None)),
    (0, 9, False, (9, 'failure', 'upload', None)),
    (0, 0, False, (0, 'success', 'finish', 'abcdef12')),
    (0, 0, True, (0, 'success', 'finish', 'abcdef12')),
])
def test_manual_hook_records_failures_and_preserves_original_exit_status(tmp_path, build_rc, upload_rc, enabled, expected, configured_store, expected_deployment):
    site = tmp_path / 'site'
    site.mkdir()
    for name in ('deploy', 'toml-check'):
        shutil.copy(ROOT / 'site' / name, site / name)
    executable(site / 'deploy-notify', (ROOT / 'site/deploy-notify').read_text().split('\n', 1)[1])
    (site / 'wrangler.toml').write_text('''name = "demo-site"
[vars]
PROD_HOST = "hf.oa.dev"
REPO_URL = "https://github.com/Open-Athena/disky"
SOURCE_REF = "hf"
''' + (f'STORE = "{configured_store}"\n' if configured_store else ''))
    (site / 'dist').mkdir()
    (site / 'dist/index.html').write_text('<html></html>\n')
    bins = tmp_path / 'bin'
    bins.mkdir()
    executable(bins / 'git', f'''import sys
if sys.argv[1:]==['rev-parse','HEAD']: print('{SHA}')
''')
    executable(bins / 'pnpm', f'''import sys
if sys.argv[1:]==['build']: sys.exit({build_rc})
if {upload_rc}:
    print('Upload failed; prior deploy https://oldhash1.demo-site.pages.dev')
    sys.exit({upload_rc})
print('Deployment complete! https://abcdef12.demo-site.pages.dev')
''')
    path = tmp_path / 'receipt.json'
    result = subprocess.run([str(site / 'deploy'), '-P'], cwd=tmp_path, capture_output=True, text=True, env={
        'PATH': str(bins) + os.pathsep + os.path.dirname(sys.executable) + os.pathsep + '/usr/bin:/bin',
        'HOME': str(tmp_path), 'USER': 'tester', 'CLOUDFLARE_ACCOUNT_ID': 'test-account',
        'DEPLOY_NOTICE_RECEIPT': str(path), 'DEPLOY_NOTIFICATIONS_ENABLED': '1' if enabled else '0',
    })
    r = loads(path.read_text())
    assert (result.returncode, r['status'], r['stage'], r['cloudflare_deployment']) == expected
    assert UUID(r.pop('attempt')).version == 4
    assert r == {
        'notice_version': 1, 'deployment': expected_deployment, 'mode': 'prod', 'status': expected[1], 'stage': expected[2],
        'sha': SHA, 'source_branch': 'hf', 'repository': 'Open-Athena/disky', 'host': 'hf.oa.dev',
        'verified': False, 'dirty': False, 'actor': 'tester', 'actor_source': 'local',
        'cloudflare_deployment': expected[3],
        'cloudflare_url': 'https://abcdef12.demo-site.pages.dev' if expected[3] else None, 'run_url': None,
    }
    assert result.stderr == ('Error: SLACK_DEPLOY_WEBHOOK must be a channel-scoped Slack incoming webhook\n'
                             'WARN: deploy notification failed; original deployment exit status preserved\n' if enabled else '')
