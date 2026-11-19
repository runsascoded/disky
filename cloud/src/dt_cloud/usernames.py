"""Canonical username spelling: a path-safe segment.

Every spelling of a person (a login, an email, a home dir's name) reduces to it, so a home dir
named by the same rule resolves to its user with no alias entry. A deployment whose tooling names
home dirs by its own rule keeps that rule in step with this one or covers the difference with
aliases (gcs: Marin's `username_segment`, the same rule).
"""

from __future__ import annotations

import re

# A path segment is lowercase alphanumerics plus '_' and '-'; collapse anything else.
_USER_SEGMENT_RE = re.compile(r"[^a-z0-9_-]+")


def sanitize_username(raw: str) -> str:
    """Reduce a raw login spelling to the canonical path-safe user segment.

    An email-like login drops its domain but keeps the whole local name
    (``russell.power@host`` -> ``russell-power``), and the result is lowercased
    with any remaining character collapsed to ``-``. Raises ``RuntimeError`` if
    nothing usable remains.
    """
    name = raw.strip()
    if "@" in name:
        name = name.split("@", 1)[0]
    segment = _USER_SEGMENT_RE.sub("-", name.lower()).strip("-")
    if not segment:
        raise RuntimeError(f"username {raw!r} did not sanitize to a usable path segment")
    return segment
