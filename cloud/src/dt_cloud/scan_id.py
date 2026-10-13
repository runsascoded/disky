"""Scan ids: a scan's identity is its id, never its date (specs/scan-ids-not-dates.md).

A scan id is date-only ``YYYY-MM-DD`` or timed ``YYYY-MM-DDTHHMM`` (UTC — a job's
``SNAP_ID``); any deployment may scan at any cadence. A day may hold several scans, so a
date-ish argument is a *prefix*: it resolves to the latest scan whose id starts
with it (`latest_scan`), the same rule as the site's `site/src/scanSlug.ts`.
"""
from __future__ import annotations

import datetime as dt
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

# The one scan-id pattern (use `.fullmatch`); `static_names` and the rest import it from here.
SCAN_ID = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2}))?")


# A published scan's `meta.json` path (`…/<scan id>/meta.json`); group 1 is the id.
META_PATH = re.compile(rf"/({SCAN_ID.pattern})/meta\.json$")


def is_scan_id(value: object) -> bool:
    """A full, calendar-valid scan id (``YYYY-MM-DD`` or ``YYYY-MM-DDTHHMM``)."""
    if not isinstance(value, str) or not (m := SCAN_ID.fullmatch(value)):
        return False
    y, mo, d, hh, mm = m.groups()
    try:
        dt.date(int(y), int(mo), int(d))
    except ValueError:
        return False
    return hh is None or (int(hh) <= 23 and int(mm) <= 59)


def check_scan_id(value: str) -> str:
    """`value` if it's a scan id, else a `ValueError` naming both accepted forms."""
    if not is_scan_id(value):
        raise ValueError(f"not a scan id: {value!r} (want YYYY-MM-DD or YYYY-MM-DDTHHMM)")
    return value


def scan_id_option(ctx, param, value):  # noqa: ANN001 — a click callback
    """Click callback: validate an optional scan-id option/argument."""
    from click import BadParameter

    if value is None:
        return None
    try:
        return check_scan_id(value)
    except ValueError as e:
        raise BadParameter(str(e)) from None


def scan_time(scan: str) -> dt.datetime:
    """A scan id's UTC instant; a date-only id reads as its midnight (`site/src/scanSlug.ts` `scanTime`)."""
    if not (m := SCAN_ID.fullmatch(scan)):
        raise ValueError(f"not a scan id: {scan!r}")
    y, mo, d, hh, mm = m.groups()
    return dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), tzinfo=dt.timezone.utc)


# Date-only scan ids → their start minute (``YYYY-MM-DDTHHMM``, UTC; `start_key`): only the ids whose start is
# known, and only date-only ids on a day with another scan need one (`times_needed`). `scanSlug.ts` `ScanTimes`.
ScanTimes = Mapping[str, str]


def start_key(scan: str, started: object) -> str | None:
    """A date-only scan's start key from its ISO `started` instant (``meta.started``): the UTC minute, when it falls
    on the id's own day — else None (a start on another day can't key that day's scan; the midnight form stands)."""
    if len(scan) != 10 or not is_scan_id(scan) or not isinstance(started, str):
        return None
    try:
        t = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        return None
    t = t.astimezone(dt.timezone.utc)
    return f"{scan}T{t:%H%M}" if t.strftime("%Y-%m-%d") == scan else None


def times_needed(scans: Iterable[str]) -> list[str]:
    """The date-only scans whose start matters: those sharing their day with another scan (`scanSlug.ts`
    `timesNeeded`). A single-scan day's ``261009`` is already unique."""
    scans = list(scans)
    per_day = Counter(s[:10] for s in scans)
    return [s for s in scans if len(s) == 10 and per_day[s] > 1]


def scan_key(scan: str, times: ScanTimes | None = None) -> str:
    """A scan id's matching key (`scanSlug.ts` `scanKey`): a timed id is itself; a date-only id its start
    (`times`), else its midnight ``T0000`` — the instant `scan_time` gives it. A date and its ``T0000`` are one
    instant, so `check_order` refuses the pair: the key never collides with a real scan."""
    return ((times or {}).get(scan) or f"{scan}T0000") if len(scan) == 10 else scan


def scan_under(scan: str, prefix: str, times: ScanTimes | None = None) -> bool:
    """Whether `scan` falls under the decoded `prefix`: by id, by key, or (a date-only scan) by its midnight alias,
    which stays valid when its start is known so a midnight link keeps resolving (`scanSlug.ts` `scanUnder`)."""
    return (
        scan.startswith(prefix)
        or scan_key(scan, times).startswith(prefix)
        or (len(scan) == 10 and f"{scan}T0000".startswith(prefix))
    )


def scan_slug(scan: str, times: ScanTimes | None = None) -> str:
    """A scan id's exact `?d=` slug (`scanSlug.ts` `exactSlug`), resolving to
    exactly that scan: dashless `YYMMDDHHMM`, the year's leading `20` dropped —
    `2026-09-15T0001` → `2609150001`, and a date-only `2026-09-15` its start
    (`times`), else its midnight, `2609150000` (never `260915`, the day slug:
    that day's *latest* scan)."""
    if not SCAN_ID.fullmatch(scan):
        raise ValueError(f"not a scan id: {scan!r}")
    k = scan_key(scan, times)
    return f"{k[2:4]}{k[5:7]}{k[8:10]}{k[11:15]}"


