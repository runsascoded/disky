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


def sections(body):
    """`body`'s `section` blocks' texts, as thrds splits a message (`section_blocks`)."""
    from thrds.slack import section_blocks

    return [b['text']['text'] for b in section_blocks(body, lines=True)]


class Slack:
    #: The workspace's custom emoji (`Transport.have`): unknown, so every starter's own emoji is kept
    have = None

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

    def post_sections(self, body, parent, operation, sender=None, broadcast=False):
        """As Slack's: one message of `section` blocks (thrds' `section_blocks`' texts kept on it, as `sections`), and
        `broadcast` (`reply_broadcast`)."""
        try:
            return self.post(body, parent, operation, sender)
        finally:  # a lost response (`timeout`) still leaves the message as posted
            self.messages_by_id[next(reversed(self.messages_by_id))].update(sections=sections(body), broadcast=broadcast)

    def edit(self, ts, body):
        self.calls.append(('edit', ts, body))
        self.messages_by_id[ts]['text'] = body

    def edit_sections(self, ts, body):
        self.edit(ts, body)
        self.messages_by_id[ts]['sections'] = sections(body)

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


def test_durations_take_hours_and_minutes_from_an_hour():
    # a half minute rounds up (3630 s = 60.5 min), as pages-kit's `took`
    assert [N.hm(s) for s in (0, 24, 562, 3599, 3600, 3629, 3630, 7366, 38461)] == [
        "0m 00s", "0m 24s", "9m 22s", "59m 59s", "1h 0m", "1h 0m", "1h 1m", "2h 3m", "10h 41m"]


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
    assert [(c[0], c[1]) for c in w.slack.calls] == [('post', '4.0'), ('edit', '1.0'), ('post', '5.0')]
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
    assert [(c[0], c[1]) for c in w.slack.calls] == [('edit', '1.0'), ('post', '5.0'), ('edit', '1.0'), ('post', '6.0'), ('edit', '4.0')]
    assert bodies(w.slack)[3:] == [('post', '6.0', failed), ('edit', '4.0', failed + ' · [thread](https://slack.test/p5.0)')]
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
                        'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2) (clean) · [previous thread](https://slack.test/p1.0).\n\n'
                        'Latest: [11/05 08:00Z](https://site.test/scans/r4): failed in probe.\n\n'
                        'Each reply links its scan record. [All scans](https://site.test/scans).'),
        ('edit', '1.0', '✅ **Test scans: 2 consecutive clean runs (ended)**\n\n'
                        'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0); last [11/03 08:00Z](https://site.test/scans/r2).\n\n'
                        'Ended by [11/04 08:00Z](https://site.test/scans/r3): failed · [next thread](https://slack.test/p4.0). [All scans](https://site.test/scans).'),
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
                        'Since 11/06 08:00Z after [2026-11-05T0800](https://site.test/scans/r4) · [previous thread](https://slack.test/p4.0).\n\n'
                        'Latest completed: [11/06 08:00Z](https://site.test/scans/r5) · 1m 00s.\n\n'
                        'Latest 1 run: 1 scheduled / 0 manual. Each reply links its scan record. [All scans](https://site.test/scans).'),
        ('edit', '4.0', '❌ **Test scans: 2 consecutive failed runs (ended)**\n\n'
                        'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2); last [11/05 08:00Z](https://site.test/scans/r4).\n\n'
                        'Ended by [11/06 08:00Z](https://site.test/scans/r5): clean · [next thread](https://slack.test/p7.0). [All scans](https://site.test/scans).'),
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
                        'Since 11/06 08:00Z after [2026-11-05T0800](https://site.test/scans/r4) (failed in interval-store: OOM again) · [previous thread](https://slack.test/p1.0).\n\n'
                        'Latest: [11/06 08:00Z](https://site.test/scans/r5): phase issues: probe.\n\n'
                        'Each reply links its scan record. [All scans](https://site.test/scans).'),
        ('edit', '1.0', '❌ **Test scans: 2 consecutive failed runs (ended)**\n\n'
                        'Since 11/04 08:00Z after [2026-11-02T0800](https://site.test/scans/r1); last [11/05 08:00Z](https://site.test/scans/r4).\n\n'
                        'Ended by [11/06 08:00Z](https://site.test/scans/r5): phase issues: probe · [next thread](https://slack.test/p4.0). [All scans](https://site.test/scans).'),
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
                       'Since 11/03 08:00Z after [2026-11-02T0800](https://site.test/scans/r1) (clean) · [previous thread](https://slack.test/p1.0).\n\n'
                       'Latest: [11/04 08:00Z](https://site.test/scans/r3): phase issues: static-names.\n\n'
                       'Each reply links its scan record. [All scans](https://site.test/scans).')
    w.slack.calls.clear()
    f4 = row(4, 'failed', failed_phase='index')
    w.run([f4, i3, i2, *history(1)])
    assert bodies(w.slack) == [
        ('post', '6.0', '❌ :alarm_clock: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s · failed in index'),
        ('edit', '3.0', '❌ **Test scans: 3 consecutive runs failed or with phase issues**\n\n'
                        'Since 11/03 08:00Z after [2026-11-02T0800](https://site.test/scans/r1) (clean) · [previous thread](https://slack.test/p1.0).\n\n'
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
    text = N.canvas_text(cfg, rows, 'https://slack.test/p1.0', {'meta': 503})
    assert text.split('\n\n') == [
        '✅ Test scans: 1 consecutive clean run · [thread](https://slack.test/p1.0)',
        'Updated 2026-11-03 08:01 UTC.',
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
                        'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2) (clean) · [previous thread](https://slack.test/p1.0).\n\n'
                        'Latest: [11/04 08:00Z](https://site.test/scans/r3): phase issues: probe, static-names.\n\n'
                        'Each reply links its scan record. [All scans](https://site.test/scans).'),
        ('edit', '1.0', '✅ **Test scans: 2 consecutive clean runs (ended)**\n\n'
                        'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0); last [11/03 08:00Z](https://site.test/scans/r2).\n\n'
                        'Ended by [11/04 08:00Z](https://site.test/scans/r3): phase issues · [next thread](https://slack.test/p6.0). [All scans](https://site.test/scans).'),
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
                        'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2) (clean) · [previous thread](https://slack.test/p1.0).\n\n'
                        'Latest: [11/04 08:00Z](https://site.test/scans/r3): failed in index: boom.\n\n'
                        'Each reply links its scan record. [All scans](https://site.test/scans).'),
        ('edit', '1.0', '✅ **Test scans: 2 consecutive clean runs (ended)**\n\n'
                        'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0); last [11/03 08:00Z](https://site.test/scans/r2).\n\n'
                        'Ended by [11/04 08:00Z](https://site.test/scans/r3): failed · [next thread](https://slack.test/p3.0). [All scans](https://site.test/scans).'),
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
                        'Since 11/05 08:00Z after [2026-11-04T0800](https://site.test/scans/r3) · [previous thread](https://slack.test/p3.0).\n\n'
                        'Latest completed: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s.\n\n'
                        'Latest 1 run: 1 scheduled / 0 manual. One reply lists the runs, each linking its scan record. [All scans](https://site.test/scans).'),
        ('edit', '3.0', '❌ **Test scans: 1 consecutive failed run (ended)**\n\n'
                        'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2); last [11/04 08:00Z](https://site.test/scans/r3).\n\n'
                        'Ended by [11/05 08:00Z](https://site.test/scans/r4): clean · [next thread](https://slack.test/p6.0). [All scans](https://site.test/scans).'),
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


