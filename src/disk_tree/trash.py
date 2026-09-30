"""Trash, not ``rm``: where the laptop drainer puts what a real run deletes (spec
``specs/m3-site.md`` Phase 3).

Each path is *renamed* into ``<root>/<run>/<path>`` — ``~/.Trash/disk-tree/``
by default (``DISK_TREE_TRASH_DIR`` overrides; tests point it at a tmp dir) —
a same-volume rename: instant, no copy, and Finder shows it in the Trash. A
per-run ``.manifest.jsonl`` records ``original → trashed``, so :func:`restore`
puts a run back exactly (local paths get an undo) and :func:`empty` frees its
bytes (``rm -rf`` the run dir); Finder's *Empty Trash* frees them too. A path
on another volume can't be renamed in (:class:`CrossVolume`): the caller
decides (refuse, or delete outright).
"""
from __future__ import annotations

import errno
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

MANIFEST = ".manifest.jsonl"


class CrossVolume(OSError):
    """The path lives on a different volume than the trash root."""


def trash_root() -> Path:
    return Path(os.environ.get("DISK_TREE_TRASH_DIR") or Path.home() / ".Trash" / "disk-tree")


def run_dir(run_id: str) -> Path:
    """A run's trash dir; ``/`` in a run id (``<plan>-<scan>/<ts>``) becomes ``__``."""
    return trash_root() / run_id.replace("/", "__")


def local_path(uri: str) -> str:
    """``file:///Users/x/`` (a canonical plan prefix), ``file:///Users/x`` or a
    bare path → the local path, no trailing slash (``/`` stays ``/``)."""
    p = uri[len("file://"):] if uri.startswith("file://") else uri
    return p.rstrip("/") or "/"


def _manifest_path(run_id: str) -> Path:
    return run_dir(run_id) / MANIFEST


def manifest(run_id: str) -> list[dict]:
    """The run's ``{path, trashed, ts}`` records, in trash order."""
    p = _manifest_path(run_id)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _write_manifest(run_id: str, records: list[dict]) -> None:
    p = _manifest_path(run_id)
    if records:
        p.write_text("".join(json.dumps(r) + "\n" for r in records))
    elif p.exists():
        p.unlink()


def trash(uri: str, run_id: str, now: Callable[[], float] = time.time) -> str:
    """Rename ``uri`` into the run's trash dir; returns where it went."""
    src = local_path(uri)
    if not os.path.lexists(src):
        raise FileNotFoundError(src)
    dest = run_dir(run_id) / src.lstrip("/")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dest)
    except OSError as e:
        if e.errno == errno.EXDEV:
            raise CrossVolume(f"{src}: not on the trash root's volume ({trash_root()}); a rename can't move it") from e
        raise
    with _manifest_path(run_id).open("a") as f:
        f.write(json.dumps({"path": src, "trashed": str(dest), "ts": int(now())}) + "\n")
    return str(dest)


@dataclass(frozen=True)
class Restored:
    restored: list[str]   # original paths put back
    skipped: list[str]    # originals that exist again (left in the trash)


def restore(run_id: str) -> Restored:
    """Put the run's paths back where they were (newest first, so a parent
    trashed after its child goes back after it). A path whose original exists
    again is skipped and stays in the trash."""
    records = manifest(run_id)
    restored, skipped, remaining = [], [], []
    for r in reversed(records):
        if not os.path.lexists(r["trashed"]):
            continue  # emptied or moved by hand: nothing to put back
        if os.path.lexists(r["path"]):
            skipped.append(r["path"])
            remaining.append(r)
            continue
        Path(r["path"]).parent.mkdir(parents=True, exist_ok=True)
        os.rename(r["trashed"], r["path"])
        restored.append(r["path"])
    _write_manifest(run_id, list(reversed(remaining)))
    return Restored(restored=list(reversed(restored)), skipped=list(reversed(skipped)))


def du(path: str | Path) -> tuple[int, int]:
    """(allocated bytes, files) under ``path`` — what emptying frees."""
    nbytes = files = 0
    p = Path(path)
    if p.is_symlink() or p.is_file():
        st = p.lstat()
        return st.st_blocks * 512, 1
    for root, _dirs, names in os.walk(p):
        for n in names:
            if n == MANIFEST:
                continue  # bookkeeping, not trashed data
            try:
                st = os.lstat(os.path.join(root, n))
            except FileNotFoundError:
                continue
            nbytes += st.st_blocks * 512
            files += 1
    return nbytes, files


def empty(run_id: str) -> tuple[int, int]:
    """``rm -rf`` the run's trash dir; returns (bytes, files) freed."""
    d = run_dir(run_id)
    if not d.exists():
        return 0, 0
    freed = du(d)
    shutil.rmtree(d)
    return freed


@dataclass(frozen=True)
class TrashedRun:
    run_id: str
    ts: int          # when the run started trashing (its first record)
    bytes: int
    files: int


def runs() -> list[TrashedRun]:
    """Every run still in the trash, oldest first."""
    root = trash_root()
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if not d.is_dir():
            continue
        run_id = d.name.replace("__", "/")
        recs = manifest(run_id)
        ts = min((r["ts"] for r in recs), default=int(d.stat().st_mtime))
        nbytes, files = du(d)
        out.append(TrashedRun(run_id=run_id, ts=ts, bytes=nbytes, files=files))
    return sorted(out, key=lambda r: r.ts)


def expired(ttl_s: int, now: Callable[[], float] = time.time) -> list[TrashedRun]:
    """Runs trashed more than ``ttl_s`` seconds ago — what a TTL sweep empties."""
    cutoff = now() - ttl_s
    return [r for r in runs() if r.ts <= cutoff]
