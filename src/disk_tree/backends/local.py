import os
import re
import shutil
import subprocess
from os.path import abspath, isdir, isfile
from typing import Iterator

from .base import Backend, ErrorCollector, ProgressCallback
from .gfind import run_gfind


# macOS CloudStorage paths (File Provider virtual filesystem)
# These directories proxy to cloud services and block on network I/O
CLOUDSTORAGE_PATHS = [
    '/Library/CloudStorage',
    os.path.expanduser('~/Library/CloudStorage'),
]

#: GNU find: `gfind` where it's Homebrew's findutils (macOS), else the system
#: `find` (Linux, where it's already GNU and has `-printf`).
FIND = shutil.which('gfind') and 'gfind' or 'find'


_MOUNT_RE = re.compile(r'^.+? on (?P<path>/.*?)(?: type \S+)? \([^()]*\)$')


def mount_points() -> list[str]:
    """Every mounted filesystem's mount point, from `mount` (macOS and Linux)."""
    out = subprocess.run(['mount'], check=True, capture_output=True, text=True).stdout
    return [m['path'] for line in out.splitlines() if (m := _MOUNT_RE.match(line))]


def mounts_below(root: str, mounts: list[str]) -> list[str]:
    """Mount points strictly inside `root`: what a one-filesystem walk prunes.

    On macOS, `/` is the sealed System volume and the Data volume is reached
    through firmlinks (`/Users`, `/Applications`, …), which aren't mount points;
    Data's own mount path (`/System/Volumes/Data`) is, so pruning it walks
    Data exactly once. `find -xdev` can't do this: firmlinked dirs carry the
    Data volume's `st_dev`, so it would prune `/Users`.
    """
    prefix = root.rstrip('/') + '/'
    return sorted(m for m in mounts if m.startswith(prefix) and m != root)


class LocalBackend(Backend):
    """Local filesystem, scanned via `gfind`."""

    scheme = 'file'

    @property
    def is_local(self) -> bool:
        return True

    @property
    def supports_sudo(self) -> bool:
        return True

    def list(
        self,
        url: str,
        *,
        errors: ErrorCollector | None = None,
        excludes: list[str] | None = None,
        sudo: bool = False,
        one_fs: bool = False,
        progress_callback: ProgressCallback | None = None,
        progress_interval: float = 1.0,
        progress: bool = True,
    ) -> Iterator[dict]:
        path0 = abspath(url)

        if excludes is None:
            excludes = CLOUDSTORAGE_PATHS
        # `rstrip` so a `/` root's prefix is `/`, not `//` (which matched nothing,
        # so a `/` scan walked into CloudStorage).
        prefix = path0.rstrip('/') + '/'
        applicable_excludes = [
            abs_pattern
            for pattern in excludes
            for abs_pattern in [abspath(os.path.expanduser(pattern))]
            if (abs_pattern.startswith(prefix)
                or path0.startswith(abs_pattern.rstrip('/') + '/')
                or abs_pattern == path0)
        ]

        # Opt-in native walker (see specs/tauri-native-app.md): a drop-in for the
        # `gfind` subprocess that emits the same `%y %b %T@ %p\0` stream, so the
        # `run_gfind` parser below consumes it unchanged. The gfind path stays the
        # default; `DISK_TREE_WALKER=<path-to-dt-walker>` swaps only the source cmd.
        # `one_fs`: don't descend into other filesystems mounted below the root
        # (the walker checks each dir's mount status; gfind prunes the current
        # mount points, which is equivalent for a walk's duration).
        walker = os.environ.get('DISK_TREE_WALKER')
        if one_fs and not walker:
            applicable_excludes = [*applicable_excludes, *mounts_below(path0, mount_points())]
        if walker:
            cmd = [walker, '--no-default-excludes', *(['--one-fs'] if one_fs else [])]
            for abs_pattern in applicable_excludes:
                cmd.extend(['--exclude', abs_pattern])
            cmd.append(path0)
        else:
            # %b = 512-byte blocks actually allocated (handles sparse files correctly)
            fmt = r'%y %b %T@ %p\0'
            cmd = [FIND, path0]
            for abs_pattern in applicable_excludes:
                # Print the pruned dir itself (an empty leaf), as the walker and
                # `find -xdev` do: the tree shows a mount / excluded dir is there.
                cmd.extend(['-path', abs_pattern, '-printf', fmt, '-prune', '-o'])
            cmd.extend(['-printf', fmt])
        if sudo:
            cmd = ['sudo', *cmd]

        yield from run_gfind(
            cmd,
            path0,
            uri_for=lambda p: p,
            errors=errors,
            progress_callback=progress_callback,
            progress_interval=progress_interval,
            progress=progress,
        )

    def delete(self, url: str) -> None:
        if isfile(url):
            os.remove(url)
        elif isdir(url):
            shutil.rmtree(url)

    def exists(self, url: str) -> bool:
        return isfile(url) or isdir(url)
