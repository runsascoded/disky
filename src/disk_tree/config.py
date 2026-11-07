import re
from os import environ as env, makedirs, pathsep
from os.path import expanduser, exists, join
from typing import Callable

from disk_tree.env import get as env_get, name as env_name
from disk_tree.blobfs import exists as blob_exists, is_url, join as blob_join

ROOT_VAR = env_name('ROOT')
SCAN_DIRS_VAR = env_name('SCAN_DIRS')
HOME = env['HOME']
CONFIG_DIR = join(HOME, '.config')
DEFAULT_ROOT_DIR = join(CONFIG_DIR, 'disk-tree')

#: Callbacks fired after `set_write_target` repoints the blob write dir (the
#: backend singleton must be rebuilt against it).
_write_target_hooks: list[Callable[[], None]] = []

#: Root-derived globals, bound by `_apply_root` at import (and on re-root).
ROOT_DIR: str
DEFAULT_SCANS_DIR: str
SQLITE_PATH: str
SCANS_DIR: str


def _ensure_dir(path: str) -> None:
    """`makedirs(path)` for a local dir; a no-op for a URL (object stores have
    no directories to create)."""
    if is_url(path):
        return
    if not exists(path):
        makedirs(path)


def _apply_root(path: str) -> None:
    """Bind the root-derived globals to `path`, ensuring the dir exists."""
    global ROOT_DIR, DEFAULT_SCANS_DIR, SQLITE_PATH, SCANS_DIR
    ROOT_DIR = expanduser(path)
    _ensure_dir(ROOT_DIR)
    DEFAULT_SCANS_DIR = join(ROOT_DIR, 'scans')
    SQLITE_PATH = join(ROOT_DIR, 'disk-tree.db')
    SCANS_DIR = scan_write_dir()


#: `:` separates entries, but `://` opens a URL — split only on a `:` that
#: doesn't. (Object-store URLs carry no port, so that's the only case.)
_DIRS_SEP = re.compile(re.escape(pathsep) + r'(?!//)')


def split_dirs(raw: str) -> list[str]:
    """Entries of a `DISKY_SCAN_DIRS` value — local dirs and URLs alike."""
    return [p for p in _DIRS_SEP.split(raw) if p]


def configured_scan_dirs() -> list[str]:
    """Scan dirs in priority order — the first is the write target for new blobs."""
    raw = env_get('SCAN_DIRS')
    if raw:
        return [p if is_url(p) else expanduser(p) for p in split_dirs(raw)]
    return [DEFAULT_SCANS_DIR]


def scan_write_dir() -> str:
    """Where new blobs go: the first configured dir."""
    return configured_scan_dirs()[0]


def scan_read_dirs() -> list[str]:
    """Every dir a blob might live in, nearest first.

    `SCANS_DIR` leads so that a monkeypatched (or env-overridden) write dir is
    always searched first, and the internal default always trails so blobs
    written there before `DISKY_SCAN_DIRS` was set stay reachable.
    """
    seen, out = set(), []
    for d in [SCANS_DIR, *configured_scan_dirs(), DEFAULT_SCANS_DIR]:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def resolve_scan_blob(name: str, prefer: str | None = None) -> str:
    """Path (or URL) for a blob basename — the first read dir that has it.

    Local dirs are checked first, remote ones only if none has it: a blob lives
    in exactly one place (immutable, UUID-named), so a local hit never costs a
    network round-trip. Falls back to the write dir so callers creating a blob
    get a sensible path; a missing blob then fails at open time with the path
    it looked for.
    """
    found = _find_blob(name, prefer)
    return found if found is not None else blob_join(prefer or SCANS_DIR, name)


def _find_blob(name: str, prefer: str | None = None) -> str | None:
    """The read dir that actually holds blob `name` (local dirs first, then
    remote), or `None` if no reachable dir has it — no write-dir fallback."""
    dirs = ([prefer] if prefer else []) + scan_read_dirs()
    for d in dirs:
        if not is_url(d) and exists(join(d, name)):
            return join(d, name)
    for d in dirs:
        if is_url(d) and blob_exists(blob_join(d, name)):
            return blob_join(d, name)
    return None


def on_write_target_change(cb: Callable[[], None]) -> None:
    """Register a callback to run after `set_write_target` repoints the write dir."""
    _write_target_hooks.append(cb)


def set_write_target(target: str) -> str:
    """Make `target` — a local dir or a URL (`r2://bucket/prefix`) — this
    process's blob write dir, ahead of the configured search path (so blobs
    written there also resolve on read). A URL is validated up front (scheme,
    driver, R2 endpoint) so a misconfiguration fails before a long scan, not
    after it.
    """
    global SCANS_DIR
    if is_url(target):
        from disk_tree.blobfs import fs_for
        fs_for(target)
    else:
        target = expanduser(target)
        _ensure_dir(target)
    raw = env_get('SCAN_DIRS')
    rest = [d for d in (split_dirs(raw) if raw else configured_scan_dirs()) if d != target]
    env[SCAN_DIRS_VAR] = pathsep.join([target, *rest])
    SCANS_DIR = target
    for cb in list(_write_target_hooks):
        cb()
    return target


#: Bind the root-derived globals (`ROOT_DIR`, `DEFAULT_SCANS_DIR`, `SQLITE_PATH`,
#: `SCANS_DIR`) from the env at import.
#: `SCANS_DIR` is the write target, resolved once here.
_apply_root(env_get('ROOT', DEFAULT_ROOT_DIR))
