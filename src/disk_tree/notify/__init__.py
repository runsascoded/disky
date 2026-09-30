"""disk-tree notify/digest engine (``specs/comms-notify.md``).

A generic Slack + Discord digest mechanism — converge state, one thread per
period, an OP edited in place, day-keyed replies, hosted plot — driven by a
per-deployment ``DigestProfile`` (body builder, plot panels, metric semantics).
Generalized from ``marin-gcs-usage``'s ``dt_cloud`` comms.

``discord_api`` is stdlib-only; the posting engine needs the ``notify`` extra
(``thrds``), and the plot the ``plot`` extra (``kaleido``, already core).
"""
from __future__ import annotations

from .digest import (
    Period,
    converge_discord,
    converge_slack,
    deg,
    discordify,
    emoji_name,
    period_of,
    select_window,
)
from .profile import BytesProfile, BytesRow, DigestProfile, Row

__all__ = [
    "BytesProfile",
    "BytesRow",
    "DigestProfile",
    "Period",
    "Row",
    "converge_discord",
    "converge_slack",
    "deg",
    "discordify",
    "emoji_name",
    "period_of",
    "select_window",
]
