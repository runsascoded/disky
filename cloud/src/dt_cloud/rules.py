"""Validate identities.yaml and export it (with trailing comments) as JSON.

The export feeds the site's "attribution rules" section so the manual mapping
layer (users, aliases, prefix_owners) is reviewable by everyone the
numbers get shown to. Trailing ``# ...`` comments on user/prefix rows survive
into the JSON as ``note`` fields (that's where the "TODO confirm" caveats
live), so the parse is line-based alongside the YAML load.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from functools import partial
from pathlib import Path

import yaml

from .identity import read_identities

err = partial(print, file=sys.stderr)


def parse_notes(text: str) -> tuple[dict[str, str], dict[str, str]]:
    """Trailing comments keyed by user id / prefix (first one wins per entry)."""
    user_notes: dict[str, str] = {}
    prefix_notes: dict[str, str] = {}
    section = None
    cur_user = None
    cur_prefix = None
    for line in text.splitlines():
        if re.match(r"^users:", line):
            section = "users"
            continue
        if re.match(r"^prefix_owners:", line):
            section = "prefix"
            continue
        if section == "users":
            if m := re.match(r"^  ([a-z0-9_-]+):", line):
                cur_user = m.group(1)
                continue
            if cur_user and (m := re.match(r"^    (?:aliases):.*?#\s*(.*\S)\s*$", line)):
                user_notes.setdefault(cur_user, m.group(1))
        elif section == "prefix":
            if m := re.match(r"^  - prefix: (\S+)", line):
                cur_prefix = m.group(1)
                continue
            if cur_prefix and (m := re.match(r"^    (?:user):.*?#\s*(.*\S)\s*$", line)):
                prefix_notes.setdefault(cur_prefix, m.group(1))
    return user_notes, prefix_notes


def check_rules(doc: dict) -> list[str]:
    """Consistency findings (empty list = clean)."""
    findings = []
    users: dict = doc.get("users") or {}
    alias_counts: Counter[str] = Counter()
    for user, row in users.items():
        row = row or {}
        for alias in row.get("aliases") or []:
            alias_counts[alias] += 1
            if alias == user:
                findings.append(f"user {user}: alias {alias!r} is redundant (equals canonical id)")
            elif alias in users:
                findings.append(f"alias {alias!r} (of {user}) shadows canonical user id {alias!r}")
    for alias, n in alias_counts.items():
        if n > 1:
            findings.append(f"alias {alias!r} appears under {n} users")
    seen_prefixes: Counter[str] = Counter()
    for row in doc.get("prefix_owners") or []:
        prefix = row.get("prefix", "")
        seen_prefixes[prefix] += 1
        if not prefix.startswith("gs://") or not prefix.endswith("/"):
            findings.append(f"prefix_owners: {prefix!r} must look like gs://bucket/path/")
        if (user := row.get("user")) and user not in users:
            findings.append(f"prefix_owners {prefix}: user {user!r} not in users map")
    for prefix, n in seen_prefixes.items():
        if n > 1:
            findings.append(f"prefix_owners: {prefix!r} listed {n} times")
    return findings


def export_rules(identities_path: str | Path) -> tuple[dict, list[str]]:
    """(site JSON payload, findings) for identities.yaml."""
    text = read_identities(identities_path)
    doc = yaml.safe_load(text)
    user_notes, prefix_notes = parse_notes(text)
    findings = check_rules(doc)
    users = [
        {
            "u": user,
            "aliases": (row or {}).get("aliases") or [],
            **({"name": row["name"]} if (row or {}).get("name") else {}),
            **({"github": row["github"]} if (row or {}).get("github") else {}),
            **({"note": user_notes[user]} if user in user_notes else {}),
        }
        for user, row in (doc.get("users") or {}).items()
    ]
    prefix_owners = [
        {
            "prefix": row["prefix"],
            **({"user": row["user"]} if row.get("user") else {}),
            **({"note": prefix_notes[row["prefix"]]} if row["prefix"] in prefix_notes else {}),
        }
        for row in doc.get("prefix_owners") or []
    ]
    return {"users": users, "prefix_owners": prefix_owners}, findings
