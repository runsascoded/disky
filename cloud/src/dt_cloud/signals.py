"""Path signals over a listing: rows of the sparse dir → user attribution table."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from .identity import IdentityMap


@dataclass(frozen=True)
class AttributionRow:
    """One sparse dir-level ownership assertion (see specs/storage-cost-attribution.md)."""

    prefix: str  # gs://bucket/dir/ (trailing slash)
    user: str | None  # canonical id; None = explicitly nobody (manual un-own rule)
    source: str  # user-prefix | manual | a deployment's own producer's name
    evidence: str | None
    asof: dt.date


def user_dir_rows(dirs: Iterable[tuple[str, str, str]], identities: IdentityMap, asof: dt.date) -> list[AttributionRow]:
    """One `user-prefix` row per distinct home dir: `(bucket, lead, segment)` is `gs://<bucket>/<lead><segment>/`,
    owned by `segment`'s user (`lead` from a `user_dirs` pattern, `identity.user_dir_lead`)."""
    return [
        AttributionRow(
            prefix=f"gs://{bucket}/{lead}{segment}/",
            user=identities.resolve(segment),
            source="user-prefix",
            evidence=None,
            asof=asof,
        )
        for bucket, lead, segment in sorted(set(dirs))
    ]


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
