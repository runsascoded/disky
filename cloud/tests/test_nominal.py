"""Exact nominal-thread copy, authoritative outcomes, adoption and interrupted/concurrent writer behavior."""
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from dt_cloud import nominal as N
from dt_cloud.append_runner import Busy, LocalLeaseFile, coordinator_lease

T0 = int(datetime(2026, 11, 1, 8, tzinfo=timezone.utc).timestamp())
#: `clean` grouping with a reply per run, each post the app's: most tests below pin its copy; `color` (the default) is
#: tested with `replace(CFG, group='color')`, quiet clean threads (the default) with `QUIET`, and every default (status
#: as the sender too) with `SENT`.
CFG = N.Config(state='unused', site='https://site.test', channel='C123', title='Test scans', group='clean', quiet=False, senders=False)


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
        self.deleted = 0
        self.senders = {}

    def owns(self, message):
        return message.get('bot_id') == 'B1'

    def messages(self, parent):
        return [m for m in self.messages_by_id.values() if m['ts'] == parent or m['parent'] == parent]

    def find(self, operation, parent, since):
        return next((m['ts'] for m in self.messages_by_id.values() if m['operation'] == operation and m['parent'] == parent), None)

    def post(self, body, parent, operation, sender=None):
        ts = f'{len(self.messages_by_id) + self.deleted + 1}.0'
        self.messages_by_id[ts] = dict(ts=ts, parent=parent, operation=operation, text=body, bot_id='B1', sender=sender)
        self.senders[ts] = sender
        self.calls.append(('post', ts, parent, body))
        if self.timeout:
            self.timeout = False
            raise TimeoutError('response lost after Slack accepted the post')
        return ts

    def edit(self, ts, body):
        self.calls.append(('edit', ts, body))
        self.messages_by_id[ts]['text'] = body

    def delete(self, ts):
        """As Slack's: a message already gone is not an error."""
        self.calls.append(('delete', ts, None))
        if self.messages_by_id.pop(ts, None):
            self.deleted += 1

    alerts = {}

    def alert(self, link, since):
        return self.alerts.get(link)

    def top_alert(self, link, text, metadata=None):
        """A run's top-level alert in the channel, as its steps post it (`alerts` finds it by the run's link)."""
        ts = f'{len(self.messages_by_id) + self.deleted + 1}.0'
        self.messages_by_id[ts] = dict(ts=ts, parent=None, operation=None, text=text, bot_id='B1', link=link, metadata=metadata or {})
        return ts

    def alert_messages(self, link, since):
        """As Slack's: top-level messages linking `link`, but none of the threads' own (their `operation` metadata)."""
        return [{'ts': m['ts'], 'text': m['text'], 'metadata': m.get('metadata', {})}
                for m in self.messages_by_id.values() if m['parent'] is None and link in m['text'] and m['operation'] is None]

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
        'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0).\n\n'
        'Latest completed: [11/03 08:00Z](https://site.test/scans/r2) · 1m 00s.\n\n'
        'Latest 2 runs: 1 scheduled / 1 manual. Each reply links its scan record. [All scans](https://site.test/scans).'
    )
    assert [N.reply(CFG, r) for r in reversed(good)] == [
        '✅ :alarm_clock: [11/02 08:00Z](https://site.test/scans/r1) · 1m 00s',
        '✅ :bust_in_silhouette: [11/03 08:00Z](https://site.test/scans/r2) · 1m 00s',
    ]
    manual = row(5, trigger='manual')
    assert N.reply(CFG, {**manual, 'run': {**manual['run'], 'trigger_by': 'ada@example.org'}}) == (
        '✅ :bust_in_silhouette: [11/06 08:00Z](https://site.test/scans/r5) · 1m 00s · ada@example.org'
    )
    assert N.reply(CFG, {**manual, 'run': {**manual['run'], 'trigger_by': '<!channel>'}}) == (
        '✅ :bust_in_silhouette: [11/06 08:00Z](https://site.test/scans/r5) · 1m 00s'
    )
    assert N.reply(CFG, {**manual, 'run': {**manual['run'], 'trigger_by': 'ada@example.org', 'trigger_emoji': ':ada:'}}) == (
        '✅ :ada: [11/06 08:00Z](https://site.test/scans/r5) · 1m 00s'
    )
    assert N.reply(CFG, {**manual, 'run': {**manual['run'], 'trigger_by': 'ada@example.org', 'trigger_emoji': '<!channel>'}}) == (
        '✅ :bust_in_silhouette: [11/06 08:00Z](https://site.test/scans/r5) · 1m 00s · ada@example.org'
    )
    sched = row(6)
    assert N.reply(CFG, {**sched, 'run': {**sched['run'], 'trigger_by': 'ada@example.org'}}) == (
        '✅ :alarm_clock: [11/07 08:00Z](https://site.test/scans/r6) · 1m 00s'
    )
    assert N.reply(CFG, {**row(3, trigger=None), 'secs': None}) == (
        '✅ :grey_question: [11/04 08:00Z](https://site.test/scans/r3) · duration unknown'
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
            ('post', '4.0', '⏳ :alarm_clock: [11/04 08:00Z](https://site.test/scans/r3) · running'),
            ('edit', '1.0', RUNNING_PARENT),
        ]
        assert (w.saved['posted'], w.saved['running']) == ({'r1': '2.0', 'r2': '3.0'}, {'r3': {'ts': '4.0', 'parent': '1.0'}})


#: history(2)'s thread head while r3 runs
RUNNING_PARENT = (
    '✅ **Test scans: 2 consecutive clean runs**\n\n'
    'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0).\n\n'
    'Latest completed: [11/03 08:00Z](https://site.test/scans/r2) · 1m 00s.\n\n'
    'Running now: [11/04 08:00Z](https://site.test/scans/r3).\n\n'
    'Latest 2 runs: 2 scheduled / 0 manual. Each reply links its scan record. [All scans](https://site.test/scans).'
)
ADA = dict(trigger='manual', trigger_by='ada@example.org', trigger_emoji=':ada:')


def running_writer(cfg=CFG):
    """history(2)'s thread, with r3 (Ada's) running: its ⏳ reply 4.0, the head 1.0 naming it."""
    w = Writer(cfg)
    w.run(history(2))
    w.run([row(3, 'running', finished_ts=None, **ADA), *history(2)])
    assert w.slack.calls[-2:] == [
        ('post', '4.0', '1.0', '⏳ :ada: [11/04 08:00Z](https://site.test/scans/r3) · running'),
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
        ('edit', '4.0', '✅ :ada: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s'),
        ('edit', '1.0', '✅ **Test scans: 3 consecutive clean runs**\n\n'
                        'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0).\n\n'
                        'Latest completed: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s.\n\n'
                        'Latest 3 runs: 2 scheduled / 1 manual. Each reply links its scan record. [All scans](https://site.test/scans).'),
    ]
    assert (w.saved['posted'], 'running' in w.saved) == ({'r1': '2.0', 'r2': '3.0', 'r3': '4.0'}, False)


