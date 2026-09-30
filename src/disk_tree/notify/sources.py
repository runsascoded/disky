"""Row sources for the digest: where a profile's ``(date, meta)`` pairs come
from. The engine and profile don't know or care about the source — the CLI
picks one, loads the period's rows, and hands them to the converge lifecycle.

:func:`scan_rows` reads disk-tree's own ``Scan`` DB (a bucket's scans over
time). The pure windowing + row-building is :func:`rows_from_totals`, unit-tested
without a DB; :func:`scan_totals` is the thin DB read.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .digest import Period, select_window

if TYPE_CHECKING:
    from .profile import DigestProfile, Row


def scan_totals(uri: str) -> list[tuple[str, int]]:
    """``(date, root_bytes)`` for each scan of ``uri``, one per day (the latest
    that day wins), in date order. Reads the ``Scan`` DB; scans with no recorded
    ``size`` are skipped."""
    from disk_tree.backends import canonical
    from disk_tree.sqla import Scan, init

    db = init()
    scans = (
        db.session.query(Scan)
        .filter(Scan.path == canonical(uri))
        .order_by(Scan.time)
        .all()
    )
    by_day: dict[str, int] = {}
    for s in scans:
        if s.size is not None:
            by_day[s.time.date().isoformat()] = s.size
    return sorted(by_day.items())


def rows_from_totals(profile: DigestProfile, totals: list[tuple[str, int]], period: Period) -> list[Row]:
    """Window ``totals`` (sorted ``(date, total_bytes)``) to ``period`` plus one
    lead-in scan, build the profile's rows, and slice the lead-in off. Pure."""
    dates = [d for d, _ in totals]
    window, has_lead_in = select_window(dates, period)
    if not window:
        return []
    total = dict(totals)
    dated_meta = [(d, {"total_bytes": total[d]}) for d in window]
    rows = profile.rows_from_meta(dated_meta)
    return rows[1:] if has_lead_in else rows


def scan_rows(profile: DigestProfile, uri: str, period: Period) -> list[Row]:
    """``period``'s digest rows for ``uri`` from the ``Scan`` DB."""
    return rows_from_totals(profile, scan_totals(uri), period)