def test_a_thread_from_before_quiet_mode_converges_to_its_head_and_rolling_reply():
    """A clean thread opened with a reply per run, once quiet, gains the rolling reply; each run then links it (saved
    first), and the old per-run replies are deleted (Ryan, 2026-11-22: the quiet shape, whatever posted before)."""
    w = Writer(replace(CFG, quiet=False))
    w.run(history(2))
    assert w.saved['posted'] == {'r1': '2.0', 'r2': '3.0'}
    w.cfg, w.slack.calls = replace(CFG, quiet=True), []
    w.run(history(2))
    assert [(c[0], c[1]) for c in w.slack.calls if c[0] in ('post', 'delete')] == [('post', '4.0'), ('delete', '2.0'), ('delete', '3.0')]
    assert w.saved['posted'] == {'r1': '4.0', 'r2': '4.0'}
    assert sorted(w.slack.messages_by_id) == ['1.0', '4.0']


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
RUNS = {'username': 'Test scans · runs'}
FAILING = {'username': 'Test scans · failing', 'icon_emoji': ':x:'}
#: history(2)'s quiet thread head under its sender: no leading ✅ and title
SENT_HEAD = QUIET_HEAD.replace('✅ **Test scans: ', '**')
F3 = row(3, 'failed', failed_phase='index', error='boom', **ADA)
#: The failure thread F3 opens after history(2)'s quiet thread (its head 3.0)
RED_HEAD = ('**1 consecutive failed run**\n\n'
            'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2) (clean) · [previous thread](https://slack.test/p1.0).\n\n'
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
    """Ryan, 2026-11-22: a run reply's first line is its sender, `<stamp> · <duration> · <outcome>`, the outcome its
    icon; the text keeps what a name can't: the record's link, who, the failed phase and its error, the phases with issues
    (Slack's client cuts a long name, 2026-11-26)."""
    runs = [row(1), F3, row(4, issues=[{'phase': 'static-names'}]), row(5, 'cancelled', trigger=None),
            row(6, 'running', finished_ts=None), row(7, 'failed', failed_phase='p' * 90)]
    assert [N.run_post(SENT, r) for r in runs] == [
        (':alarm_clock: [record](https://site.test/scans/r1)', {'username': '11/02 08:00Z · 1m 00s · clean', 'icon_emoji': ':white_check_mark:'}),
        (':ada: [record](https://site.test/scans/r3) · failed in index: boom', {'username': '11/04 08:00Z · 1m 00s · failed', 'icon_emoji': ':x:'}),
        (':alarm_clock: [record](https://site.test/scans/r4) · phase issues: static-names',
         {'username': '11/05 08:00Z · 1m 00s · phase issues', 'icon_emoji': ':warning:'}),
        (':grey_question: [record](https://site.test/scans/r5)', {'username': '11/06 08:00Z · 1m 00s · cancelled', 'icon_emoji': ':no_entry_sign:'}),
        (':alarm_clock: [record](https://site.test/scans/r6)', {'username': '11/07 08:00Z · running', 'icon_emoji': ':hourglass_flowing_sand:'}),
        (':alarm_clock: [record](https://site.test/scans/r7) · failed in ' + 'p' * 90, {'username': '11/08 08:00Z · 1m 00s · failed', 'icon_emoji': ':x:'}),
    ]
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


R3_EDITS = [
    ('edit', '3.0', None, f'{TRAIL}\n⏳ :ada: [11/04 08:00Z](https://site.test/scans/r3) · running'),
    ('edit', '1.0', None, SENT_HEAD.replace('Latest 2 runs', 'Running now: [11/04 08:00Z](https://site.test/scans/r3).\n\nLatest 2 runs')),
]


def rolling_state(w):
    return {k: w.saved.get(k) for k in ('rolling', 'rolling_sender', 'rolling_retired', 'posted')}


def test_a_rolling_reply_kept_from_before_its_sender_was_recorded_is_posted_again_once_then_edited_in_place():
    """Ryan, 2026-11-23: the runs reply under the app's icon, not 🧾. An edit can't change a post's icon, so a reply whose
    sender isn't recorded (posted before `rolling_sender`) is posted again under today's, then deleted; once."""
    w = Writer(SENT)
    w.run(history(2))
    assert sent(w.slack) == [('post', '1.0', GREEN, SENT_HEAD), ('post', '2.0', RUNS, TRAIL)]
    assert rolling_state(w) == {'rolling': '2.0', 'rolling_sender': RUNS, 'rolling_retired': None, 'posted': {'r1': '2.0', 'r2': '2.0'}}
    del w.saved['rolling_sender']
    w.run(history(2))
    assert sent(w.slack) == [('post', '3.0', RUNS, TRAIL), ('delete', '2.0', None, None)]
    assert rolling_state(w) == {'rolling': '3.0', 'rolling_sender': RUNS, 'rolling_retired': None, 'posted': {'r1': '3.0', 'r2': '3.0'}}
    assert sorted(w.slack.messages_by_id) == ['1.0', '3.0']
    w.run(history(2))
    assert sent(w.slack) == []
    w.run([row(3, 'running', finished_ts=None, **ADA), *history(2)])
    assert sent(w.slack) == R3_EDITS


def test_a_changed_runs_icon_posts_the_rolling_reply_again_and_its_runs_link_the_new_one():
    w = Writer(SENT)
    w.run(history(2))
    state, writes = deepcopy(w.saved), []
    N.link_threads(state, w.slack, writes.append, w.save)
    assert writes == [[('r1', 'https://slack.test/p2.0'), ('r2', 'https://slack.test/p2.0')]]
    w.slack.calls.clear()
    icon = {'username': 'Test scans · runs', 'icon_url': 'https://icons.test/runs.png'}
    w.cfg = replace(SENT, icons=(('runs', 'https://icons.test/runs.png'),))
    w.run(history(2))
    assert sent(w.slack) == [('post', '3.0', icon, TRAIL), ('delete', '2.0', None, None)]
    assert rolling_state(w) == {'rolling': '3.0', 'rolling_sender': icon, 'rolling_retired': None, 'posted': {'r1': '3.0', 'r2': '3.0'}}
    state, writes = deepcopy(w.saved), []
    N.link_threads(state, w.slack, writes.append, w.save)
    assert writes == [[('r1', 'https://slack.test/p3.0'), ('r2', 'https://slack.test/p3.0')]]
    w.run(history(2))
    assert sent(w.slack) == []


def test_an_interrupted_rolling_repost_is_recovered_not_posted_twice_and_the_old_reply_still_goes():
    """The repost's intent is its own (never the old reply's, which recovery would otherwise find), and the old reply
    is held (`rolling_retired`) until it is deleted."""
    w = Writer(SENT)
    w.run(history(2))
    del w.saved['rolling_sender']
    w.slack.calls.clear()
    w.slack.timeout = True
    with pytest.raises(TimeoutError, match='response lost after Slack accepted the post'):
        w.run(history(2))
    assert (w.saved['rolling_retired'], 'rolling' in w.saved, w.saved['pending']['key']) == ('2.0', False, 'rolling')
    w.run(history(2))
    assert sent(w.slack) == [('post', '3.0', RUNS, TRAIL), ('delete', '2.0', None, None)]
    assert rolling_state(w) == {'rolling': '3.0', 'rolling_sender': RUNS, 'rolling_retired': None, 'posted': {'r1': '3.0', 'r2': '3.0'}}
    assert sorted(w.slack.messages_by_id) == ['1.0', '3.0']


def test_a_closed_threads_rolling_reply_is_only_edited_whatever_its_sender():
    """A thread that closes keeps its rolling reply where it stands: its final trail is an edit, never a repost."""
    calls = []
    for legacy in (False, True):
        w = Writer(SENT)
        w.run(history(2))
        if legacy:
            del w.saved['rolling_sender']
        w.slack.calls.clear()
        w.run([F3, *history(2)])
        calls.append(sent(w.slack))
    assert calls[1] == calls[0]
    assert [(c[0], c[1]) for c in calls[1]] == [('edit', '1.0'), ('edit', '2.0'), ('post', '3.0'), ('edit', '1.0'), ('post', '4.0')]


def test_a_rolling_reply_posted_as_the_app_stands_once_senders_are_on_and_one_of_unknown_icon_is_posted_again_once():
    """Only the icon counts (a name alone isn't worth a repost); a reply from before `rolling_sender` was kept could
    carry any icon, so it is posted again once, whatever the configuration."""
    w = Writer(QUIET)
    w.run(history(2))
    assert w.saved['rolling_sender'] is None
    w.slack.calls.clear()
    w.cfg = SENT
    w.run(history(2))
    assert sent(w.slack) == []
    w.cfg = QUIET
    del w.saved['rolling_sender']
    w.run(history(2))
    assert sent(w.slack) == [('post', '3.0', None, TRAIL), ('delete', '2.0', None, None)]
    assert rolling_state(w) == {'rolling': '3.0', 'rolling_sender': None, 'rolling_retired': None, 'posted': {'r1': '3.0', 'r2': '3.0'}}
    w.run(history(2))
    assert sent(w.slack) == []


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
        ('edit', '1.0', None, '**2 consecutive clean runs (ended)**\n\n'
                              'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0); last [11/03 08:00Z](https://site.test/scans/r2).\n\n'
                              'Ended by [11/04 08:00Z](https://site.test/scans/r3): failed · [next thread](https://slack.test/p3.0). [All scans](https://site.test/scans).'),
        ('post', '4.0', {'username': '11/04 08:00Z · 1m 00s · failed', 'icon_emoji': ':x:'}, ':ada: [record](https://site.test/scans/r3) · failed in index: boom'),
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
        ('post', '6.0', {'username': '11/05 08:00Z · 1m 00s · phase issues', 'icon_emoji': ':warning:'},
         ':alarm_clock: [record](https://site.test/scans/r4) · phase issues: probe'),
        ('edit', '3.0', None, '**2 consecutive runs failed or with phase issues**\n\n'
                              'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2) (clean) · [previous thread](https://slack.test/p1.0).\n\n'
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
                               'Since 11/05 08:00Z after [2026-11-04T0800](https://site.test/scans/r3) · [previous thread](https://slack.test/p3.0).\n\n'
                               'Latest completed: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s.\n\n'
                               'Latest 1 run: 1 scheduled / 0 manual. One reply lists the runs, each linking its scan record. [All scans](https://site.test/scans).'),
        ('edit', '3.0', None, '**1 consecutive failed run (ended)**\n\n'
                              'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2); last [11/04 08:00Z](https://site.test/scans/r3).\n\n'
                              'Ended by [11/05 08:00Z](https://site.test/scans/r4): clean · [next thread](https://slack.test/p6.0). [All scans](https://site.test/scans).'),
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


def test_a_deferred_append_leaves_a_run_clean_and_says_so():
    """#82: a compaction holding the interval store defers the scan's append (a later run's takes it); the run stays in
    its clean thread, its reply (and its sender) naming what was deferred. Any other issue beside it still counts."""
    cfg = replace(CFG, group='color')
    d = {'phase': 'interval-store', 'outcome': 'deferred', 'recovery': None}
    other = {'phase': 'warm-cache', 'outcome': 'failed', 'recovery': None}
    assert [N.grade(r) for r in [row(1, issues=[d]), row(1, issues=[d, other]), row(1, 'failed', issues=[d])]] == ['clean', 'issues', 'failed']
    assert N.segment(cfg, [row(3), row(2, issues=[d]), row(1)]) == ('clean', [row(3), row(2, issues=[d]), row(1)], None)
    assert N.reply(cfg, row(2, issues=[d])) == '✅ :alarm_clock: [11/03 08:00Z](https://site.test/scans/r2) · 1m 00s · deferred: interval-store'
    assert N.reply(cfg, row(2, issues=[d, other])) == '⚠️ :alarm_clock: [11/03 08:00Z](https://site.test/scans/r2) · 1m 00s · phase issues: warm-cache'
    assert N.signature(row(2, issues=[d, other])) == 'issues:warm-cache'
    assert N.run_sender(SENT, row(2, issues=[d])) == {
        'username': '11/03 08:00Z · 1m 00s · clean · deferred: interval-store', 'icon_emoji': ':white_check_mark:'}


#: Two status card URLs as the site's `/api/scan-runs/card-url` signs them (`/og/deploy.png`, open question #87)
CARD1 = 'https://site.test/og/deploy.png?v=0123456789ab&sig=AB0123456789'
CARD2 = 'https://site.test/og/deploy.png?v=ba9876543210&sig=AB0123456789'


def card_line(url, at):
    """A head's status card: its trailing image line (thrds posts it as an `image` block), as of the run at `at`."""
    return f'\n\n![Test scans status card: its latest scan runs by step and 30 days of size, cost and run time, as of the {at} run]({url})'


def test_the_card_url_is_taken_only_as_the_sites_signed_status_card(capsys):
    answers = [
        (200, f'{{"url": "{CARD1}", "v": "0123456789ab", "expires": 1796083200}}'.encode()),
        (404, b'{"error": "no scan run card-url"}'),
        (503, b'{"error": "card URLs not configured (SESSION_SECRET unset)"}'),
        (0, b''),
        (200, b'{"configured": true, "runs": []}'),
        (200, b'<html>'),
        (200, b'{"url": "https://other.test/og/deploy.png?v=1&sig=2"}'),
        (200, f'{{"url": "{CARD1})"}}'.encode()),
        (200, b'{"url": "https://site.test/og/map.png?sig=2"}'),
    ]
    assert [N.card_url(QUIET, status, body) for status, body in answers] == [CARD1, *[None] * 8]
    assert capsys.readouterr().err.split('\n') == [
        'nominal: no status card (card-url HTTP 404)',
        'nominal: no status card (card-url HTTP 503)',
        'nominal: no status card (card-url HTTP 0)',
        'nominal: no status card (card-url answered KeyError)',
        'nominal: no status card (card-url answered JSONDecodeError)',
        'nominal: no status card (card-url answered another URL)',
        'nominal: no status card (card-url answered another URL)',
        'nominal: no status card (card-url answered another URL)',
        '',
    ]


def test_the_card_is_asked_for_the_threads_store_in_its_colour_and_can_be_turned_off():
    assert N.card_query(QUIET) == {'store': 'primary'}
    assert N.card_query(replace(QUIET, store='meta', color='#EA4335')) == {'store': 'meta', 'color': 'EA4335'}
    assert (QUIET.card, QUIET.color) == (True, None)
    with pytest.raises(ValueError, match='nominal.color: #rrggbb'):
        replace(QUIET, color='red')
    with pytest.raises(ValueError, match='nominal.card: true or false'):
        N.config({**asdict(QUIET), 'card': 'yes'})


def test_a_head_takes_the_card_as_its_trailing_image_unless_too_long_to_sit_beside_one():
    r2 = row(2)
    assert N.with_card(QUIET, QUIET_HEAD, CARD1, r2) == QUIET_HEAD + card_line(CARD1, '11/03 08:00Z')
    assert N.with_card(QUIET, QUIET_HEAD, None, r2) == QUIET_HEAD
    long = 'x' * (N.CARD_BODY_MAX + 1)
    assert N.with_card(QUIET, long, CARD1, r2) == long
    # nothing in the alt text ends the image line early
    assert N.with_card(replace(QUIET, title='[Test] scans'), 'head', CARD1, r2) == (
        f'head\n\n![Test scans status card: its latest scan runs by step and 30 days of size, cost and run time, as of the 11/03 08:00Z run]({CARD1})')


def test_an_open_head_carries_the_status_card_and_a_closed_one_drops_it():
    """#87 phase 2: the open thread's head shows the deployment's status card (the site's signed `/og/deploy.png`), edited
    in when the card's URL changes (a new run: a new `v`); the head a thread closes with carries none, since its URL
    expires after a week."""
    w = Writer(QUIET)

    def report(rows, card):
        return N.converge(w.cfg, rows, deepcopy(w.saved), w.slack, w.save, lambda: None, now=lambda: 1000, card=card)

    report(history(2), CARD1)
    assert bodies(w.slack) == [('post', '1.0', QUIET_HEAD + card_line(CARD1, '11/03 08:00Z')), ('post', '2.0', TRAIL)]
    w.slack.calls.clear()
    # nothing new: the same URL, nothing edited
    report(history(2), CARD1)
    assert w.slack.calls == []
    # a new card URL alone: the head edited to it
    report(history(2), CARD2)
    assert bodies(w.slack) == [('edit', '1.0', QUIET_HEAD + card_line(CARD2, '11/03 08:00Z'))]
    w.slack.calls.clear()
    f3 = row(3, 'failed', failed_phase='index', error='boom', **ADA)
    report([f3, *history(2)], CARD1)
    failed = '❌ :ada: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s · failed in index: boom'
    closed = ('✅ **Test scans: 2 consecutive clean runs (ended)**\n\n'
              'Since 11/02 08:00Z after [2026-11-01T0800](https://site.test/scans/r0); last [11/03 08:00Z](https://site.test/scans/r2).\n\n'
              'Ended by [11/04 08:00Z](https://site.test/scans/r3): failed')
    assert bodies(w.slack) == [
        ('edit', '1.0', f'{closed}. [All scans](https://site.test/scans).'),
        ('edit', '2.0', f'{TRAIL}\n{failed}'),
        ('post', '3.0', '❌ **Test scans: 1 consecutive failed run**\n\n'
                        'Since 11/04 08:00Z after [2026-11-03T0800](https://site.test/scans/r2) (clean) · [previous thread](https://slack.test/p1.0).\n\n'
                        'Latest: [11/04 08:00Z](https://site.test/scans/r3): failed in index: boom.\n\n'
                        'Each reply links its scan record. [All scans](https://site.test/scans).' + card_line(CARD1, '11/04 08:00Z')),
        ('edit', '1.0', f'{closed} · [next thread](https://slack.test/p3.0). [All scans](https://site.test/scans).'),
        ('post', '4.0', failed),
    ]


def test_a_preview_shows_the_heads_card_and_asks_the_site_for_it(tmp_path, monkeypatch):
    import json

    from dt_cloud import probe

    asked = []

    def fetch(path):
        asked.append(path)
        if path.startswith('/api/scan-runs/card-url?'):
            return SimpleNamespace(status=200, body=json.dumps({'url': CARD1}).encode())
        return SimpleNamespace(status=200, body=json.dumps(dict(configured=True, runs=history(2))))
    monkeypatch.setattr(probe, 'http_fetch', lambda *args: fetch)
    cfg = replace(QUIET, state=str(tmp_path / 'state'), color='#4C5FF0')
    assert N.run(asdict(cfg), {'SITE_TOKEN': 'viewer'}, 'project') == f'{QUIET_HEAD}{card_line(CARD1, "11/03 08:00Z")}\n\n+++\n\n{TRAIL}'
    assert asked == ['/api/scan-runs?store=primary&emoji=1', '/api/scan-runs/card-url?store=primary&color=4C5FF0']
    asked.clear()
    assert N.run(asdict(replace(cfg, card=False)), {'SITE_TOKEN': 'viewer'}, 'project') == f'{QUIET_HEAD}\n\n+++\n\n{TRAIL}'
    assert asked == ['/api/scan-runs?store=primary&emoji=1']


# Monthly threads (specs/admin-monthly-thread.md, `Config.monthly`)

MONTHLY = replace(SENT, monthly=True)
#: November 2026's end: its last report, or December's first, closes it
END = int(N.month_end('2026-11'))
F1 = row(1, 'failed', failed_phase='index', error='boom')


def week(monday):
    """A weekly runs reply's sender: its ISO week's Monday, under the app's own icon."""
    return {'username': f'wk of {monday} · runs'}


class Site:
    """The site's `POST /api/scan-runs/month-op` over the ledger `rows` as of `now`, as `MonthOp`: a status line (an open
    month's only), the month's run count and runs going, the months either side linked as the route's `linkLine`
    writes them (`digest.month_links`), and an open month's card."""

    def __init__(self):
        self.rows, self.now, self.calls = [], 0, []

    def __call__(self, month, prev_url, next_url):
        from datetime import date

        from dt_cloud.digest import month_links

        self.calls.append((month, prev_url, next_url))
        runs = [r for r in self.rows if N.month_of(r) == month]
        going = sum(N.live(r) for r in runs)
        closed = self.now >= N.month_end(month)
        count = f"{N.month_name(month).split()[0] if closed else 'This month'}: {len(runs)} runs" + (f' · {going} running' if going else '')
        links = month_links(date(int(month[:4]), int(month[5:]), 1), prev_url, next_url)
        return '\n'.join([*([] if closed else ['status']), count, *([links] if links else [])]), None if closed else CARD1


class Monthly:
    """`converge_monthly` over the fake Slack (`Pins` unless given) and the fake site, at a given time."""

    def __init__(self, cfg=MONTHLY, slack=None, saved=None):
        self.cfg, self.slack, self.site = cfg, slack or Pins(), Site()
        self.saved = deepcopy(saved) if saved else {'namespace': cfg.namespace}

    def save(self, value):
        self.saved = deepcopy(value)

    def run(self, rows, at):
        self.site.rows, self.site.now = rows, at
        return N.converge_monthly(self.cfg, rows, deepcopy(self.saved), self.slack, self.save, lambda: None, self.site, now=lambda: at)


def thread(slack):
    """Each call since the last, then forgets them: a post as `(post, ts, parent, sender, text, broadcast)`, an edit as
    `(edit, ts, text)`, a delete as `(delete, ts, None)`, a pin as `(pin, ts)`."""
    out = [('post', c[1], c[2], slack.senders[c[1]], c[3], slack.messages_by_id.get(c[1], {}).get('broadcast', False)) if c[0] == 'post'
           else tuple(c) for c in slack.calls]
    slack.calls.clear()
    return out


def as_pins(slack, *pinned):
    """`slack`'s messages in a `Pins` fake, with `pinned` pinned."""
    out = Pins()
    out.messages_by_id, out.deleted, out.senders, out.pinned = slack.messages_by_id, slack.deleted, slack.senders, list(pinned)
    return out


def test_monthly_is_off_by_default_and_must_be_a_boolean():
    assert (SENT.monthly, MONTHLY.monthly) == (False, True)
    with pytest.raises(ValueError, match='nominal.monthly: true or false'):
        N.config(dict(state='unused', site='https://site.test', channel='C123', title='Test scans', monthly='yes'))


def test_months_and_iso_weeks():
    assert [N.month_of(row(0)), N.month_of(row(30)), N.prior('2026-01'), N.following('2026-12'), N.month_name('2026-11')] == [
        '2026-11', '2026-12', '2025-12', '2027-01', 'November 2026']
    assert (END, int(N.month_end('2026-12'))) == (int(datetime(2026, 12, 1, tzinfo=timezone.utc).timestamp()),
                                                 int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp()))
    # 11/01 is a Sunday: its ISO week began in October
    assert [(N.week_of(row(i)), N.week_label(N.week_of(row(i)))) for i in (0, 1, 7, 8, 29, 30)] == [
        ('2026-W44', 'wk of 10/26'), ('2026-W45', 'wk of 11/02'), ('2026-W45', 'wk of 11/02'), ('2026-W46', 'wk of 11/09'),
        ('2026-W49', 'wk of 11/30'), ('2026-W49', 'wk of 11/30')]