@pytest.mark.parametrize('alert, suffix', [('99.0', ' · [alert](https://slack.test/p99.0)'), (None, '')])
def test_a_clean_threads_running_reply_that_fails_closes_it_and_links_the_alert(alert, suffix):
    w = running_writer()
    w.slack.alerts = {'https://site.test/scans/r3': alert} if alert else {}
    w.run([row(3, 'failed', failed_phase='index', error='boom', **ADA), *history(2)])
    assert bodies(w.slack) == [
        ('edit', '1.0', '✅ **Test scans: 2 consecutive clean runs (ended)**\n\n'
                        'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0); last [11/03 08:00Z](https://site.test/scans/r2).\n\n'
                        'Ended by [11/04 08:00Z](https://site.test/scans/r3): failed. [All scans](https://site.test/scans).'),
        ('edit', '4.0', '❌ :ada: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s · failed in index: boom' + suffix),
    ]
    assert (w.saved['closed'], 'running' in w.saved) == (True, False)


def test_a_running_reply_whose_run_opens_a_failure_thread_links_it():
    from dataclasses import replace

    w = running_writer(replace(CFG, group='color'))
    w.run([row(3, 'failed', failed_phase='index', error='boom', **ADA), *history(2)])
    failed = '❌ :ada: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s · failed in index: boom'
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
        'Since 11/02 08:00Z (available run history).',
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
                          dry_run=False, env={}, run_id='r1', state=lambda: {}, redact=lambda s: s.replace('secret-123', '<SECRET>'), sink=lambda: None)
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
    monkeypatch.setattr(N, 'run', lambda raw, env, project, run_id=None, execute=False, sink=None: calls.append(run_id) or 'nominal: ok')
    job = SimpleNamespace(profile=SimpleNamespace(nominal=vars(CFG), project='p', store='primary'), recording=True,
                          dry_run=False, env={}, run_id='r1', state=lambda: facts, redact=lambda s: s, sink=lambda: None)
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
    sink = SimpleNamespace(statements=[])
    sink.execute = sink.statements.extend
    assert N.run(asdict(cfg), env, 'project', 'r2', execute=True, sink=sink) == 'nominal: parent 1.0; 2 run replies'
    assert sink.statements == [
        "UPDATE scan_runs SET thread_url = 'https://slack.test/p2.0' WHERE run_id = 'r1'",
        "UPDATE scan_runs SET thread_url = 'https://slack.test/p3.0' WHERE run_id = 'r2'",
    ]
    slack.calls.clear()
    assert N.run(asdict(cfg), env, 'project', 'r2', execute=True, sink=sink) == 'nominal: parent 1.0; 2 run replies'
    assert slack.calls == []
    assert len(sink.statements) == 2
    with pytest.raises(ValueError, match='terminal run record is not visible'):
        N.run(asdict(cfg), env, 'project', 'not-recorded', execute=True)
    assert slack.calls == []


def test_each_runs_reply_link_is_written_once_retried_after_a_failed_write_and_moves_with_its_reply():
    w = Writer()
    w.run(history(2))
    state, writes = deepcopy(w.saved), []
    N.link_threads(state, w.slack, writes.append, w.save)
    assert writes == [[('r1', 'https://slack.test/p2.0'), ('r2', 'https://slack.test/p3.0')]]
    N.link_threads(state, w.slack, writes.append, w.save)
    assert len(writes) == 1
    w.run(history(3))
    state = deepcopy(w.saved)

    def refused(links):
        raise RuntimeError('no such column: thread_url')
    with pytest.raises(RuntimeError, match='no such column'):
        N.link_threads(state, w.slack, refused, w.save)
    assert w.saved['linked'] == {'r1': '2.0', 'r2': '3.0'}
    N.link_threads(state, w.slack, writes.append, w.save)
    assert writes[1:] == [[('r3', 'https://slack.test/p4.0')]]
    assert w.saved['linked'] == {'r1': '2.0', 'r2': '3.0', 'r3': '4.0'}


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
        "❌ :alarm_clock: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s · "
        "failed in interval-store: OOM ‹!channel› 'x' *y* exit 137"
    )
    long = row(4, 'failed', error='e' * 200)
    assert N.reply(CFG, long) == "❌ :alarm_clock: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s · failed: " + 'e' * 159 + '…'
    partial = row(4, issues=[{'phase': 'probe'}, {'phase': 'healthcheck'}, {'phase': 'probe'}])
    assert N.reply(CFG, partial) == (
        '⚠️ :alarm_clock: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s · phase issues: healthcheck, probe'
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
        ('edit', '1.0', '✅ **Test scans: 2 consecutive clean runs (ended)**\n\n'
                        'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0); last [11/03 08:00Z](https://site.test/scans/r2).\n\n'
                        'Ended by [11/04 08:00Z](https://site.test/scans/r3): failed. [All scans](https://site.test/scans).'),
        ('post', '4.0', '❌ **Test scans: 2 consecutive failed runs**\n\n'
                        'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2) (clean).\n\n'
                        'Latest: [11/05 08:00Z](https://site.test/scans/r4): failed in probe.\n\n'
                        'Each reply links its scan record. [All scans](https://site.test/scans).'),
        ('post', '5.0', '❌ :alarm_clock: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s · failed in index: boom'),
        ('post', '6.0', '❌ :alarm_clock: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s · failed in probe'),
    ]
    assert (w.saved['key'], w.saved['parent'], w.saved['posted']) == ('fail', '4.0', {'r3': '5.0', 'r4': '6.0'})
    w.slack.calls.clear()
    w.run([f4, f3, *history(2)])
    assert w.slack.calls == []
    w.run([row(5), f4, f3, *history(2)])
    assert bodies(w.slack) == [
        ('edit', '4.0', '❌ **Test scans: 2 consecutive failed runs (ended)**\n\n'
                        'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2); last [11/05 08:00Z](https://site.test/scans/r4).\n\n'
                        'Ended by [11/06 08:00Z](https://site.test/scans/r5): clean. [All scans](https://site.test/scans).'),
        ('post', '7.0', '✅ **Test scans: 1 consecutive clean run**\n\n'
                        'Since 11/06 08:00Z after [2026-11-05T0800](https://site.test/scans/r4).\n\n'
                        'Latest completed: [11/06 08:00Z](https://site.test/scans/r5) · 1m 00s.\n\n'
                        'Latest 1 run: 1 scheduled / 0 manual. Each reply links its scan record. [All scans](https://site.test/scans).'),
        ('post', '8.0', '✅ :alarm_clock: [11/06 08:00Z](https://site.test/scans/r5) · 1m 00s'),
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
        'Since 11/04 08:00Z after [2026-11-02T0800](https://site.test/scans/r1) (clean).',
    ]
    assert (w.saved['key'], w.saved['boundary']) == ('fail:failed:interval-store', 'r1')
    w.slack.calls.clear()
    b5 = row(5, issues=[{'phase': 'probe'}])
    w.run([b5, a4, a3, *history(1)])
    assert bodies(w.slack) == [
        ('edit', '1.0', '❌ **Test scans: 2 consecutive failed runs (ended)**\n\n'
                        'Since 11/04 08:00Z after [2026-11-02T0800](https://site.test/scans/r1); last [11/05 08:00Z](https://site.test/scans/r4).\n\n'
                        'Ended by [11/06 08:00Z](https://site.test/scans/r5): phase issues: probe. [All scans](https://site.test/scans).'),
        ('post', '4.0', '⚠️ **Test scans: 1 consecutive run with phase issues**\n\n'
                        'Since 11/06 08:00Z after [2026-11-05T0800](https://site.test/scans/r4) (failed in interval-store: OOM again).\n\n'
                        'Latest: [11/06 08:00Z](https://site.test/scans/r5): phase issues: probe.\n\n'
                        'Each reply links its scan record. [All scans](https://site.test/scans).'),
        ('post', '5.0', '⚠️ :alarm_clock: [11/06 08:00Z](https://site.test/scans/r5) · 1m 00s · phase issues: probe'),
    ]
    assert (w.saved['key'], w.saved['boundary'], w.saved['posted']) == ('fail:issues:probe', 'r4', {'r5': '5.0'})


