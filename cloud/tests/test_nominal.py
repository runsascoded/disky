"""Exact nominal-thread copy, authoritative outcomes, adoption and interrupted/concurrent writer behavior."""
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from dt_cloud import nominal as N
from dt_cloud.append_runner import Busy, LocalLeaseFile, coordinator_lease

T0 = int(datetime(2026, 11, 1, 8, tzinfo=timezone.utc).timestamp())
CFG = N.Config(state='unused', site='https://site.test', channel='C123', title='Test scans')


def row(i, status='succeeded', issues=None, **fields):
    run = dict(run_id=f'r{i}', scan=f'2026-11-{i + 1:02d}T0800', store='primary', kind='scan', parent=None,
               status=status, started_ts=T0 + 86400 * i, finished_ts=T0 + 86400 * i + 60, trigger='schedule')
    run.update(fields)
    return dict(run=run, phase_issues=[] if issues is None else issues, secs=60)


def history(n):
    return [*(row(i) for i in range(n, 0, -1)), row(0, 'failed')]


class Slack:
    def __init__(self):
        self.messages_by_id = {}
        self.calls = []
        self.timeout = False

    def owns(self, message):
        return message.get('bot_id') == 'B1'

    def messages(self, parent):
        return [m for m in self.messages_by_id.values() if m['ts'] == parent or m['parent'] == parent]

    def find(self, operation, parent, since):
        return next((m['ts'] for m in self.messages_by_id.values() if m['operation'] == operation and m['parent'] == parent), None)

    def post(self, body, parent, operation):
        ts = f'{len(self.messages_by_id) + 1}.0'
        self.messages_by_id[ts] = dict(ts=ts, parent=parent, operation=operation, text=body, bot_id='B1')
        self.calls.append(('post', ts, parent, body))
        if self.timeout:
            self.timeout = False
            raise TimeoutError('response lost after Slack accepted the post')
        return ts

    def edit(self, ts, body):
        self.calls.append(('edit', ts, body))
        self.messages_by_id[ts]['text'] = body


class Writer:
    def __init__(self, cfg=CFG):
        self.cfg, self.slack = cfg, Slack()
        self.saved = {'namespace': cfg.namespace}

    def save(self, value):
        self.saved = deepcopy(value)

    def run(self, rows):
        return N.converge(self.cfg, rows, deepcopy(self.saved), self.slack, self.save, lambda: None, now=lambda: 1000)


def test_copy_states_boundary_trigger_mix_and_exact_run_links():
    rows = [row(2, trigger='manual'), row(1), row(0, 'failed')]
    good, boundary = N.streak(rows)
    assert N.parent(CFG, good, boundary) == (
        '✅ **Test scans: 2 consecutive clean runs**\n\n'
        'Since 2026-11-02 08:00 UTC after [2026-11-01T0800](https://site.test/scans/r0).\n\n'
        'Latest completed: [2026-11-03 08:00 UTC](https://site.test/scans/r2). No phase issues.\n\n'
        'Latest 2 runs: 1 scheduled / 1 manual. Each reply links its scan record. [All scans](https://site.test/scans).'
    )
    assert [N.reply(CFG, r) for r in reversed(good)] == [
        '✅ [2026-11-02 08:00 UTC](https://site.test/scans/r1) · scheduled · 1m 00s · no phase issues',
        '✅ [2026-11-03 08:00 UTC](https://site.test/scans/r2) · manual · 1m 00s · no phase issues',
    ]
    assert N.reply(CFG, {**row(3, trigger=None), 'secs': None}) == (
        '✅ [2026-11-04 08:00 UTC](https://site.test/scans/r3) · trigger unknown · duration unknown · no phase issues'
    )


def test_initial_bootstrap_is_ten_then_catches_up_all_unreported_runs_once():
    w = Writer()
    w.run(history(12))
    assert list(w.saved['posted']) == [f'r{i}' for i in range(3, 13)]
    w.slack.calls.clear()
    w.run(history(15))
    assert [(c[0], c[1]) for c in w.slack.calls] == [('post', '12.0'), ('post', '13.0'), ('post', '14.0'), ('edit', '1.0')]
    assert list(w.saved['posted']) == [f'r{i}' for i in range(6, 16)]
    w.slack.calls.clear()
    w.run(history(15))
    assert w.slack.calls == []


