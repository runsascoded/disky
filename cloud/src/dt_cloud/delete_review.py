"""Require a native deletion plan to match reviewed keys and sizes exactly."""
from __future__ import annotations

import json
from pathlib import Path


def validate(review: Path, root: str, files: dict[str, int], *, d1: bool = False) -> None:
    """An optional review adds a gate; it never bypasses the native planner's safety checks.

    Format: {roots: [{root, files: [{key, bytes}]}], d1_changes: false}.
    Other review fields are informational. This gate does not authorize deletion.
    """
    doc = json.loads(review.read_text())
    roots = [r for r in doc['roots'] if r['root'].rstrip('/') == root.rstrip('/')]
    if len(roots) != 1:
        raise ValueError(f'deletion review: expected exactly one entry for {root}')
    if d1 and doc.get('d1_changes') is not True:
        raise ValueError('deletion review: D1 changes were not reviewed')
    rows = roots[0]['files']
    expected = {r['key']: r['bytes'] for r in rows}
    if len(expected) != len(rows):
        raise ValueError('deletion review: duplicate keys')
    if any(not k or k.startswith('/') or any(p in ('', '.', '..') for p in k.split('/'))
           or type(n) is not int or n < 0 for k, n in expected.items()):
        raise ValueError('deletion review: invalid key or size')
    extra = sorted(files.keys() - expected.keys())
    missing = sorted(expected.keys() - files.keys())
    changed = sorted(k for k in files.keys() & expected.keys() if files[k] != expected[k])
    if extra or missing or changed:
        raise ValueError(f'deletion review mismatch: {len(extra)} extra, {len(missing)} missing, {len(changed)} size changes; nothing deleted')