def test_a_month_thread_opens_on_its_first_run_and_keeps_a_reply_a_week_and_one_a_failure():
    """The month's first report posts its OP (under `<title> · runs`, the card under it) and its week's runs reply; each
    later report edits them; a failure gets its own reply, also sent to the channel, which its week's line links."""
    m = Monthly()
    m.run([row(0, 'running', finished_ts=None)], T0 + 10)
    assert thread(m.slack) == [
        ('post', '1.0', None, RUNS, 'status\nThis month: 1 runs · 1 running' + card_line(CARD1, '11/01 08:00Z'), False),
        ('post', '2.0', '1.0', week('10/26'), '⏳ :alarm_clock: [11/01 08:00Z](https://site.test/scans/r0) · running', False),
    ]
    # the OP is asked for twice: to post it, then as every open month's is each report (unchanged here: no edit)
    assert m.site.calls == [('2026-11', None, None), ('2026-11', None, None)]
    m.run([row(0)], T0 + 120)
    assert thread(m.slack) == [
        ('edit', '2.0', '✅ :alarm_clock: [11/01 08:00Z](https://site.test/scans/r0) · 1m 00s'),
        ('edit', '1.0', 'status\nThis month: 1 runs' + card_line(CARD1, '11/01 08:00Z')),
    ]
    m.run([row(1, 'running', finished_ts=None), row(0)], T0 + 86400 + 10)
    assert thread(m.slack) == [
        ('post', '3.0', '1.0', week('11/02'), '⏳ :alarm_clock: [11/02 08:00Z](https://site.test/scans/r1) · running', False),
        ('edit', '1.0', 'status\nThis month: 2 runs · 1 running' + card_line(CARD1, '11/02 08:00Z')),
    ]
    writes = []
    N.link_threads(m.saved, m.slack, writes.append, m.save)
    assert writes == [[('r0', 'https://slack.test/p2.0'), ('r1', 'https://slack.test/p3.0')]]
    m.run([F1, row(0)], T0 + 86400 + 120)
    assert thread(m.slack) == [
        ('post', '4.0', '1.0', {'username': '11/02 08:00Z · 1m 00s · failed', 'icon_emoji': ':x:'},
         ':alarm_clock: [record](https://site.test/scans/r1) · failed in index: boom', True),
        ('edit', '3.0', '❌ :alarm_clock: [11/02 08:00Z](https://site.test/scans/r1) · 1m 00s · failed in index: boom · [reply](https://slack.test/p4.0)'),
        ('edit', '1.0', 'status\nThis month: 2 runs' + card_line(CARD1, '11/02 08:00Z')),
    ]
    assert m.saved == {
        'namespace': MONTHLY.namespace, 'since': T0 + 10, 'posted': {'r0': '2.0', 'r1': '4.0'}, 'linked': {'r0': '2.0', 'r1': '3.0'},
        'months': {'2026-11': {
            'op_ts': '1.0', 'op_url': 'https://slack.test/p1.0', 'op_text': 'status\nThis month: 2 runs', 'op_card': True,
            'prev_url': None, 'next_url': None,
            'weeks': {'2026-W44': '2.0', '2026-W45': '3.0'},
            'week_bodies': {'2026-W44': '✅ :alarm_clock: [11/01 08:00Z](https://site.test/scans/r0) · 1m 00s',
                            '2026-W45': '❌ :alarm_clock: [11/02 08:00Z](https://site.test/scans/r1) · 1m 00s · failed in index: boom · [reply](https://slack.test/p4.0)'},
            'failures': {'r1': '4.0'}, 'failure_urls': {'r1': 'https://slack.test/p4.0'},
        }},
    }
    # the failed run's link moves from its week's reply to its own
    N.link_threads(m.saved, m.slack, writes.append, m.save)
    assert writes[1:] == [[('r1', 'https://slack.test/p4.0')]]
    # nothing new: no Slack writes
    m.run([F1, row(0)], T0 + 86400 + 120)
    assert thread(m.slack) == []


