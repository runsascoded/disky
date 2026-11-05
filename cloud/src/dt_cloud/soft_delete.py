"""`dt-cloud soft-delete`: each bucket's soft-delete retention now, asked of GCS.

A sweep's undo restores from GCS soft delete, so it works only while the bucket still has a soft-delete policy: with the
policy off, GCS refuses to restore (or list) a soft-deleted version, whatever the bucket retained when the object was
deleted. The site's own account can't read bucket metadata, so the job's `soft-delete` step (`scan_steps`) writes what
its account reads, one document a day under `snapshots/soft-delete/`:

    {"version": 1, "date": "2026-11-05", "as_of": "2026-11-05T07:12:03Z",
     "buckets": {"<bucket>": {"retention_seconds": 604800}, …}}        # 0 = soft delete off

The site reads the newest (`site/functions/_lib/softDeleteNow.ts`): a recorded undo on a bucket it shows off reads
"undo unavailable", and a bucket it doesn't name is unknown there. `sweep undo` doesn't read it: it asks GCS itself
(`sweep_exec.undo_run`). Read-only, except `-o`'s document.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from click import UsageError, command, option

#: The `-o` document's shape; the site reads only a version it knows.
SNAPSHOT_VERSION = 1


def retention_seconds(meta: Mapping) -> int:
    """A Storage JSON API bucket resource's soft-delete retention; 0: off (a bucket with no policy reads the same)."""
    return int(meta.get("softDeletePolicy", {}).get("retentionDurationSeconds", 0))


def snapshot(metas: Mapping[str, Mapping], as_of: str) -> dict:
    """`metas` (bucket → its JSON API resource) as the day's document; `as_of`: when they were read (RFC 3339 UTC)."""
    return {
        "version": SNAPSHOT_VERSION,
        "date": as_of[:10],
        "as_of": as_of,
        "buckets": {name: {"retention_seconds": retention_seconds(meta)} for name, meta in sorted(metas.items())},
    }


def render(snap: Mapping) -> str:
    """One line per bucket: `<bucket>  off` or `<bucket>  7d` (`3600s` for a retention that isn't whole days)."""
    def held(seconds: int) -> str:
        return "off" if seconds == 0 else f"{seconds // 86400}d" if seconds % 86400 == 0 else f"{seconds}s"

    width = max(len(name) for name in snap["buckets"])
    return "".join(f"{name:<{width}}  {held(b['retention_seconds'])}\n" for name, b in snap["buckets"].items())


@command("soft-delete")
@option("-b", "--bucket", "buckets", multiple=True, help="A bucket to read (repeatable)")
@option("-j", "--json", "as_json", is_flag=True, help="The document as JSON")
@option("-o", "--out", "outs", multiple=True, help="Write the day's document, `<OUT>/<date>.json` (a dir or URL; repeatable), and print its locations instead")
def soft_delete_cmd(buckets: Sequence[str], as_json: bool, outs: Sequence[str]) -> None:
    """Each `-b` bucket's soft-delete retention now, read from GCS (`storage.buckets.get`): what a sweep's undo has to
    work with. Read-only, except `-o`'s daily document (the job's `soft-delete` step; the site's "undo unavailable")."""
    from .deploy_cost import _rfc3339, clock, fetch_buckets, write_snapshot

    if not buckets:
        raise UsageError("no bucket: pass -b")
    snap = snapshot(fetch_buckets(sorted(set(buckets))), _rfc3339(clock()))
    if outs:
        for written in write_snapshot(snap, outs):
            print(written)
        return
    print(json.dumps(snap, indent=2) if as_json else render(snap), end="\n" if as_json else "")
