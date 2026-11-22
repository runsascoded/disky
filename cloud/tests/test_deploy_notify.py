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
        'verified': True, 'dirty': False, 'actor': 'louisa', 'actor_source': 'github', 'attempt': 'df94aa1e-1922-4f93-86ec-5110e231222d',
        'cloudflare_deployment': 'abcdef12', 'cloudflare_url': 'https://abcdef12.oa-hf-usage.pages.dev',
        'run_url': 'https://github.com/Open-Athena/disky/actions/runs/123', **changes,
    }


REPO = 'https://github.com/Open-Athena/disky'
SUCCESS = (f'[hf]({REPO}/tree/hf) ([bbbbbbbbb]({REPO}/commit/{SHA})) · :gh: [louisa](https://github.com/louisa) · attempt `df94aa1e`'
           f' · :cf: [abcdef12](https://abcdef12.oa-hf-usage.pages.dev) · [site](https://dev.hf.oa.dev) · [logs]({REPO}/actions/runs/123)\n')
SENDER = '🧪 HF dev deployment succeeded'


def test_render_exact_success_omits_private_controller_fields():
    assert N.render(doc(account_id='private', credentials={'secret': 'private'}, error='private')) == SUCCESS


def test_github_bot_actor_is_rendered_without_rejecting_automated_releases():
    assert N.render(doc(actor='github-actions[bot]')) == SUCCESS.replace(':gh: [louisa](https://github.com/louisa)', ':gh: `github-actions[bot]`')


@pytest.mark.parametrize('site,label', [('gcs', 'GCS'), ('cw', 'CW'), ('hf', 'HF'), ('r2', 'R2')])
@pytest.mark.parametrize('mode', ['dev', 'prod'])
def test_each_site_and_environment_uses_its_own_identity(site, label, mode):
    host = f'{"dev." if mode == "dev" else ""}{site}.oa.dev'
    value = doc(deployment=site, source_branch=site, mode=mode, host=host)
    expected = SUCCESS.replace('[hf](', f'[{site}](').replace('/tree/hf)', f'/tree/{site})').replace('https://dev.hf.oa.dev', f'https://{host}')
    assert (N.headline(value), N.render(value)) == (f'{"🧪" if mode == "dev" else "🚀"} {label} {mode} deployment succeeded', expected)


def test_render_build_failure_has_no_fictitious_cf_receipt_or_verification():
    value = doc(status='failure', stage='build', verified=False, cloudflare_deployment=None,
                cloudflare_url=None, actor=None, actor_source=None, sha=None, source_branch=None)
    assert (N.headline(value), N.render(value)) == (
        '💥 HF dev deployment failed during build',
        f'source not recorded · attempt `df94aa1e` · [site](https://dev.hf.oa.dev) · [logs]({REPO}/actions/runs/123)\n',
    )


def test_render_unverified_dirty_manual_upload_and_cancellation():
    value = doc(mode='prod', host='hf.oa.dev', verified=False, dirty=True, actor_source='local', run_url=None)
    expected = (f'[hf]({REPO}/tree/hf) ([bbbbbbbbb]({REPO}/commit/{SHA})) · ⚠️ dirty · ⚠️ unverified · :computer: `louisa`'
                ' · attempt `df94aa1e` · :cf: [abcdef12](https://abcdef12.oa-hf-usage.pages.dev) · [site](https://hf.oa.dev)\n')
    assert (N.headline(value), N.render(value)) == ('🚀 HF prod deployment succeeded', expected)
    # a cancellation isn't a success, so it isn't "unverified"; an unrecorded tree says so
    cancelled = {**value, 'status': 'cancelled', 'dirty': None}
    assert (N.headline(cancelled), N.render(cancelled)) == (
        '⏹️ HF prod deployment cancelled', expected.replace(' · ⚠️ dirty · ⚠️ unverified', ' · ⚠️ tree state not recorded'),
    )