def test_color_heads_name_runs_with_phase_issues_apart_from_failures():
    """cw 2026-11-21: an overlapping manual scan's static-names skipped (busy) and the scan it overlapped probed search
    behind. Both succeeded; the thread says ⚠️ runs with phase issues, and ❌ once a failure joins them."""
    from dataclasses import replace

    w = Writer(replace(CFG, group='color'))
    w.run(history(1))
    w.slack.calls.clear()
    i2 = row(2, issues=[{'phase': 'probe'}])
    i3 = row(3, issues=[{'phase': 'static-names'}])
    w.run([i3, i2, *history(1)])
    assert bodies(w.slack)[1] == (
        'post', '3.0', '⚠️ **Test scans: 2 consecutive runs with phase issues**\n\n'
                       'Since 11/03 08:00Z after [2026-11-02T0800](https://site.test/scans/r1) (clean).\n\n'
                       'Latest: [11/04 08:00Z](https://site.test/scans/r3): phase issues: static-names.\n\n'
                       'Each reply links its scan record. [All scans](https://site.test/scans).')
    w.slack.calls.clear()
    f4 = row(4, 'failed', failed_phase='index')
    w.run([f4, i3, i2, *history(1)])
    assert bodies(w.slack) == [
        ('post', '6.0', '❌ :alarm_clock: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s · failed in index'),
        ('edit', '3.0', '❌ **Test scans: 3 consecutive runs failed or with phase issues**\n\n'
                        'Since 11/03 08:00Z after [2026-11-02T0800](https://site.test/scans/r1) (clean).\n\n'
                        'Latest: [11/05 08:00Z](https://site.test/scans/r4): failed in index.\n\n'
                        'Each reply links its scan record. [All scans](https://site.test/scans).'),
    ]


def test_outcome_emoji_are_configurable():
    cfg = N.config(dict(state='unused', site='https://site.test', channel='C123', title='Test scans', group='clean',
                        emoji={'issues': ':large_yellow_circle:', 'failed': '🔴'}))
    assert [N.reply(cfg, r).split(' ', 1)[0] for r in (row(1), row(2, issues=[{'phase': 'probe'}]), row(3, 'failed'))] == [
        '✅', ':large_yellow_circle:', '🔴']
    assert N.running(cfg, row(4, 'running', finished_ts=None)).split(' ', 1)[0] == '⏳'
    with pytest.raises(ValueError, match='nominal.emoji: keys among clean, issues, failed, running, cancelled'):
        N.config(dict(state='unused', site='https://site.test', channel='C123', title='Test scans', emoji={'warn': '⚠️'}))
    with pytest.raises(ValueError, match='nominal.emoji: a Slack :name: code or emoji characters'):
        N.config(dict(state='unused', site='https://site.test', channel='C123', title='Test scans', emoji={'failed': '<!channel>'}))


def test_a_cancelled_run_is_no_outcome_its_running_reply_reads_cancelled_and_the_streak_holds():
    # a run stopped on purpose (🚫) neither ends the clean thread nor counts in it; its ⏳ reply is edited where it stands
    w = running_writer()
    cancelled = row(3, 'cancelled', error='cancelled from /scans', **ADA)
    w.run([cancelled, *history(2)])
    assert bodies(w.slack) == [
        ('edit', '4.0', '🚫 :ada: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s · cancelled'),
        ('edit', '1.0', '✅ **Test scans: 2 consecutive clean runs**\n\n'
                        'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0).\n\n'
                        'Latest completed: [11/03 08:00Z](https://site.test/scans/r2) · 1m 00s.\n\n'
                        'Latest 2 runs: 2 scheduled / 0 manual. Each reply links its scan record. [All scans](https://site.test/scans).'),
    ]
    assert (w.saved['posted'], 'running' in w.saved, 'closed' in w.saved) == ({'r1': '2.0', 'r2': '3.0'}, False, False)
    w.slack.calls.clear()
    w.run([row(4), cancelled, *history(2)])
    assert bodies(w.slack) == [
        ('post', '5.0', '✅ :alarm_clock: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s'),
        ('edit', '1.0', '✅ **Test scans: 3 consecutive clean runs**\n\n'
                        'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0).\n\n'
                        'Latest completed: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s.\n\n'
                        'Latest 3 runs: 3 scheduled / 0 manual. Each reply links its scan record. [All scans](https://site.test/scans).'),
    ]


def test_a_cancelled_runs_grade_emoji_and_the_canvas_status_skipping_it():
    cfg = N.config(dict(state='unused', site='https://site.test', channel='C123', title='Test scans', group='clean',
                        emoji={'cancelled': ':no_entry_sign:'}))
    cancelled = row(3, 'cancelled', error='cancelled')
    assert (N.grade(cancelled), N.describe(cancelled), N.reply(cfg, cancelled).split(' ', 1)[0]) == ('cancelled', 'cancelled', ':no_entry_sign:')
    assert N.status(CFG, [cancelled, *history(2)]) == '✅ Test scans: 2 consecutive clean runs'


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
        'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0).',
        'Latest completed: [11/03 08:00Z](https://site.test/scans/r2) · 39m 03s · $0.16 · output 440 M (−818 K).',
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


