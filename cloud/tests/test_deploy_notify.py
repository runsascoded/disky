from importlib.machinery import SourceFileLoader
from json import dumps, loads
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType
from urllib.error import URLError
from uuid import UUID

from click import ClickException
from click.testing import CliRunner
import pytest


ROOT = Path(__file__).resolve().parents[2]
N = ModuleType('deploy_notify')
N.__file__ = str(ROOT / 'site/deploy-notify')
SourceFileLoader(N.__name__, N.__file__).exec_module(N)
SHA = 'b' * 40


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


def test_github_bot_actor_is_rendered_without_rejecting_automated_releases():
    assert N.render(doc(actor='github-actions[bot]')) == SUCCESS.replace('`louisa`', '`github-actions[bot]`')


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


ENV = {'DEPLOY_NOTIFICATIONS_ENABLED': '1', 'SLACK_DEPLOY_BOT_TOKEN': 'xoxb-test-token', 'SLACK_DEPLOY_CHANNEL': 'C0TESTCHAN'}
BOT = ('U0BOT', 'B0BOT')
WIRE = f'''✅ *HF dev deployment succeeded*

Source: <https://github.com/Open-Athena/disky/tree/hf|hf> · <https://github.com/Open-Athena/disky/commit/{SHA}|{SHA}> · clean

GitHub actor: `louisa` · attempt `123-1`

Serving verification: passed. CF deployment: abcdef12.

<https://dev.hf.oa.dev|Site> · <https://abcdef12.oa-hf-usage.pages.dev|CF deployment> · <https://github.com/Open-Athena/disky/actions/runs/123|Run logs>
'''


def notice(ts: str, headline: str, bot: str = 'B0BOT') -> dict:
    return {'ts': ts, 'bot_id': bot, 'text': f':x: *{headline}*\n\nSource: …'}


class FakeSlack:
    def __init__(self, history=(), fail=None):
        self.history, self.fail, self.calls = list(history), fail, []
        self.bot_ids = BOT

    def list_channel_history(self, channel, limit):
        self.calls.append(('history', channel, limit))
        return self.history

    def post(self, content, thread_id=None):
        self.calls.append(('post', content, thread_id))
        if self.fail:
            raise self.fail


def fake(monkeypatch, slack):
    made = []
    def client(token, channel):
        made.append((token, channel))
        return slack
    monkeypatch.setattr(N, 'slack_client', client)
    return made


def test_cli_defaults_to_review_even_with_credentials_and_enabled_delivery(tmp_path, monkeypatch):
    path = tmp_path / 'receipt.json'
    path.write_text(dumps(doc()))
    def unexpected(*args):
        pytest.fail('review must not open a connection')
    monkeypatch.setattr(N, 'slack_client', unexpected)
    result = CliRunner().invoke(N.main, ['send', str(path)], env=ENV)
    assert (result.exit_code, result.output) == (0, SUCCESS)
    result = CliRunner().invoke(N.main, ['send', '-x', str(path)], env={**ENV, 'DEPLOY_NOTIFICATIONS_ENABLED': '0'})
    assert (result.exit_code, result.output) == (1, 'Error: Deploy notifications are disabled pending template/transport review\n')


@pytest.mark.parametrize('history,thread', [
    # The newest notice for this deployment is green: the run continues in its thread, across dev and prod.
    ([notice('9.0', 'HF prod deployment succeeded'), notice('8.0', 'HF dev deployment failed during build')], '9.0'),
    # A red ended the run (failure or cancellation): the next green opens a new one.
    ([notice('9.0', 'HF dev deployment failed during upload'), notice('8.0', 'HF dev deployment succeeded')], None),
    ([notice('9.0', 'HF prod deployment cancelled'), notice('8.0', 'HF dev deployment succeeded')], None),
    # Another deployment's notices, another bot's posts and other messages don't count.
    ([notice('9.5', 'GCS prod deployment failed during build'), notice('9.4', 'HF dev deployment failed during build', bot='B0OTHER'),
      {'ts': '9.3', 'bot_id': 'B0BOT', 'text': 'Scan health: all green'}, notice('9.0', 'HF dev deployment succeeded')], '9.0'),
    # No notice within the history window: a new thread.
    ([], None),
])
def test_greens_are_run_length_threaded(monkeypatch, history, thread):
    slack = FakeSlack(history)
    made = fake(monkeypatch, slack)
    assert N.send(doc(), ENV) == thread
    assert made == [('xoxb-test-token', 'C0TESTCHAN')]
    assert slack.calls == [('history', 'C0TESTCHAN', 100), ('post', SUCCESS, thread)]


