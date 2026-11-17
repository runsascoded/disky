"""Nominal scan threads (specs/done/nominal-run-threads.md): each run's reply, posted ⏳ when it starts and edited to
its outcome once the authoritative run record has finished.

The site's run summaries are the outcome oracle. A renewed object-generation lease serializes writers; a
persisted send intent and owned Slack message metadata recover a post whose response/state write was lost.
Existing threads can be adopted without changing or deleting their replies. Nothing posts unless configured.
"""
from __future__ import annotations

import json
import re
import sys
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
    #: The stores the head summarizes (`stores_text`), as `{label: store}`: each one's size, object count and change over
    #: the last week, linked to its page. Empty: no store lines.
    stores: tuple[tuple[str, str], ...] = ()
    #: Pin the open thread's head in the channel, unpinning the head it replaced (`pin_head`; the bot's `pins:write`,
    #: `channels:join`).
    pin: bool = False
    #: Keep the channel's canvas as the deployment's status page (`canvas_text`; the bot's `canvases:write`).
    canvas: bool = False

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
        if any(not label or not store for label, store in self.stores):
            raise ValueError("nominal.stores: nonempty labels and store names")

    @property
    def namespace(self) -> str:
        return f"{self.site.rstrip('/')}|{self.store}|{self.kind}|{self.channel}"

    @property
    def scans(self) -> str:
        return self.site.rstrip('/') + (f"/{quote(self.store, safe='')}" if self.store != "primary" else "") + "/scans"

    def link(self, run: dict) -> str:
        return f"{self.scans}/{quote(run['run_id'], safe='')}"

    def page(self, store: str) -> str:
        return self.site.rstrip('/') + ('/' if store == 'primary' else f"/{quote(store, safe='')}")


def config(raw: Mapping) -> Config:
    raw = dict(raw)
    if isinstance(raw.get('stores'), Mapping):
        raw['stores'] = tuple(raw['stores'].items())
    return Config(**raw)


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


def live(row: dict) -> bool:
    """A run still going: recorded running, or not yet finalized. Its outcome isn't known, so it never counts."""
    return row['run']['status'] == 'running' or row['run']['finished_ts'] is None


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
#: A manual run's starter's own Slack emoji (`trigger_emoji`: `/api/scan-runs?emoji=1` asks OA's broker) replaces 👤 and their address
SLACK_EMOJI_RE = re.compile(r":[a-z0-9_+'-]{1,100}:")
TRIGGER_EMOJI = {'schedule': ':alarm_clock:', 'manual': ':bust_in_silhouette:', None: ':grey_question:'}


def starter(row: dict) -> tuple[str, str | None]:
    """A run's trigger emoji (a manual run's starter's own when known), and who started it when that is recorded and
    their emoji isn't shown."""
    by = row['run'].get('trigger_by') if row['run']['trigger'] == 'manual' else None
    by = by if by and TRIGGER_BY_RE.fullmatch(by) else None  # never markup into mrkdwn, whatever a row holds
    face = row['run'].get('trigger_emoji') if by else None
    face = face if face and SLACK_EMOJI_RE.fullmatch(face) else None
    return face or TRIGGER_EMOJI[row['run']['trigger']], None if face else by


def reply(cfg: Config, row: dict) -> str:
    """A run's line in its thread: ✅ or ❌, the trigger (`starter`), its time linked to its record, the duration, who
    started it, and a failed run's reason."""
    seconds = row.get('secs')
    duration = 'duration unknown' if seconds is None else f"{int(seconds) // 60}m {int(seconds) % 60:02d}s"
    emoji, by = starter(row)
    ok = clean(row)
    return (f"{'✅' if ok else '❌'} {emoji} [{stamp(row)}]({cfg.link(row['run'])}) · {duration}" + (f" · {by}" if by else "")
            + ('' if ok else f" · {describe(row)}"))


def si(n: float, sign: bool = False) -> str:
    """Bytes at three significant figures in the site's SI units (`289 T`, `46.8 G`, `440 M`); `sign`: `+`/`−` first."""
    head = ('+' if n >= 0 else '−') if sign else ('-' if n < 0 else '')
    n = abs(n)
    for div, unit in ((1e15, 'P'), (1e12, 'T'), (1e9, 'G'), (1e6, 'M'), (1e3, 'K')):
        if n >= div:
            v = n / div
            return f"{head}{v:.{0 if v >= 100 else 1 if v >= 10 else 2}f} {unit}"
    return f"{head}{int(n)} B"


