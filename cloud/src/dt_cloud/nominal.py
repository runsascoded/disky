"""Nominal scan threads (specs/done/nominal-run-threads.md): each run's reply, posted ⏳ when it starts and edited to
its outcome once the authoritative run record has finished; in a quiet clean thread (`Config.quiet`), no per-run
replies, only the head and one rolling reply edited in place. Each post's sender (`Config.sender`: a fixed name and
icon) says what it is: a thread's kind, a run's outcome, the rolling reply (specs/done/scan-thread-senders.md).

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

from . import emoji_vocab as V
from . import slack_api
from .scan_id import scan_time
from .scan_runs import TRIGGER_BY_RE

if TYPE_CHECKING:
    from .scan_runs import JobRun, Sink


GROUPS = ('clean', 'color', 'signature')

#: Each run outcome's emoji (`Config.emoji` overrides any): `clean`; `issues`, a run that succeeded with phase issues
#: (a step failed, skipped or behind, the scan published: the fleet strip's ⚠️); `failed`, any other finished run that
#: isn't clean; `running`, a live run's reply; `cancelled`, a run stopped on purpose (`counted`: in no streak)
EMOJI = {o: V.stock(f'run.{o}') for o in V.OUTCOMES}
#: An `emoji` override: a Slack `:name:` code, or emoji characters (nothing Slack reads as markup)
EMOJI_RE = re.compile(r":[a-z0-9_+'-]{1,100}:|[^\x00-\x7f]{1,16}")
#: Each sender's stock icon (`icon_emoji`, which takes only `:name:` codes): the outcomes' (`EMOJI`). The quiet clean
#: thread's rolling reply (`runs`) has none: it posts under the app's own icon
ICON: dict[str, str | None] = {**{o: V.key(f'run.{o}').code for o in V.OUTCOMES}, 'runs': None}
#: The emoji characters an `emoji` override may be that have an icon: their Slack codes (variation selectors dropped).
#: Any other character keeps its kind's stock icon; give the override as a `:name:` code to move it into the icon.
CODES = {'✅': ':white_check_mark:', '✔': ':heavy_check_mark:', '☑': ':ballot_box_with_check:', '🟢': ':large_green_circle:',
         '⚠': ':warning:', '🟡': ':large_yellow_circle:', '🟠': ':large_orange_circle:', '❌': ':x:', '🔴': ':red_circle:',
         '💥': ':boom:', '⏳': ':hourglass_flowing_sand:', '⌛': ':hourglass:', '🔄': ':arrows_counterclockwise:',
         '🚫': ':no_entry_sign:', '⏹': ':black_square_for_stop:', '🛑': ':octagonal_sign:', '🧾': ':receipt:'}
#: A fixed sender's word in its name (`<title> · <word>`): a thread head's kind (its opening run's outcome: a failed run's
#: thread is `failing`), and the quiet clean thread's rolling reply. A run's reply is named by the run (`run_sender`).
WORDS = {'clean': 'clean', 'issues': 'phase issues', 'failed': 'failing', 'runs': 'runs'}
#: A run's trigger as a `badges` template names it
TRIGGERS = {'schedule': 'schedule', 'manual': 'manual', None: 'unknown'}
#: Slack's longest sender name
USERNAME_MAX = 80


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
    #: How runs group into threads (`GROUPS`): `color` (the default) threads consecutive clean runs and consecutive
    #: failed runs, each run of a color its own thread; `clean` threads only clean runs, a failure closing the thread
    #: (its alert posts top-level); `signature` threads consecutive failed runs only while they fail the same way (the
    #: failed phase, or the phases with issues). Under `color` and `signature` the thread is the failure's report: a
    #: run's `fail` alerts defer to it (`defers_failures`), and post top-level only if the run isn't a reply in a
    #: failure thread once the report has run; the alerts its steps posted while it ran are folded into its reply
    #: (`fold`), each left as a one-line pointer to it.
    group: str = "color"
    #: The stores the head summarizes (`stores_text`), as `{label: store}`: each one's size, object count and change over
    #: the last week, linked to its page. Empty: no store lines.
    stores: tuple[tuple[str, str], ...] = ()
    #: Pin the open thread's head in the channel, unpinning the head it replaced (`pin_head`; the bot's `pins:write`,
    #: `channels:join`).
    pin: bool = False
    #: Keep the channel's canvas as the deployment's status page (`canvas_text`; the bot's `canvases:write`).
    canvas: bool = False
    #: Outcome emoji overrides, as `{outcome: emoji}` over `EMOJI` (`mark`)
    emoji: tuple[tuple[str, str], ...] = ()
    #: Quiet clean threads (Ryan, 2026-11-21: Slack notifies only when something is wrong): while the open thread is a
    #: clean streak, a run posts no reply and no ⏳ reply; the head and one rolling reply (`trail`: the thread's latest
    #: runs, and the ones going now) are edited in place, which notifies nobody. A run that isn't clean posts as ever,
    #: opening or continuing its failure thread. Off: a reply per run, as before.
    quiet: bool = True
    #: Status as the sender (specs/done/scan-thread-senders.md): each post carries a name and icon saying what it is,
    #: which `chat.update` can't change: a thread's head its kind (`sender`: `<title> · clean` ✅, `· failing` ❌), the
    #: rolling reply `<title> · runs` (the app's icon), a run's reply its own line (`run_sender`: `<stamp> · <duration> · <outcome>`,
    #: the outcome's icon) over its record link (`record`). So a head's text drops its leading status emoji and title; a
    #: run's ⏳ reply, when the run ends, is deleted and posted again under its outcome. Off: every post the app's, each
    #: saying its status in its text.
    senders: bool = True
    #: Sender icon images over the stock emoji (`ICON`), as `{kind: https URL}` (`kind` an outcome or `runs`); Slack
    #: fetches each, so it must be public (the deployment's `icons_base`, say).
    icons: tuple[tuple[str, str], ...] = ()
    #: A run reply's icon image (`run_sender`): an https URL template over `{outcome}` (`grade`, or `running`) and
    #: `{trigger}` (`TRIGGERS`), the outcome with the trigger as a corner badge (`…/run-{outcome}-{trigger}.png`: ❌ + ⏰
    #: for a failed scheduled run). Public, as `icons`. Unset: the outcome's icon alone (`icons`, else its emoji).
    badges: str | None = None
    #: The deployment's status card (open question #87, `specs/disky-oa-dev.md`) as an image under an open thread's head
    #: (`with_card`): the site draws it (`GET /api/scan-runs/card-url` signs a `/og/deploy.png` URL for a week), the
    #: same card the fleet posts under its own post. A site without the route, or any failure to get a URL, posts the
    #: head without it. Off: never asked.
    card: bool = True
    #: The card's dot (`#rrggbb`): the deployment's colour in the fleet's `FLEET_JSON`; unset, the site's grey.
    color: str | None = None

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
        if any(k not in EMOJI for k, _ in self.emoji):
            raise ValueError(f"nominal.emoji: keys among {', '.join(EMOJI)}")
        if any(not isinstance(v, str) or not EMOJI_RE.fullmatch(v) for _, v in self.emoji):
            raise ValueError("nominal.emoji: a Slack :name: code or emoji characters")
        if type(self.quiet) is not bool:
            raise ValueError("nominal.quiet: true or false")
        if type(self.senders) is not bool:
            raise ValueError("nominal.senders: true or false")
        if any(k not in ICON for k, _ in self.icons):
            raise ValueError(f"nominal.icons: keys among {', '.join(ICON)}")
        if any(not isinstance(v, str) or not v.startswith('https://') or any(c in v for c in '<>| \n') for _, v in self.icons):
            raise ValueError("nominal.icons: public HTTPS image URLs")
        if self.badges is not None:
            try:
                ok = isinstance(self.badges, str) and self.badges.startswith('https://') and not any(c in self.badges for c in '<>| \n')
                ok = ok and self.badges.format(outcome='o', trigger='t') != self.badges
            except (KeyError, IndexError, ValueError):
                ok = False
            if not ok:
                raise ValueError("nominal.badges: an HTTPS URL template over {outcome} and {trigger}")
        if type(self.card) is not bool:
            raise ValueError("nominal.card: true or false")
        if self.color is not None and not (isinstance(self.color, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', self.color)):
            raise ValueError("nominal.color: #rrggbb")

    def mark(self, outcome: str) -> str:
        """An outcome's emoji (`EMOJI`, overridden by `emoji`)."""
        return dict(self.emoji).get(outcome) or EMOJI[outcome]

    def icon(self, kind: str) -> str | None:
        """A sender's `icon_emoji`: its `emoji` override as a Slack code (`CODES`), else its stock one (`ICON`; None:
        the app's icon)."""
        mark = dict(self.emoji).get(kind)
        if mark and mark.startswith(':'):
            return mark
        return (CODES.get(mark.replace('\ufe0f', '')) if mark else None) or ICON[kind]

    def avatar(self, kind: str) -> dict[str, str]:
        """A sender's icon: its `icons` image, else its emoji (`icon`), else none (the app's icon)."""
        if url := dict(self.icons).get(kind):
            return {'icon_url': url}
        code = self.icon(kind)
        return {'icon_emoji': code} if code else {}

    def sender(self, kind: str) -> dict[str, str] | None:
        """A fixed sender (`senders`; None when off), for a post whose text changes and whose kind doesn't: a thread's
        head (`clean`, `issues`, `failed`: the run that opened it) or the rolling reply (`runs`). Its name
        `<title> · <word>` (`WORDS`), clipped to Slack's 80 characters, and its kind's icon (`avatar`)."""
        if not self.senders:
            return None
        word = WORDS[kind]
        name = f"{self.title} · {word}"
        if len(name) > USERNAME_MAX:
            name = self.title[:USERNAME_MAX - len(word) - 4] + f"… · {word}"
        return {'username': name, **self.avatar(kind)}

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
    for k in ('stores', 'emoji', 'icons'):
        if isinstance(raw.get(k), Mapping):
            raw[k] = tuple(raw[k].items())
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