@pytest.mark.parametrize('during', ['parent', 'reply'])
def test_lost_response_recovers_owned_metadata_without_a_second_post(during):
    w = Writer()
    if during == 'reply':
        w.run(history(1))
    w.slack.timeout = True
    with pytest.raises(TimeoutError, match='response lost after Slack accepted the post'):
        w.run(history(2))
    w.run(history(2))
    assert [(c[0], c[1]) for c in w.slack.calls] == (
        [('post', '1.0'), ('post', '2.0'), ('post', '3.0')] if during == 'parent' else
        [('post', '1.0'), ('post', '2.0'), ('post', '3.0'), ('edit', '1.0')]
    )
    assert w.saved['posted'] == {'r1': '2.0', 'r2': '3.0'}
    assert w.saved.get('pending') is None


@pytest.mark.parametrize('during', ['parent', 'reply'])
def test_delayed_slack_history_retains_ambiguous_send_without_posting_again(during):
    w = Writer()
    if during == 'reply':
        w.run(history(1))
    w.slack.timeout = True
    with pytest.raises(TimeoutError, match='response lost after Slack accepted the post'):
        w.run(history(2))
    before = deepcopy(w.slack.calls)
    original_find = w.slack.find
    w.slack.find = lambda *args: None
    with pytest.raises(RuntimeError, match='send remains ambiguous'):
        w.run(history(2))
    assert w.slack.calls == before
    assert w.saved['pending']['key'] == ('parent' if during == 'parent' else 'r2')
    assert w.saved['operator_required'] == w.saved['pending']['operation']
    w.slack.find = original_find
    w.run(history(2))
    assert [(c[0], c[1]) for c in w.slack.calls] == (
        [('post', '1.0'), ('post', '2.0'), ('post', '3.0')] if during == 'parent' else
        [('post', '1.0'), ('post', '2.0'), ('post', '3.0'), ('edit', '1.0')]
    )
    assert w.saved.get('operator_required') is None
    assert w.saved['posted'] == {'r1': '2.0', 'r2': '3.0'}


def test_adopt_existing_parent_and_replies_preserves_foreign_reply():
    from dataclasses import replace

    w = Writer()
    w.run(history(2))
    w.slack.messages_by_id['foreign'] = dict(ts='foreign', parent='1.0', text=N.reply(CFG, row(3)), bot_id='OTHER', operation='foreign')
    cfg = replace(CFG, adopt_parent='1.0')
    w.saved = N.adopt(cfg, history(4), w.slack)
    w.slack.calls.clear()
    w.run(history(4))
    assert [(c[0], c[1]) for c in w.slack.calls] == [('post', '5.0'), ('post', '6.0'), ('edit', '1.0')]
    assert w.saved['parent'] == '1.0'
    assert w.slack.messages_by_id['foreign'] == dict(ts='foreign', parent='1.0', text=N.reply(CFG, row(3)), bot_id='OTHER', operation='foreign')


@pytest.mark.parametrize('bad', [
    row(3, 'failed'), row(3, 'nop'), row(3, 'unknown'),
    row(3, issues=[{'phase': 'probe', 'outcome': 'failed', 'recovery': None}]),
    row(3, issues=[{'phase': 'static-names', 'outcome': 'skipped', 'recovery': {'run_id': 'later'}}]),
])
def test_every_nonclean_boundary_closes_once_then_recovery_gets_new_parent(bad):
    w = Writer()
    w.run(history(2))
    w.slack.calls.clear()
    w.run([bad, *history(2)])
    assert [(c[0], c[1]) for c in w.slack.calls] == [('edit', '1.0')]
    assert w.saved['closed'] is True
    w.slack.calls.clear()
    w.run([bad, *history(2)])
    assert w.slack.calls == []
    w.run([row(4), bad, *history(2)])
    assert [(c[0], c[1]) for c in w.slack.calls] == [('post', '4.0'), ('post', '5.0')]
    assert w.saved['parent'] == '4.0'
    assert w.saved['posted'] == {'r4': '5.0'}


def test_running_or_unfinalized_head_does_not_announce_success():
    w = Writer()
    for unfinished in [row(2, 'running', finished_ts=None), row(2, finished_ts=None)]:
        w.run([unfinished, *history(1)])
    assert w.slack.calls == []


def test_lost_history_boundary_does_not_announce_a_false_recovery_or_reset():
    w = Writer()
    w.run(history(2))
    w.slack.calls.clear()
    with pytest.raises(ValueError, match='history no longer covers the recorded streak boundary'):
        w.run([row(3), row(2), row(1)])
    assert w.slack.calls == []