def outcome(row: dict, duration: bool = True) -> str:
    """A finished run's duration, cost and output (its generated files' size and change), as far as recorded."""
    seconds = row.get('secs') if duration else None
    parts = [] if seconds is None else [f"{int(seconds) // 60}m {int(seconds) % 60:02d}s"]
    if row.get('cost_usd') is not None:
        parts.append(f"${row['cost_usd']:.2f}")
    if row.get('out_bytes') is not None:
        delta = row.get('delta_bytes')
        parts.append(f"output {si(row['out_bytes'])}" + ('' if delta is None else f" ({si(delta, sign=True)})"))
    return ' · '.join(parts)


WEEK = 7 * 86400


def series_ts(date: str) -> float:
    return datetime.strptime(date, '%Y-%m-%dT%H%M').replace(tzinfo=timezone.utc).timestamp()


def store_line(cfg: Config, label: str, store: str, series: dict | int) -> str:
    """One store's line: its size and objects at its newest scan and the change since the newest scan at least a week
    before (`over Nd`, N the span); `series` is its `/api/series` body, or the HTTP status that failed to read it."""
    head = f"[{label}]({cfg.page(store)})"
    if isinstance(series, int):
        return f"{head}: size unavailable (HTTP {series})."
    points = series.get('points') or []
    if not points:
        return f"{head}: no scans yet."
    last = points[-1]
    text = f"{head}: {si(last['bytes'])} · {last['objects']:,} objects"
    end = series_ts(last['date'])
    before = [p for p in points if series_ts(p['date']) <= end - WEEK]
    if before:
        days = round((end - series_ts(before[-1]['date'])) / 86400)
        text += f" · {si(last['bytes'] - before[-1]['bytes'], sign=True)} over {days}d"
    return text + '.'


def stores_text(cfg: Config, series: Mapping[str, dict | int]) -> str:
    """The head's store lines (`Config.stores`), each its own paragraph, or nothing."""
    return ''.join(store_line(cfg, label, store, series[store]) + '\n\n' for label, store in cfg.stores)


def running(cfg: Config, row: dict) -> str:
    """A live run's reply, posted when it starts and edited to its `reply` when it ends."""
    emoji, by = starter(row)
    return f"⏳ {emoji} [{stamp(row)}]({cfg.link(row['run'])}) · running" + (f" · {by}" if by else "")


def running_now(cfg: Config, live_rows: list[dict]) -> str:
    """A thread head's line for the runs going now (oldest first), or nothing."""
    if not live_rows:
        return ''
    return 'Running now: ' + ', '.join(f"[{stamp(r)}]({cfg.link(r['run'])})" for r in reversed(live_rows)) + '.\n\n'


def parent(cfg: Config, good: list[dict], boundary: dict | None, live_rows: list[dict] | None = None, stores: str = '') -> str:
    """The thread's head: a clean thread's streak, or a failure thread's (`failed`), naming the runs going now, with
    the latest run's outcome and the deployment's store lines (`stores_text`)."""
    if good and not clean(good[0]):
        return failed(cfg, good, boundary, live_rows, stores)
    window = good[:10]
    counts = {k: sum(r['run']['trigger'] == k for r in window) for k in ('schedule', 'manual', None)}
    mix = f"{counts['schedule']} scheduled / {counts['manual']} manual" + (f" / {counts[None]} unknown" if counts[None] else '')
    since = stamp(good[-1])
    qualifier = '' if boundary else 'at least '
    after = f" after [{boundary['run']['scan']}]({cfg.link(boundary['run'])})" if boundary else ' (available run history)'
    return (f"✅ **{cfg.title}: {qualifier}{len(good)} consecutive clean run{'' if len(good) == 1 else 's'}**\n\n"
            f"Since {since}{after}.\n\n"
            f"Latest completed: [{stamp(good[0])}]({cfg.link(good[0]['run'])})" + (f" · {done}" if (done := outcome(good[0])) else '') + ".\n\n"
            f"{stores}"
            f"{running_now(cfg, live_rows)}"
            f"Latest {plural(len(window), 'run')}: {mix}. Each reply links its scan record. [All scans]({cfg.scans}).")