def test_a_month_posts_lost_response_is_recovered_not_posted_again():
    """A failure reply Slack accepted but whose response was lost is found by its intent on the next report, not posted
    again; that report then links it from its week's reply."""
    m = Monthly()
    m.run([row(1, 'running', finished_ts=None), row(0)], T0 + 86400 + 10)
    m.slack.calls.clear()
    m.slack.timeout = True
    with pytest.raises(TimeoutError, match='response lost after Slack accepted the post'):
        m.run([F1, row(0)], T0 + 86400 + 120)
    assert m.saved['pending']['key'] == 'fail:2026-11:r1'
    m.run([F1, row(0)], T0 + 86400 + 120)
    assert [c[:2] for c in m.slack.calls] == [('post', '4.0'), ('edit', '3.0'), ('edit', '1.0')]
    assert (m.saved['months']['2026-11']['failures'], m.slack.messages_by_id['4.0']['broadcast'], 'pending' in m.saved) == ({'r1': '4.0'}, True, False)


def test_a_replys_sections_are_expanded_so_slack_shows_every_line():
    """Slack folds a section after about five lines ("Show more"); each of a reply's sections is `expand`ed."""
    lines = [f'line {i:02d} ' + 'x' * 60 for i in range(48)]
    blocks = N.expanded_sections('\n'.join(lines))
    assert blocks == [
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': '\n'.join(lines[:43])}, 'expand': True},
        {'type': 'section', 'text': {'type': 'mrkdwn', 'text': '\n'.join(lines[43:])}, 'expand': True},
    ]


