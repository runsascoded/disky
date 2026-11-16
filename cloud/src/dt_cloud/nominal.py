"""Nominal scan threads, after the authoritative run has finished (specs/done/nominal-run-threads.md).

The site's run summaries are the outcome oracle. A renewed object-generation lease serializes writers; a
persisted send intent and owned Slack message metadata recover a post whose response/state write was lost.
Existing threads can be adopted without changing or deleting their replies. Nothing posts unless configured.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from urllib.parse import quote, unquote, urlencode, urlparse
from uuid import NAMESPACE_URL, uuid5

from .scan_runs import TRIGGER_BY_RE

if TYPE_CHECKING:
    from .scan_runs import JobRun


GROUPS = ('clean', 'color', 'signature')


@dataclass(frozen=True)
class Config:
    state: str
    site: str
    channel: str
    title: str
    store: str = "primary"
    kind: str = "scan"
    adopt_parent: str | None = None
    site_token_secret: str | None = None
    #: How runs group into threads (`GROUPS`): `clean` threads only clean runs, a failure closing the thread (its alert
    #: posts top-level); `color` also threads consecutive failed runs; `signature` threads consecutive failed runs only
    #: while they fail the same way (`signature`: the failed phase, or the phases with issues). Under `color` and
    #: `signature` the thread is the failure's report: a run's `fail` alerts defer to it (`defers_failures`), and post
    #: top-level only if the run isn't a reply in a failure thread once the report has run.
    group: str = "clean"

    def __post_init__(self) -> None:
        if not self.site.startswith("https://") or urlparse(self.site).query or urlparse(self.site).fragment:
            raise ValueError("nominal.site: an HTTPS site URL without query or fragment")
        if not re.fullmatch(r"[CG][A-Z0-9]+", self.channel):
            raise ValueError("nominal.channel: a Slack channel ID")
        if "://" in self.state and not self.state.startswith("gs://"):
            raise ValueError("nominal.state: a GCS URL or local directory (generation lease required)")
        if not self.state or not self.title or not self.store or not self.kind:
            raise ValueError("nominal: state, title, store and kind must be nonempty")
        if self.group not in GROUPS:
            raise ValueError(f"nominal.group: one of {', '.join(GROUPS)}")

    @property
    def namespace(self) -> str:
        return f"{self.site.rstrip('/')}|{self.store}|{self.kind}|{self.channel}"

    @property
    def scans(self) -> str:
        return self.site.rstrip('/') + (f"/{quote(self.store, safe='')}" if self.store != "primary" else "") + "/scans"

    def link(self, run: dict) -> str:
        return f"{self.scans}/{quote(run['run_id'], safe='')}"


def config(raw: Mapping) -> Config:
    return Config(**dict(raw))


def ordered(doc: dict, cfg: Config) -> list[dict]:
    if doc.get("configured") is not True:
        raise ValueError("nominal: the run ledger is not configured")
    rows = [r for r in doc["runs"] if r["run"]["store"] == cfg.store and r["run"]["kind"] == cfg.kind and not r["run"]["parent"]]
    if len({r['run']['run_id'] for r in rows}) != len(rows):
        raise ValueError("nominal: duplicate run IDs")
    for row in rows:
        if "phase_issues" not in row or not isinstance(row["phase_issues"], list):
            raise ValueError("nominal: missing phase-issue evidence")
        if row['run']['started_ts'] is None:
            raise ValueError("nominal: a run has no start time")
    return sorted(rows, key=lambda r: (r['run']['started_ts'], r['run']['run_id']), reverse=True)


def clean(row: dict) -> bool:
    # A later recovery does not rewrite a historically partial run into a clean run.
    return row['run']['status'] == 'succeeded' and row['run']['finished_ts'] is not None and not row['phase_issues']


def streak(rows: list[dict]) -> tuple[list[dict], dict | None]:
    result: list[dict] = []
    for row in rows:
        if not clean(row):
            return result, row
        result.append(row)
    return result, None


def signature(row: dict) -> str:
    """How a non-clean run failed, for `signature` grouping: the phase it failed in (`failed:<phase>`), else the phases
    its issues name (`issues:<a>,<b>`), else its status. Error text is left out: it carries times, ids and sizes that
    differ between runs failing the same way."""
    run = row['run']
    if run['status'] == 'failed':
        return f"failed:{run.get('failed_phase') or ''}"
    if row['phase_issues']:
        return 'issues:' + ','.join(sorted({str(i.get('phase')) for i in row['phase_issues']}))
    return run['status']


def key(cfg: Config, row: dict) -> str | None:
    """A run's thread key: `clean`, the failure thread it joins, or None (no thread: `clean` grouping's failures)."""
    if clean(row):
        return 'clean'
    return None if cfg.group == 'clean' else 'fail' if cfg.group == 'color' else 'fail:' + signature(row)


def segment(cfg: Config, rows: list[dict]) -> tuple[str | None, list[dict], dict | None]:
    """The newest runs sharing the head's thread key, and the run before them (None: the history's start)."""
    head = key(cfg, rows[0]) if rows else None
    if head is None:
        return None, [], rows[0] if rows else None
    out: list[dict] = []
    for row in rows:
        if key(cfg, row) != head:
            return head, out, row
        out.append(row)
    return head, out, None


#: An error in a reply: one line, clipped, with nothing Slack reads as a link, mention or code span
_MARKUP = str.maketrans({'<': '‹', '>': '›', '`': "'", '\n': ' ', '\r': ' '})


def describe(row: dict) -> str:
    run = row['run']
    if run['status'] == 'failed':
        error = ' '.join(str(run.get('error') or '').translate(_MARKUP).split())
        error = error if len(error) <= 160 else error[:159] + '…'
        return 'failed' + (f" in {run['failed_phase']}" if run.get('failed_phase') else '') + (f": {error}" if error else '')
    if row['phase_issues']:
        return 'phase issues: ' + ', '.join(sorted({str(i.get('phase')) for i in row['phase_issues']}))
    return str(run['status'])


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ('' if n == 1 else 's')


def stamp(row: dict) -> str:
    return datetime.fromtimestamp(row['run']['started_ts'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


#: A reply's trigger, beside its ✅: the deployment's cron, a hand submit (the site's "Run a scan" or a CLI execute; who,
#: named after the duration when recorded: `scan_runs.trigger_by`), or a deployment that doesn't state its triggers. The
#: parent's run mix spells them out.
#: A manual run's starter's own Slack emoji (`trigger_emoji`: `/api/scan-runs?emoji=1` asks OA's broker) replaces 👤
SLACK_EMOJI_RE = re.compile(r":[a-z0-9_+'-]{1,100}:")
TRIGGER_EMOJI = {'schedule': ':alarm_clock:', 'manual': ':bust_in_silhouette:', None: ':grey_question:'}


def reply(cfg: Config, row: dict) -> str:
    """A run's line in its thread: ✅ or ❌, the trigger, its time linked to its record, the duration, who started it
    (a manual run that recorded it), and a failed run's reason."""
    seconds = row.get('secs')
    duration = 'duration unknown' if seconds is None else f"{int(seconds) // 60}m {int(seconds) % 60:02d}s"
    by = row['run'].get('trigger_by') if row['run']['trigger'] == 'manual' else None
    by = by if by and TRIGGER_BY_RE.fullmatch(by) else None  # never markup into mrkdwn, whatever a row holds
    face = row['run'].get('trigger_emoji') if by else None
    emoji = face if face and SLACK_EMOJI_RE.fullmatch(face) else TRIGGER_EMOJI[row['run']['trigger']]
    ok = clean(row)
    return (f"{'✅' if ok else '❌'} {emoji} [{stamp(row)}]({cfg.link(row['run'])}) · {duration}" + (f" · {by}" if by else "")
            + ('' if ok else f" · {describe(row)}"))


