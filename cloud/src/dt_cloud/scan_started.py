"""A scan's start: the earliest ``started`` across its bucket listings' ``_SUCCESS.json`` markers
(``disk_tree/find/bulk*.py``), recorded as ``meta.started`` (specs/scan-ids-not-dates.md, "Remaining" item 1).

A date-only scan id (``2026-10-09``) can't say when in its day it ran; on a day with another scan, the site keys
it by this start (``2610090430``) instead of its midnight (`scan_id.start_key`). ``path-index`` stamps it on every
new meta; ``stamp-started`` back-stamps the metas published before it did.
"""
from __future__ import annotations

import datetime as dt
import json
import posixpath
import sys

from click import argument, command, option

from .scan_id import is_scan_id


def err(*args: object) -> None:
    """Log to stderr (resolved per call, so a CLI runner's capture sees it)."""
    print(*args, file=sys.stderr)


SUCCESS_MARKER = "_SUCCESS.json"


def iso_z(t: dt.datetime) -> str:
    """A UTC instant as ``meta.published`` spells it: millisecond ISO with a ``Z``."""
    return t.astimezone(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def earliest_started(markers: list[dict]) -> str | None:
    """The earliest ``started`` among listing markers (`iso_z`), or None when none carries one."""
    starts = []
    for m in markers:
        s = m.get("started")
        if isinstance(s, str):
            t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
            if t.tzinfo is None:
                raise ValueError(f"listing marker `started` without a timezone: {s!r}")
            starts.append(t)
    return iso_z(min(starts)) if starts else None


def read_markers(pattern: str) -> list[dict]:
    """The ``_SUCCESS.json`` markers a glob names (local or any fsspec URL)."""
    import fsspec

    fs, path = fsspec.core.url_to_fs(pattern)
    return [json.loads(fs.cat_file(p)) for p in sorted(fs.glob(path))]


def listing_started(listings: tuple[str, ...] | list[str]) -> str | None:
    """A scan's start from its listing globs (``path-index -l``): the earliest ``started`` among the
    ``_SUCCESS.json`` markers beside them (each glob's directory part). None when there are none (an inventory
    listing, or a listing written before the markers carried ``started``)."""
    markers = []
    for g in dict.fromkeys(f"{posixpath.dirname(g)}/{SUCCESS_MARKER}" for g in listings):
        markers.extend(read_markers(g))
    return earliest_started(markers)


@command("stamp-started")
@option("-a", "--all", "all_scans", is_flag=True, help="Stamp timed scans too (default: date-only ids, the only ones the site keys by their start)")
@option("-l", "--listing-prefix", default="listing", help="Listing dir under ROOT: markers at `<ROOT>/<prefix>/<id>/*/_SUCCESS.json` (default `listing`)")
@option("-n", "--dry-run", is_flag=True, help="Print what would be stamped; write nothing")
@option("-s", "--snapshots-prefix", default="snapshots", help="Snapshot dir under ROOT: metas at `<ROOT>/<prefix>/<id>/meta.json` (default `snapshots`)")
@argument("root")
@argument("scans", nargs=-1)
def stamp_started(all_scans: bool, listing_prefix: str, dry_run: bool, snapshots_prefix: str, root: str, scans: tuple[str, ...]) -> None:
    """Back-stamp ``meta.started`` on published scans (ROOT: the data bucket URL, e.g. ``gs://<bucket>``).

    Each scan's start is the earliest ``started`` across ``<ROOT>/<listing>/<id>/*/_SUCCESS.json``, as
    ``path-index`` now records it. SCANS default to every scan id under ``<ROOT>/<snapshots>/``. A meta that
    already has ``started`` is left alone, as is one whose markers carry no ``started`` (none, or older ones).
    Prints one tab-separated row per scan — ``id``, ``started`` (or ``-``), ``markers``, ``action`` (``stamp`` /
    ``stamped`` / ``has`` / ``no-started`` / ``no-meta``) — and a summary on stderr."""
    import fsspec

    fs, base = fsspec.core.url_to_fs(root.rstrip("/"))
    snap = f"{base}/{snapshots_prefix.strip('/')}"
    if scans:
        ids = list(scans)
        for s in ids:
            if not is_scan_id(s):
                raise ValueError(f"not a scan id: {s!r}")
    else:
        ids = sorted(n for n in (posixpath.basename(p.rstrip("/")) for p in fs.ls(snap, detail=False)) if is_scan_id(n))
    if not all_scans:
        ids = [s for s in ids if len(s) == 10]
    counts: dict[str, int] = {}
    for sid in ids:
        meta_path = f"{snap}/{sid}/meta.json"
        markers_glob = f"{base}/{listing_prefix.strip('/')}/{sid}/*/{SUCCESS_MARKER}"
        if not fs.exists(meta_path):
            action, started, n = "no-meta", None, 0
        else:
            meta = json.loads(fs.cat_file(meta_path))
            if "started" in meta:
                action, started, n = "has", meta["started"], 0
            else:
                markers = [json.loads(fs.cat_file(p)) for p in sorted(fs.glob(markers_glob))]
                n = len(markers)
                started = earliest_started(markers)
                if started is None:
                    action = "no-started"
                elif dry_run:
                    action = "stamp"
                else:
                    fs.pipe_file(meta_path, (json.dumps({**meta, "started": started}, indent=2) + "\n").encode())
                    action = "stamped"
        counts[action] = counts.get(action, 0) + 1
        print(f"{sid}\t{started or '-'}\t{n}\t{action}")
    err(f"{len(ids)} scans: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + (" (dry run)" if dry_run else ""))