def test_missing_evidence_refused_and_other_store_or_child_never_counts():
    doc = dict(configured=True, runs=[row(2), row(3, store='meta'), row(4, parent='r2'), row(1), row(0, 'failed')])
    assert [r['run']['run_id'] for r in N.ordered(doc, CFG)] == ['r2', 'r1', 'r0']
    del doc['runs'][0]['phase_issues']
    with pytest.raises(ValueError, match='missing phase-issue evidence'):
        N.ordered(doc, CFG)
    with pytest.raises(ValueError, match='run ledger is not configured'):
        N.ordered(dict(configured=False, runs=[]), CFG)


def test_uncertain_history_is_not_presented_as_an_exact_all_history_streak():
    body = N.parent(CFG, [row(2), row(1)], None)
    assert body.splitlines()[:3] == [
        '✅ **Test scans: at least 2 consecutive clean runs**', '',
        'Since 2026-11-02 08:00 UTC (available run history).',
    ]


def test_state_namespace_and_lost_lease_refuse_side_effects():
    w = Writer()
    with pytest.raises(ValueError, match='another site/store/channel'):
        N.converge(CFG, history(1), {'namespace': 'other'}, w.slack, w.save, lambda: None)
    def lost():
        raise RuntimeError('lease lost')
    with pytest.raises(RuntimeError, match='lease lost'):
        N.converge(CFG, history(1), w.saved, w.slack, w.save, lost)
    assert w.slack.calls == []


def test_second_writer_cannot_enter_while_generation_lease_is_held(tmp_path):
    lease = LocalLeaseFile(tmp_path / 'lease.json')
    with coordinator_lease(lease, holder='first'):
        with pytest.raises(Busy, match='held by first'):
            with coordinator_lease(lease, holder='second'):
                pytest.fail('concurrent writer entered')
    assert lease.read() is None


def test_reporting_failure_keeps_job_result_and_redacts_error(monkeypatch):
    from dt_cloud import scan_runs

    said = []
    monkeypatch.setattr(scan_runs, 'err', said.append)
    def fail(*args, **kwargs):
        raise ValueError('secret-123 refused')
    monkeypatch.setattr(N, 'run', fail)
    job = SimpleNamespace(profile=SimpleNamespace(nominal=vars(CFG), project='p', store='primary'), recording=True,
                          dry_run=False, env={}, run_id='r1', state=lambda: {}, redact=lambda s: s.replace('secret-123', '<SECRET>'))
    assert N.report_job(job) is None
    assert said == ['nominal report failed: ValueError: <SECRET> refused']


@pytest.mark.parametrize('env', [{'DT_ALERTS': 'stderr'}, {'DT_ALERTS': 'off'}, {'DT_ALERTS': '0'}])
def test_suppressed_reports_do_not_read_or_post(monkeypatch, env):
    def forbidden(*args, **kwargs):
        pytest.fail('suppressed reporter ran')
    monkeypatch.setattr(N, 'run', forbidden)
    job = SimpleNamespace(profile=SimpleNamespace(nominal=vars(CFG)), recording=True, dry_run=False, env=env)
    assert N.report_job(job) is None


def test_preview_never_calls_slack_or_writes_state_and_execute_is_idempotent(tmp_path, monkeypatch):
    import json
    from dataclasses import asdict, replace

    from dt_cloud import probe

    cfg = replace(CFG, state=str(tmp_path / 'state'))
    slack = Slack()
    monkeypatch.setattr(probe, 'http_fetch', lambda *args: lambda path: SimpleNamespace(status=200, body=json.dumps(dict(configured=True, runs=history(2)))))
    def transport(*args):
        return slack
    monkeypatch.setattr(N, 'Slack', transport)
    preview = N.run(asdict(cfg), {'SITE_TOKEN': 'viewer'}, 'project')
    assert preview == '\n\n+++\n\n'.join([N.parent(cfg, history(2)[:2], history(2)[2]), N.reply(cfg, row(1)), N.reply(cfg, row(2))])
    assert sorted(tmp_path.iterdir()) == []
    assert slack.calls == []
    env = {'SITE_TOKEN': 'viewer', 'SLACK_BOT_TOKEN': 'bot'}
    assert N.run(asdict(cfg), env, 'project', 'r2', execute=True) == 'nominal: parent 1.0; 2 run replies'
    slack.calls.clear()
    assert N.run(asdict(cfg), env, 'project', 'r2', execute=True) == 'nominal: parent 1.0; 2 run replies'
    assert slack.calls == []
    with pytest.raises(ValueError, match='terminal run record is not visible'):
        N.run(asdict(cfg), env, 'project', 'not-recorded', execute=True)
    assert slack.calls == []