def cancelled(row: dict) -> bool:
    """A run stopped on purpose (`scan_runs.STATUSES`): no outcome of the deployment's, so it neither extends a clean
    streak nor breaks one, nor joins a failure thread. Its ⏳ reply, if it had one, is edited to say so (🚫)."""
    return row['run']['status'] == 'cancelled'


def counted(row: dict) -> bool:
    """A run the threads are decided by: finished (not `live`) and not `cancelled`."""
    return not live(row) and not cancelled(row)


#: Phase issues that leave a run clean: a `deferred` append (a compaction held the interval store; a later run's append
#: takes the scan, and its step alerts once per episode when one doesn't: `scan_steps.interval_store`). The site's
#: `scanRunsModel` `DEFERRED`.
TOLERATED = ('deferred',)


def issues(row: dict) -> list[dict]:
    """A run's phase issues that count against it (not `TOLERATED`)."""
    return [i for i in row['phase_issues'] if i.get('outcome') not in TOLERATED]


def deferred(row: dict) -> str:
    """A clean run's `TOLERATED` issues, as ` · deferred: <phases>` (else nothing), for its reply and its sender."""
    phases = sorted({str(i.get('phase')) for i in row['phase_issues'] if i.get('outcome') in TOLERATED})
    return f" · deferred: {', '.join(phases)}" if phases else ''


def clean(row: dict) -> bool:
    # A later recovery does not rewrite a historically partial run into a clean run; a deferred append was never a failure.
    return row['run']['status'] == 'succeeded' and row['run']['finished_ts'] is not None and not issues(row)


def grade(row: dict) -> str:
    """A finished run's outcome (`EMOJI`'s keys): `clean`, `issues` (it succeeded, with phase issues), `cancelled`, else
    `failed`."""
    if clean(row):
        return 'clean'
    if cancelled(row):
        return 'cancelled'
    run = row['run']
    return 'issues' if run['status'] == 'succeeded' and run['finished_ts'] is not None and issues(row) else 'failed'


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
    if issues(row):
        return 'issues:' + ','.join(sorted({str(i.get('phase')) for i in issues(row)}))
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


def short(row: dict) -> str:
    """A finished unclean run's outcome in a few words: `failed in <phase>`, `cancelled`, `phase issues: <phases>`, else
    its status (`describe` without a failure's error)."""
    run = row['run']
    if run['status'] == 'failed':
        return 'failed' + (f" in {run['failed_phase']}" if run.get('failed_phase') else '')
    if run['status'] == 'cancelled':
        return 'cancelled'
    if issues(row):
        return 'phase issues: ' + ', '.join(sorted({str(i.get('phase')) for i in issues(row)}))
    return str(run['status'])


def detail(row: dict) -> str:
    """A failed run's recorded error, one line, clipped, with no markup (`_MARKUP`); else nothing."""
    if row['run']['status'] != 'failed':
        return ''
    error = ' '.join(str(row['run'].get('error') or '').translate(_MARKUP).split())
    return error if len(error) <= 160 else error[:159] + '…'


def describe(row: dict) -> str:
    """A finished unclean run's outcome and why (`short`, then a failure's error)."""
    return short(row) + (f": {d}" if (d := detail(row)) else '')


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ('' if n == 1 else 's')


def stamp(row: dict) -> str:
    return datetime.fromtimestamp(row['run']['started_ts'], timezone.utc).strftime('%m/%d %H:%MZ')


#: A reply's trigger, beside its ✅: the deployment's cron, a hand submit (the site's "Run a scan" or a CLI execute; who,
#: named after the duration when recorded: `scan_runs.trigger_by`), or a deployment that doesn't state its triggers. The
#: parent's run mix spells them out.
#: A manual run's starter's own Slack emoji (`trigger_emoji`: `/api/scan-runs?emoji=1` asks OA's broker) replaces 👤 and their address
SLACK_EMOJI_RE = re.compile(r":[a-z0-9_+'-]{1,100}:")
TRIGGER_EMOJI = {t: V.key(f'trigger.{t or "unknown"}').code for t in ('schedule', 'manual', None)}


