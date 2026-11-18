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

    alerts = {}

    def alert(self, link, since):
        return self.alerts.get(link)

    def permalink(self, ts):
        return f'https://slack.test/p{ts}'


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
        'Latest completed: [2026-11-03 08:00 UTC](https://site.test/scans/r2) · 1m 00s.\n\n'
        'Latest 2 runs: 1 scheduled / 1 manual. Each reply links its scan record. [All scans](https://site.test/scans).'
    )
    assert [N.reply(CFG, r) for r in reversed(good)] == [
        '✅ :alarm_clock: [2026-11-02 08:00 UTC](https://site.test/scans/r1) · 1m 00s',
        '✅ :bust_in_silhouette: [2026-11-03 08:00 UTC](https://site.test/scans/r2) · 1m 00s',
    ]
    manual = row(5, trigger='manual')
    assert N.reply(CFG, {**manual, 'run': {**manual['run'], 'trigger_by': 'ada@example.org'}}) == (
        '✅ :bust_in_silhouette: [2026-11-06 08:00 UTC](https://site.test/scans/r5) · 1m 00s · ada@example.org'
    )
    assert N.reply(CFG, {**manual, 'run': {**manual['run'], 'trigger_by': '<!channel>'}}) == (
        '✅ :bust_in_silhouette: [2026-11-06 08:00 UTC](https://site.test/scans/r5) · 1m 00s'
    )
    assert N.reply(CFG, {**manual, 'run': {**manual['run'], 'trigger_by': 'ada@example.org', 'trigger_emoji': ':ada:'}}) == (
        '✅ :ada: [2026-11-06 08:00 UTC](https://site.test/scans/r5) · 1m 00s'
    )
    assert N.reply(CFG, {**manual, 'run': {**manual['run'], 'trigger_by': 'ada@example.org', 'trigger_emoji': '<!channel>'}}) == (
        '✅ :bust_in_silhouette: [2026-11-06 08:00 UTC](https://site.test/scans/r5) · 1m 00s · ada@example.org'
    )
    sched = row(6)
    assert N.reply(CFG, {**sched, 'run': {**sched['run'], 'trigger_by': 'ada@example.org'}}) == (
        '✅ :alarm_clock: [2026-11-07 08:00 UTC](https://site.test/scans/r6) · 1m 00s'
    )
    assert N.reply(CFG, {**row(3, trigger=None), 'secs': None}) == (
        '✅ :grey_question: [2026-11-04 08:00 UTC](https://site.test/scans/r3) · duration unknown'
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
    for unfinished in [row(3, 'running', finished_ts=None), row(3, finished_ts=None)]:
        w = Writer()
        w.run(history(2))
        w.slack.calls.clear()
        w.run([unfinished, *history(2)])
        assert bodies(w.slack) == [
            ('post', '4.0', '⏳ :alarm_clock: [2026-11-04 08:00 UTC](https://site.test/scans/r3) · running'),
            ('edit', '1.0', RUNNING_PARENT),
        ]
        assert (w.saved['posted'], w.saved['running']) == ({'r1': '2.0', 'r2': '3.0'}, {'r3': {'ts': '4.0', 'parent': '1.0'}})


#: history(2)'s thread head while r3 runs
RUNNING_PARENT = (
    '✅ **Test scans: 2 consecutive clean runs**\n\n'
    'Since 2026-11-02 08:00 UTC after [2026-11-01T0800](https://site.test/scans/r0).\n\n'
    'Latest completed: [2026-11-03 08:00 UTC](https://site.test/scans/r2) · 1m 00s.\n\n'
    'Running now: [2026-11-04 08:00 UTC](https://site.test/scans/r3).\n\n'
    'Latest 2 runs: 2 scheduled / 0 manual. Each reply links its scan record. [All scans](https://site.test/scans).'
)
ADA = dict(trigger='manual', trigger_by='ada@example.org', trigger_emoji=':ada:')


def running_writer(cfg=CFG):
    """history(2)'s thread, with r3 (Ada's) running: its ⏳ reply 4.0, the head 1.0 naming it."""
    w = Writer(cfg)
    w.run(history(2))
    w.run([row(3, 'running', finished_ts=None, **ADA), *history(2)])
    assert w.slack.calls[-2:] == [
        ('post', '4.0', '1.0', '⏳ :ada: [2026-11-04 08:00 UTC](https://site.test/scans/r3) · running'),
        ('edit', '1.0', RUNNING_PARENT),
    ]
    w.slack.calls.clear()
    w.run([row(3, 'running', finished_ts=None, **ADA), *history(2)])
    assert w.slack.calls == []
    return w


def test_a_running_reply_becomes_its_runs_reply_in_place():
    w = running_writer()
    w.run([row(3, **ADA), *history(2)])
    assert bodies(w.slack) == [
        ('edit', '4.0', '✅ :ada: [2026-11-04 08:00 UTC](https://site.test/scans/r3) · 1m 00s'),
        ('edit', '1.0', '✅ **Test scans: 3 consecutive clean runs**\n\n'
                        'Since 2026-11-02 08:00 UTC after [2026-11-01T0800](https://site.test/scans/r0).\n\n'
                        'Latest completed: [2026-11-04 08:00 UTC](https://site.test/scans/r3) · 1m 00s.\n\n'
                        'Latest 3 runs: 2 scheduled / 1 manual. Each reply links its scan record. [All scans](https://site.test/scans).'),
    ]
    assert (w.saved['posted'], 'running' in w.saved) == ({'r1': '2.0', 'r2': '3.0', 'r3': '4.0'}, False)


@pytest.mark.parametrize('alert, suffix', [('99.0', ' · [alert](https://slack.test/p99.0)'), (None, '')])
def test_a_clean_threads_running_reply_that_fails_closes_it_and_links_the_alert(alert, suffix):
    w = running_writer()
    w.slack.alerts = {'https://site.test/scans/r3': alert} if alert else {}
    w.run([row(3, 'failed', failed_phase='index', error='boom', **ADA), *history(2)])
    assert bodies(w.slack) == [
        ('edit', '1.0', '**Test scans: clean-run sequence ended after 2 runs**\n\n'
                        'Since 2026-11-02 08:00 UTC after [2026-11-01T0800](https://site.test/scans/r0); last [2026-11-03 08:00 UTC](https://site.test/scans/r2).\n\n'
                        'Ended by [2026-11-04 08:00 UTC](https://site.test/scans/r3): failed. [All scans](https://site.test/scans).'),
        ('edit', '4.0', '❌ :ada: [2026-11-04 08:00 UTC](https://site.test/scans/r3) · 1m 00s · failed in index: boom' + suffix),
    ]
    assert (w.saved['closed'], 'running' in w.saved) == (True, False)


def test_a_running_reply_whose_run_opens_a_failure_thread_links_it():
    from dataclasses import replace

    w = running_writer(replace(CFG, group='color'))
    w.run([row(3, 'failed', failed_phase='index', error='boom', **ADA), *history(2)])
    failed = '❌ :ada: [2026-11-04 08:00 UTC](https://site.test/scans/r3) · 1m 00s · failed in index: boom'
    assert [(c[0], c[1]) for c in w.slack.calls] == [('edit', '1.0'), ('post', '5.0'), ('post', '6.0'), ('edit', '4.0')]
    assert bodies(w.slack)[2:] == [('post', '6.0', failed), ('edit', '4.0', failed + ' · [thread](https://slack.test/p5.0)')]
    assert (w.saved['parent'], w.saved['posted'], 'running' in w.saved) == ('5.0', {'r3': '6.0'}, False)


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

    def exit_(*args, **kwargs):
        raise SystemExit(2)
    monkeypatch.setattr(N, 'run', exit_)
    said.clear()
    assert N.report_job(job) is None
    assert said == ['nominal report failed: SystemExit: 2']


@pytest.mark.parametrize('facts, ran', [({}, [None]), ({'parent': 'p0'}, []), ({'kind': 'reproc'}, [])])
def test_start_report_reads_the_live_ledger_for_top_level_runs_and_never_fails_the_job(monkeypatch, facts, ran):
    from dt_cloud import scan_runs

    said, calls = [], []
    monkeypatch.setattr(scan_runs, 'err', said.append)
    monkeypatch.setattr(N, 'run', lambda raw, env, project, run_id=None, execute=False: calls.append(run_id) or 'nominal: ok')
    job = SimpleNamespace(profile=SimpleNamespace(nominal=vars(CFG), project='p', store='primary'), recording=True,
                          dry_run=False, env={}, run_id='r1', state=lambda: facts, redact=lambda s: s)
    N.report_start(job)
    assert (calls, said) == (ran, ['nominal: ok'] if ran else [])

    for exc in (ValueError('lease busy'), SystemExit(2)):
        def fail(*args, exc=exc, **kwargs):
            raise exc
        monkeypatch.setattr(N, 'run', fail)
        said.clear()
        N.report_start(job)
        assert said == ([f'nominal start report failed: {type(exc).__name__}: {exc}'] if ran else [])


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


def bodies(slack):
    return [(c[0], c[1], c[-1]) for c in slack.calls]


def test_a_single_clean_run_reads_singular():
    good, boundary = N.streak([row(1), row(0, 'failed')])
    assert N.parent(CFG, good, boundary).split('\n\n')[0] == '✅ **Test scans: 1 consecutive clean run**'
    assert N.parent(CFG, good, boundary).split('\n\n')[3] == (
        'Latest 1 run: 1 scheduled / 0 manual. Each reply links its scan record. [All scans](https://site.test/scans).'
    )


def test_unknown_group_is_refused():
    from dataclasses import replace

    with pytest.raises(ValueError, match='nominal.group: one of clean, color, signature'):
        replace(CFG, group='phase')


def test_failed_reply_names_phase_and_error_without_markup():
    bad = row(4, 'failed', failed_phase='interval-store', error='OOM <!channel> `x` *y*\nexit 137')
    assert N.reply(CFG, bad) == (
        "❌ :alarm_clock: [2026-11-05 08:00 UTC](https://site.test/scans/r4) · 1m 00s · "
        "failed in interval-store: OOM ‹!channel› 'x' *y* exit 137"
    )
    long = row(4, 'failed', error='e' * 200)
    assert N.reply(CFG, long) == "❌ :alarm_clock: [2026-11-05 08:00 UTC](https://site.test/scans/r4) · 1m 00s · failed: " + 'e' * 159 + '…'
    partial = row(4, issues=[{'phase': 'probe'}, {'phase': 'healthcheck'}, {'phase': 'probe'}])
    assert N.reply(CFG, partial) == (
        '❌ :alarm_clock: [2026-11-05 08:00 UTC](https://site.test/scans/r4) · 1m 00s · phase issues: healthcheck, probe'
    )


def test_color_threads_consecutive_failures_and_each_change_of_color_closes_the_last():
    from dataclasses import replace

    w = Writer(replace(CFG, group='color'))
    w.run(history(2))
    w.slack.calls.clear()
    f3 = row(3, 'failed', failed_phase='index', error='boom')
    f4 = row(4, 'failed', failed_phase='probe')
    w.run([f4, f3, *history(2)])
    assert bodies(w.slack) == [
        ('edit', '1.0', '**Test scans: clean-run sequence ended after 2 runs**\n\n'
                        'Since 2026-11-02 08:00 UTC after [2026-11-01T0800](https://site.test/scans/r0); last [2026-11-03 08:00 UTC](https://site.test/scans/r2).\n\n'
                        'Ended by [2026-11-04 08:00 UTC](https://site.test/scans/r3): failed. [All scans](https://site.test/scans).'),
        ('post', '4.0', '❌ **Test scans: 2 consecutive failed runs**\n\n'
                        'Since 2026-11-04 08:00 UTC after [2026-11-03T0800](https://site.test/scans/r2) (clean).\n\n'
                        'Latest: [2026-11-05 08:00 UTC](https://site.test/scans/r4): failed in probe.\n\n'
                        'Each reply links its scan record. [All scans](https://site.test/scans).'),
        ('post', '5.0', '❌ :alarm_clock: [2026-11-04 08:00 UTC](https://site.test/scans/r3) · 1m 00s · failed in index: boom'),
        ('post', '6.0', '❌ :alarm_clock: [2026-11-05 08:00 UTC](https://site.test/scans/r4) · 1m 00s · failed in probe'),
    ]
    assert (w.saved['key'], w.saved['parent'], w.saved['posted']) == ('fail', '4.0', {'r3': '5.0', 'r4': '6.0'})
    w.slack.calls.clear()
    w.run([f4, f3, *history(2)])
    assert w.slack.calls == []
    w.run([row(5), f4, f3, *history(2)])
    assert bodies(w.slack) == [
        ('edit', '4.0', '**Test scans: failure sequence ended after 2 runs**\n\n'
                        'Since 2026-11-04 08:00 UTC after [2026-11-03T0800](https://site.test/scans/r2); last [2026-11-05 08:00 UTC](https://site.test/scans/r4).\n\n'
                        'Ended by [2026-11-06 08:00 UTC](https://site.test/scans/r5): clean. [All scans](https://site.test/scans).'),
        ('post', '7.0', '✅ **Test scans: 1 consecutive clean run**\n\n'
                        'Since 2026-11-06 08:00 UTC after [2026-11-05T0800](https://site.test/scans/r4).\n\n'
                        'Latest completed: [2026-11-06 08:00 UTC](https://site.test/scans/r5) · 1m 00s.\n\n'
                        'Latest 1 run: 1 scheduled / 0 manual. Each reply links its scan record. [All scans](https://site.test/scans).'),
        ('post', '8.0', '✅ :alarm_clock: [2026-11-06 08:00 UTC](https://site.test/scans/r5) · 1m 00s'),
    ]
    assert ('key' in w.saved, w.saved['posted']) == (False, {'r5': '8.0'})


def test_signature_threads_a_failure_while_it_repeats_and_a_different_one_opens_its_own():
    from dataclasses import replace

    w = Writer(replace(CFG, group='signature'))
    a3, a4 = row(3, 'failed', failed_phase='interval-store', error='OOM'), row(4, 'failed', failed_phase='interval-store', error='OOM again')
    w.run([a4, a3, *history(1)])
    assert [(c[0], c[1]) for c in w.slack.calls] == [('post', '1.0'), ('post', '2.0'), ('post', '3.0')]
    assert w.slack.calls[0][-1].split('\n\n')[:2] == [
        '❌ **Test scans: 2 consecutive failed runs in interval-store**',
        'Since 2026-11-04 08:00 UTC after [2026-11-02T0800](https://site.test/scans/r1) (clean).',
    ]
    assert (w.saved['key'], w.saved['boundary']) == ('fail:failed:interval-store', 'r1')
    w.slack.calls.clear()
    b5 = row(5, issues=[{'phase': 'probe'}])
    w.run([b5, a4, a3, *history(1)])
    assert bodies(w.slack) == [
        ('edit', '1.0', '**Test scans: failure sequence ended after 2 runs**\n\n'
                        'Since 2026-11-04 08:00 UTC after [2026-11-02T0800](https://site.test/scans/r1); last [2026-11-05 08:00 UTC](https://site.test/scans/r4).\n\n'
                        'Ended by [2026-11-06 08:00 UTC](https://site.test/scans/r5): phase issues: probe. [All scans](https://site.test/scans).'),
        ('post', '4.0', '❌ **Test scans: 1 consecutive failed run with phase issues**\n\n'
                        'Since 2026-11-06 08:00 UTC after [2026-11-05T0800](https://site.test/scans/r4) (failed in interval-store: OOM again).\n\n'
                        'Latest: [2026-11-06 08:00 UTC](https://site.test/scans/r5): phase issues: probe.\n\n'
                        'Each reply links its scan record. [All scans](https://site.test/scans).'),
        ('post', '5.0', '❌ :alarm_clock: [2026-11-06 08:00 UTC](https://site.test/scans/r5) · 1m 00s · phase issues: probe'),
    ]
    assert (w.saved['key'], w.saved['boundary'], w.saved['posted']) == ('fail:issues:probe', 'r4', {'r5': '5.0'})


def test_clean_grouping_never_threads_failures():
    w = Writer()
    w.run([row(2, 'failed'), row(1, 'failed'), *history(1)])
    assert w.slack.calls == []
    assert w.saved == {'namespace': CFG.namespace}


def deferring_job(tmp_path, group, monkeypatch, env=None):
    from dataclasses import asdict, replace

    from dt_cloud import scan_runs as S

    monkeypatch.setenv('TMPDIR', str(tmp_path))
    posted = []
    profile = SimpleNamespace(nominal=asdict(replace(CFG, group=group)), project='p', store='primary', alerts={})
    job = S.JobRun.__new__(S.JobRun)
    job.profile, job.recording, job.dry_run, job.env, job.run_id = profile, True, False, env or {}, 'r9'
    job.key, job.dir = 'k', tmp_path / 'dt-scan-run'
    job.redact = lambda s: s
    job.post_alert = lambda level, message: posted.append((level, message))
    return job, posted


@pytest.mark.parametrize('threaded, expected', [(True, []), (False, [('fail', 'run failed (exit 1) in index')])])
def test_a_failure_thread_takes_the_alert_and_an_unthreaded_failure_still_posts_it(tmp_path, monkeypatch, threaded, expected):
    job, posted = deferring_job(tmp_path, 'signature', monkeypatch)
    job.alert('fail', 'run failed (exit 1) in index')
    assert (posted, job.state()['deferred_alerts']) == ([], ['run failed (exit 1) in index'])
    def fake_run(*args, final, **kwargs):
        final.append({'key': 'fail:failed:index', 'posted': {'r9': '2.0'} if threaded else {}})
        return 'nominal: ok'
    monkeypatch.setattr(N, 'run', fake_run)
    N.report_job(job)
    assert posted == expected


def test_a_failing_report_posts_the_deferred_alert(tmp_path, monkeypatch):
    job, posted = deferring_job(tmp_path, 'color', monkeypatch)
    job.alert('fail', 'run failed (exit 1)')
    def broken(*args, **kwargs):
        raise ValueError('ledger HTTP 500')
    monkeypatch.setattr(N, 'run', broken)
    N.report_job(job)
    assert posted == [('fail', 'run failed (exit 1)')]


@pytest.mark.parametrize('group, env', [('clean', {}), ('color', {'DT_ALERTS': 'stderr'})])
def test_alerts_post_at_once_without_failure_grouping_or_with_reports_off(tmp_path, monkeypatch, group, env):
    job, posted = deferring_job(tmp_path, group, monkeypatch, env)
    job.alert('fail', 'run failed (exit 1)')
    job.alert('warn', 'probe slow')
    assert (posted, job.state().get('deferred_alerts')) == ([('fail', 'run failed (exit 1)'), ('warn', 'probe slow')], None)


def test_head_carries_the_latest_runs_outcome_and_each_configured_stores_size_and_week():
    cfg = N.config(dict(state='unused', site='https://site.test', channel='C123', title='Test scans',
                        stores={'Data': 'primary', 'Metadata': 'meta'}))
    assert cfg.stores == (('Data', 'primary'), ('Metadata', 'meta'))
    latest = {**row(2), 'secs': 2343, 'cost_usd': 0.1607, 'out_bytes': 439_552_060, 'delta_bytes': -818_040}
    week = [dict(date='2026-11-02T0800', bytes=288_000_000_000_000, objects=3_700_000),
            dict(date='2026-11-03T0800', bytes=289_890_807_590_998, objects=3_719_843),
            dict(date='2026-11-09T0800', bytes=289_500_000_000_000, objects=3_710_000),
            dict(date='2026-11-10T0900', bytes=288_966_905_203_791, objects=3_703_266)]
    stores = N.stores_text(cfg, {'primary': {'path': '', 'points': week}, 'meta': 503})
    assert N.parent(cfg, [latest, row(1)], row(0, 'failed'), stores=stores).split('\n\n') == [
        '✅ **Test scans: 2 consecutive clean runs**',
        'Since 2026-11-02 08:00 UTC after [2026-11-01T0800](https://site.test/scans/r0).',
        'Latest completed: [2026-11-03 08:00 UTC](https://site.test/scans/r2) · 39m 03s · $0.16 · output 440 M (−818 K).',
        '[Data](https://site.test/): 289 T · 3,703,266 objects · −924 G over 7d.',
        '[Metadata](https://site.test/meta): size unavailable (HTTP 503).',
        'Latest 2 runs: 2 scheduled / 0 manual. Each reply links its scan record. [All scans](https://site.test/scans).',
    ]
    assert [N.store_line(cfg, 'Meta', 'meta', s) for s in ({'points': []}, {'points': week[:1]})] == [
        '[Meta](https://site.test/meta): no scans yet.',
        '[Meta](https://site.test/meta): 288 T · 3,700,000 objects.',
    ]
    assert [N.si(v) for v in (0, 999, 46_844_611_976, 7_176_822_198, 288_966_905_203_791, 1.2e15)] == [
        '0 B', '999 B', '46.8 G', '7.18 G', '289 T', '1.20 P']
    assert N.outcome({**row(1), 'secs': None}) == ''


class Pins(Slack):
    """The fake Slack with the channel's pins and canvas."""

    def __init__(self):
        super().__init__()
        self.pinned, self.canvases = [], {}

    def join(self):
        self.calls.append(('join',))

    def pin(self, ts):
        self.calls.append(('pin', ts))
        if ts not in self.pinned:
            self.pinned.append(ts)

    def unpin(self, ts):
        self.calls.append(('unpin', ts))
        self.pinned = [p for p in self.pinned if p != ts]

    def canvas_create(self, body):
        canvas = f'F{len(self.canvases) + 1}'
        self.canvases[canvas] = body
        self.calls.append(('canvas_create', canvas))
        return canvas

    def canvas_edit(self, canvas, body):
        self.calls.append(('canvas_edit', canvas))
        if self.canvases.get(canvas) is None:
            return False
        self.canvases[canvas] = body
        return True


def test_the_open_heads_pin_moves_when_a_new_thread_starts_and_the_canvas_is_written_only_on_change():
    w = Writer()
    w.slack = Pins()
    w.run(history(2))
    N.pin_head(w.saved, w.slack, w.save)
    N.pin_head(w.saved, w.slack, w.save)
    assert (w.slack.pinned, w.saved['pinned']) == (['1.0'], '1.0')
    # a failure closes the thread and unpins its head at once; the recovery's head takes the pin
    w.run([row(3, 'failed'), *history(2)])
    N.pin_head(w.saved, w.slack, w.save)
    N.pin_head(w.saved, w.slack, w.save)
    assert (w.slack.pinned, w.saved['closed'], 'pinned' in w.saved) == ([], True, False)
    w.run([row(4), row(3, 'failed'), *history(2)])
    assert w.saved['parent'] == '4.0'
    N.pin_head(w.saved, w.slack, w.save)
    assert (w.slack.pinned, w.saved['pinned']) == (['4.0'], '4.0')

    w.slack.calls.clear()
    N.write_canvas(w.saved, w.slack, 'a\n', w.save)
    N.write_canvas(w.saved, w.slack, 'a\n', w.save)
    N.write_canvas(w.saved, w.slack, 'b\n', w.save)
    assert w.slack.calls == [('join',), ('canvas_create', 'F1'), ('join',), ('canvas_edit', 'F1')]
    w.run([row(6, 'failed'), row(5), row(4), row(3, 'failed'), *history(2)])
    w.run([row(7), row(6, 'failed'), row(5), row(4), row(3, 'failed'), *history(2)])
    assert {k: w.saved[k] for k in N.KEPT} == {'pinned': '4.0', 'canvas': 'F1', 'canvas_body': 'b\n'}
    # deleted by hand: the next write creates it again
    w.slack.canvases['F1'] = None
    w.slack.calls.clear()
    N.write_canvas(w.saved, w.slack, 'c\n', w.save)
    assert (w.slack.calls, w.saved['canvas'], w.slack.canvases['F2']) == (
        [('join',), ('canvas_edit', 'F1'), ('canvas_create', 'F2')], 'F2', 'c\n')


def test_canvas_names_the_status_stores_latest_run_and_its_longest_phases_and_recent_runs():
    cfg = N.config(dict(state='unused', site='https://site.test', channel='C123', title='Test scans', stores={'Meta': 'meta'}))
    latest = {**row(2, **ADA), 'cost_usd': 0.1607, 'out_bytes': 439_552_060, 'delta_bytes': -818_040,
              'spans': [dict(phase='list a', start=0, end=562), dict(phase='index', start=600, end=624),
                        dict(phase='static-names', start=700, end=1176), dict(phase='open', start=1200, end=None)]}
    rows = [row(3, 'running', finished_ts=None), latest, row(1, 'failed', failed_phase='index', error='boom')]
    text = N.canvas_text(cfg, rows, 'https://slack.test/p1.0', {'meta': 503}, N.series_ts('2026-11-03T0900'))
    assert text.split('\n\n') == [
        '# Test scans',
        '✅ Test scans: 1 consecutive clean run · [thread](https://slack.test/p1.0)',
        'Updated 2026-11-03 09:00 UTC.',
        '## Stores',
        '- [Meta](https://site.test/meta): size unavailable (HTTP 503).',
        '## Latest run',
        '✅ :ada: [2026-11-03 08:00 UTC](https://site.test/scans/r2) · 1m 00s · $0.16 · output 440 M (−818 K)',
        'Longest phases: list a 9m 22s · static-names 7m 56s · index 0m 24s',
        '## Recent runs',
        '- ✅ :ada: [2026-11-03 08:00 UTC](https://site.test/scans/r2) · 1m 00s · $0.16\n'
        '- ❌ :alarm_clock: [2026-11-02 08:00 UTC](https://site.test/scans/r1) · 1m 00s · failed in index: boom',
        '## Pages',
        '[All scans](https://site.test/scans) · [Health](https://site.test/health) · [Meta](https://site.test/meta)\n',
    ]


def test_canvas_status_is_the_open_streak_else_the_newest_failure():
    failed = row(3, 'failed', failed_phase='list', error='throttled')
    issues = row(3, issues=[dict(phase='probe', outcome='failed', recovery=None)])
    assert [N.status(CFG, rows) for rows in (history(2), [failed, *history(2)], [issues, *history(2)], [])] == [
        '✅ Test scans: 2 consecutive clean runs',
        '❌ Test scans: latest run failed in list: throttled',
        '❌ Test scans: latest run phase issues: probe',
        'Test scans: no finished runs yet',
    ]