def test_a_date_only_scan_id_in_a_stores_series_reads_as_its_midnight():
    """A deployment's first scans may be ids of a bare date (hf's `2026-10-15`): a series point is any scan id."""
    cfg = N.config(dict(state='unused', site='https://site.test', channel='C123', title='Test scans'))
    points = [dict(date='2026-10-15', bytes=1_000_000, objects=10),
              dict(date='2026-10-22T0800', bytes=1_500_000, objects=12)]
    assert N.store_line(cfg, 'Data', 'primary', {'points': points}) == '[Data](https://site.test/): 1.50 M · 12 objects · +500 K over 7d.'


class Pins(Slack):
    """The fake Slack with the channel's pins and canvas."""

    def __init__(self):
        super().__init__()
        self.pinned, self.canvases, self.titles = [], {}, {}

    def join(self):
        self.calls.append(('join',))

    def pin(self, ts):
        self.calls.append(('pin', ts))
        if ts not in self.pinned:
            self.pinned.append(ts)

    def unpin(self, ts):
        self.calls.append(('unpin', ts))
        self.pinned = [p for p in self.pinned if p != ts]

    def canvas_create(self, body, title):
        canvas = f'F{len(self.canvases) + 1}'
        self.canvases[canvas] = body
        self.titles[canvas] = title
        self.calls.append(('canvas_create', canvas))
        return canvas

    def canvas_edit(self, canvas, body):
        self.calls.append(('canvas_edit', canvas))
        if self.canvases.get(canvas) is None:
            return False
        self.canvases[canvas] = body
        return True

    def canvas_rename(self, canvas, title):
        self.calls.append(('canvas_rename', canvas))
        if self.canvases.get(canvas) is None:
            return False
        self.titles[canvas] = title
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
    N.write_canvas(w.saved, w.slack, 'a\n', 'T', w.save)
    N.write_canvas(w.saved, w.slack, 'a\n', 'T', w.save)
    N.write_canvas(w.saved, w.slack, 'b\n', 'T', w.save)
    assert (w.slack.calls, w.slack.titles) == ([('join',), ('canvas_create', 'F1'), ('join',), ('canvas_edit', 'F1')], {'F1': 'T'})
    w.run([row(6, 'failed'), row(5), row(4), row(3, 'failed'), *history(2)])
    w.run([row(7), row(6, 'failed'), row(5), row(4), row(3, 'failed'), *history(2)])
    assert {k: w.saved[k] for k in N.KEPT if k in w.saved} == {'pinned': '4.0', 'canvas': 'F1', 'canvas_body': 'b\n', 'canvas_title': 'T'}
    # a canvas made before titles (state without one) is renamed once, its body unchanged
    del w.saved['canvas_title']
    w.slack.calls.clear()
    N.write_canvas(w.saved, w.slack, 'b\n', 'T2', w.save)
    N.write_canvas(w.saved, w.slack, 'b\n', 'T2', w.save)
    assert (w.slack.calls, w.slack.titles, w.saved['canvas_title']) == (
        [('join',), ('canvas_edit', 'F1'), ('canvas_rename', 'F1')], {'F1': 'T2'}, 'T2')
    # deleted by hand: the next write creates it again
    w.slack.canvases['F1'] = None
    w.slack.calls.clear()
    N.write_canvas(w.saved, w.slack, 'c\n', 'T2', w.save)
    assert (w.slack.calls, w.saved['canvas'], w.slack.canvases['F2'], w.slack.titles['F2']) == (
        [('join',), ('canvas_edit', 'F1'), ('canvas_create', 'F2')], 'F2', 'c\n', 'T2')


def test_canvas_names_the_status_stores_latest_run_and_its_longest_phases_and_recent_runs():
    cfg = N.config(dict(state='unused', site='https://site.test', channel='C123', title='Test scans', stores={'Meta': 'meta'}))
    latest = {**row(2, **ADA), 'cost_usd': 0.1607, 'out_bytes': 439_552_060, 'delta_bytes': -818_040,
              'spans': [dict(phase='list a', start=0, end=562), dict(phase='index', start=600, end=624),
                        dict(phase='static-names', start=700, end=1176), dict(phase='open', start=1200, end=None)]}
    rows = [row(3, 'running', finished_ts=None), latest, row(1, 'failed', failed_phase='index', error='boom')]
    text = N.canvas_text(cfg, rows, 'https://slack.test/p1.0', {'meta': 503}, N.series_ts('2026-11-03T0900'))
    assert text.split('\n\n') == [
        '✅ Test scans: 1 consecutive clean run · [thread](https://slack.test/p1.0)',
        'Updated 2026-11-03 09:00 UTC.',
        '## Stores',
        '- [Meta](https://site.test/meta): size unavailable (HTTP 503).',
        '## Latest run',
        '✅ :ada: [11/03 08:00Z](https://site.test/scans/r2) · 1m 00s · $0.16 · output 440 M (−818 K)',
        'Longest phases: list a 9m 22s · static-names 7m 56s · index 0m 24s',
        '## Recent runs',
        '- ✅ :ada: [11/03 08:00Z](https://site.test/scans/r2) · 1m 00s · $0.16\n'
        '- ❌ :alarm_clock: [11/02 08:00Z](https://site.test/scans/r1) · 1m 00s · failed in index: boom',
        '## Pages',
        '[All scans](https://site.test/scans) · [Health](https://site.test/health) · [Meta](https://site.test/meta)\n',
    ]


def test_canvas_status_is_the_open_streak_else_the_newest_failure():
    failed = row(3, 'failed', failed_phase='list', error='throttled')
    issues = row(3, issues=[dict(phase='probe', outcome='failed', recovery=None)])
    assert [N.status(CFG, rows) for rows in (history(2), [failed, *history(2)], [issues, *history(2)], [])] == [
        '✅ Test scans: 2 consecutive clean runs',
        '❌ Test scans: latest run failed in list: throttled',
        '⚠️ Test scans: latest run phase issues: probe',
        'Test scans: no finished runs yet',
    ]


def test_color_is_the_default_grouping():
    assert N.Config(state='unused', site='https://site.test', channel='C123', title='Test scans').group == 'color'