def parent(cfg: Config, good: list[dict], boundary: dict | None) -> str:
    """The thread's head: a clean thread's streak, or a failure thread's (`failed`)."""
    if good and not clean(good[0]):
        return failed(cfg, good, boundary)
    window = good[:10]
    counts = {k: sum(r['run']['trigger'] == k for r in window) for k in ('schedule', 'manual', None)}
    mix = f"{counts['schedule']} scheduled / {counts['manual']} manual" + (f" / {counts[None]} unknown" if counts[None] else '')
    since = stamp(good[-1])
    qualifier = '' if boundary else 'at least '
    after = f" after [{boundary['run']['scan']}]({cfg.link(boundary['run'])})" if boundary else ' (available run history)'
    return (f"✅ **{cfg.title}: {qualifier}{len(good)} consecutive clean run{'' if len(good) == 1 else 's'}**\n\n"
            f"Since {since}{after}.\n\n"
            f"Latest completed: [{stamp(good[0])}]({cfg.link(good[0]['run'])}).\n\n"
            f"Latest {plural(len(window), 'run')}: {mix}. Each reply links its scan record. [All scans]({cfg.scans}).")


def failed(cfg: Config, bad: list[dict], boundary: dict | None) -> str:
    """A failure thread's head: how many runs in a row failed (under `signature`, failed the same way), since when and
    after which run (the last clean one, or the last that failed differently), and the newest failure's reason."""
    qualifier = '' if boundary else 'at least '
    how = ''
    if cfg.group == 'signature':
        run = bad[0]['run']
        how = (f" in {run['failed_phase']}" if run['status'] == 'failed' and run.get('failed_phase') else
               ' with phase issues' if run['status'] != 'failed' and bad[0]['phase_issues'] else '')
    after = (f" after [{boundary['run']['scan']}]({cfg.link(boundary['run'])})" + (' (clean)' if clean(boundary) else f" ({describe(boundary)})")
             if boundary else ' (available run history)')
    return (f"❌ **{cfg.title}: {qualifier}{len(bad)} consecutive failed run{'' if len(bad) == 1 else 's'}{how}**\n\n"
            f"Since {stamp(bad[-1])}{after}.\n\n"
            f"Latest: [{stamp(bad[0])}]({cfg.link(bad[0]['run'])}): {describe(bad[0])}.\n\n"
            f"Each reply links its scan record. [All scans]({cfg.scans}).")