def test_a_section_messages_text_fallback_is_its_first_lines_under_chat_updates_limit():
    """`chat.update` refuses a `text` near 4,000 characters (`msg_too_long`: gcs's W47 reply, 30 runs and 4 failure
    links, 2026-11-26); the blocks carry the whole body, the `text` fallback its lines up to `FALLBACK_MAX`, then `…`."""
    lines = [f'line {i:02d} ' + 'x' * 60 for i in range(60)]  # 68 characters a line, 4,139 in all
    wire = '\n'.join(lines)
    assert [N.fallback_text(wire), N.fallback_text(wire[:3000]), N.fallback_text('y' * 3001)] == [
        '\n'.join(lines[:43]) + '\n…', wire[:3000], 'y' * 2998 + '\n…']
    calls = []
    slack = N.Slack.__new__(N.Slack)
    slack._have = None  # the workspace unknown: `have` reads it before asking Slack
    slack.client = SimpleNamespace(channel='C123', _request=lambda api, args: calls.append((api, args)) or {'ts': '9.0'})
    slack.edit_sections('2.0', wire)
    [(api, args)] = calls
    assert (api, args['ts'], args['text'], [b['text']['text'] for b in args['blocks']]) == (
        'chat.update', '2.0', '\n'.join(lines[:43]) + '\n…', ['\n'.join(lines[:43]), '\n'.join(lines[43:])])