def test_a_failure_threads_reply_folds_its_runs_top_level_alerts_and_leaves_each_a_pointer():
    """r2 2026-11-18: a run's static-names and probe warnings posted top-level (the second before alerts grouped). In a
    failure thread the run's reply carries their lines, and each alert becomes a one-line pointer to that reply."""
    from dataclasses import replace

    w = Writer(replace(CFG, group='color'))
    w.run(history(2))
    link = 'https://site.test/scans/r3'
    grouped = w.slack.top_alert(link, f'*⚠️ Test scans 2026-11-04T0800*\nstatic-names failed (exit 1) · <https://logs.test/u1|task logs>\n<{link}|scan>', {
        'event_type': 'disky_alert', 'event_payload': {'group': 'r3', 'title': '⚠️ Test scans 2026-11-04T0800', 'priority': 0, 'links': [], 'lines': [
            {'message': 'static-names failed (exit 1)', 'url': 'https://logs.test/u1', 'url_title': 'task logs'}]}})
    plain = w.slack.top_alert(link, f'*⚠️ Test scans 2026-11-04T0800*\nprobe failed (exit 1): 4 failed\n<https://logs.test/u2|task logs> · <{link}|scan>')
    w.slack.calls.clear()
    r3 = row(3, issues=[{'phase': 'static-names'}, {'phase': 'probe'}])
    w.run([r3, *history(2)])
    assert bodies(w.slack)[1:] == [
        ('post', '6.0', '⚠️ **Test scans: 1 consecutive run with phase issues**\n\n'
                        'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2) (clean).\n\n'
                        'Latest: [11/04 08:00Z](https://site.test/scans/r3): phase issues: probe, static-names.\n\n'
                        'Each reply links its scan record. [All scans](https://site.test/scans).'),
        ('post', '7.0', '⚠️ :alarm_clock: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s · phase issues: probe, static-names\n'
                        'static-names failed (exit 1) · [task logs](https://logs.test/u1)\n'
                        'probe failed (exit 1): 4 failed · [task logs](https://logs.test/u2)'),
        ('edit', grouped, '↪ ⚠️ Test scans 2026-11-04T0800: in the [failure thread](https://slack.test/p7.0)'),
        ('edit', plain, '↪ ⚠️ Test scans 2026-11-04T0800: in the [failure thread](https://slack.test/p7.0)'),
    ]
    w.slack.calls.clear()
    w.run([r3, *history(2)])
    assert w.slack.calls == []


def test_alert_messages_never_returns_a_threads_own_head():
    """r2 2026-11-18: a failure thread's head links its newest run, so the run's fold took the head for one of its alerts
    and edited it into a pointer. A thread's messages carry `disky_nominal` metadata; alerts don't."""
    slack = object.__new__(N.Slack)
    link = 'https://site.test/scans/r3'
    history = [
        {'ts': '3.0', 'bot_id': 'B1', 'text': f'❌ *Test scans: 1 consecutive failed run* … <{link}|…>',
         'metadata': {'event_type': 'disky_nominal', 'event_payload': {'operation': 'op'}}},
        {'ts': '2.0', 'bot_id': 'B1', 'text': f'*⚠️ scan* probe failed <{link}|scan>', 'metadata': {'event_type': 'disky_alert', 'event_payload': {}}},
        {'ts': '1.0', 'bot_id': 'B1', 'text': f'*⚠️ scan* static-names failed <{link}|scan>'},
        {'ts': '0.5', 'bot_id': 'B1', 'text': 'another run <https://site.test/scans/r2|scan>'},
        {'ts': '0.4', 'user': 'U9', 'text': f'a person quoting <{link}|it>'},
    ]
    slack.client = SimpleNamespace(channel='C123', bot_ids=(None, 'B1'), _request=lambda api, args, **kw: {'messages': history})
    assert [m['ts'] for m in slack.alert_messages(link, 0)] == ['1.0', '2.0']
    assert slack.alert(link, 0) == '1.0'


QUIET = replace(CFG, group='color', quiet=True)
#: history(2)'s quiet thread head (1.0) and its rolling reply (2.0)
QUIET_HEAD = (
    '✅ **Test scans: 2 consecutive clean runs**\n\n'
    'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0).\n\n'
    'Latest completed: [11/03 08:00Z](https://site.test/scans/r2) · 1m 00s.\n\n'
    'Latest 2 runs: 2 scheduled / 0 manual. One reply lists the runs, each linking its scan record. [All scans](https://site.test/scans).'
)
TRAIL = ('✅ :alarm_clock: [11/02 08:00Z](https://site.test/scans/r1) · 1m 00s\n'
         '✅ :alarm_clock: [11/03 08:00Z](https://site.test/scans/r2) · 1m 00s')
R3_RUNNING = '⏳ :ada: [11/04 08:00Z](https://site.test/scans/r3) · running'


def quiet_running_writer():
    """history(2)'s quiet thread, then r3 (Ada's) starting: the head and the rolling reply edited, nothing posted."""
    w = Writer(QUIET)
    w.run(history(2))
    assert bodies(w.slack) == [('post', '1.0', QUIET_HEAD), ('post', '2.0', TRAIL)]
    assert (w.saved['parent'], w.saved['rolling'], w.saved['posted']) == ('1.0', '2.0', {'r1': '2.0', 'r2': '2.0'})
    w.slack.calls.clear()
    w.run([row(3, 'running', finished_ts=None, **ADA), *history(2)])
    assert bodies(w.slack) == [
        ('edit', '2.0', f'{TRAIL}\n{R3_RUNNING}'),
        ('edit', '1.0', QUIET_HEAD.replace('Latest 2 runs', 'Running now: [11/04 08:00Z](https://site.test/scans/r3).\n\nLatest 2 runs')),
    ]
    assert ('running' in w.saved) is False
    w.slack.calls.clear()
    return w


def test_quiet_is_the_default_and_must_be_a_boolean():
    assert N.Config(state='unused', site='https://site.test', channel='C123', title='Test scans').quiet is True
    with pytest.raises(ValueError, match='nominal.quiet: true or false'):
        N.config(dict(state='unused', site='https://site.test', channel='C123', title='Test scans', quiet='yes'))


def test_a_quiet_clean_thread_posts_its_head_and_one_rolling_reply_then_only_edits_them():
    """Ryan, 2026-11-21: a clean run is no news. Its thread posts once (the head, and the rolling reply under it); each
    later run, starting and ending, edits both, which notifies nobody."""
    w = quiet_running_writer()
    w.run([row(3, **ADA), *history(2)])
    assert bodies(w.slack) == [
        ('edit', '2.0', f'{TRAIL}\n✅ :ada: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s'),
        ('edit', '1.0', '✅ **Test scans: 3 consecutive clean runs**\n\n'
                        'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0).\n\n'
                        'Latest completed: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s.\n\n'
                        'Latest 3 runs: 2 scheduled / 1 manual. One reply lists the runs, each linking its scan record. [All scans](https://site.test/scans).'),
    ]
    assert w.saved['posted'] == {'r1': '2.0', 'r2': '2.0', 'r3': '2.0'}
    w.slack.calls.clear()
    w.run([row(3, **ADA), *history(2)])
    assert w.slack.calls == []