def failed(cfg: Config, bad: list[dict], boundary: dict | None, live_rows: list[dict] | None = None, stores: str = '') -> str:
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
            f"{stores}"
            f"{running_now(cfg, live_rows)}"
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
    def alert(self, link: str, since: float) -> str | None: ...
    def permalink(self, ts: str) -> str: ...
    def join(self) -> None: ...
    def pin(self, ts: str) -> None: ...
    def unpin(self, ts: str) -> None: ...
    def canvas_create(self, body: str) -> str: ...
    def canvas_edit(self, canvas: str, body: str) -> bool: ...


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

    def alert(self, link: str, since: float) -> str | None:
        """The first top-level message of ours since `since` linking `link`: a run's `fail` alert, when it went to this
        channel."""
        hits = [m['ts'] for m in self._read(None, since) if self.owns(m) and m.get('thread_ts', m['ts']) == m['ts'] and link in m.get('text', '')]
        return min(hits, key=float) if hits else None

    def permalink(self, ts: str) -> str:
        return self.client._request('chat.getPermalink', {'channel': self.client.channel, 'message_ts': ts}, method='GET')['permalink']

    def _ok(self, method: str, args: dict, *benign: str) -> dict:
        """A call whose listed errors mean the change is already in place."""
        try:
            return self.client._request(method, args)
        except RuntimeError as e:
            if any(str(e) == f'Slack API error: {code}' for code in benign):
                return {}
            raise

    def join(self) -> None:
        self._ok('conversations.join', {'channel': self.client.channel}, 'method_not_supported_for_channel_type')

    def pin(self, ts: str) -> None:
        self._ok('pins.add', {'channel': self.client.channel, 'timestamp': ts}, 'already_pinned')

    def unpin(self, ts: str) -> None:
        self._ok('pins.remove', {'channel': self.client.channel, 'timestamp': ts}, 'no_pin', 'message_not_found')

    def canvas_create(self, body: str) -> str:
        content = {'type': 'markdown', 'markdown': body}
        try:
            return self.client._request('conversations.canvases.create', {'channel_id': self.client.channel, 'document_content': content})['canvas_id']
        except RuntimeError as e:
            if str(e) != 'Slack API error: channel_canvas_already_exists':
                raise
        info = self.client._request('conversations.info', {'channel': self.client.channel}, method='GET')
        canvas = info['channel']['properties']['canvas']['file_id']
        if not self.canvas_edit(canvas, body):
            raise RuntimeError('nominal: the channel names a canvas that no longer exists')
        return canvas

    def canvas_edit(self, canvas: str, body: str) -> bool:
        """Replace the canvas's content; False when the canvas is gone (deleted)."""
        return bool(self._ok('canvases.edit', {'canvas_id': canvas, 'changes': [
            {'operation': 'replace', 'document_content': {'type': 'markdown', 'markdown': body}}]}, 'canvas_not_found', 'canvas_deleted'))


#: State that outlives a thread: the pinned head (unpinned once a new head is pinned) and the channel canvas
KEPT = ('pinned', 'canvas', 'canvas_body')


def pin_head(state: dict, slack: Transport, save: Callable[[dict], None]) -> None:
    """Pin the open thread's head, then unpin the head it replaced. Idempotent: a head already pinned, or one no longer
    pinned (unpinned by hand, or deleted), is not an error."""
    head, was = state.get('parent'), state.get('pinned')
    if not head or head == was:
        return
    slack.join()
    slack.pin(head)
    if was:
        slack.unpin(was)
    state['pinned'] = head
    save(state)


def phases(row: dict, n: int = 5) -> str:
    """A run's `n` longest phases, longest first (`list marin-na 9m 22s`)."""
    spans = sorted((s for s in row.get('spans') or [] if s.get('end') is not None and s.get('start') is not None),
                   key=lambda s: (s['start'] - s['end'], s['phase']))[:n]
    return ' · '.join(f"{s['phase']} {int(s['end'] - s['start']) // 60}m {int(s['end'] - s['start']) % 60:02d}s" for s in spans)