def missing(good: list[dict], posted: dict) -> list[dict]:
    known = next((i for i, row in enumerate(good) if row['run']['run_id'] in posted), None)
    return good[:known] if known is not None else good[:10]


class Transport(Protocol):
    def messages(self, parent: str) -> list[dict]: ...
    def owns(self, message: dict) -> bool: ...
    def find(self, operation: str, parent: str | None, since: float) -> str | None: ...
    def post(self, body: str, parent: str | None, operation: str) -> str: ...
    def edit(self, ts: str, body: str) -> None: ...


class Slack:
    """Use thrds for formatting, sends, edits and transport retries; paginate recovery reads explicitly."""

    def __init__(self, token: str, channel: str):
        from thrds import SlackClient

        self.client = SlackClient(token=token, channel=channel)

    def owns(self, message: dict) -> bool:
        return any(message.get(k) == v for k, v in zip(('user', 'bot_id'), self.client.bot_ids) if v)

    def _read(self, parent: str | None, since: float | None = None) -> list[dict]:
        args = {'channel': self.client.channel, 'limit': 100, 'include_all_metadata': True}
        if parent:
            args['ts'] = parent
        if since is not None:
            args['oldest'] = str(since)
        out = []
        while True:
            result = self.client._request('conversations.replies' if parent else 'conversations.history', args, method='GET')
            out.extend(result.get('messages', []))
            cursor = result.get('response_metadata', {}).get('next_cursor')
            if not cursor:
                return out
            args['cursor'] = cursor

    def messages(self, parent: str) -> list[dict]:
        return self._read(parent)

    def find(self, operation: str, parent: str | None, since: float) -> str | None:
        matches = [m['ts'] for m in self._read(parent, since) if self.owns(m) and
                   m.get('metadata', {}).get('event_payload', {}).get('operation') == operation]
        if len(matches) > 1:
            raise ValueError('nominal: multiple messages for one send intent')
        return matches[0] if matches else None

    def post(self, body: str, parent: str | None, operation: str) -> str:
        # thrds' own sync uses this metadata seam around post(). The payload contains no credentials.
        previous = self.client._metadata_by_content
        self.client._metadata_by_content = {body: {'event_type': 'disky_nominal', 'event_payload': {'operation': operation}}}
        try:
            return self.client.post(body, thread_id=parent).id
        finally:
            self.client._metadata_by_content = previous

    def edit(self, ts: str, body: str) -> None:
        self.client.edit(ts, body)