def test_a_quiet_threads_rolling_reply_keeps_the_newest_ten_runs():
    w = Writer(QUIET)
    w.run(history(12))
    assert [(c[0], c[1]) for c in w.slack.calls] == [('post', '1.0'), ('post', '2.0')]
    assert w.slack.calls[1][-1] == '\n'.join(N.reply(QUIET, row(i)) for i in range(3, 13))
    assert list(w.saved['posted']) == [f'r{i}' for i in range(3, 13)]


def test_a_failure_in_a_quiet_thread_posts_and_closes_it_and_a_recovery_opens_a_quiet_one():
    w = quiet_running_writer()
    f3 = row(3, 'failed', failed_phase='index', error='boom', **ADA)
    w.run([f3, *history(2)])
    failed = '❌ :ada: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s · failed in index: boom'
    assert bodies(w.slack) == [
        ('edit', '1.0', '✅ **Test scans: 2 consecutive clean runs (ended)**\n\n'
                        'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0); last [11/03 08:00Z](https://site.test/scans/r2).\n\n'
                        'Ended by [11/04 08:00Z](https://site.test/scans/r3): failed. [All scans](https://site.test/scans).'),
        ('edit', '2.0', f'{TRAIL}\n{failed}'),
        ('post', '3.0', '❌ **Test scans: 1 consecutive failed run**\n\n'
                        'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2) (clean).\n\n'
                        'Latest: [11/04 08:00Z](https://site.test/scans/r3): failed in index: boom.\n\n'
                        'Each reply links its scan record. [All scans](https://site.test/scans).'),
        ('post', '4.0', failed),
    ]
    assert (w.saved['key'], w.saved['parent'], w.saved['posted'], 'rolling' in w.saved) == ('fail', '3.0', {'r3': '4.0'}, False)
    w.slack.calls.clear()
    # a failure thread isn't quiet: a run starting in it posts its ⏳ reply, as ever
    w.run([row(4, 'running', finished_ts=None), f3, *history(2)])
    assert [(c[0], c[1]) for c in w.slack.calls] == [('post', '5.0'), ('edit', '3.0')]
    w.slack.calls.clear()
    w.run([row(4), f3, *history(2)])
    assert bodies(w.slack) == [
        ('edit', '3.0', '❌ **Test scans: 1 consecutive failed run (ended)**\n\n'
                        'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2); last [11/04 08:00Z](https://site.test/scans/r3).\n\n'
                        'Ended by [11/05 08:00Z](https://site.test/scans/r4): clean. [All scans](https://site.test/scans).'),
        ('post', '6.0', '✅ **Test scans: 1 consecutive clean run**\n\n'
                        'Since 11/05 08:00Z after [2026-11-04T0800](https://site.test/scans/r3).\n\n'
                        'Latest completed: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s.\n\n'
                        'Latest 1 run: 1 scheduled / 0 manual. One reply lists the runs, each linking its scan record. [All scans](https://site.test/scans).'),
        ('post', '7.0', '✅ :alarm_clock: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s'),
        ('edit', '5.0', '✅ :alarm_clock: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s · [thread](https://slack.test/p6.0)'),
    ]
    assert (w.saved['parent'], w.saved['rolling'], w.saved['posted'], 'running' in w.saved) == ('6.0', '7.0', {'r4': '7.0'}, False)


def test_a_cancelled_run_in_a_quiet_thread_posts_nothing():
    w = quiet_running_writer()
    w.run([row(3, 'cancelled', error='cancelled from /scans', **ADA), *history(2)])
    assert bodies(w.slack) == [('edit', '2.0', TRAIL), ('edit', '1.0', QUIET_HEAD)]


def test_a_quiet_threads_runs_each_link_its_rolling_reply():
    w = Writer(QUIET)
    w.run(history(2))
    state, writes = deepcopy(w.saved), []
    N.link_threads(state, w.slack, writes.append, w.save)
    assert writes == [[('r1', 'https://slack.test/p2.0'), ('r2', 'https://slack.test/p2.0')]]


def test_a_quiet_preview_renders_the_head_and_the_rolling_reply(tmp_path, monkeypatch):
    import json

    from dt_cloud import probe

    cfg = replace(QUIET, state=str(tmp_path / 'state'))
    monkeypatch.setattr(probe, 'http_fetch', lambda *args: lambda path: SimpleNamespace(status=200, body=json.dumps(dict(configured=True, runs=history(2)))))

    def transport(*args):
        pytest.fail('a preview must not reach Slack')
    monkeypatch.setattr(N, 'Slack', transport)
    assert N.run(asdict(cfg), {'SITE_TOKEN': 'viewer'}, 'project') == f'{QUIET_HEAD}\n\n+++\n\n{TRAIL}'
    assert sorted(tmp_path.iterdir()) == []


#: Every default: `color` grouping, quiet clean threads, and status as the sender (specs/done/scan-thread-senders.md)
SENT = N.Config(state='unused', site='https://site.test', channel='C123', title='Test scans')
GREEN = {'username': 'Test scans · clean', 'icon_emoji': ':white_check_mark:'}
RUNS = {'username': 'Test scans · runs', 'icon_emoji': ':receipt:'}
FAILING = {'username': 'Test scans · failing', 'icon_emoji': ':x:'}
#: history(2)'s quiet thread head under its sender: no leading ✅ and title
SENT_HEAD = QUIET_HEAD.replace('✅ **Test scans: ', '**')
F3 = row(3, 'failed', failed_phase='index', error='boom', **ADA)
#: The failure thread F3 opens after history(2)'s quiet thread (its head 3.0)
RED_HEAD = ('**1 consecutive failed run**\n\n'
            'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2) (clean).\n\n'
            'Latest: [11/04 08:00Z](https://site.test/scans/r3): failed in index: boom.\n\n'
            'Each reply links its scan record. [All scans](https://site.test/scans).')


def sent(slack):
    """Each call since the last, with a post's sender; then forgets them."""
    out = [(c[0], c[1], slack.senders.get(c[1]) if c[0] == 'post' else None, c[-1]) for c in slack.calls]
    slack.calls.clear()
    return out


def test_status_as_the_sender_is_the_default_and_its_names_and_icons_are_fixed_per_kind():
    assert SENT.senders is True
    assert [SENT.sender(k) for k in ('clean', 'issues', 'failed', 'runs')] == [
        GREEN, {'username': 'Test scans · phase issues', 'icon_emoji': ':warning:'}, FAILING, RUNS,
    ]
    assert CFG.sender('clean') is None
    cfg = N.config(dict(state='unused', site='https://site.test', channel='C123', title='Test scans',
                        emoji={'clean': '🟢', 'failed': ':rotating_light:', 'issues': '🦆'}, icons={'runs': 'https://icons.test/runs.png'}))
    # an override that is a code, or a character with one, is the icon; any other character keeps the stock icon
    assert [cfg.sender(k) for k in ('clean', 'issues', 'failed', 'runs')] == [
        {'username': 'Test scans · clean', 'icon_emoji': ':large_green_circle:'},
        {'username': 'Test scans · phase issues', 'icon_emoji': ':warning:'},
        {'username': 'Test scans · failing', 'icon_emoji': ':rotating_light:'},
        {'username': 'Test scans · runs', 'icon_url': 'https://icons.test/runs.png'},
    ]
    long = replace(SENT, title='T' * 90).sender('failed')
    assert (long, len(long['username'])) == ({'username': 'T' * 69 + '… · failing', 'icon_emoji': ':x:'}, 80)