def canvas_text(cfg: Config, rows: list[dict], head: str, thread: str | None, series: Mapping[str, dict | int], now: float) -> str:
    """The channel canvas: the thread head's first line, each store, the latest finished run's outcome and longest
    phases, the last ten finished runs, and the site's pages. `rows` newest first; `thread` the head's permalink."""
    done = [r for r in rows if not live(r)]
    title = head.split('\n', 1)[0].replace('**', '')
    at = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    out = [f"# {cfg.title}", f"{title}" + (f" · [thread]({thread})" if thread else '') + f"\n\nUpdated {at}."]
    if cfg.stores:
        out += ['## Stores', '\n'.join(f"- {store_line(cfg, label, store, series[store])}" for label, store in cfg.stores)]
    if done:
        latest = done[0]
        line = f"{reply(cfg, latest)}" + (f" · {o}" if (o := outcome(latest, duration=False)) else '')
        out += ['## Latest run', line + (f"\n\nLongest phases: {p}" if (p := phases(latest)) else '')]
        out += ['## Recent runs', '\n'.join(f"- {reply(cfg, r)}" + (f" · ${r['cost_usd']:.2f}" if r.get('cost_usd') is not None else '')
                                              for r in done[:10])]
    site = cfg.site.rstrip('/')
    out += ['## Pages', f"[All scans]({cfg.scans}) · [Health]({site}/health)"
            + ''.join(f" · [{label}]({cfg.page(store)})" for label, store in cfg.stores)]
    return '\n\n'.join(out) + '\n'