@pytest.mark.parametrize('changes,message', [
    ({'notice_version': 2}, 'notice_version must be 1'),
    ({'sha': 'b' * 10}, 'sha must be a full 40-character revision or null'),
    ({'host': 'bad.example/@channel'}, 'host must be a DNS name'),
    ({'source_branch': '<!channel>'}, 'source_branch must be a plain source ref or null'),
    ({'actor': '<@USER>'}, 'actor must be a plain user identifier or null'),
    ({'run_url': 'https://secret@github.com/example'}, 'run_url: expected an HTTPS URL without credentials, port, query or fragment'),
    ({'status': 'failure'}, 'verified must be a boolean and requires success'),
    ({'emoji': {'success': ':tada:'}}, 'emoji must map success/failure/cancelled to an emoji character (the sender name shows no :name: codes)'),
    ({'emoji': {'merged': '🎉'}}, 'emoji must map success/failure/cancelled to an emoji character (the sender name shows no :name: codes)'),
    ({'threads': 'mode'}, 'threads must be env or shared'),
    ({'quiet': 'yes'}, 'quiet must be a boolean'),
])
def test_invalid_or_injectable_receipt_is_refused(changes, message):
    with pytest.raises(ClickException) as error:
        N.render(doc(**changes))
    assert str(error.value) == message


def test_a_deployment_overrides_the_default_emoji_per_outcome():
    """`deploy_emoji` on a site (`sites.json`, carried in the receipt by `record`) replaces only the outcomes it names."""
    value = doc(emoji={'success': '✅'})
    assert [N.headline({**value, **c}) for c in ({}, {'status': 'failure', 'verified': False}, {'status': 'cancelled', 'verified': False})] == [
        '✅ HF dev deployment succeeded', '💥 HF dev deployment failed during finish', '⏹️ HF dev deployment cancelled',
    ]


def test_prod_and_dev_lead_with_their_own_emoji():
    outcomes = ({}, {'status': 'failure', 'verified': False}, {'status': 'cancelled', 'verified': False})
    assert [[N.headline(doc(mode=mode, **c)).split(' ', 1)[0] for c in outcomes] for mode in ('prod', 'dev')] == [
        ['🚀', '💥', '⏹️'], ['🧪', '💥', '⏹️'],
    ]


@pytest.mark.parametrize('config,emoji', [
    # today's flat form: both environments
    ({'success': '✅'}, ({'success': '✅'}, {'success': '✅'})),
    # keyed by environment; the environment's own entry wins over a flat one
    ({'dev': {'success': '🧫'}}, (None, {'success': '🧫'})),
    ({'success': '✅', 'failure': '🔥', 'prod': {'success': '🛳️'}}, ({'success': '🛳️', 'failure': '🔥'}, {'success': '✅', 'failure': '🔥'})),
    (None, (None, None)),
])
def test_site_emoji_resolves_per_environment(config, emoji):
    assert (N.site_emoji(config, 'prod'), N.site_emoji(config, 'dev')) == emoji


@pytest.mark.parametrize('config', [{'merged': '🎉'}, {'dev': '🧫'}, {'prod': {'merged': '🎉'}}, ['🚀']])
def test_site_emoji_refuses_unknown_keys(config):
    with pytest.raises(ClickException) as error:
        N.site_emoji(config, 'dev')
    assert str(error.value) == 'deploy_emoji must map success/failure/cancelled (each, or under dev/prod) to an emoji character'