@pytest.mark.parametrize('raw,message', [
    ({'senders': 'yes'}, 'nominal.senders: true or false'),
    ({'icons': {'nope': 'https://icons.test/a.png'}}, 'nominal.icons: keys among clean, issues, failed, running, cancelled, runs'),
    ({'icons': {'runs': 'http://icons.test/a.png'}}, 'nominal.icons: public HTTPS image URLs'),
    ({'badges': 'https://icons.test/{nope}.png'}, r'nominal.badges: an HTTPS URL template over \{outcome\} and \{trigger\}'),
    ({'badges': 'https://icons.test/a.png'}, r'nominal.badges: an HTTPS URL template over \{outcome\} and \{trigger\}'),
])
def test_sender_options_are_checked(raw, message):
    with pytest.raises(ValueError, match=message):
        N.config(dict(state='unused', site='https://site.test', channel='C123', title='Test scans', **raw))


def test_a_run_replys_sender_is_its_line_and_its_text_the_record_link():
    """Ryan, 2026-11-22: a run reply's first line is its sender, `<stamp> · <duration> · <outcome>` (≤ 80 characters,
    the outcome clipped), the outcome its icon; the text keeps what a name can't: the record's link, who, the error."""
    runs = [row(1), F3, row(4, issues=[{'phase': 'static-names'}]), row(5, 'cancelled', trigger=None),
            row(6, 'running', finished_ts=None), row(7, 'failed', failed_phase='p' * 90)]
    assert [N.run_post(SENT, r) for r in runs] == [
        (':alarm_clock: [record](https://site.test/scans/r1)', {'username': '11/02 08:00Z · 1m 00s · clean', 'icon_emoji': ':white_check_mark:'}),
        (':ada: [record](https://site.test/scans/r3) · boom', {'username': '11/04 08:00Z · 1m 00s · failed in index', 'icon_emoji': ':x:'}),
        (':alarm_clock: [record](https://site.test/scans/r4)',
         {'username': '11/05 08:00Z · 1m 00s · phase issues: static-names', 'icon_emoji': ':warning:'}),
        (':grey_question: [record](https://site.test/scans/r5)', {'username': '11/06 08:00Z · 1m 00s · cancelled', 'icon_emoji': ':no_entry_sign:'}),
        (':alarm_clock: [record](https://site.test/scans/r6)', {'username': '11/07 08:00Z · running', 'icon_emoji': ':hourglass_flowing_sand:'}),
        (':alarm_clock: [record](https://site.test/scans/r7)', {'username': '11/08 08:00Z · 1m 00s · failed in ' + 'p' * 45 + '…', 'icon_emoji': ':x:'}),
    ]
    assert len(N.run_post(SENT, runs[-1])[1]['username']) == 80
    badged = replace(SENT, badges='https://icons.test/run-{outcome}-{trigger}.png')
    assert [N.run_post(badged, r)[1]['icon_url'] for r in runs[:5]] == [
        'https://icons.test/run-clean-schedule.png', 'https://icons.test/run-failed-manual.png', 'https://icons.test/run-issues-schedule.png',
        'https://icons.test/run-cancelled-unknown.png', 'https://icons.test/run-running-schedule.png',
    ]
    assert N.run_post(CFG, runs[1]) == (N.reply(CFG, runs[1]), None)


def test_a_quiet_green_thread_posts_its_head_and_rolling_reply_under_fixed_senders_then_only_edits_them():
    w = Writer(SENT)
    w.run(history(2))
    assert sent(w.slack) == [('post', '1.0', GREEN, SENT_HEAD), ('post', '2.0', RUNS, TRAIL)]
    assert w.saved['head_sender'] == 'Test scans · clean'
    w.run([row(3, 'running', finished_ts=None, **ADA), *history(2)])
    assert sent(w.slack) == [
        ('edit', '2.0', None, f'{TRAIL}\n⏳ :ada: [11/04 08:00Z](https://site.test/scans/r3) · running'),
        ('edit', '1.0', None, SENT_HEAD.replace('Latest 2 runs', 'Running now: [11/04 08:00Z](https://site.test/scans/r3).\n\nLatest 2 runs')),
    ]


def red_writer():
    """history(2)'s quiet thread, closed by F3, whose failure thread (3.0) holds its reply (4.0)."""
    w = Writer(SENT)
    w.run(history(2))
    w.slack.calls.clear()
    w.run([F3, *history(2)])
    assert sent(w.slack) == [
        ('edit', '1.0', None, '**2 consecutive clean runs (ended)**\n\n'
                              'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0); last [11/03 08:00Z](https://site.test/scans/r2).\n\n'
                              'Ended by [11/04 08:00Z](https://site.test/scans/r3): failed. [All scans](https://site.test/scans).'),
        ('edit', '2.0', None, f'{TRAIL}\n❌ :ada: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s · failed in index: boom'),
        ('post', '3.0', FAILING, RED_HEAD),
        ('post', '4.0', {'username': '11/04 08:00Z · 1m 00s · failed in index', 'icon_emoji': ':x:'}, ':ada: [record](https://site.test/scans/r3) · boom'),
    ]
    assert (w.saved['parent'], w.saved['posted'], w.saved['opener'], w.saved['head_sender']) == ('3.0', {'r3': '4.0'}, 'r3', 'Test scans · failing')
    return w


def test_a_red_threads_run_posts_its_hourglass_reply_then_deletes_it_and_posts_its_outcome():
    w = red_writer()
    w.run([row(4, 'running', finished_ts=None), F3, *history(2)])
    assert sent(w.slack) == [
        ('post', '5.0', {'username': '11/05 08:00Z · running', 'icon_emoji': ':hourglass_flowing_sand:'}, ':alarm_clock: [record](https://site.test/scans/r4)'),
        ('edit', '3.0', None, RED_HEAD.replace('Each reply', 'Running now: [11/05 08:00Z](https://site.test/scans/r4).\n\nEach reply')),
    ]
    w.run([row(4, issues=[{'phase': 'probe'}]), F3, *history(2)])
    assert sent(w.slack) == [
        ('delete', '5.0', None, None),
        ('post', '6.0', {'username': '11/05 08:00Z · 1m 00s · phase issues: probe', 'icon_emoji': ':warning:'},
         ':alarm_clock: [record](https://site.test/scans/r4)'),
        ('edit', '3.0', None, '**2 consecutive runs failed or with phase issues**\n\n'
                              'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2) (clean).\n\n'
                              'Latest: [11/05 08:00Z](https://site.test/scans/r4): phase issues: probe.\n\n'
                              'Each reply links its scan record. [All scans](https://site.test/scans).'),
    ]
    assert ('running' in w.saved, w.saved['posted']) == (False, {'r3': '4.0', 'r4': '6.0'})
    # the run that opened the thread links its head; the others their replies
    state, writes = deepcopy(w.saved), []
    N.link_threads(state, w.slack, writes.append, w.save)
    assert writes == [[('r3', 'https://slack.test/p3.0'), ('r4', 'https://slack.test/p6.0')]]