def test_a_weeks_runs_reply_past_a_sections_limit_is_one_message_of_several_sections():
    """cw's 4 runs a day pass a `section`'s 3,000 characters within a week: one reply of lines, which thrds'
    `section_blocks(lines=True)` splits between lines into several sections of the one message."""
    runs = [row(1, run_id=f'h{k:02d}', started_ts=T0 + 86400 + 3600 * k, finished_ts=T0 + 86400 + 3600 * k + 60) for k in range(48)]
    lines = [N.reply(MONTHLY, r) for r in runs]
    # hourly from 11/02 08:00Z; 68 characters a line
    assert (lines[0], {len(line) for line in lines}) == ('✅ :alarm_clock: [11/02 08:00Z](https://site.test/scans/h00) · 1m 00s', {68})
    m = Monthly()
    # 30 lines (2,069 characters): one section
    m.run(runs[29::-1], T0 + 86400 + 3600 * 29 + 120)
    assert [(c[0], c[1]) for c in m.slack.calls] == [('post', '1.0'), ('post', '2.0')]
    assert m.slack.messages_by_id['2.0']['sections'] == ['\n'.join(lines[:30])]
    m.slack.calls.clear()
    # 48 (3,311): still lines; 43 fit a section (43 × 68 + 42 = 2,966), the other 5 start another
    m.run(runs[::-1], T0 + 86400 + 3600 * 47 + 120)
    assert thread(m.slack) == [
        ('edit', '2.0', '\n'.join(lines)),
        ('edit', '1.0', 'status\nThis month: 48 runs' + card_line(CARD1, '11/04 07:00Z')),
    ]
    assert m.slack.messages_by_id['2.0']['sections'] == ['\n'.join(lines[:43]), '\n'.join(lines[43:])]
    m.run(runs[::-1], T0 + 86400 + 3600 * 47 + 120)
    assert thread(m.slack) == []


def test_the_next_months_first_report_opens_it_closes_the_last_and_links_both_ways():
    """December's first run opens its thread linking November's; November, past its end with no run going, is closed:
    its OP rendered once more by the site (no status line, no card) linking December, then frozen. The pin moves."""
    m = Monthly()
    m.run([row(0)], T0 + 120)
    N.pin_month(m.saved, m.slack, m.save)
    thread(m.slack)
    d0 = row(30, 'running', finished_ts=None)
    m.site.calls.clear()
    m.run([d0, row(0)], T0 + 30 * 86400 + 10)
    assert thread(m.slack) == [
        ('post', '3.0', None, RUNS, 'status\nThis month: 1 runs · 1 running\n[← November](https://slack.test/p1.0)' + card_line(CARD1, '12/01 08:00Z'), False),
        ('edit', '1.0', 'November: 1 runs\n[December →](https://slack.test/p3.0)'),
        ('post', '4.0', '3.0', week('11/30'), '⏳ :alarm_clock: [12/01 08:00Z](https://site.test/scans/r30) · running', False),
    ]
    assert m.site.calls == [('2026-12', 'https://slack.test/p1.0', None), ('2026-11', None, 'https://slack.test/p3.0'),
                            ('2026-12', 'https://slack.test/p1.0', None)]
    assert m.saved['months']['2026-11'] == {
        'op_ts': '1.0', 'op_url': 'https://slack.test/p1.0', 'op_text': 'November: 1 runs\n[December →](https://slack.test/p3.0)', 'op_card': False,
        'prev_url': None, 'next_url': 'https://slack.test/p3.0', 'closed': True,
        'weeks': {'2026-W44': '2.0'}, 'week_bodies': {'2026-W44': '✅ :alarm_clock: [11/01 08:00Z](https://site.test/scans/r0) · 1m 00s'},
        'failures': {}, 'failure_urls': {},
    }
    assert m.saved['posted'] == {'r0': '2.0', 'r30': '4.0'}
    N.pin_month(m.saved, m.slack, m.save)
    assert (thread(m.slack), m.slack.pinned, m.saved['pinned']) == ([('join',), ('pin', '3.0'), ('unpin', '1.0')], ['3.0'], '3.0')
    # a closed month is never asked for again
    m.site.calls.clear()
    m.run([d0, row(0)], T0 + 30 * 86400 + 10)
    N.pin_month(m.saved, m.slack, m.save)
    assert (thread(m.slack), m.site.calls, m.saved['posted']) == ([], [('2026-12', 'https://slack.test/p1.0', None)], {'r30': '4.0'})