def test_record_carries_the_sites_deploy_emoji_and_omits_it_when_unset(tmp_path):
    (tmp_path / 'wrangler.toml').write_text('''name = "oa-hf-usage"
[vars]
STORE = "hf"
PROD_HOST = "hf.oa.dev"
REPO_URL = "https://github.com/Open-Athena/disky"
SOURCE_REF = "hf"
''')
    sites = tmp_path / 'sites.json'
    out = tmp_path / 'notice.json'
    args = ['record', '-c', str(tmp_path / 'wrangler.toml'), '-e', 'prod', '-g', SHA, '-l', str(tmp_path / 'none.log'),
            '-o', str(out), '-S', str(sites), '-s', 'success', '-t', 'finish', '-u', 'false', '-v']
    emojis = []
    for config in ({'sites': {'hf': {'deploy_emoji': {'success': '🛳️'}, 'deploy_threads': 'shared', 'deploy_quiet': False}}},
                   {'sites': {'hf': {'deploy_emoji': {'dev': {'success': '🧫'}, 'prod': {'success': '🛸'}}}}},
                   {'sites': {'hf': {}}}):
        sites.write_text(dumps(config))
        result = CliRunner().invoke(N.main, args, env={'GITHUB_ACTOR': ''})
        assert result.exit_code == 0, result.output
        r = loads(out.read_text())
        emojis.append((r.get('emoji'), r['threads'], r['quiet'], N.headline(r)))
    assert emojis == [
        ({'success': '🛳️'}, 'shared', False, '🛳️ HF prod deployment succeeded'),
        ({'success': '🛸'}, 'env', True, '🛸 HF prod deployment succeeded'),
        (None, 'env', True, '🚀 HF prod deployment succeeded'),
    ]


ENV = {'DEPLOY_NOTIFICATIONS_ENABLED': '1', 'SLACK_DEPLOY_BOT_TOKEN': 'xoxb-test-token', 'SLACK_DEPLOY_CHANNEL': 'C0TESTCHAN'}
BOT = ('U0BOT', 'B0BOT')
WIRE = (f'<{REPO}/tree/hf|hf> (<{REPO}/commit/{SHA}|bbbbbbbbb>) · :gh: <https://github.com/louisa|louisa> · attempt `df94aa1e`'
        f' · :cf: <https://abcdef12.oa-hf-usage.pages.dev|abcdef12> · <https://dev.hf.oa.dev|site> · <{REPO}/actions/runs/123|logs>\n')


def notice(ts: str, headline: str, bot: str = 'B0BOT') -> dict:
    """A notice as `conversations.history` returns it: the outcome in its sender name."""
    return {'ts': ts, 'bot_id': bot, 'username': f'❌ {headline}', 'text': '[hf](…) · …'}


def legacy(ts: str, headline: str, bot: str = 'B0BOT') -> dict:
    """A notice from before 2026-11-14: the bot's own name, the outcome the text's bold first line."""
    return {'ts': ts, 'bot_id': bot, 'text': f':x: *{headline}*\n\nSource: …'}


class FakeSlack:
    def __init__(self, history=(), fail=None):
        self.history, self.fail, self.calls = list(history), fail, []
        self.bot_ids = BOT

    def list_channel_history(self, channel, limit):
        self.calls.append(('history', channel, limit))
        return self.history

    def post(self, content, thread_id=None, username=None):
        self.calls.append(('post', username, content, thread_id))
        if self.fail:
            raise self.fail

    def edit(self, message_id, content, raw=None):
        self.calls.append(('edit', message_id, content, raw))
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
    assert (result.exit_code, result.output) == (0, f'{SENDER}\n{SUCCESS}')
    result = CliRunner().invoke(N.main, ['send', '-x', str(path)], env={**ENV, 'DEPLOY_NOTIFICATIONS_ENABLED': '0'})
    assert (result.exit_code, result.output) == (1, 'Error: Deploy notifications are disabled pending template/transport review\n')