def test_an_interrupted_repost_posts_the_outcome_once():
    w = red_writer()
    w.run([row(4, 'running', finished_ts=None), F3, *history(2)])
    w.slack.calls.clear()
    w.slack.timeout = True
    with pytest.raises(TimeoutError, match='response lost after Slack accepted the post'):
        w.run([row(4, 'failed'), F3, *history(2)])
    w.run([row(4, 'failed'), F3, *history(2)])
    assert [(c[0], c[1]) for c in w.slack.calls] == [('delete', '5.0'), ('post', '6.0'), ('edit', '3.0')]
    assert (w.saved['posted'], 'pending' in w.saved, 'running' in w.saved) == ({'r3': '4.0', 'r4': '6.0'}, False, False)


def test_a_cancelled_runs_hourglass_reply_in_a_red_thread_is_posted_again_as_cancelled():
    w = red_writer()
    w.run([row(4, 'running', finished_ts=None), F3, *history(2)])
    w.slack.calls.clear()
    w.run([row(4, 'cancelled'), F3, *history(2)])
    assert sent(w.slack) == [
        ('delete', '5.0', None, None),
        ('post', '6.0', {'username': '11/05 08:00Z · 1m 00s · cancelled', 'icon_emoji': ':no_entry_sign:'}, ':alarm_clock: [record](https://site.test/scans/r4)'),
        ('edit', '3.0', None, RED_HEAD),
    ]
    assert (w.saved['relink'], 'running' in w.saved) == ({'r4': '6.0'}, False)
    state, writes = deepcopy(w.saved), []
    N.link_threads(state, w.slack, writes.append, w.save)
    assert writes == [[('r3', 'https://slack.test/p3.0'), ('r4', 'https://slack.test/p6.0')]]
    assert w.saved.get('relink') is None


def test_a_red_threads_run_that_ends_clean_leaves_nothing_there_and_heads_the_new_green_thread():
    w = red_writer()
    w.run([row(4, 'running', finished_ts=None), F3, *history(2)])
    w.slack.calls.clear()
    w.run([row(4), F3, *history(2)])
    assert sent(w.slack) == [
        ('edit', '3.0', None, '**1 consecutive failed run (ended)**\n\n'
                              'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2); last [11/04 08:00Z](https://site.test/scans/r3).\n\n'
                              'Ended by [11/05 08:00Z](https://site.test/scans/r4): clean. [All scans](https://site.test/scans).'),
        ('post', '6.0', GREEN, '**1 consecutive clean run**\n\n'
                               'Since 11/05 08:00Z after [2026-11-04T0800](https://site.test/scans/r3).\n\n'
                               'Latest completed: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s.\n\n'
                               'Latest 1 run: 1 scheduled / 0 manual. One reply lists the runs, each linking its scan record. [All scans](https://site.test/scans).'),
        ('post', '7.0', RUNS, '✅ :alarm_clock: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s'),
        ('delete', '5.0', None, None),
    ]
    state, writes = deepcopy(w.saved), []
    N.link_threads(state, w.slack, writes.append, w.save)
    assert writes == [[('r4', 'https://slack.test/p6.0')]]


def test_a_clean_groupings_failure_deletes_its_hourglass_reply_and_links_its_alert(monkeypatch):
    w = Writer(replace(SENT, group='clean', quiet=False))
    w.run([row(3, 'running', finished_ts=None), *history(2)])
    w.slack.calls.clear()
    monkeypatch.setattr(w.slack, 'alerts', {'https://site.test/scans/r3': '9.0'})
    w.run([row(3, 'failed'), *history(2)])
    assert sent(w.slack) == [
        ('edit', '1.0', None, '**2 consecutive clean runs (ended)**\n\n'
                              'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0); last [11/03 08:00Z](https://site.test/scans/r2).\n\n'
                              'Ended by [11/04 08:00Z](https://site.test/scans/r3): failed. [All scans](https://site.test/scans).'),
        ('delete', '4.0', None, None),
    ]
    state, writes = deepcopy(w.saved), []
    N.link_threads(state, w.slack, writes.append, w.save)
    assert writes == [[('r1', 'https://slack.test/p2.0'), ('r2', 'https://slack.test/p3.0'), ('r3', 'https://slack.test/p9.0')]]


def test_a_head_posted_before_senders_keeps_its_full_text():
    """A thread opened as the app (state with no `head_sender`) is edited in its own words: its head says the title."""
    w = Writer(QUIET)
    w.run(history(2))
    w.cfg = SENT
    w.slack.calls.clear()
    w.run([row(3), *history(2)])
    assert sent(w.slack) == [
        ('edit', '2.0', None, f'{TRAIL}\n✅ :alarm_clock: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s'),
        ('edit', '1.0', None, '✅ **Test scans: 3 consecutive clean runs**\n\n'
                              'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0).\n\n'
                              'Latest completed: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s.\n\n'
                              'Latest 3 runs: 3 scheduled / 0 manual. One reply lists the runs, each linking its scan record. [All scans](https://site.test/scans).'),
    ]


def test_a_preview_shows_each_posts_sender(tmp_path, monkeypatch):
    import json

    from dt_cloud import probe

    cfg = replace(SENT, state=str(tmp_path / 'state'), quiet=False)
    monkeypatch.setattr(probe, 'http_fetch', lambda *args: lambda path: SimpleNamespace(status=200, body=json.dumps(dict(configured=True, runs=history(2)))))
    assert N.run(asdict(cfg), {'SITE_TOKEN': 'viewer'}, 'project').split('\n\n+++\n\n') == [
        '[:white_check_mark: Test scans · clean]\n' + SENT_HEAD.replace('One reply lists the runs, each linking', 'Each reply links'),
        '[:white_check_mark: 11/02 08:00Z · 1m 00s · clean]\n:alarm_clock: [record](https://site.test/scans/r1)',
        '[:white_check_mark: 11/03 08:00Z · 1m 00s · clean]\n:alarm_clock: [record](https://site.test/scans/r2)',
    ]
