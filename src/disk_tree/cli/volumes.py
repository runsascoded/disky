"""`disk-tree volumes [PATH]` — the APFS container PATH lives on: every volume's
usage, its mount point and snapshots, and the container's free space. What a
scan of one tree can't show (Preboot, VM/swap, Recovery, the sealed System
volume, OS-update snapshots). macOS only."""

from __future__ import annotations

import json
import sys

from click import argument, option
from humanize import naturalsize

from disk_tree.cli.base import cli


@cli.command('volumes')
@option('-H', '--no-human', is_flag=True, help='Print raw bytes instead of human-readable sizes')
@option('-j', '--json', 'as_json', is_flag=True, help='Emit JSON')
@argument('path', default='/')
def volumes_cmd(no_human: bool, as_json: bool, path: str):
    """The APFS container holding PATH (default `/`): volumes, usage, snapshots."""
    from disk_tree.apfs import container_for

    if sys.platform != 'darwin':
        raise SystemExit(f'`volumes` reads APFS via diskutil (macOS only; got {sys.platform})')
    c = container_for(path)
    if as_json:
        print(json.dumps(c.to_json(), indent=2))
        return
    fmt = (lambda n: str(n)) if no_human else (lambda n: naturalsize(n, binary=True, format='%.1f'))
    print(f'container {c.device}: {fmt(c.used)} used of {fmt(c.capacity)}, {fmt(c.free)} free')
    for v in c.volumes:
        roles = ','.join(v.roles) or '-'
        print(f'  {fmt(v.used):>10}  {v.name:<16} {roles:<10} {v.device:<10} {v.mount or "(not mounted)"}')
        for s in v.snapshots:
            flags = ', '.join(f for f, on in [('purgeable', s.purgeable), ('limits shrink', s.limits_shrink)] if on)
            print(f'              snapshot {s.name}' + (f' ({flags})' if flags else ''))
