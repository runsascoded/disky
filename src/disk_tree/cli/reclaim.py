"""`disk-tree reclaim` — how much deleting these paths would *actually* free.

Scan sizes are per-path block counts, so anything sharing extents with a cache
(uv and pnpm both clone out of theirs) is charged twice and the reported size
overstates what a deletion recovers. This asks the filesystem instead.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from click import argument, option
from humanize import naturalsize
from utz import err

from disk_tree.cli.base import cli
from disk_tree.extents import SUPPORTED, default_partners, measure


def _fmt(n: int, human: bool) -> str:
    return naturalsize(n, binary=True, format='%.3g') if human else str(n)


@cli.command('reclaim')
@option('-H', '--no-human', is_flag=True, help='Print raw bytes instead of human-readable sizes')
@option('-j', '--json', 'as_json', is_flag=True, help='Emit JSON')
@option('-p', '--partner', 'partners', multiple=True, help='Extra root that will survive the deletion (repeatable); defaults to the uv/pnpm caches')
@option('-P', '--no-default-partners', is_flag=True, help='Skip the auto-detected uv/pnpm caches')
@argument('paths', nargs=-1, required=True)
def reclaim_cmd(no_human: bool, as_json: bool, partners: tuple[str, ...], no_default_partners: bool, paths: tuple[str, ...]):
    """Estimate the space freed by deleting PATHS, discounting shared extents."""
    if not SUPPORTED:
        raise SystemExit(f'physical extent mapping is Darwin-only (got {sys.platform})')
    roots = list(partners)
    if not no_default_partners:
        roots += default_partners()
    err(f'mapping {len(paths)} subtree(s)…')
    rec = measure(list(paths), roots)
    err(f'scanned {rec.partner_files:,} partner files under {len(rec.partners)} root(s)')

    shared_set = rec.shared_set

    rows: list[dict[str, Any]] = []
    for s in rec.subtrees:
        shared = sum(length for _, length in s.extents & shared_set)
        rows.append({
            'path': s.path,
            'apparent': s.apparent,
            'shared': shared,
            'frees': s.apparent - shared,
            'n_files': s.n_files,
            'n_errors': s.n_errors,
        })

    if as_json:
        print(json.dumps({
            'partners': rec.partners,
            'apparent': rec.apparent,
            'shared': rec.shared,
            'frees': rec.unique,
            'rows': rows,
        }, indent=2))
        return

    human = not no_human
    w = max(4, max(len(r['path']) for r in rows))
    print(f"{'apparent':>9}  {'shared':>9}  {'frees':>9}  {'files':>9}  path")
    for r in rows:
        print(f"{_fmt(r['apparent'], human):>9}  {_fmt(r['shared'], human):>9}  {_fmt(r['frees'], human):>9}  {r['n_files']:>9,}  {r['path']}")
    if len(rows) > 1:
        print(f"{_fmt(rec.apparent, human):>9}  {_fmt(rec.shared, human):>9}  {_fmt(rec.unique, human):>9}  {sum(r['n_files'] for r in rows):>9,}  TOTAL")
    if rec.partners:
        print(f"(partners kept: {', '.join(rec.partners)})")