def faces(rows: list[dict], have: set[str] | None) -> list[dict]:
    """``rows`` with each starter's own emoji (`trigger_emoji`, OA's broker's) dropped where the posting workspace
    (``have``: its custom emoji names) lacks it, so `starter` shows 👤 and their address there instead of the raw
    ``:name:``; ``rows`` as they are when the workspace is unknown (None)."""
    if have is None:
        return rows
    return [{**r, 'run': {k: v for k, v in r['run'].items() if k != 'trigger_emoji'}}
            if (f := r['run'].get('trigger_emoji')) and f.strip(':') not in have else r for r in rows]


def starter(row: dict) -> tuple[str, str | None]:
    """A run's trigger emoji (a manual run's starter's own when known), and who started it when that is recorded and
    their emoji isn't shown."""
    by = row['run'].get('trigger_by') if row['run']['trigger'] == 'manual' else None
    by = by if by and TRIGGER_BY_RE.fullmatch(by) else None  # never markup into mrkdwn, whatever a row holds
    face = row['run'].get('trigger_emoji') if by else None
    face = face if face and SLACK_EMOJI_RE.fullmatch(face) else None
    return face or TRIGGER_EMOJI[row['run']['trigger']], None if face else by


def took(row: dict) -> str:
    """A run's duration (`115m 54s`), or `duration unknown`."""
    seconds = row.get('secs')
    return 'duration unknown' if seconds is None else f"{int(seconds) // 60}m {int(seconds) % 60:02d}s"


def reply(cfg: Config, row: dict) -> str:
    """A run's line in its thread: its outcome's emoji (`grade`: ✅, ⚠️ or ❌), the trigger (`starter`), its time linked to
    its record, the duration, who started it, and an unclean run's reason."""
    duration = took(row)
    emoji, by = starter(row)
    ok = clean(row)
    return (f"{cfg.mark(grade(row))} {emoji} [{stamp(row)}]({cfg.link(row['run'])}) · {duration}" + (f" · {by}" if by else "")
            + (deferred(row) if ok else f" · {describe(row)}"))


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
    """A series point's scan id as a timestamp; a date-only id (a deployment's first scans) is its midnight."""
    return scan_time(date).timestamp()


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
    """A live run's reply, posted when it starts, and edited to its `reply` when it ends (with `Config.senders`, deleted
    and posted again: `run_post`)."""
    emoji, by = starter(row)
    return f"{cfg.mark('running')} {emoji} [{stamp(row)}]({cfg.link(row['run'])}) · running" + (f" · {by}" if by else "")


def run_sender(cfg: Config, row: dict, live: bool = False) -> dict[str, str]:
    """A run reply's sender (Ryan, 2026-11-22): its line's facts as the name, `<stamp> · <duration> · <outcome>` (`short`;
    a live run's `<stamp> · running`), the outcome clipped (…) to Slack's 80 characters; its icon the outcome, with the
    trigger as a corner badge where `badges` names images, else the outcome's icon alone (`avatar`)."""
    outcome = 'running' if live else grade(row)
    head = f"{stamp(row)} · " + ('' if live else f"{took(row)} · ")
    what = 'running' if live else 'clean' + deferred(row) if outcome == 'clean' else short(row)
    if len(head + what) > USERNAME_MAX:
        what = what[:USERNAME_MAX - len(head) - 1] + '…'
    icon = ({'icon_url': cfg.badges.format(outcome=outcome, trigger=TRIGGERS[row['run']['trigger']])} if cfg.badges
            else cfg.avatar(outcome))
    return {'username': head + what, **icon}


def record(cfg: Config, row: dict) -> str:
    """A run reply's text under its sender (`run_sender`, which says when, how long and how it ended): the trigger
    (`starter`) and its scan record's link, who started it, and a failure's error."""
    emoji, by = starter(row)
    return (f"{emoji} [record]({cfg.link(row['run'])})" + (f" · {by}" if by else '')
            + (f" · {d}" if not live(row) and (d := detail(row)) else ''))


def run_post(cfg: Config, row: dict) -> tuple[str, dict[str, str] | None]:
    """A run's reply as posted, `(text, sender)`: with `Config.senders`, `record` under `run_sender`; else its line
    (`reply`, or a live run's `running`) as the app."""
    if cfg.senders:
        return record(cfg, row), run_sender(cfg, row, live(row))
    return (running if live(row) else reply)(cfg, row), None


def running_now(cfg: Config, live_rows: list[dict] | None) -> str:
    """A thread head's line for the runs going now (oldest first), or nothing."""
    if not live_rows:
        return ''
    return 'Running now: ' + ', '.join(f"[{stamp(r)}]({cfg.link(r['run'])})" for r in reversed(live_rows)) + '.\n\n'


def parent(
    cfg: Config,
    good: list[dict],
    boundary: dict | None,
    live_rows: list[dict] | None = None,
    stores: str = '',
    quiet: bool = False,
    bare: bool = False,
    prev: str | None = None,
) -> str:
    """The thread's head: a clean thread's streak, or a failure thread's (`failed`), naming the runs going now, with
    the latest run's outcome, the deployment's store lines (`stores_text`) and the thread before it (`prev`, a
    permalink: `previous`). `quiet`: its runs are listed in one rolling reply (`trail`), not a reply each. `bare`: no
    leading emoji and title, which its sender says (`lead`)."""
    if good and not clean(good[0]):
        return failed(cfg, good, boundary, live_rows, stores, bare, prev)
    window = good[:10]
    counts = {k: sum(r['run']['trigger'] == k for r in window) for k in ('schedule', 'manual', None)}
    mix = f"{counts['schedule']} scheduled / {counts['manual']} manual" + (f" / {counts[None]} unknown" if counts[None] else '')
    since = stamp(good[-1])
    qualifier = '' if boundary else 'at least '
    after = f" after [{boundary['run']['scan']}]({cfg.link(boundary['run'])})" if boundary else ' (available run history)'
    return (f"{lead(cfg, cfg.mark('clean'), bare)}{qualifier}{len(good)} consecutive clean run{'' if len(good) == 1 else 's'}**\n\n"
            f"Since {since}{after}{previous(prev)}.\n\n"
            f"Latest completed: [{stamp(good[0])}]({cfg.link(good[0]['run'])})" + (f" · {done}" if (done := outcome(good[0])) else '') + ".\n\n"
            f"{stores}"
            f"{running_now(cfg, live_rows)}"
            f"Latest {plural(len(window), 'run')}: {mix}. "
            + ("One reply lists the runs, each linking its scan record." if quiet else "Each reply links its scan record.")
            + f" [All scans]({cfg.scans}).")


