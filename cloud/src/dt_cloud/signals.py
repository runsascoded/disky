"""Pure-path attribution signals over a GCS listing.

Operates on small pre-filtered DataFrames (see ``cli.py``) with the marin
``scan_gcs`` objects columns (``bucket``, ``name``). The Iris task-id
path-shape signal from the spec is deliberately not implemented yet — the
exact tmp/log layouts need verifying against a real listing first.
"""

from __future__ import annotations

import datetime as dt
import posixpath
import re
from dataclasses import dataclass

import pandas as pd

from .identity import IdentityMap

# Only `.artifact.json` carries provenance.built_by; the legacy `.executor_info`
# sidecar records name/config/deps only, so it contributes no user signal.
RECORD_BASENAME = ".artifact.json"
_USERS_PREFIX_RE = re.compile(r"^users/([^/]+)/")


@dataclass(frozen=True)
class AttributionRow:
    """One sparse dir-level ownership assertion (see specs/storage-cost-attribution.md)."""

    prefix: str  # gs://bucket/dir/ (trailing slash)
    user: str | None  # canonical id; None = explicitly nobody (manual un-own rule)
    source: str  # user-prefix | artifact-record | manual
    evidence: str | None
    asof: dt.date


def user_prefix_rows(listing: pd.DataFrame, identities: IdentityMap, asof: dt.date) -> list[AttributionRow]:
    """One row per distinct ``gs://{bucket}/users/<seg>/`` in the listing."""
    pairs = set()
    for bucket, name in zip(listing["bucket"], listing["name"]):
        m = _USERS_PREFIX_RE.match(name)
        if m:
            pairs.add((bucket, m.group(1)))
    rows = []
    for bucket, segment in sorted(pairs):
        user = identities.resolve(segment)
        rows.append(
            AttributionRow(
                prefix=f"gs://{bucket}/users/{segment}/",
                user=user,
                source="user-prefix",
                evidence=None,
                asof=asof,
            )
        )
    return rows


def record_file_paths(listing: pd.DataFrame) -> list[str]:
    """Distinct ``gs://bucket/path/.artifact.json`` file paths in the listing."""
    paths = set()
    for bucket, name in zip(listing["bucket"], listing["name"]):
        if posixpath.basename(name) == RECORD_BASENAME:
            paths.add(f"gs://{bucket}/{name}")
    return sorted(paths)


def manual_rows(identities: IdentityMap, asof: dt.date) -> list[AttributionRow]:
    return [
        AttributionRow(
            prefix=owner.prefix,
            user=owner.user,
            source="manual",
            evidence=None,
            asof=asof,
        )
        for owner in identities.prefix_owners
    ]
