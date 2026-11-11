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

if TYPE_CHECKING:
    from .scan_runs import JobRun


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

    def __post_init__(self) -> None:
        if not self.site.startswith("https://") or urlparse(self.site).query or urlparse(self.site).fragment:
            raise ValueError("nominal.site: an HTTPS site URL without query or fragment")
        if not re.fullmatch(r"[CG][A-Z0-9]+", self.channel):
            raise ValueError("nominal.channel: a Slack channel ID")
        if "://" in self.state and not self.state.startswith("gs://"):
            raise ValueError("nominal.state: a GCS URL or local directory (generation lease required)")
        if not self.state or not self.title or not self.store or not self.kind:
            raise ValueError("nominal: state, title, store and kind must be nonempty")

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


def stamp(row: dict) -> str:
    return datetime.fromtimestamp(row['run']['started_ts'], timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def reply(cfg: Config, row: dict) -> str:
    trigger = {'schedule': 'scheduled', 'manual': 'manual', None: 'trigger unknown'}[row['run']['trigger']]
    seconds = row.get('secs')
    duration = 'duration unknown' if seconds is None else f"{int(seconds) // 60}m {int(seconds) % 60:02d}s"
    return f"✅ [{stamp(row)}]({cfg.link(row['run'])}) · {trigger} · {duration} · no phase issues"


def parent(cfg: Config, good: list[dict], boundary: dict | None) -> str:
    window = good[:10]
    counts = {k: sum(r['run']['trigger'] == k for r in window) for k in ('schedule', 'manual', None)}
    mix = f"{counts['schedule']} scheduled / {counts['manual']} manual" + (f" / {counts[None]} unknown" if counts[None] else '')
    since = stamp(good[-1])
    qualifier = '' if boundary else 'at least '
    after = f" after [{boundary['run']['scan']}]({cfg.link(boundary['run'])})" if boundary else ' (available run history)'
    return (f"✅ **{cfg.title}: {qualifier}{len(good)} consecutive clean runs**\n\n"
            f"Since {since}{after}.\n\n"
            f"Latest completed: [{stamp(good[0])}]({cfg.link(good[0]['run'])}). No phase issues.\n\n"
            f"Latest {len(window)} runs: {mix}. Each reply links its scan record. [All scans]({cfg.scans}).")


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
    good, boundary = streak(rows)
    boundary_id = boundary['run']['run_id'] if boundary else None
    if good and boundary is None and state.get('parent') and state.get('boundary') is not None:
        raise ValueError('nominal: run history no longer covers the recorded streak boundary')
    if state.get('parent') and (not good or state.get('boundary') != boundary_id):
        if not state.get('closed'):
            stop = boundary or rows[0]
            reason = stop['run']['status'] if not stop['phase_issues'] else 'phase issues'
            body = f"**{cfg.title}: clean-run sequence ended**\n\n[{stamp(stop)}]({cfg.link(stop['run'])}): {reason}. [All scans]({cfg.scans})."
            check()
            slack.edit(state['parent'], body)
            state['closed'] = True
            persist()
        if good:
            state.clear()
            state.update(namespace=cfg.namespace, boundary=boundary_id, posted={})
            persist()
    if not good:
        return state
    state['boundary'] = boundary_id

    def send(key: str, body: str, root: str | None) -> None:
        operation = str(uuid5(NAMESPACE_URL, f'{cfg.namespace}|{boundary_id}|{key}'))
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
        response = http_fetch(cfg.site, token)('/api/scan-runs?' + urlencode({'store': cfg.store}))
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
        good, boundary = streak(rows)
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
            pending_rows = missing(good, state.get('posted', {})) if state.get('boundary') == (boundary['run']['run_id'] if boundary else None) else good[:10]
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
    return f"nominal: parent {state.get('parent', 'none')}; {len(state.get('posted', {}))} run replies"


def report_job(job: JobRun) -> None:
    """Opt-in finalizer: failure is logged/redacted, never substituted for the scan's exit status."""
    if not job.profile.nominal or not job.recording or job.dry_run or (job.env.get('DT_ALERTS') or '').strip() in ('stderr', 'off', '0'):
        return
    try:
        from .scan_runs import err

        facts = job.state()
        cfg = config(job.profile.nominal)
        if facts.get('parent') or facts.get('kind', 'scan') != cfg.kind or facts.get('store', job.profile.store) != cfg.store:
            return
        err(run(job.profile.nominal, job.env, job.profile.project, job.run_id, execute=True))
    except Exception as exc:  # Reporting must not fail the job; its failure remains visible in the head log.
        from .scan_runs import err

        err(job.redact(f'nominal report failed: {type(exc).__name__}: {exc}'))