def trail(cfg: Config, runs: list[dict], live_rows: list[dict] | None = None) -> str:
    """A quiet clean thread's one rolling reply: its newest ten runs' lines (`reply`), oldest first, then the runs going
    now (`running`); edited in place as runs start and end. `runs` newest first."""
    lines = [reply(cfg, r) for r in reversed(runs[:10])] + [running(cfg, r) for r in reversed(live_rows or [])]
    return '\n'.join(lines)


def unclean(cfg: Config, runs: list[dict], qualifier: str = '') -> tuple[str, str]:
    """A run of unclean runs' emoji and words: ⚠️ `N consecutive runs with phase issues` when every one succeeded with
    issues, else ❌ `N consecutive failed runs` (`runs failed or with phase issues` when they mix)."""
    grades = {grade(r) for r in runs}
    n, s = len(runs), '' if len(runs) == 1 else 's'
    if grades == {'issues'}:
        return cfg.mark('issues'), f"{qualifier}{n} consecutive run{s} with phase issues"
    return cfg.mark('failed'), f"{qualifier}{n} consecutive " + (f"run{s} failed or with phase issues" if 'issues' in grades else f"failed run{s}")


def previous(prev: str | None) -> str:
    """A head's link to the thread before it, after its "Since …"."""
    return f" · [previous thread]({prev})" if prev else ''


def lead(cfg: Config, mark: str, bare: bool) -> str:
    """A head's first line up to its words: its emoji and the title, or (`bare`: posted with a sender, which says them)
    only the bold's start."""
    return '**' if bare else f"{mark} **{cfg.title}: "


def failed(
    cfg: Config,
    bad: list[dict],
    boundary: dict | None,
    live_rows: list[dict] | None = None,
    stores: str = '',
    bare: bool = False,
    prev: str | None = None,
) -> str:
    """A failure thread's head (`unclean`): how many runs in a row failed or had phase issues (under `signature`, the
    same way), since when and after which run (the last clean one, or the last that failed differently), and the newest
    one's reason."""
    qualifier = '' if boundary else 'at least '
    how = ''
    if cfg.group == 'signature':
        run = bad[0]['run']
        how = f" in {run['failed_phase']}" if run['status'] == 'failed' and run.get('failed_phase') else ''
    mark, words = unclean(cfg, bad, qualifier)
    after = (f" after [{boundary['run']['scan']}]({cfg.link(boundary['run'])})" + (' (clean)' if clean(boundary) else f" ({describe(boundary)})")
             if boundary else ' (available run history)')
    return (f"{lead(cfg, mark, bare)}{words}{how}**\n\n"
            f"Since {stamp(bad[-1])}{after}{previous(prev)}.\n\n"
            f"Latest: [{stamp(bad[0])}]({cfg.link(bad[0]['run'])}): {describe(bad[0])}.\n\n"
            f"{stores}"
            f"{running_now(cfg, live_rows)}"
            f"Each reply links its scan record. [All scans]({cfg.scans}).")


def closed_runs(rows: list[dict], stop: dict, boundary_id: str | None) -> tuple[list[dict], dict | None]:
    """A closing thread's runs (newest first: those older than `stop`, back to its recorded boundary) and that
    boundary's row (None when run history no longer reaches it)."""
    ids = [r['run']['run_id'] for r in rows]
    start = ids.index(stop['run']['run_id']) + 1 if stop['run']['run_id'] in ids else 0
    end = ids.index(boundary_id) if boundary_id in ids else len(rows)
    return rows[start:end], (rows[end] if end < len(rows) else None)


def closed_head(
    cfg: Config,
    was: str,
    old: tuple[list[dict], dict | None],
    stop: dict,
    reason: str,
    bare: bool = False,
) -> str:
    """A closed thread's head: how many runs it held, since when, its newest, and the run that ended it (`bare`: as
    `parent`'s)."""
    runs, boundary = old
    if not runs:
        kind, mark = ('clean-run', cfg.mark('clean')) if was == 'clean' else ('failure', cfg.mark('failed'))
        return f"{lead(cfg, mark, bare)}{kind} sequence ended**\n\n[{stamp(stop)}]({cfg.link(stop['run'])}): {reason}. [All scans]({cfg.scans})."
    qualifier = '' if boundary else 'at least '
    after = f" after [{boundary['run']['scan']}]({cfg.link(boundary['run'])})" if boundary else ' (available run history)'
    mark, words = ((cfg.mark('clean'), f"{qualifier}{len(runs)} consecutive clean run{'' if len(runs) == 1 else 's'}") if was == 'clean'
                   else unclean(cfg, runs, qualifier))
    return (f"{lead(cfg, mark, bare)}{words} (ended)**\n\n"
            f"Since {stamp(runs[-1])}{after}; last [{stamp(runs[0])}]({cfg.link(runs[0]['run'])}).\n\n"
            f"Ended by [{stamp(stop)}]({cfg.link(stop['run'])}): {reason}. [All scans]({cfg.scans}).")


#: Slack's longest `section` text beside an image block (thrds lifts a trailing `![alt](url)` line into one and refuses a
#: longer body): a head past it posts without its card. Less a margin for markdown → mrkdwn and emoji fallbacks.
CARD_BODY_MAX = 3000 - 100
#: A signed card URL as `/api/scan-runs/card-url` answers it: the site's own `/og/deploy.png`, nothing an image line or
#: Slack would read as markup.
CARD_URL_RE = r'/og/deploy\.png\?[A-Za-z0-9._~%=&+-]+'


def card_query(cfg: Config) -> dict[str, str]:
    """`/api/scan-runs/card-url`'s params: the thread's store and the card's colour."""
    return {'store': cfg.store, **({'color': cfg.color.lstrip('#')} if cfg.color else {})}


def card_url(cfg: Config, status: int, body: bytes) -> str | None:
    """The card's signed URL from `/api/scan-runs/card-url`'s answer (`status`, `body`), or None when there is none to
    post: the route missing (a site before it: 404), unconfigured (503), failing, or answering anything but one of the
    site's `/og/deploy.png` URLs. Why is logged; the head posts without its card."""
    if status != 200:
        print(f'nominal: no status card (card-url HTTP {status})', file=sys.stderr)
        return None
    try:
        url = json.loads(body)['url']
    except (ValueError, KeyError, TypeError) as e:
        print(f'nominal: no status card (card-url answered {type(e).__name__})', file=sys.stderr)
        return None
    if not isinstance(url, str) or not re.fullmatch(re.escape(cfg.site.rstrip('/')) + CARD_URL_RE, url):
        print('nominal: no status card (card-url answered another URL)', file=sys.stderr)
        return None
    return url


def with_card(cfg: Config, body: str, url: str | None, row: dict) -> str:
    """An open thread's head with the status card under it (`url`, `card_url`'s; None: as it is), as a trailing image
    line, which thrds posts as a Slack `image` block, its alt text naming the deployment and the head's latest run
    (`row`). A closed head (`closed_head`) carries none: its URL would expire under it. A body too long to sit beside an
    image (`CARD_BODY_MAX`) posts without it."""
    if not url or len(body) > CARD_BODY_MAX:
        return body
    alt = re.sub(r'[\[\]\n]', '', f"{cfg.title} status card: its latest scan runs by step and 30 days of size, cost and run time, as of the {stamp(row)} run")
    return f"{body}\n\n![{alt}]({url})"