def adopt(cfg: Config, rows: list[dict], slack: Transport) -> dict:
    if not cfg.adopt_parent:
        raise ValueError('nominal: no parent configured for adoption')
    messages = slack.messages(cfg.adopt_parent)
    if not messages or messages[0]['ts'] != cfg.adopt_parent or not slack.owns(messages[0]):
        raise ValueError('nominal: adopted parent is missing or belongs to another sender')
    pattern = re.compile(re.escape(cfg.scans) + r'/([^>|)\s]+)')
    ids = {r['run']['run_id']: i for i, r in enumerate(rows)}
    latest_line = next((line for line in messages[0].get('text', '').splitlines() if re.match(r'Latest(?: completed)?:', line)), '')
    root = pattern.search(latest_line)
    if not root or unquote(root[1]) not in ids:
        raise ValueError('nominal: adopted parent has no covered run link')
    good, boundary = streak(rows[ids[unquote(root[1])]:])
    if not good:
        raise ValueError('nominal: adopted parent does not describe a clean streak')
    posted = {}
    for m in messages[1:]:
        match = pattern.search(m.get('text', ''))
        if not slack.owns(m) or not match:
            continue
        rid = unquote(match[1])
        if rid in posted:
            raise ValueError('nominal: duplicate owned run replies in adopted thread')
        posted[rid] = m['ts']
    return {'namespace': cfg.namespace, 'boundary': boundary['run']['run_id'] if boundary else None,
            'parent': cfg.adopt_parent, 'posted': posted}