def test_supervisor_reports_after_signal_overrides_early_success(tmp_path, monkeypatch):
    import signal

    from dt_cloud import scan_runs as S

    events = []
    job = SimpleNamespace(state=lambda: {'finished': 0},
                          finish=lambda rc, why: events.append(('finish', rc, why)), clear=lambda: events.append(('clear',)))
    monkeypatch.setattr(S.JobRun, 'pointed', lambda path: job)
    monkeypatch.setattr(N, 'report_job', lambda j: events.append(('report',)))
    S.supervised_finish(tmp_path / 'pointer', S.Ran(rc=143, signal=signal.SIGTERM), 20)
    assert events == [('finish', 143, 'terminated (SIGTERM)'), ('report',), ('clear',)]


def test_adoption_review_proposes_only_missing_replies_and_writes_nothing(tmp_path, monkeypatch):
    import json
    from dataclasses import asdict, replace

    from dt_cloud import probe

    w = Writer()
    w.run(history(2))
    w.slack.calls.clear()
    cfg = replace(CFG, state=str(tmp_path / 'state'), adopt_parent='1.0')
    monkeypatch.setattr(N, 'Slack', lambda *args: w.slack)
    monkeypatch.setattr(probe, 'http_fetch', lambda *args: lambda path: SimpleNamespace(status=200, body=json.dumps(dict(configured=True, runs=history(4)))))
    preview = N.run(asdict(cfg), {'SITE_TOKEN': 'viewer', 'SLACK_BOT_TOKEN': 'bot'}, 'project', adopt_check=True)
    assert preview == '\n\n+++\n\n'.join([N.parent(cfg, history(4)[:4], history(4)[4]), N.reply(cfg, row(3)), N.reply(cfg, row(4))])
    assert w.slack.calls == []
    assert sorted(tmp_path.iterdir()) == []


def test_finish_defers_report_until_supervisor_and_plain_finish_reports_after_record(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from dt_cloud import scan_runs as S

    events = []
    job = SimpleNamespace(finish=lambda *args: events.append(('finish',)),
                          save=lambda **kw: events.append(('save', kw)), clear=lambda: events.append(('clear',)), supervised=True)
    monkeypatch.setattr(S, 'JobRun', lambda *args: job)
    monkeypatch.setattr(N, 'report_job', lambda j: events.append(('report',)))
    runner = CliRunner()
    result = runner.invoke(S.cli, ['finish', '-x', '0'])
    assert (result.exit_code, events) == (0, [('finish',), ('save', {'finished': 0})])
    job.supervised = False
    events.clear()
    result = runner.invoke(S.cli, ['finish', '-x', '0'])
    assert (result.exit_code, events) == (0, [('finish',), ('report',), ('clear',)])


def test_slack_recovery_paginates_checks_ownership_and_attaches_metadata():
    calls = []
    def request(endpoint, args, **kw):
        calls.append((endpoint, dict(args)))
        return {'messages': [dict(ts='foreign', bot_id='OTHER', metadata={'event_payload': {'operation': 'op'}})], 'response_metadata': {'next_cursor': 'next'}} if 'cursor' not in args else {
            'messages': [dict(ts='owned', bot_id='B1', metadata={'event_payload': {'operation': 'op'}})], 'response_metadata': {}}
    slack = N.Slack.__new__(N.Slack)
    client = SimpleNamespace(channel='C123', bot_ids=('U1', 'B1'), _request=request, _metadata_by_content=None)
    slack.client = client
    assert slack.find('op', 'root', 100) == 'owned'
    assert calls == [
        ('conversations.replies', dict(channel='C123', limit=100, include_all_metadata=True, ts='root', oldest='100')),
        ('conversations.replies', dict(channel='C123', limit=100, include_all_metadata=True, ts='root', oldest='100', cursor='next')),
    ]
    sent = []
    def post(body, thread_id):
        sent.append((body, thread_id, deepcopy(client._metadata_by_content)))
        return SimpleNamespace(id='new')
    client.post = post
    assert slack.post('body', 'root', 'op') == 'new'
    assert sent == [('body', 'root', {'body': {'event_type': 'disky_nominal', 'event_payload': {'operation': 'op'}}})]
    assert client._metadata_by_content is None