@pytest.mark.parametrize('history,threads,thread', [
    # Each environment runs on its own: the newest dev notice is red, so this dev green opens a thread, whatever prod did.
    ([notice('9.0', 'HF prod deployment succeeded'), notice('8.0', 'HF dev deployment failed during build')], 'env', None),
    ([notice('9.0', 'HF prod deployment failed during build'), notice('8.0', 'HF dev deployment succeeded')], 'env', '8.0'),
    # `deploy_threads: shared`: one run across dev and prod, as before 2026-11-21.
    ([notice('9.0', 'HF prod deployment succeeded'), notice('8.0', 'HF dev deployment failed during build')], 'shared', '9.0'),
    ([notice('9.0', 'HF prod deployment failed during build'), notice('8.0', 'HF dev deployment succeeded')], 'shared', None),
    # A red ended the run (failure or cancellation): the next green opens a new one.
    ([notice('9.0', 'HF dev deployment failed during upload'), notice('8.0', 'HF dev deployment succeeded')], 'env', None),
    ([notice('9.0', 'HF dev deployment cancelled'), notice('8.0', 'HF dev deployment succeeded')], 'env', None),
    # Another deployment's notices, another bot's posts and other messages don't count.
    ([notice('9.5', 'GCS dev deployment failed during build'), notice('9.4', 'HF dev deployment failed during build', bot='B0OTHER'),
      {'ts': '9.3', 'bot_id': 'B0BOT', 'text': 'Scan health: all green'}, notice('9.0', 'HF dev deployment succeeded')], 'env', '9.0'),
    # A notice from before the one-line format still counts.
    ([legacy('9.0', 'HF dev deployment succeeded')], 'env', '9.0'),
    ([legacy('9.0', 'HF prod deployment succeeded')], 'env', None),
    # The lead emoji doesn't matter: notices from before 🚀/💥 (✅/❌) and after both count.
    ([{**notice('9.0', 'HF dev deployment succeeded'), 'username': '🚀 HF dev deployment succeeded'}], 'env', '9.0'),
    ([{**notice('9.0', 'HF dev deployment succeeded'), 'username': '✅ HF dev deployment succeeded'}], 'env', '9.0'),
    ([{**notice('9.0', 'HF dev deployment failed during build'), 'username': '💥 HF dev deployment failed during build'}], 'env', None),
    ([legacy('9.0', 'HF dev deployment failed during upload')], 'env', None),
    # No notice within the history window: a new thread.
    ([], 'env', None),
])
def test_greens_are_run_length_threaded_per_environment(monkeypatch, history, threads, thread):
    """With `quiet` off, a green joining a green thread replies in it, as reds do."""
    slack = FakeSlack(history)
    made = fake(monkeypatch, slack)
    assert N.send(doc(threads=threads, quiet=False), ENV) == (thread, False)
    assert made == [('xoxb-test-token', 'C0TESTCHAN')]
    assert slack.calls == [('history', 'C0TESTCHAN', 100), ('post', SENDER, SUCCESS, thread)]


#: 2026-11-21 14:05 UTC, a quiet fold's time
NOW = 1795269900
#: `SUCCESS`'s wire line, as the fold line carries it
LATEST = WIRE.rstrip('\n')


@pytest.mark.parametrize('head,threads,text', [
    # The thread's head as first posted: its own line stays, a line counting the run (2: it and this one) follows.
    ({'text': WIRE}, 'env', f'{LATEST}\n2 successful dev deploys in a row · latest 2026-11-21 14:05 UTC: {LATEST}'),
    # A thread from before quiet greens: its replies count.
    ({'text': WIRE, 'reply_count': 3}, 'env', f'{LATEST}\n5 successful dev deploys in a row · latest 2026-11-21 14:05 UTC: {LATEST}'),
    # An earlier fold's line is replaced, its count carried on.
    ({'text': f'{LATEST}\n4 successful dev deploys in a row · latest 2026-11-20 09:00 UTC: <https://dev.hf.oa.dev|site>'}, 'env',
     f'{LATEST}\n5 successful dev deploys in a row · latest 2026-11-21 14:05 UTC: {LATEST}'),
    # `deploy_threads: shared`: the run spans both environments, so the newest says which.
    ({'text': WIRE}, 'shared', f'{LATEST}\n2 successful deploys in a row · latest 2026-11-21 14:05 UTC (dev): {LATEST}'),
])
def test_a_quiet_green_in_a_green_thread_edits_its_head_and_posts_nothing(monkeypatch, head, threads, text):
    """Ryan, 2026-11-21: a reply notifies the thread's followers and leaves an unread; an edit does neither."""
    slack = FakeSlack([{**notice('9.0', 'HF dev deployment succeeded'), **head}])
    fake(monkeypatch, slack)
    assert N.send(doc(threads=threads), ENV, now=NOW) == ('9.0', True)
    assert slack.calls == [('history', 'C0TESTCHAN', 100), ('edit', '9.0', text, True)]


