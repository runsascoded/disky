"""Canonical identity map for storage attribution.

Loads a deployment's ``identities.yaml`` and resolves raw username spellings (Iris job
owners, provenance ``built_by``, ``users/<seg>/`` path segments) to canonical
user ids. The canonical spelling is
:func:`dt_cloud.usernames.sanitize_username` output, so a raw spelling that
sanitizes directly to a canonical id needs no alias entry; aliases cover
everything else.

Ownership has one axis: a person. There is no group/team facet (excised
2026-09-06 — it let most of the fleet sit under a "communal" label with nobody
to sign off on keeping or deleting it).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .usernames import sanitize_username

# The roster is deployment data (people's handles and aliases), never part of
# the base: each deployment keeps its map outside the repo — a local path or an
# fsspec URL (`gs://…`, `r2://…`) — and points `-i` or this variable at it.
IDENTITIES_ENV = "DT_CLOUD_IDENTITIES"


def read_identities(src: str | Path) -> str:
    """The identity map's YAML text, from a local path or an fsspec URL."""
    s = str(src)
    if "://" not in s:
        return Path(s).read_text()
    import fsspec

    with fsspec.open(s, "r") as f:
        return f.read()


@dataclass(frozen=True)
class PrefixOwner:
    """Manual owner for a prefix no per-user signal covers. ``user=None`` is an
    explicit "nobody" — a deeper rule that un-owns a subtree of a user prefix."""

    prefix: str
    user: str | None = None


@dataclass(frozen=True)
class IdentityMap:
    users: frozenset[str]
    alias_to_user: dict[str, str]
    prefix_owners: tuple[PrefixOwner, ...]

    def resolve(self, raw: str) -> str:
        """Canonical user id for a raw spelling.

        An unaliased spelling resolves to its own sanitized segment: a
        ``users/<seg>/`` prefix is definitionally owned by ``<seg>``, so an
        unmapped user is still attributed rather than dropped. Unmapped users
        are surfaced by reports to drive map curation.
        """
        segment = sanitize_username(raw)
        return self.alias_to_user.get(segment, segment)

    def known(self, user: str) -> bool:
        return user in self.users


def load_identities(src: str | Path) -> IdentityMap:
    """Load and validate the identity map; raises ``ValueError`` on a bad map."""
    doc = yaml.safe_load(read_identities(src))
    users: set[str] = set()
    alias_to_user: dict[str, str] = {}
    for user in (doc.get("users") or {}):
        if sanitize_username(user) != user:
            raise ValueError(f"canonical user id {user!r} is not in sanitized form")
        users.add(user)
    for user, entry in (doc.get("users") or {}).items():
        for alias in (entry or {}).get("aliases") or ():
            segment = sanitize_username(alias)
            if segment in users and segment != user:
                raise ValueError(f"alias {alias!r} of {user!r} collides with canonical user {segment!r}")
            existing = alias_to_user.get(segment)
            if existing is not None and existing != user:
                raise ValueError(f"alias {alias!r} ({segment!r}) maps to both {existing!r} and {user!r}")
            alias_to_user[segment] = user
    prefix_owners = []
    for row in doc.get("prefix_owners") or ():
        if "prefix" not in row:
            raise ValueError(f"prefix_owner row without a prefix: {row!r}")
        prefix_owners.append(PrefixOwner(prefix=row["prefix"], user=row.get("user")))
    return IdentityMap(
        users=frozenset(users),
        alias_to_user=alias_to_user,
        prefix_owners=tuple(prefix_owners),
    )