def converge(
    cfg: Config,
    rows: list[dict],
    state: dict,
    slack: Transport,
    save: Callable[[dict], None],
    check: Callable[[], None],
    now: Callable[[], float] = time.time,
) -> dict:
    """Caller holds the generation lease. Persist every send intent and returned/recovered message ID."""
    if state.get('namespace') != cfg.namespace:
        raise ValueError('nominal: state belongs to another site/store/channel')

    def persist() -> None:
        check()
        save(state)

    def received(pending: dict, ts: str) -> None:
        if pending['key'] == 'parent':
            state.update(parent=ts, parent_body=pending['body'])
        else:
            state.setdefault('posted', {})[pending['key']] = ts
        state.pop('pending', None)
        state.pop('operator_required', None)
        persist()

    if pending := state.get('pending'):
        # An empty history page is not evidence that a timed-out send failed. Keep the intent until a receipt
        # is found; bounded polling ends in an explicit operator state, never an automatic second send. Later
        # invocations still check once, so eventual visibility can recover it without manual edits.
        attempts = 1 if state.get('operator_required') else 3
        for attempt in range(attempts):
            check()
            found = slack.find(pending['operation'], pending['parent'], pending['since'])
            if found:
                received(pending, found)
                break
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
        else:
            state['operator_required'] = pending['operation']
            persist()
            raise RuntimeError(f"nominal: send remains ambiguous ({pending['operation']}); inspect Slack and the persisted intent; no resend attempted")
    if not rows or rows[0]['run']['status'] == 'running' or rows[0]['run']['finished_ts'] is None:
        return state
    head, good, boundary = segment(cfg, rows)
    boundary_id = boundary['run']['run_id'] if boundary else None
    was = state.get('key', 'clean')
    if good and boundary is None and state.get('parent') and state.get('boundary') is not None:
        raise ValueError('nominal: run history no longer covers the recorded streak boundary')
    if state.get('parent') and (not good or was != head or state.get('boundary') != boundary_id):
        if not state.get('closed'):
            # What ended the thread: the run after its newest reply (else, as before replies existed, the boundary)
            newest = next((i for i, r in enumerate(rows) if r['run']['run_id'] in state.get('posted', {})), None)
            stop = rows[newest - 1] if newest else boundary or rows[0]
            if was == 'clean':
                reason = stop['run']['status'] if not stop['phase_issues'] else 'phase issues'
                body = f"**{cfg.title}: clean-run sequence ended**\n\n[{stamp(stop)}]({cfg.link(stop['run'])}): {reason}. [All scans]({cfg.scans})."
            else:
                reason = 'clean' if clean(stop) else describe(stop)
                body = f"**{cfg.title}: failure sequence ended**\n\n[{stamp(stop)}]({cfg.link(stop['run'])}): {reason}. [All scans]({cfg.scans})."
            check()
            slack.edit(state['parent'], body)
            state['closed'] = True
            persist()
        if good:
            state.clear()
            state.update(namespace=cfg.namespace, boundary=boundary_id, posted={})
            if head != 'clean':
                state['key'] = head
            persist()
    if not good:
        return state
    state['boundary'] = boundary_id
    if head == 'clean':
        state.pop('key', None)
    else:
        state['key'] = head
    # A clean thread keeps the operation ids it always had; a failure thread's name its key too
    scope = boundary_id if head == 'clean' else f'{head}|{boundary_id}'

    def send(key: str, body: str, root: str | None) -> None:
        operation = str(uuid5(NAMESPACE_URL, f'{cfg.namespace}|{scope}|{key}'))
        pending = {'key': key, 'body': body, 'parent': root, 'operation': operation, 'since': now() - 1}
        state['pending'] = pending
        persist()
        check()
        received(pending, slack.post(body, root, operation))

    body = parent(cfg, good, boundary)
    if not state.get('parent'):
        send('parent', body, None)
    # New threads bootstrap ten; established threads append every missing run after the newest known one.
    posted = state.setdefault('posted', {})
    for row in reversed(missing(good, posted)):
        send(row['run']['run_id'], reply(cfg, row), state['parent'])
    if state.get('parent_body') != body:
        check()
        slack.edit(state['parent'], body)
        state['parent_body'] = body
        persist()
    recent = {r['run']['run_id']: posted[r['run']['run_id']] for r in reversed(good[:10]) if r['run']['run_id'] in posted}
    if recent != posted:
        state['posted'] = recent
        persist()
    return state