def next_thread(cfg: Config, body: str, url: str) -> str:
    """A closed head (`closed_head`) whose "Ended by …" line links the thread after it (`url`, a permalink)."""
    tail = f". [All scans]({cfg.scans})."
    if not body.endswith(tail):
        raise ValueError("nominal: a closed head without its All scans link")
    return body[:-len(tail)] + f" · [next thread]({url})" + tail


#: A folded alert's text starts with this; it is never folded again.
FOLDED = '↪'


def alert_lines(alert: dict) -> tuple[str, list[str]]:
    """A run's top-level alert as its title and its lines (each step's message, with its task logs), in the thread's
    markdown: from its `disky_alert` metadata (`push.Notice.group`), else parsed from an ungrouped alert's text."""
    payload = alert.get('metadata', {}).get('event_payload') or {}
    if alert.get('metadata', {}).get('event_type') == 'disky_alert' and payload.get('lines'):
        return payload['title'], [line['message'] + (f" · [{line.get('url_title') or 'logs'}]({line['url']})" if line.get('url') else '')
                                  for line in payload['lines']]
    head, *rest = alert['text'].split('\n')
    logs = ''
    if rest and rest[-1].startswith('<'):
        hit = re.search(r'<([^|>]+)\|task logs>', rest.pop())
        logs = f" · [task logs]({hit.group(1)})" if hit else ''
    return head.strip('*'), [' '.join(rest) + logs] if rest else []


def fold(cfg: Config, slack: Transport, row: dict) -> tuple[list[dict], str]:
    """A run's unfolded top-level alerts, and the lines they add to its reply in a failure thread."""
    found = [a for a in slack.alert_messages(cfg.link(row['run']), row['run']['started_ts']) if not a['text'].startswith(FOLDED)]
    return found, ''.join(f"\n{line}" for a in found for line in alert_lines(a)[1])


def folded(slack: Transport, alerts: list[dict], reply_ts: str) -> None:
    """Each folded alert left as a pointer to the reply that now carries its lines."""
    link = slack.permalink(reply_ts)
    for a in alerts:
        slack.edit(a['ts'], f"{FOLDED} {alert_lines(a)[0]}: in the [failure thread]({link})")


def missing(good: list[dict], posted: dict) -> list[dict]:
    known = next((i for i, row in enumerate(good) if row['run']['run_id'] in posted), None)
    return good[:known] if known is not None else good[:10]


class Transport(Protocol):
    @property
    def have(self) -> set[str] | None:
        """The workspace's custom emoji names (None: unknown), for `faces`; read-only (`Slack` reads them once)."""
        ...

    def messages(self, parent: str) -> list[dict]: ...
    def owns(self, message: dict) -> bool: ...
    def find(self, operation: str, parent: str | None, since: float) -> str | None: ...
    def post(self, body: str, parent: str | None, operation: str, sender: Mapping[str, str] | None = None) -> str: ...
    def edit(self, ts: str, body: str) -> None: ...
    def delete(self, ts: str) -> None: ...
    def alert(self, link: str, since: float) -> str | None: ...
    def alert_messages(self, link: str, since: float) -> list[dict]: ...
    def permalink(self, ts: str) -> str: ...
    def join(self) -> None: ...
    def pin(self, ts: str) -> None: ...
    def unpin(self, ts: str) -> None: ...
    def canvas_create(self, body: str, title: str) -> str: ...
    def canvas_edit(self, canvas: str, body: str) -> bool: ...
    def canvas_rename(self, canvas: str, title: str) -> bool: ...


class Slack:
    """Use thrds for formatting, sends, edits and transport retries; paginate recovery reads explicitly."""

    def __init__(self, token: str, channel: str):
        from thrds import SlackClient

        self.client = SlackClient(token=token, channel=channel)

    @property
    def have(self) -> set[str] | None:
        """The workspace's custom emoji names, read once, at first use (`emoji_vocab.workspace_custom`); None when
        unreadable."""
        if '_have' not in vars(self):
            self._have = V.workspace_custom(self.client, 'nominal')
        return self._have

    def owns(self, message: dict) -> bool:
        """Ours by the bot's user or bot id, never its name: a post with a sender (`Config.sender`) is a `bot_message`
        under that post's own `username`, with no `user`, but the bot's `bot_id`."""
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

    def post(self, body: str, parent: str | None, operation: str, sender: Mapping[str, str] | None = None) -> str:
        """Post `body` (in `parent`'s thread) under `sender` (`Config.sender`: `username` and `icon_emoji` or `icon_url`,
        fixed once posted; the bot's `chat:write.customize`), else as the app."""
        # A vocabulary custom emoji the workspace lacks goes out as its stock one (`emoji_vocab.fallback`), before the
        # metadata is keyed by the text. thrds' own sync uses this metadata seam around post(). The payload contains no
        # credentials.
        body = V.fallback(body, self.have)
        previous = self.client._metadata_by_content
        self.client._metadata_by_content = {body: {'event_type': 'disky_nominal', 'event_payload': {'operation': operation}}}
        try:
            return self.client.post(body, thread_id=parent, **dict(sender or {})).id
        finally:
            self.client._metadata_by_content = previous

    def edit(self, ts: str, body: str) -> None:
        self.client.edit(ts, V.fallback(body, self.have))

    def delete(self, ts: str) -> None:
        """Delete our message (a reply with no replies of its own leaves no tombstone); one already gone is not an
        error, so a report interrupted after it deleted can delete again."""
        slack_api.call_ok(self.client, 'chat.delete', {'channel': self.client.channel, 'ts': ts}, 'message_not_found')

    def alert_messages(self, link: str, since: float) -> list[dict]:
        """Our top-level messages since `since` linking `link` (a run's alerts, when they went to this channel), oldest
        first, each `{ts, text, metadata}`; never a thread's own head (`disky_nominal` metadata), which links runs too."""
        hits = [m for m in self._read(None, since) if self.owns(m) and m.get('thread_ts', m['ts']) == m['ts'] and link in m.get('text', '')
                and (m.get('metadata') or {}).get('event_type') != 'disky_nominal']
        return [{'ts': m['ts'], 'text': m.get('text', ''), 'metadata': m.get('metadata') or {}} for m in sorted(hits, key=lambda m: float(m['ts']))]

    def alert(self, link: str, since: float) -> str | None:
        """The first top-level message of ours since `since` linking `link`: a run's `fail` alert, when it went to this
        channel."""
        hits = self.alert_messages(link, since)
        return hits[0]['ts'] if hits else None

    def permalink(self, ts: str) -> str:
        return self.client._request('chat.getPermalink', {'channel': self.client.channel, 'message_ts': ts}, method='GET')['permalink']

    def _ok(self, method: str, args: dict, *benign: str) -> dict:
        """A call whose listed errors mean the change is already in place."""
        return slack_api.call_ok(self.client, method, args, *benign)

    def join(self) -> None:
        slack_api.join(self.client, self.client.channel)

    def pin(self, ts: str) -> None:
        slack_api.pin(self.client, self.client.channel, ts)

    def unpin(self, ts: str) -> None:
        slack_api.unpin(self.client, self.client.channel, ts)

    def canvas_create(self, body: str, title: str) -> str:
        content = {'type': 'markdown', 'markdown': body}
        try:
            return self.client._request('conversations.canvases.create', {
                'channel_id': self.client.channel, 'title': title, 'document_content': content})['canvas_id']
        except RuntimeError as e:
            if str(e) != 'Slack API error: channel_canvas_already_exists':
                raise
        info = self.client._request('conversations.info', {'channel': self.client.channel}, method='GET')
        canvas = info['channel']['properties']['canvas']['file_id']
        if not self.canvas_edit(canvas, body) or not self.canvas_rename(canvas, title):
            raise RuntimeError('nominal: the channel names a canvas that no longer exists')
        return canvas

    def canvas_edit(self, canvas: str, body: str) -> bool:
        """Replace the canvas's content; False when the canvas is gone (deleted)."""
        return bool(self._ok('canvases.edit', {'canvas_id': canvas, 'changes': [
            {'operation': 'replace', 'document_content': {'type': 'markdown', 'markdown': body}}]}, 'canvas_not_found', 'canvas_deleted'))

    def canvas_rename(self, canvas: str, title: str) -> bool:
        """Set the canvas's title (its channel tab's label); False when the canvas is gone."""
        return bool(self._ok('canvases.edit', {'canvas_id': canvas, 'changes': [
            {'operation': 'rename', 'title_content': {'type': 'markdown', 'markdown': title}}]}, 'canvas_not_found', 'canvas_deleted'))