@pytest.mark.parametrize('history,changes,thread', [
    # A green after a red, or with no notice in the window, still opens a thread: recovery is news.
    ([notice('9.0', 'HF dev deployment failed during upload')], {}, None),
    ([], {}, None),
    # A red still posts: a new thread after a green, a reply after a red.
    ([notice('9.0', 'HF dev deployment succeeded')], {'status': 'failure', 'stage': 'upload', 'verified': False}, None),
    ([notice('9.0', 'HF dev deployment failed during build')], {'status': 'failure', 'stage': 'upload', 'verified': False}, '9.0'),
])
def test_quiet_greens_never_silence_a_red_or_a_recovery(monkeypatch, history, changes, thread):
    slack = FakeSlack(history)
    fake(monkeypatch, slack)
    assert N.send(doc(**changes), ENV, now=NOW) == (thread, False)
    assert slack.calls == [('history', 'C0TESTCHAN', 100), ('post', N.headline(doc(**changes)), N.render(doc(**changes)), thread)]


RED = {'status': 'failure', 'stage': 'upload', 'verified': False}


@pytest.mark.parametrize('changes', [RED, {'status': 'failure', 'stage': 'build', 'verified': False}, {'status': 'cancelled', 'verified': False}])
@pytest.mark.parametrize('history,thread', [
    # The newest notice for this deployment is red (a failure or a cancellation): the red run continues in its thread.
    ([notice('9.0', 'HF dev deployment failed during upload'), notice('8.0', 'HF dev deployment succeeded')], '9.0'),
    ([notice('9.0', 'HF dev deployment cancelled'), notice('8.0', 'HF dev deployment succeeded')], '9.0'),
    # Prod's red run is its own: this dev red opens a new one.
    ([notice('9.0', 'HF prod deployment cancelled'), notice('8.0', 'HF dev deployment succeeded')], None),
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
    assert N.send(doc(**changes), ENV) == (thread, False)
    assert slack.calls == [('history', 'C0TESTCHAN', 100), ('post', N.headline(doc(**changes)), N.render(doc(**changes)), thread)]


def test_thrds_renders_the_wire_text():
    from thrds.mrkdwn import to_slack
    assert to_slack(SUCCESS) == WIRE
    # What Slack stores is what the run lookup reads back.
    head = {'ts': '1.0', 'bot_id': 'B0BOT', 'username': SENDER, 'text': WIRE}
    assert N.run_head([head], 'hf', 'dev', True, BOT) == head


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
@pytest.mark.parametrize('install_rc,build_rc,upload_rc,enabled,expected', [
    (1, 0, 0, False, (1, 'failure', 'build', None)),
    (0, 7, 0, False, (7, 'failure', 'build', None)),
    (0, 130, 0, False, (130, 'cancelled', 'build', None)),
    (0, 0, 9, False, (9, 'failure', 'upload', None)),
    (0, 0, 0, False, (0, 'success', 'finish', 'abcdef12')),
    (0, 0, 0, True, (0, 'success', 'finish', 'abcdef12')),
])
def test_manual_hook_records_failures_and_preserves_original_exit_status(tmp_path, install_rc, build_rc, upload_rc, enabled, expected, configured_store, expected_deployment):
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
    # `site/deploy` installs the lockfile's packages first (a frozen install, from the repo root), then builds, then uploads.
    executable(bins / 'pnpm', f'''import sys
if sys.argv[1:]==['install','--frozen-lockfile']: sys.exit({install_rc})
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
        'threads': 'env', 'quiet': True,
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