RED = {'status': 'failure', 'stage': 'upload', 'verified': False}


@pytest.mark.parametrize('changes', [RED, {'status': 'failure', 'stage': 'build', 'verified': False}, {'status': 'cancelled', 'verified': False}])
@pytest.mark.parametrize('history,thread', [
    # The newest notice for this deployment is red (a failure or a cancellation): the red run continues in its thread.
    ([notice('9.0', 'HF dev deployment failed during upload'), notice('8.0', 'HF dev deployment succeeded')], '9.0'),
    ([notice('9.0', 'HF prod deployment cancelled'), notice('8.0', 'HF dev deployment succeeded')], '9.0'),
    # A green ended the red run: this red opens a new one.
    ([notice('9.0', 'HF dev deployment succeeded'), notice('8.0', 'HF dev deployment failed during upload')], None),
    # Another deployment's red doesn't count.
    ([notice('9.5', 'GCS dev deployment failed during upload'), notice('9.0', 'HF dev deployment succeeded')], None),
    # No notice within the history window: a new thread.
    ([], None),
])
def test_reds_are_run_length_threaded(monkeypatch, changes, history, thread):
    slack = FakeSlack(history)
    fake(monkeypatch, slack)
    assert N.send(doc(**changes), ENV) == thread
    assert slack.calls == [('history', 'C0TESTCHAN', 100), ('post', N.render(doc(**changes)), thread)]


def test_thrds_renders_the_wire_text():
    from thrds.mrkdwn import to_slack
    assert to_slack(SUCCESS) == WIRE
    # What Slack stores is what the run lookup reads back.
    assert N.run_thread([{'ts': '1.0', 'bot_id': 'B0BOT', 'text': WIRE}], 'hf', True, BOT) == '1.0'


@pytest.mark.parametrize('env,message', [
    ({'SLACK_DEPLOY_BOT_TOKEN': 'xoxp-user-token'}, 'SLACK_DEPLOY_BOT_TOKEN must be a Slack bot token'),
    ({'SLACK_DEPLOY_BOT_TOKEN': ''}, 'SLACK_DEPLOY_BOT_TOKEN must be a Slack bot token'),
    ({'SLACK_DEPLOY_CHANNEL': '#hf-admin'}, 'SLACK_DEPLOY_CHANNEL must be a Slack channel ID'),
])
def test_transport_configuration_is_checked_before_connecting(monkeypatch, env, message):
    def unexpected(*args):
        pytest.fail('a misconfigured send must not connect')
    monkeypatch.setattr(N, 'slack_client', unexpected)
    with pytest.raises(ClickException) as error:
        N.send(doc(), {**ENV, **env})
    assert str(error.value) == message


@pytest.mark.parametrize('failure', [RuntimeError('Slack API error: channel_not_found'), URLError('private')])
def test_uncertain_delivery_is_not_retried_or_leaked(monkeypatch, failure):
    slack = FakeSlack(fail=failure)
    fake(monkeypatch, slack)
    with pytest.raises(ClickException) as error:
        N.send(doc(status='failure', stage='upload', verified=False), ENV)
    assert (len(slack.calls), str(error.value)) == (2, 'Slack notification failed; acceptance unknown, not retried')


def test_failed_history_lookup_posts_nothing(monkeypatch):
    slack = FakeSlack()
    def broken(channel, limit):
        raise RuntimeError('Slack API error: missing_scope')
    slack.list_channel_history = broken
    fake(monkeypatch, slack)
    with pytest.raises(ClickException) as error:
        N.send(doc(), ENV)
    assert (slack.calls, str(error.value)) == ([], 'Slack history lookup failed; nothing posted')


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
    # `site/deploy` releases only committed source (`scripts/require-clean`): this tree is the fake git's clean `SHA`.
    (tmp_path / 'scripts').mkdir()
    executable(tmp_path / 'scripts/require-clean', f'''import sys
if len(sys.argv) == 1: print('{SHA}')
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
    assert result.stderr == ('Error: SLACK_DEPLOY_BOT_TOKEN must be a Slack bot token\n'
                             'WARN: deploy notification failed; original deployment exit status preserved\n' if enabled else '')


@pytest.mark.parametrize('env, url', [
    ({'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_REPOSITORY': 'Open-Athena/disky', 'GITHUB_RUN_ID': '123'},
     'https://github.com/Open-Athena/disky/actions/runs/123'),
    ({'GITHUB_SERVER_URL': 'https://github.com', 'GITHUB_REPOSITORY': 'Open-Athena/disky'}, None),
    ({}, None),
])
def test_run_url_names_the_recording_actions_run(env, url):
    assert N.run_url(env) == url
