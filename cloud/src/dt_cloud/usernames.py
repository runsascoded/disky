"""Canonical username spelling.

Mirror of ``rigging.provenance`` in marin-community/marin: the sanitize rules
here MUST match ``username_segment()`` there, because that function generates
the ``users/<seg>/`` GCS prefixes this repo attributes. Mirrored (rather than
imported) to keep this repo free of the marin dependency tree; once marin
exposes ``sanitize_username(raw)`` upstream, switch to importing it.
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