def test_a_month_closes_once_its_last_run_ends_and_is_relinked_when_the_next_opens():
    """A run going at the month's end keeps it open (the site draws the closed month, the run still going); its end
    closes it. December's first report then edits only November's link line: a closed OP is never rendered again."""
    m = Monthly()
    r29 = row(29, 'running', finished_ts=None)
    m.run([r29], T0 + 29 * 86400 + 10)
    assert [(c[0], c[1]) for c in m.slack.calls] == [('post', '1.0'), ('post', '2.0')]
    m.slack.calls.clear()
    m.run([r29], END + 600)
    assert (thread(m.slack), m.saved['months']['2026-11'].get('closed')) == ([('edit', '1.0', 'November: 1 runs · 1 running')], None)
    done = row(29, finished_ts=END + 2400)
    m.run([done], END + 2410)
    assert thread(m.slack) == [
        ('edit', '2.0', '✅ :alarm_clock: [11/30 08:00Z](https://site.test/scans/r29) · 1m 00s'),
        ('edit', '1.0', 'November: 1 runs'),
    ]
    assert m.saved['months']['2026-11']['closed'] is True
    m.site.calls.clear()
    d0 = row(30, 'running', finished_ts=None)
    m.run([d0, done], T0 + 30 * 86400 + 10)
    assert thread(m.slack) == [
        ('post', '3.0', None, RUNS, 'status\nThis month: 1 runs · 1 running\n[← November](https://slack.test/p1.0)' + card_line(CARD1, '12/01 08:00Z'), False),
        ('post', '4.0', '3.0', week('11/30'), '⏳ :alarm_clock: [12/01 08:00Z](https://site.test/scans/r30) · running', False),
        ('edit', '1.0', 'November: 1 runs\n[December →](https://slack.test/p3.0)'),
    ]
    assert m.site.calls == [('2026-12', 'https://slack.test/p1.0', None), ('2026-12', 'https://slack.test/p1.0', None)]
    assert (m.saved['months']['2026-11']['next_url'], m.saved['months']['2026-11']['op_text']) == (
        'https://slack.test/p3.0', 'November: 1 runs\n[December →](https://slack.test/p3.0)')
    m.run([d0, done], T0 + 30 * 86400 + 10)
    assert thread(m.slack) == []
    assert N.relink_op('November: 1 runs\n[← October](https://slack.test/p0.0)', '2026-11', 'https://slack.test/p0.0', 'https://slack.test/p3.0') == (
        'November: 1 runs\n[← October](https://slack.test/p0.0) · [December →](https://slack.test/p3.0)')


def test_the_cutover_from_a_quiet_thread_links_it_seeds_the_month_and_repoints_its_runs():
    """A deployment's first monthly report: the month thread opens with this month's runs so far (a failure already
    reported in the status threads gets its reply, not sent to the channel again); the open status thread's head gets a
    last edit linking it, its rolling reply loses the runs going now; the pin moves; each run's link moves to its line's
    reply."""
    w = quiet_running_writer()
    writes = []
    N.link_threads(w.saved, w.slack, writes.append, w.save)
    assert writes == [[('r1', 'https://slack.test/p2.0'), ('r2', 'https://slack.test/p2.0')]]
    head = w.saved['parent_body']
    assert head == QUIET_HEAD.replace('Latest 2 runs', 'Running now: [11/04 08:00Z](https://site.test/scans/r3).\n\nLatest 2 runs')
    m = Monthly(slack=as_pins(w.slack, '1.0'), saved={**w.saved, 'pinned': '1.0'})
    rows = [row(3, 'running', finished_ts=None, **ADA), *history(2)]
    m.run(rows, T0 + 3 * 86400 + 10)
    assert thread(m.slack) == [
        ('post', '3.0', None, RUNS, 'status\nThis month: 4 runs · 1 running' + card_line(CARD1, '11/04 08:00Z'), False),
        ('edit', '1.0', head + '\n\nRuns now report in the monthly thread: [November 2026](https://slack.test/p3.0).'),
        ('edit', '2.0', TRAIL),
        # time order: each week's runs reply, then its failures' replies, then the week's reply linking them
        ('post', '4.0', '3.0', week('10/26'), '❌ :alarm_clock: [11/01 08:00Z](https://site.test/scans/r0) · 1m 00s · failed', False),
        ('post', '5.0', '3.0', {'username': '11/01 08:00Z · 1m 00s · failed', 'icon_emoji': ':x:'}, ':alarm_clock: [record](https://site.test/scans/r0)', False),
        ('edit', '4.0', '❌ :alarm_clock: [11/01 08:00Z](https://site.test/scans/r0) · 1m 00s · failed · [reply](https://slack.test/p5.0)'),
        ('post', '6.0', '3.0', week('11/02'), f'{TRAIL}\n{R3_RUNNING}', False),
    ]
    assert (sorted(m.saved), m.saved['status_thread'], m.saved['posted']) == (
        ['linked', 'months', 'namespace', 'pinned', 'posted', 'since', 'status_thread'], '1.0',
        {'r0': '5.0', 'r1': '6.0', 'r2': '6.0', 'r3': '6.0'})
    N.pin_month(m.saved, m.slack, m.save)
    assert (thread(m.slack), m.slack.pinned) == ([('join',), ('pin', '3.0'), ('unpin', '1.0')], ['3.0'])
    N.link_threads(m.saved, m.slack, writes.append, m.save)
    assert writes[1:] == [[('r0', 'https://slack.test/p5.0'), ('r1', 'https://slack.test/p6.0'), ('r2', 'https://slack.test/p6.0'),
                           ('r3', 'https://slack.test/p6.0')]]
    m.run(rows, T0 + 3 * 86400 + 10)
    N.pin_month(m.saved, m.slack, m.save)
    assert thread(m.slack) == []


def test_the_cutover_from_a_failure_thread_finishes_it_and_a_new_failure_folds_its_alerts_into_its_reply():
    """The failure thread's head links the month thread and its ⏳ reply goes (the week's runs reply lists the run); each
    of the month's failures gets its reply. A failure after the cutover is news: its reply carries its step alerts
    (each left a pointer to it) and is also sent to the channel."""
    w = red_writer()
    w.run([row(4, 'running', finished_ts=None), F3, *history(2)])
    assert w.saved['running'] == {'r4': {'ts': '5.0', 'parent': '3.0'}}
    head = w.saved['parent_body']
    w.slack.calls.clear()
    m = Monthly(slack=w.slack, saved=w.saved)
    m.run([row(4, 'running', finished_ts=None), F3, *history(2)], T0 + 4 * 86400 + 10)
    f3 = '❌ :ada: [11/04 08:00Z](https://site.test/scans/r3) · 1m 00s · failed in index: boom'
    r0 = '❌ :alarm_clock: [11/01 08:00Z](https://site.test/scans/r0) · 1m 00s · failed'
    r4 = '⏳ :alarm_clock: [11/05 08:00Z](https://site.test/scans/r4) · running'
    assert thread(m.slack) == [
        ('post', '6.0', None, RUNS, 'status\nThis month: 5 runs · 1 running' + card_line(CARD1, '11/05 08:00Z'), False),
        ('edit', '3.0', head + '\n\nRuns now report in the monthly thread: [November 2026](https://slack.test/p6.0).'),
        ('delete', '5.0', None),
        ('post', '7.0', '6.0', week('10/26'), r0, False),
        ('post', '8.0', '6.0', {'username': '11/01 08:00Z · 1m 00s · failed', 'icon_emoji': ':x:'}, ':alarm_clock: [record](https://site.test/scans/r0)', False),
        ('edit', '7.0', r0 + ' · [reply](https://slack.test/p8.0)'),
        ('post', '9.0', '6.0', week('11/02'), f'{TRAIL}\n{f3}\n{r4}', False),
        ('post', '10.0', '6.0', {'username': '11/04 08:00Z · 1m 00s · failed', 'icon_emoji': ':x:'}, ':ada: [record](https://site.test/scans/r3) · failed in index: boom', False),
        ('edit', '9.0', f'{TRAIL}\n{f3} · [reply](https://slack.test/p10.0)\n{r4}'),
    ]
    f3 += ' · [reply](https://slack.test/p10.0)'
    assert (sorted(m.saved), m.saved['status_thread'], m.saved['posted']) == (
        ['months', 'namespace', 'posted', 'since', 'status_thread'], '3.0', {'r0': '8.0', 'r1': '9.0', 'r2': '9.0', 'r3': '10.0', 'r4': '9.0'})
    link = 'https://site.test/scans/r4'
    alert = m.slack.top_alert(link, f'*⚠️ Test scans 2026-11-05T0800*\nprobe failed (exit 1): 4 failed\n<https://logs.test/u2|task logs> · <{link}|scan>')
    assert alert == '11.0'
    r4 = row(4, issues=[{'phase': 'probe'}])
    m.run([r4, F3, *history(2)], T0 + 4 * 86400 + 120)
    assert thread(m.slack) == [
        ('post', '12.0', '6.0', {'username': '11/05 08:00Z · 1m 00s · phase issues', 'icon_emoji': ':warning:'},
         ':alarm_clock: [record](https://site.test/scans/r4) · phase issues: probe\nprobe failed (exit 1): 4 failed · [task logs](https://logs.test/u2)', True),
        ('edit', '11.0', '↪ ⚠️ Test scans 2026-11-05T0800: in the [month thread](https://slack.test/p12.0)'),
        ('edit', '9.0', f'{TRAIL}\n{f3}\n⚠️ :alarm_clock: [11/05 08:00Z](https://site.test/scans/r4) · 1m 00s · phase issues: probe · [reply](https://slack.test/p12.0)'),
        ('edit', '6.0', 'status\nThis month: 5 runs' + card_line(CARD1, '11/05 08:00Z')),
    ]
    m.run([r4, F3, *history(2)], T0 + 4 * 86400 + 120)
    assert thread(m.slack) == []