#: State that outlives a thread: the pinned head (unpinned once a new head is pinned), the channel canvas, and the run
#: links still to write (`relink`)
KEPT = ('pinned', 'canvas', 'canvas_body', 'canvas_title', 'relink')
#: The most run links `relink` holds unwritten (a report without a database writes none)
RELINK_MAX = 10


def pin_head(state: dict, slack: Transport, save: Callable[[dict], None]) -> None:
    """Pin the open thread's head, then unpin the head it replaced; a closed thread's head is unpinned at once (the
    channel then pins nothing until the next thread opens). Idempotent: a head already pinned, or one no longer
    pinned (unpinned by hand, or deleted), is not an error."""
    head, was = state.get('parent'), state.get('pinned')
    if state.get('closed'):
        if was:
            slack.join()
            slack.unpin(was)
            del state['pinned']
            save(state)
        return
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


def status(cfg: Config, done: list[dict]) -> str:
    """The deployment's state in a line: an open thread's headline (its streak), else the newest counted run's failure
    (`counted`: a cancelled run is no state of the deployment's)."""
    done = [r for r in done if counted(r)]
    _, good, boundary = segment(cfg, done)
    if good:
        return parent(cfg, good, boundary).split('\n', 1)[0].replace('**', '')
    return f"{cfg.mark(grade(done[0]))} {cfg.title}: latest run {describe(done[0])}" if done else f"{cfg.title}: no finished runs yet"


