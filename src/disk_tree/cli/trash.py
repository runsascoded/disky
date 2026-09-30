"""`disk-tree trash`: what the laptop drainer trashed (`disk_tree.trash`) —
list it, put a run back, or empty it to actually free the bytes."""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from functools import partial

from click import argument, group, option
from humanize import naturalsize

from disk_tree import trash
from disk_tree.cli.base import cli

err = partial(print, file=sys.stderr)


def parse_duration(s: str) -> int:
    """`7d`, `12h`, `30m`, `90s` (or bare seconds) → seconds."""
    m = re.fullmatch(r"(\d+)\s*([smhd]?)", s.strip())
    if not m:
        raise ValueError(f"bad duration {s!r} (expected e.g. 7d, 12h, 30m, 90s)")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


@cli.group("trash")
def trash_group():
    """What the laptop drainer trashed: list, restore, or empty (free) it."""


@trash_group.command("ls")
def ls_cmd():
    """List trashed runs (oldest first) with what emptying each would free."""
    rs = trash.runs()
    if not rs:
        err(f"nothing in {trash.trash_root()}")
        return
    for r in rs:
        when = datetime.fromtimestamp(r.ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        print(f"{r.run_id}\t{when}\t{naturalsize(r.bytes, binary=True)}\t{r.files} file(s)")


@trash_group.command("restore")
@argument("run_id")
def restore_cmd(run_id: str):
    """Put a run's paths back where they were (a path that exists again is skipped)."""
    r = trash.restore(run_id)
    for p in r.restored:
        print(p)
    for p in r.skipped:
        err(f"skipped (exists again): {p}")
    err(f"{run_id}: restored {len(r.restored)} path(s), skipped {len(r.skipped)}")


@trash_group.command("empty")
@option("-f", "--for-real", is_flag=True, help="Actually empty (default: report what would be freed)")
@option("-o", "--older-than", default=None, help="Empty every run trashed longer ago than this (e.g. 7d) instead of one RUN_ID")
@argument("run_id", required=False)
def empty_cmd(for_real: bool, older_than: str | None, run_id: str | None):
    """Free a trashed run's bytes (`rm -rf` its trash dir) — one run, or every run past a TTL."""
    if (run_id is None) == (older_than is None):
        raise SystemExit("empty: give RUN_ID or --older-than, not both")
    targets = trash.expired(parse_duration(older_than)) if older_than else [r for r in trash.runs() if r.run_id == run_id]
    if run_id and not targets:
        raise SystemExit(f"{run_id}: not in {trash.trash_root()}")
    total = 0
    for r in targets:
        if for_real:
            nbytes, files = trash.empty(r.run_id)
            err(f"emptied {r.run_id}: {naturalsize(nbytes, binary=True)}, {files} file(s)")
            total += nbytes
        else:
            err(f"would empty {r.run_id}: {naturalsize(r.bytes, binary=True)}, {r.files} file(s)")
            total += r.bytes
    err(f"{'freed' if for_real else 'would free'} {naturalsize(total, binary=True)} across {len(targets)} run(s)" + ("" if for_real else " (-f to do it)"))