def test_the_month_op_is_asked_of_the_site_as_the_job_and_its_card_taken_only_as_the_sites(monkeypatch):
    import json

    from dt_cloud import site

    asked = []
    answers = iter([{'text': 'T', 'card_url': CARD1}, {'text': 'T', 'card_url': 'https://evil.test/og/deploy.png?v=1'},
                    {'text': 'C', 'card_url': None}, {'text': 'T', 'card_url': CARD1}])

    def post_json(url, token, path, body):
        asked.append((url, token, path, body))
        return json.dumps(next(answers)).encode()
    monkeypatch.setattr(site, 'post_json', post_json)
    cfg = replace(MONTHLY, store='meta', color='#12ab34', emoji=(('clean', '🟢'),))
    call = N.site_month_op(cfg, 'grant')
    assert [call('2026-11', None, None), call('2026-11', 'https://slack.test/p1.0', None), call('2026-10', None, 'https://slack.test/p3.0'),
            N.site_month_op(replace(MONTHLY, card=False), 'grant')('2026-11', None, None)] == [('T', CARD1), ('T', None), ('C', None), ('T', None)]
    asks = {'color': '#12ab34', 'emoji': {'clean': '🟢'}}
    assert asked == [
        ('https://site.test', 'grant', '/api/scan-runs/month-op?store=meta', {'month': '2026-11', 'prev_url': None, 'next_url': None, **asks}),
        ('https://site.test', 'grant', '/api/scan-runs/month-op?store=meta', {'month': '2026-11', 'prev_url': 'https://slack.test/p1.0', 'next_url': None, **asks}),
        ('https://site.test', 'grant', '/api/scan-runs/month-op?store=meta', {'month': '2026-10', 'prev_url': None, 'next_url': 'https://slack.test/p3.0', **asks}),
        ('https://site.test', 'grant', '/api/scan-runs/month-op?store=primary', {'month': '2026-11', 'prev_url': None, 'next_url': None}),
    ]


def test_a_monthly_preview_shows_what_the_month_would_post_and_a_report_converges_and_links_it(tmp_path, monkeypatch):
    import json

    from dt_cloud import probe, site

    cfg = replace(MONTHLY, state=str(tmp_path / 'state'))
    monkeypatch.setattr(probe, 'http_fetch', lambda *args: lambda path: SimpleNamespace(status=200, body=json.dumps(dict(configured=True, runs=[F1, row(0)]))))
    monkeypatch.setattr(site, 'post_json', lambda *args: json.dumps({'text': 'status\nThis month: 2 runs', 'card_url': CARD1}).encode())
    slack = Pins()
    monkeypatch.setattr(N, 'Slack', lambda *args: slack)
    assert N.run(asdict(cfg), {'SITE_TOKEN': 'viewer'}, 'project') == '\n\n+++\n\n'.join([
        '[Test scans · runs]\nstatus\nThis month: 2 runs' + card_line(CARD1, '11/02 08:00Z'),
        '[wk of 10/26 · runs]\n✅ :alarm_clock: [11/01 08:00Z](https://site.test/scans/r0) · 1m 00s',
        '[wk of 11/02 · runs]\n❌ :alarm_clock: [11/02 08:00Z](https://site.test/scans/r1) · 1m 00s · failed in index: boom',
        '[:x: 11/02 08:00Z · 1m 00s · failed]\n:alarm_clock: [record](https://site.test/scans/r1) · failed in index: boom',
    ])
    assert (sorted(tmp_path.iterdir()), slack.calls) == ([], [])
    env = {'SITE_TOKEN': 'viewer', 'SLACK_BOT_TOKEN': 'bot'}
    sink = SimpleNamespace(statements=[])
    sink.execute = sink.statements.extend
    assert N.run(asdict(cfg), env, 'project', 'r1', execute=True, sink=sink) == 'nominal: month 2026-11 thread 1.0; 2 weekly, 1 failure replies'
    # a first report seeds the month in time order: each week's runs reply, then its failure's reply, then the week's
    # reply edited to link it
    assert [(c[0], c[1]) for c in slack.calls] == [('post', '1.0'), ('post', '2.0'), ('post', '3.0'), ('post', '4.0'), ('edit', '3.0')]
    assert sink.statements == [
        "UPDATE scan_runs SET thread_url = 'https://slack.test/p2.0' WHERE run_id = 'r0'",
        "UPDATE scan_runs SET thread_url = 'https://slack.test/p4.0' WHERE run_id = 'r1'",
    ]
    slack.calls.clear()
    assert N.run(asdict(cfg), env, 'project', 'r1', execute=True, sink=sink) == 'nominal: month 2026-11 thread 1.0; 2 weekly, 1 failure replies'
    assert (slack.calls, len(sink.statements)) == ([], 2)


@pytest.mark.parametrize('failures, expected', [({'r9': '4.0'}, []), ({}, [('fail', 'run failed (exit 1)')])])
def test_a_month_threads_failure_reply_takes_the_runs_alert_whatever_the_grouping(tmp_path, monkeypatch, failures, expected):
    job, posted = deferring_job(tmp_path, 'clean', monkeypatch)
    job.profile.nominal = asdict(replace(CFG, group='clean', monthly=True))
    job.alert('fail', 'run failed (exit 1)')
    assert (posted, job.state()['deferred_alerts']) == ([], ['run failed (exit 1)'])

    def fake_run(*args, final, **kwargs):
        final.append({'months': {'2026-11': {'failures': failures}}})
        return 'nominal: ok'
    monkeypatch.setattr(N, 'run', fake_run)
    N.report_job(job)
    assert posted == expected