def _period_end(prefix: str) -> dt.datetime:
    """The UTC end of the period a decoded prefix (``YYYY-MM-DD[THH[MM]]``) names: its day, hour or minute."""
    start = scan_time(prefix if len(prefix) != 13 else f"{prefix}00")
    return start + (dt.timedelta(days=1) if len(prefix) == 10 else dt.timedelta(hours=1) if len(prefix) == 13 else dt.timedelta(minutes=1))


def names_for_good(
    prefix: str,
    scan: str,
    scans: Iterable[str],
    times: ScanTimes | None = None,
    now: dt.datetime | None = None,
) -> bool:
    """Whether the decoded `prefix` names `scan` alone among `scans`, and always will (`scanSlug.ts` `namesForGood`):
    a minute always; a day or an hour once it has ended (`now`, default the current time), and an hour only on a day
    whose date-only scan (if any) has a known start — an unknown one could later be keyed into any of its hours."""
    scans = list(scans)
    if [s for s in scans if scan_under(s, prefix, times)] != [scan]:
        return False
    if len(prefix) >= 15:
        return True
    if (now or dt.datetime.now(dt.timezone.utc)) < _period_end(prefix):
        return False
    day = prefix[:10]
    return len(prefix) == 10 or not any(s == day and not (times or {}).get(s) for s in scans)


def min_slug(
    scan: str,
    scans: Iterable[str],
    times: ScanTimes | None = None,
    now: dt.datetime | None = None,
) -> str:
    """A scan's canonical `?d=` slug (`scanSlug.ts` `minSlug`): the shortest of its key's day (``261008``), hour
    (``26100904``) and minute (``2610091236``) that names it for good (`names_for_good`) — a day or an hour once it
    has ended and holds no other scan, else the minute (`scan_slug`, also the fallback when `scan` isn't in `scans`).
    The longer forms keep resolving to it, so a link already posted never breaks."""
    scans = list(scans)
    k = scan_key(scan, times)
    for p in (k[:10], k[:13]):
        if names_for_good(p, scan, scans, times, now):
            return f"{p[2:4]}{p[5:7]}{p[8:10]}{p[11:13]}"
    return scan_slug(scan, times)


# A slug: dashless compact `YYMMDD[HH[MM]]` (canonical; 8 digits are always
# YYMMDDHH), the legacy `YYMMDD-HH[MM]` / `YYMMDDTHH[MM]`, or ISO
# `YYYY-MM-DD[THH[MM]]`.
_COMPACT = re.compile(r"(\d{2})(\d{2})(\d{2})(?:[T-]?(\d{2})(\d{2})?)?")
_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})(?:T(\d{2})(\d{2})?)?")


def slug_prefix(slug: str) -> str | None:
    """A slug → the scan-id prefix it names (`26100912` → `2026-10-09T12`), or
    None if it isn't one (`scanSlug.ts` `decodeScan`, minus its year-less forms)."""
    if m := _COMPACT.fullmatch(slug):
        y, mo, d, hh, mm = m.groups()
        y = f"20{y}"
    elif m := _ISO.fullmatch(slug):
        y, mo, d, hh, mm = m.groups()
    else:
        return None
    try:
        dt.date(int(y), int(mo), int(d))
    except ValueError:
        return None
    if (hh and int(hh) > 23) or (mm and int(mm) > 59):
        return None
    return f"{y}-{mo}-{d}" + (f"T{hh}{mm or ''}" if hh else "")


def latest_scan(prefix: str, scans: Iterable[str], times: ScanTimes | None = None) -> str | None:
    """The latest scan (any order in) under `prefix`, by key (`scan_key`; a tie breaks by id) — so a date-only scan
    sits at its start when known, else its midnight; None if none."""
    matches = [s for s in scans if scan_under(s, prefix, times)]
    return max(matches, key=lambda s: (scan_key(s, times), s)) if matches else None


def resolve_slug(slug: str, scans: Iterable[str], times: ScanTimes | None = None) -> str | None:
    """The resolver: the latest scan a slug names (`261009` a day, `26100912` an
    hour, `2610091236` a minute; legacy and ISO spellings too), or None — a miss,
    never the nearest or latest scan instead."""
    prefix = slug_prefix(slug)
    return latest_scan(prefix, scans, times) if prefix else None


def snapshot_scans(data_root: Path) -> list[str]:
    """The scan dirs under a site-data root (``<root>/<scan id>/meta.json``),
    newest first — `scans.json`'s contents. Timed ids included."""
    return sorted(
        (p.name for p in data_root.iterdir() if p.is_dir() and is_scan_id(p.name) and (p / "meta.json").exists()),
        reverse=True,
    )


def scan_epoch(scan: str) -> int:
    """A scan id's UTC instant as epoch seconds (`scan_time`): a version's stamp in the static name index."""
    return int(scan_time(check_scan_id(scan)).timestamp())


def scan_label(ts: int) -> str:
    """Epoch seconds as a scan-id-shaped label: the date at midnight, else the date and minute (reports only)."""
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d" if ts % 86400 == 0 else "%Y-%m-%dT%H%M")


def check_order(scans: list[str] | tuple[str, ...] | set[str] | dict) -> list[str]:
    """`scans` sorted, checked to be strictly increasing in time too: string order is time order except for a date and
    its `T0000` scan, one instant, which is refused (a stamp could not say which scan it was)."""
    out = sorted(scans)
    for a, b in zip(out, out[1:]):
        if scan_epoch(a) >= scan_epoch(b):
            raise ValueError(f"scans {a} and {b}: ids out of time order (stamps {scan_epoch(a)}, {scan_epoch(b)})")
    return out
