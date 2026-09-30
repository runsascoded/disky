import re
from glob import glob
from os import environ as env, makedirs, pathsep, sep
from os.path import expanduser, exists, ismount, join
from typing import Callable

from disk_tree.blobfs import exists as blob_exists, is_url, join as blob_join

DISK_TREE_ROOT_VAR = 'DISK_TREE_ROOT'
DISK_TREE_SCAN_DIRS_VAR = 'DISK_TREE_SCAN_DIRS'
HOME = env['HOME']
CONFIG_DIR = join(HOME, '.config')
DEFAULT_ROOT_DIR = join(CONFIG_DIR, 'disk-tree')

#: Volumes are probed here for an opted-in `disk-tree/scans` directory.
VOLUMES = join(sep, 'Volumes')

#: True when the active root was chosen deliberately (env var or `set_root`),
#: which disables volume discovery so new blobs stay inside the chosen library.
_explicit_root = DISK_TREE_ROOT_VAR in env

#: Callbacks fired after `set_root` rebinds the active root (engine rebind,
#: backend reset, cache clear). Registered by long-running consumers.
_root_change_hooks: list[Callable[[], None]] = []

#: Callbacks fired after `set_write_target` repoints the blob write dir (the
#: backend singleton must be rebuilt against it).
_write_target_hooks: list[Callable[[], None]] = []


def _volume_mounted(path: str) -> bool:
    """False when `path` sits under a `/Volumes/<name>` that isn't mounted.

    Creating a directory under an absent mount point silently writes to the
    boot disk — the very disk an external scans dir exists to spare — and the
    files vanish from view the moment the volume comes back. So an unmounted
    candidate is never a write target.
    """
    if is_url(path):
        return True  # reachability is an IO-time concern, not a mount
    parts = path.split(sep)
    if len(parts) > 2 and parts[1] == 'Volumes':
        return ismount(join(VOLUMES, parts[2]))
    return True


def _ensure_dir(path: str) -> None:
    """`makedirs(path)`, but never on the boot disk behind an unmounted volume.

    Guards the makedirs-on-boot-disk trap (see specs/macos-app.md): a root under
    an absent `/Volumes/<name>` would otherwise be created on the boot disk.
    """
    if is_url(path):
        return  # object stores have no directories to create
    if not _volume_mounted(path):
        raise RuntimeError(
            f'{path!r} is under an unmounted volume; refusing to create it on the boot disk'
        )
    if not exists(path):
        makedirs(path)


def _apply_root(path: str) -> None:
    """(Re)bind the root-derived globals to `path`, ensuring the dir exists."""
    global ROOT_DIR, DEFAULT_SCANS_DIR, SQLITE_PATH, SCANS_DIR
    ROOT_DIR = expanduser(path)
    _ensure_dir(ROOT_DIR)
    DEFAULT_SCANS_DIR = join(ROOT_DIR, 'scans')
    SQLITE_PATH = join(ROOT_DIR, 'disk-tree.db')
    SCANS_DIR = scan_write_dir()


def current_root() -> str:
    """The active library root."""
    return ROOT_DIR


def on_root_change(cb: Callable[[], None]) -> None:
    """Register a callback to run after `set_root` rebinds the active root."""
    _root_change_hooks.append(cb)


def set_root(path: str) -> str:
    """Switch the active library to `path` at runtime, firing rebind hooks.

    Marks the root explicit (disables volume discovery, so new blobs land inside
    the opened library), rebinds the derived globals, then notifies listeners
    (the SQLAlchemy engine, the backend singleton, server caches). Idempotent
    for a path already open as the explicit root.
    """
    global _explicit_root
    path = expanduser(path)
    if path == ROOT_DIR and _explicit_root:
        return ROOT_DIR
    _explicit_root = True
    _apply_root(path)
    for cb in list(_root_change_hooks):
        cb()
    return ROOT_DIR


def discovered_scan_dirs() -> list[str]:
    """External scans dirs, opted in by existing on a mounted volume.

    Creating `<volume>/disk-tree/scans` is the whole opt-in: no config file, and
    an unplugged volume simply drops out of the search path.
    """
    return sorted(d for d in glob(join(VOLUMES, '*', 'disk-tree', 'scans')) if _volume_mounted(d))


#: `:` separates entries, but `://` opens a URL — split only on a `:` that
#: doesn't. (Object-store URLs carry no port, so that's the only case.)
_DIRS_SEP = re.compile(re.escape(pathsep) + r'(?!//)')


def split_dirs(raw: str) -> list[str]:
    """Entries of a `DISK_TREE_SCAN_DIRS` value — local dirs and URLs alike."""
    return [p for p in _DIRS_SEP.split(raw) if p]


def configured_scan_dirs() -> list[str]:
    """Scan dirs in priority order — first writable one wins for new blobs."""
    raw = env.get(DISK_TREE_SCAN_DIRS_VAR)
    if raw:
        return [p if is_url(p) else expanduser(p) for p in split_dirs(raw)]
    if _explicit_root or DISK_TREE_ROOT_VAR in env:
        # An explicit root — an env var, or a library opened via `set_root` — is a
        # deliberate choice; discovery must not silently redirect writes out of it.
        return [DEFAULT_SCANS_DIR]
    return [*discovered_scan_dirs(), DEFAULT_SCANS_DIR]


def scan_write_dir() -> str:
    """Where new blobs go: the first configured dir on a mounted volume."""
    for d in configured_scan_dirs():
        if _volume_mounted(d):
            return d
    return DEFAULT_SCANS_DIR


def scan_read_dirs() -> list[str]:
    """Every dir a blob might live in, nearest first.

    `SCANS_DIR` leads so that a monkeypatched (or env-overridden) write dir is
    always searched first, and the internal default always trails so blobs
    written before an external volume existed stay reachable.
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


def blob_reachable(name: str, prefer: str | None = None) -> bool:
    """Whether blob `name` resolves to an existing file/object in some read dir.
    False when its scan's blob lives only on an unmounted volume (or was never
    written), so callers can fall back or raise a clear message instead of
    hitting a raw `FileNotFoundError` on the write-dir fallback path."""
    return _find_blob(name, prefer) is not None


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
    raw = env.get(DISK_TREE_SCAN_DIRS_VAR)
    rest = [d for d in (split_dirs(raw) if raw else configured_scan_dirs()) if d != target]
    env[DISK_TREE_SCAN_DIRS_VAR] = pathsep.join([target, *rest])
    SCANS_DIR = target
    for cb in list(_write_target_hooks):
        cb()
    return target


#: Bind the root-derived globals (`ROOT_DIR`, `DEFAULT_SCANS_DIR`, `SQLITE_PATH`,
#: `SCANS_DIR`) from the env at import. `set_root` rebinds them at runtime.
#: `SCANS_DIR` is the write target, resolved once here; re-plugging a volume
#: mid-run won't be noticed, but a new blob never lands somewhere unreadable.
_apply_root(env.get(DISK_TREE_ROOT_VAR, DEFAULT_ROOT_DIR))