def canvas_text(cfg: Config, rows: list[dict], thread: str | None, series: Mapping[str, dict | int], now: float) -> str:
    """The channel canvas: the deployment's `status`, each store, the latest finished run's outcome and longest phases,
    the last ten finished runs, and the site's pages. `rows` newest first; `thread` the open thread head's permalink."""
    done = [r for r in rows if not live(r)]
    title = status(cfg, done)
    at = datetime.fromtimestamp(now, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    out = [f"{title}" + (f" · [thread]({thread})" if thread else '') + f"\n\nUpdated {at}."]
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


def write_canvas(state: dict, slack: Transport, body: str, title: str, save: Callable[[dict], None]) -> None:
    """Create the channel's canvas titled `title` (its tab's label), or replace its content when the body changed and
    its title when that did. The canvas's Updated line changes every run, so this writes once per report."""
    if state.get('canvas_body') == body and state.get('canvas_title') == title:
        return
    slack.join()
    # A canvas deleted by hand is created again rather than failing every later report
    if not state.get('canvas') or not slack.canvas_edit(state['canvas'], body) or (
            state.get('canvas_title') != title and not slack.canvas_rename(state['canvas'], title)):
        state['canvas'] = slack.canvas_create(body, title)
    state['canvas_body'], state['canvas_title'] = body, title
    save(state)


def link_threads(state: dict, slack: Transport, write: Callable[[list[tuple[str, str]]], None], save: Callable[[dict], None]) -> None:
    """Each run's reply in the open thread (its outcome reply, else its ⏳ one) as `(run_id, permalink)`, given to `write`
    (`scan_runs.thread_url`) once per reply: `linked` remembers the reply each run was last written with, so a report
    asks Slack only for replies new since the last. A failed write leaves `linked` alone, to be retried next report.
    With senders: the run that opened the thread by changing its kind (`opener`) links its head, and a run whose ⏳
    reply was deleted with no reply in a thread (`relink`: its 🚫 reply, or its top-level alert) links that, once."""
    replies = {rid: r['ts'] for rid, r in (state.get('running') or {}).items()}
    replies.update(state.get('posted') or {})
    if state.get('opener') in replies and state.get('parent'):
        replies[state['opener']] = state['parent']
    relink = state.get('relink') or {}
    linked = state.get('linked') or {}
    todo = {rid: ts for rid, ts in {**replies, **relink}.items() if linked.get(rid) != ts}
    if not todo:
        if relink:
            del state['relink']
            save(state)
        return
    # A quiet thread's runs share its rolling reply: one permalink each reply
    urls = {ts: slack.permalink(ts) for ts in dict.fromkeys(todo.values())}
    write([(rid, urls[ts]) for rid, ts in todo.items()])
    state['linked'] = {**{rid: ts for rid, ts in linked.items() if rid in replies}, **{rid: ts for rid, ts in todo.items() if rid in replies}}
    state.pop('relink', None)
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
    card: str | None = None,
) -> dict:
    """Caller holds the generation lease. Persist every send intent and returned/recovered message ID. `card`: the status
    card's URL (`card_url`), under an open head (`with_card`)."""
    if state.get('namespace') != cfg.namespace:
        raise ValueError('nominal: state belongs to another site/store/channel')

    def persist() -> None:
        check()
        save(state)

    def relinked(rid: str, ts: str) -> None:
        links = state.setdefault('relink', {})
        links.pop(rid, None)
        links[rid] = ts
        while len(links) > RELINK_MAX:
            links.pop(next(iter(links)))

    def received(pending: dict, ts: str) -> None:
        if pending['key'] == 'parent':
            state.update(parent=ts, parent_body=pending['body'])
            if pending.get('sender'):
                # Its sender names the thread, so its text is `bare`; a head posted without one keeps its full text
                state['head_sender'] = pending['sender']['username']
        elif pending['key'] == 'rolling':
            # The sender it was posted under (None: the app's), which `chat.update` can't change: see `rolling_retired`
            state.update(rolling=ts, rolling_body=pending['body'], rolling_sender=pending.get('sender'))
        elif pending['key'].startswith('running:'):
            state.setdefault('running', {})[pending['key'].removeprefix('running:')] = {'ts': ts, 'parent': pending['parent']}
        elif pending['key'].startswith('cancelled:'):
            rid = pending['key'].removeprefix('cancelled:')
            state.get('running', {}).pop(rid, None)
            relinked(rid, ts)
        else:
            state.setdefault('posted', {})[pending['key']] = ts
        state.pop('pending', None)
        state.pop('operator_required', None)
        persist()

    def send(key: str, body: str, root: str | None, scope: str | None, sender: Mapping[str, str] | None = None) -> None:
        operation = str(uuid5(NAMESPACE_URL, f'{cfg.namespace}|{scope}|{key}'))
        pending = {'key': key, 'body': body, 'parent': root, 'operation': operation, 'since': now() - 1,
                   **({'sender': dict(sender)} if sender else {})}
        state['pending'] = pending
        persist()
        check()
        received(pending, slack.post(body, root, operation, sender))

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
    # The thread is decided by counted runs only (`counted`); a live run gets a ⏳ reply in the open thread (`running`),
    # which becomes its reply there when it ends in that thread, else is edited where it stands, linking where it was
    # reported. A cancelled run's ⏳ reply becomes its 🚫 reply where it stands; it joins no thread. With senders a
    # reply's icon can't change: an ended run's ⏳ reply is deleted, then its reply posted with its outcome's sender in
    # the same thread (in another thread, nothing more: the run is the new thread's). Every delete and post here runs
    # under the caller's lease (the job's own report and `batch-events`' converge alike), and a delete repeated after an
    # interrupted report finds nothing (`Slack.delete`), so no ⏳ reply is posted again twice.
    live_rows = [r for r in rows if live(r)]
    stopped = [r for r in rows if not live(r) and cancelled(r)]
    rows = [r for r in rows if counted(r)]
    waiting: dict = dict(state.get('running') or {})
    for row in sorted(stopped, key=lambda r: r['run']['started_ts']):
        rid = row['run']['run_id']
        if rid in waiting and cfg.senders:
            check()
            slack.delete(waiting[rid]['ts'])
            text, sender = run_post(cfg, row)
            send('cancelled:' + rid, text, waiting.pop(rid)['parent'], 'cancelled', sender)
        elif rid in waiting:
            check()
            slack.edit(waiting.pop(rid)['ts'], reply(cfg, row))
            state.get('running', {}).pop(rid, None)
            persist()
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
                reason = 'phase issues' if stop['run']['status'] == 'succeeded' and issues(stop) else stop['run']['status']
            else:
                reason = 'clean' if clean(stop) else describe(stop)
            old = closed_runs(rows, stop, state.get('boundary'))
            body = closed_head(cfg, was, old, stop, reason, bare=bool(state.get('head_sender')))
            check()
            slack.edit(state['parent'], body)
            state.update(closed=True, closed_body=body)
            persist()
            if state.get('rolling'):
                # A quiet thread's rolling reply ends as its runs and the one that ended it, with no runs going
                final = trail(cfg, [stop, *old[0]][:10])
                if state.get('rolling_body') != final:
                    check()
                    slack.edit(state['rolling'], final)
                    state['rolling_body'] = final
                    persist()
        if good:
            kept = {k: state[k] for k in KEPT if k in state}
            # The new head links the closed one (`previous_url`); once it is posted, the closed head's "Ended by" line
            # links it back (`previous_body`: the closed head as last written, dropped once relinked)
            prev = {'previous': state['parent'], 'previous_url': slack.permalink(state['parent']),
                    **({'previous_body': state['closed_body']} if state.get('closed_body') else {})}
            state.clear()
            state.update(namespace=cfg.namespace, boundary=boundary_id, posted={}, **kept, **prev)
            if head != 'clean':
                state['key'] = head
            if cfg.senders:
                # The run that changed the thread's kind: the new head is its message (`link_threads`)
                state['opener'] = good[-1]['run']['run_id']
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
        quiet = cfg.quiet and head == 'clean'
        # A head posted with a sender drops what the sender says; one posted without (before senders) keeps it
        bare = bool(state.get('head_sender')) if state.get('parent') else cfg.senders
        body = with_card(cfg, parent(cfg, good, boundary, live_rows, stores, quiet=quiet, bare=bare, prev=state.get('previous_url')), card, good[0])
        if not state.get('parent'):
            kind = 'clean' if head == 'clean' else 'issues' if grade(good[-1]) == 'issues' else 'failed'
            send('parent', body, None, scope, cfg.sender(kind))
        if state.get('previous_body'):
            check()
            slack.edit(state['previous'], next_thread(cfg, state['previous_body'], slack.permalink(state['parent'])))
            del state['previous_body']
            persist()
        # New threads bootstrap ten; established threads append every missing run after the newest known one. A quiet
        # thread posts none: its runs are lines of one rolling reply, which each run's link (`link_threads`) names.
        posted = state.setdefault('posted', {})
        todo = [] if quiet else missing(good, posted)
        if quiet:
            lines = trail(cfg, good, live_rows)
            runs = cfg.sender('runs')
            if state.get('rolling') and ('rolling_sender' not in state or face(state['rolling_sender']) != face(runs)):
                # Posted under another icon (an `emoji` / `icons` change, a stock icon's, or unknown: before
                # `rolling_sender` was kept), which an edit can't change (a name alone isn't worth a repost: a reply
                # posted as the app keeps standing once senders are on): posted again, its runs moved to the new reply
                # (`stale`, saved first), then it goes. `rolling_retired` holds it until deleted, so an interrupted
                # report deletes it next time; the repost's intent is its own (`scope`), never the old reply's.
                state['rolling_retired'] = state.pop('rolling')
                state.pop('rolling_body', None)
            retired = state.get('rolling_retired')
            if not state.get('rolling'):
                send('rolling', lines, state['parent'], f'{scope}|{retired}' if retired else scope, runs)
            elif state.get('rolling_body') != lines:
                check()
                slack.edit(state['rolling'], lines)
                state['rolling_body'] = lines
                persist()
            before = dict(posted)
            for row in reversed(good[:10]):
                posted.setdefault(row['run']['run_id'], state['rolling'])
            # Per-run replies from before the thread was quiet: their runs link the rolling reply (saved first), then
            # they go, so a quiet thread converges to its head and the one reply whatever posted it before
            gone = {ts for ts in posted.values() if ts not in (state['rolling'], state['parent'])}
            stale = sorted(gone | {retired} if retired else gone)
            for rid, ts in posted.items():
                if ts in stale:
                    posted[rid] = state['rolling']
            if posted != before:
                persist()
            for ts in stale:
                check()
                slack.delete(ts)
            if retired:
                del state['rolling_retired']
                persist()
        # In a failure thread a run's reply takes the lines its steps' top-level alerts posted (`fold`)
        folding = head != 'clean'
        for rid in sorted(here, key=lambda rid: ended[rid]['run']['started_ts']):
            alerts, extra = fold(cfg, slack, ended[rid]) if folding and not clean(ended[rid]) else ([], '')
            check()
            if cfg.senders:
                # Its ⏳ reply gone first (persisted), so an interrupted report posts its reply as a missing one; in a
                # quiet thread (a ⏳ reply from before it was quiet) the rolling reply lists the run, and nothing posts
                slack.delete(waiting.pop(rid)['ts'])
                state.get('running', {}).pop(rid, None)
                persist()
                if quiet:
                    continue
                text, sender = run_post(cfg, ended[rid])
                send(rid, text + extra, state['parent'], scope, sender)
            else:
                slack.edit(waiting[rid]['ts'], reply(cfg, ended[rid]) + extra)
                posted[rid] = waiting.pop(rid)['ts']
                state.get('running', {}).pop(rid, None)
                persist()
            folded(slack, alerts, posted[rid])
        for row in reversed(todo):
            if row['run']['run_id'] not in posted:
                alerts, extra = fold(cfg, slack, row) if folding and not clean(row) else ([], '')
                text, sender = run_post(cfg, row)
                send(row['run']['run_id'], text + extra, state['parent'], scope, sender)
                folded(slack, alerts, posted[row['run']['run_id']])
        for row in reversed([] if quiet else live_rows):
            if row['run']['run_id'] not in state.get('running', {}):
                text, sender = run_post(cfg, row)
                send('running:' + row['run']['run_id'], text, state['parent'], scope, sender)
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
        if cfg.senders:
            # Nothing more in its old thread: deleted; the thread that took the run has it (its head, `opener`), else
            # its top-level alert in this channel is where it links (`relink`), when there is one
            check()
            slack.delete(waiting.pop(rid)['ts'])
            if not (rid in state.get('posted', {}) and state.get('parent')):
                if ts := slack.alert(cfg.link(row['run']), row['run']['started_ts']):
                    relinked(rid, ts)
            persist()
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