def write_canvas(state: dict, slack: Transport, body: str, save: Callable[[dict], None]) -> None:
    """Create the channel's canvas, or replace its content when the body changed. The canvas's Updated line changes
    every run, so this writes once per report."""
    if state.get('canvas_body') == body:
        return
    slack.join()
    # A canvas deleted by hand is created again rather than failing every later report
    if not state.get('canvas') or not slack.canvas_edit(state['canvas'], body):
        state['canvas'] = slack.canvas_create(body)
    state['canvas_body'] = body
    save(state)


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
    stores: str = '',
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
        elif pending['key'].startswith('running:'):
            state.setdefault('running', {})[pending['key'].removeprefix('running:')] = {'ts': ts, 'parent': pending['parent']}
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
    # The thread is decided by finished runs only; a live run gets a ⏳ reply in the open thread (`running`), which
    # becomes its reply there when it ends in that thread, else is edited where it stands, linking where it was reported.
    live_rows = [r for r in rows if live(r)]
    rows = [r for r in rows if not live(r)]
    waiting: dict = dict(state.get('running') or {})
    if not rows:
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
            kept = {k: state[k] for k in KEPT if k in state}
            state.clear()
            state.update(namespace=cfg.namespace, boundary=boundary_id, posted={}, **kept)
            if head != 'clean':
                state['key'] = head
            persist()
    ended = {r['run']['run_id']: r for r in rows if r['run']['run_id'] in waiting}
    good_ids = {r['run']['run_id'] for r in good}
    here = {rid for rid in ended if rid in good_ids and not state.get('closed') and state.get('parent') == waiting[rid]['parent']}
    if good:
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

        body = parent(cfg, good, boundary, live_rows, stores)
        if not state.get('parent'):
            send('parent', body, None)
        # New threads bootstrap ten; established threads append every missing run after the newest known one.
        posted = state.setdefault('posted', {})
        todo = missing(good, posted)
        for rid in sorted(here, key=lambda rid: ended[rid]['run']['started_ts']):
            check()
            slack.edit(waiting[rid]['ts'], reply(cfg, ended[rid]))
            posted[rid] = waiting.pop(rid)['ts']
            state.get('running', {}).pop(rid, None)
            persist()
        for row in reversed(todo):
            if row['run']['run_id'] not in posted:
                send(row['run']['run_id'], reply(cfg, row), state['parent'])
        for row in reversed(live_rows):
            if row['run']['run_id'] not in state.get('running', {}):
                send('running:' + row['run']['run_id'], running(cfg, row), state['parent'])
        if state.get('parent_body') != body:
            check()
            slack.edit(state['parent'], body)
            state['parent_body'] = body
            persist()
        recent = {r['run']['run_id']: posted[r['run']['run_id']] for r in reversed(good[:10]) if r['run']['run_id'] in posted}
        if recent != posted:
            state['posted'] = recent
            persist()
    # A ⏳ reply whose run ended outside its thread: its outcome where it stands, linking the thread that took the run,
    # else the run's top-level alert in this channel (a `clean` thread's failure), when there is one.
    for rid, row in sorted(ended.items(), key=lambda kv: kv[1]['run']['started_ts']):
        if rid not in waiting:
            continue
        if rid in state.get('posted', {}) and state.get('parent'):
            label, ts = 'thread', state['parent']
        else:
            label, ts = 'alert', slack.alert(cfg.link(row['run']), row['run']['started_ts'])
        suffix = f" · [{label}]({slack.permalink(ts)})" if ts else ''
        check()
        slack.edit(waiting.pop(rid)['ts'], reply(cfg, row) + suffix)
    live_ids = {r['run']['run_id'] for r in live_rows}
    keep = {rid: v for rid, v in {**waiting, **(state.get('running') or {})}.items() if rid in live_ids}
    if keep and state.get('running') != keep:
        state['running'] = keep
        persist()
    elif not keep and 'running' in state:
        del state['running']
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

    def read_series() -> dict[str, dict | int]:
        series: dict[str, dict | int] = {}
        for _, name in cfg.stores:
            response = http_fetch(cfg.site, token)('/api/series?' + urlencode({'store': name}))
            # A store that can't be read is named as such in the head; the thread's run outcomes still post.
            series[name] = json.loads(response.body) if response.status == 200 else response.status
        return series

    if not execute:
        rows = read_rows()
        live_rows = [r for r in rows if live(r)]
        rows = [r for r in rows if not live(r)]
        if not rows:
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
        return '\n\n+++\n\n'.join([parent(cfg, good, boundary, live_rows, stores_text(cfg, read_series())), *(reply(cfg, r) for r in reversed(pending_rows)),
                                    *(running(cfg, r) for r in reversed(live_rows))])
    bot = (env.get('SLACK_BOT_TOKEN') or '').strip()
    if not bot:
        raise ValueError('nominal: need SLACK_BOT_TOKEN')
    with coordinator_lease(lease) as check:
        rows = read_rows()  # Re-read after acquiring the lease, so a waiting writer cannot publish stale state.
        slack = Slack(bot, cfg.channel)
        if cfg.pin or cfg.canvas:
            # Those need the bot in the channel (`channels:join`); joining first also lets the thread's reads (history,
            # a run's alert) work where the bot only ever posted by `chat:write.public`. Best effort: an app not yet
            # reinstalled with the scope still posts its thread; `pin_head` / `write_canvas` join again and fail there.
            try:
                slack.join()
            except RuntimeError as e:
                print(f'nominal: channel join failed ({e}); posting the thread anyway', file=sys.stderr)
        state = store.read_json(key) if store.exists(key) else adopt(cfg, rows, slack) if cfg.adopt_parent else {'namespace': cfg.namespace}
        if state.get('namespace') != cfg.namespace:
            raise ValueError('nominal: state belongs to another site/store/channel')
        def save(value: dict) -> None:
            store.write_text(key, json.dumps(value, indent=2) + '\n')
        check()
        save(state)
        series = read_series()
        state = converge(cfg, rows, state, slack, save, check, stores=stores_text(cfg, series))
        if cfg.pin:
            check()
            pin_head(state, slack, save)
        if cfg.canvas and state.get('parent_body'):
            check()
            write_canvas(state, slack, canvas_text(cfg, rows, state['parent_body'], slack.permalink(state['parent']), series, time.time()), save)
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


def report_start(job: JobRun) -> None:
    """Opt-in, after `start` records the run: its ⏳ reply in the open thread, and the head's "Running now". A failure is
    logged, never the job's; the run's finish reports it whatever happened here."""
    if not job.profile.nominal or not job.recording or job.dry_run or (job.env.get('DT_ALERTS') or '').strip() in ('stderr', 'off', '0'):
        return
    from .scan_runs import err

    try:
        facts = job.state()
        cfg = config(job.profile.nominal)
        if facts.get('parent') or facts.get('kind', 'scan') != cfg.kind or facts.get('store', job.profile.store) != cfg.store:
            return
        err(run(job.profile.nominal, job.env, job.profile.project, execute=True))
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — reporting must not fail (or end) the job
        err(job.redact(f'nominal start report failed: {type(exc).__name__}: {exc}'))


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
    except (Exception, SystemExit) as exc:  # Reporting must not fail (or end) the job; its failure remains visible in the head log.
        err(job.redact(f'nominal report failed: {type(exc).__name__}: {exc}'))
    finally:
        if deferred and not threaded:
            for message in deferred:
                job.post_alert('fail', message)