def run(
    raw: Mapping,
    env: Mapping[str, str],
    project: str | None,
    run_id: str | None = None,
    execute: bool = False,
    adopt_check: bool = False,
    final: list[dict] | None = None,
) -> str:
    from .append_runner import GcsLeaseFile, LocalLeaseFile, coordinator_lease
    from .blobstore import Store
    from .probe import http_fetch

    cfg = config(raw)
    token = (env.get('SITE_TOKEN') or '').strip()
    if not token and cfg.site_token_secret:
        from .gcp import session
        from .secrets import access, parse_binding

        _, resource = parse_binding('SITE_TOKEN=' + cfg.site_token_secret, project or '')
        token = access(resource, session())
    if not token:
        raise ValueError('nominal: need SITE_TOKEN or site_token_secret')
    store = Store(cfg.state)
    prefix = str(uuid5(NAMESPACE_URL, cfg.namespace))
    key = f'{prefix}/state.json'
    url = urlparse(cfg.state)
    lease = GcsLeaseFile(url.netloc, f"{url.path.strip('/')}/{prefix}/coordinator.lease.json".lstrip('/')) if url.scheme else LocalLeaseFile(Path(cfg.state) / prefix / 'lease.json')

    def read_rows() -> list[dict]:
        response = http_fetch(cfg.site, token)('/api/scan-runs?' + urlencode({'store': cfg.store, 'emoji': '1'}))
        if response.status != 200:
            raise ValueError(f'nominal: run ledger HTTP {response.status}')
        rows = ordered(json.loads(response.body), cfg)
        if run_id and not any(r['run']['run_id'] == run_id and r['run']['finished_ts'] is not None and r['run']['status'] != 'running' for r in rows):
            raise ValueError('nominal: authoritative terminal run record is not visible')
        return rows

    if not execute:
        rows = read_rows()
        if not rows or rows[0]['run']['status'] == 'running' or rows[0]['run']['finished_ts'] is None:
            return 'nominal: waiting for a terminal run'
        head, good, boundary = segment(cfg, rows)
        if not good:
            return 'nominal: no clean streak'
        pending_rows = good[:10]
        if adopt_check:
            bot = (env.get('SLACK_BOT_TOKEN') or '').strip()
            if not bot:
                raise ValueError('nominal: adoption review needs SLACK_BOT_TOKEN (read only)')
            state = store.read_json(key) if store.exists(key) else adopt(cfg, rows, Slack(bot, cfg.channel)) if cfg.adopt_parent else {}
            if state and state.get('namespace') != cfg.namespace:
                raise ValueError('nominal: state belongs to another site/store/channel')
            if state.get('pending'):
                raise RuntimeError('nominal: pending send needs reconciliation before a publish plan can be rendered')
            same = state.get('boundary') == (boundary['run']['run_id'] if boundary else None) and state.get('key', 'clean') == head
            pending_rows = missing(good, state.get('posted', {})) if same else good[:10]
        return '\n\n+++\n\n'.join([parent(cfg, good, boundary), *(reply(cfg, r) for r in reversed(pending_rows))])
    bot = (env.get('SLACK_BOT_TOKEN') or '').strip()
    if not bot:
        raise ValueError('nominal: need SLACK_BOT_TOKEN')
    with coordinator_lease(lease) as check:
        rows = read_rows()  # Re-read after acquiring the lease, so a waiting writer cannot publish stale state.
        slack = Slack(bot, cfg.channel)
        state = store.read_json(key) if store.exists(key) else adopt(cfg, rows, slack) if cfg.adopt_parent else {'namespace': cfg.namespace}
        if state.get('namespace') != cfg.namespace:
            raise ValueError('nominal: state belongs to another site/store/channel')
        def save(value: dict) -> None:
            store.write_text(key, json.dumps(value, indent=2) + '\n')
        check()
        save(state)
        state = converge(cfg, rows, state, slack, save, check)
    if final is not None:
        final.append(state)
    return f"nominal: parent {state.get('parent', 'none')}; {len(state.get('posted', {}))} run replies"


def defers_failures(job: JobRun) -> bool:
    """Whether this run's `fail` alerts wait for its thread: a failure-grouping thread (`color`, `signature`) reports
    this deployment's top-level runs of its kind and store, and nothing suppresses the report."""
    if not job.profile.nominal or not job.recording or job.dry_run or (job.env.get('DT_ALERTS') or '').strip() in ('stderr', 'off', '0'):
        return False
    cfg = config(job.profile.nominal)
    facts = job.state()
    return (cfg.group != 'clean' and not facts.get('parent') and facts.get('kind', 'scan') == cfg.kind
            and facts.get('store', job.profile.store) == cfg.store)


def report_job(job: JobRun) -> None:
    """Opt-in finalizer: failure is logged/redacted, never substituted for the scan's exit status. A run's deferred
    `fail` alerts post top-level unless the run is now a reply in a failure thread."""
    if not job.profile.nominal or not job.recording or job.dry_run or (job.env.get('DT_ALERTS') or '').strip() in ('stderr', 'off', '0'):
        return
    from .scan_runs import err

    deferred = list(job.state().get('deferred_alerts') or [])
    threaded = False
    try:
        facts = job.state()
        cfg = config(job.profile.nominal)
        if facts.get('parent') or facts.get('kind', 'scan') != cfg.kind or facts.get('store', job.profile.store) != cfg.store:
            return
        final: list[dict] = []
        err(run(job.profile.nominal, job.env, job.profile.project, job.run_id, execute=True, final=final))
        state = final[0] if final else {}
        threaded = state.get('key', 'clean') != 'clean' and job.run_id in state.get('posted', {})
    except Exception as exc:  # Reporting must not fail the job; its failure remains visible in the head log.
        err(job.redact(f'nominal report failed: {type(exc).__name__}: {exc}'))
    finally:
        if deferred and not threaded:
            for message in deferred:
                job.post_alert('fail', message)