def face(sender: Mapping[str, str] | None) -> str | None:
    """A sender's icon (`icon_emoji` or `icon_url`; None: the app's)."""
    return (sender.get('icon_emoji') or sender.get('icon_url')) if sender else None


def shown(sender: Mapping[str, str] | None, body: str) -> str:
    """A post as a preview shows it: its sender (`Config.sender`: icon and name) on a line of its own, then its body."""
    if not sender:
        return body
    icon = sender.get('icon_emoji') or sender.get('icon_url')
    return f"[{f'{icon} ' if icon else ''}{sender['username']}]\n{body}"


def run(
    raw: Mapping,
    env: Mapping[str, str],
    project: str | None,
    run_id: str | None = None,
    execute: bool = False,
    adopt_check: bool = False,
    final: list[dict] | None = None,
    sink: Sink | None = None,
) -> str:
    """Render the thread (`execute` off), or converge it in Slack; with `sink`, also record each run's reply link there
    (`link_threads`; best effort: a database without the column, migration `scan-run-thread-url`, only logs)."""
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

    def read_card() -> str | None:
        """The status card's URL (`card_url`), or None (off, or none to post: the head goes without it)."""
        if not cfg.card:
            return None
        response = http_fetch(cfg.site, token)('/api/scan-runs/card-url?' + urlencode(card_query(cfg)))
        return card_url(cfg, response.status, response.body)

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
        rows = [r for r in rows if counted(r)]
        if not rows:
            return 'nominal: waiting for a terminal run'
        head, good, boundary = segment(cfg, rows)
        if not good:
            return 'nominal: no clean streak'
        pending_rows = good[:10]
        state = {}
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
        quiet = cfg.quiet and head == 'clean'
        bare = bool(state.get('head_sender')) if state.get('parent') else cfg.senders
        kind = 'clean' if head == 'clean' else 'issues' if grade(good[-1]) == 'issues' else 'failed'
        head_body = shown(cfg.sender(kind), with_card(cfg, parent(cfg, good, boundary, live_rows, stores_text(cfg, read_series()), quiet=quiet, bare=bare), read_card(), good[0]))
        if quiet:
            return '\n\n+++\n\n'.join([head_body, shown(cfg.sender('runs'), trail(cfg, good, live_rows))])
        return '\n\n+++\n\n'.join([head_body, *(shown(run_post(cfg, r)[1], run_post(cfg, r)[0]) for r in [*reversed(pending_rows), *reversed(live_rows)])])
    bot = (env.get('SLACK_BOT_TOKEN') or '').strip()
    if not bot:
        raise ValueError('nominal: need SLACK_BOT_TOKEN')
    with coordinator_lease(lease) as check:
        rows = read_rows()  # Re-read after acquiring the lease, so a waiting writer cannot publish stale state.
        slack = Slack(bot, cfg.channel)
        rows = faces(rows, slack.have)
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
        state = converge(cfg, rows, state, slack, save, check, stores=stores_text(cfg, series), card=read_card())
        if cfg.pin:
            check()
            pin_head(state, slack, save)
        if cfg.canvas:
            check()
            thread = slack.permalink(state['parent']) if state.get('parent') and not state.get('closed') else None
            write_canvas(state, slack, canvas_text(cfg, rows, thread, series, time.time()), cfg.title, save)
        if sink is not None:
            from .scan_runs import thread_url_sql

            check()
            try:
                link_threads(state, slack, lambda links: sink.execute([thread_url_sql(rid, url) for rid, url in links]), save)
            except Exception as e:  # noqa: BLE001 — the thread is posted; its links on /scans are a convenience
                print(f'nominal: thread links not recorded ({type(e).__name__}: {e})', file=sys.stderr)
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


def job_sink(job: JobRun) -> Sink | None:
    """The run's database, for its reply links (`run`'s `sink`); None when it can't be opened (logged): the thread
    still posts."""
    from .scan_runs import err

    try:
        return job.sink()
    except (Exception, SystemExit) as exc:  # noqa: BLE001 — a missing database must not cost the report
        err(job.redact(f'nominal: no database for thread links: {type(exc).__name__}: {exc}'))
        return None


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
        err(run(job.profile.nominal, job.env, job.profile.project, execute=True, sink=job_sink(job)))
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
        err(run(job.profile.nominal, job.env, job.profile.project, job.run_id, execute=True, final=final, sink=job_sink(job)))
        state = final[0] if final else {}
        threaded = state.get('key', 'clean') != 'clean' and job.run_id in state.get('posted', {})
    except (Exception, SystemExit) as exc:  # Reporting must not fail (or end) the job; its failure remains visible in the head log.
        err(job.redact(f'nominal report failed: {type(exc).__name__}: {exc}'))
    finally:
        if deferred and not threaded:
            for message in deferred:
                job.post_alert('fail', message)
