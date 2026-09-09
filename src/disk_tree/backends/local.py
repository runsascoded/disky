import os
import shutil
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
        progress_callback: ProgressCallback | None = None,
        progress_interval: float = 1.0,
        progress: bool = True,
    ) -> Iterator[dict]:
        path0 = abspath(url)

        if excludes is None:
            excludes = CLOUDSTORAGE_PATHS
        applicable_excludes = [
            abs_pattern
            for pattern in excludes
            for abs_pattern in [abspath(os.path.expanduser(pattern))]
            if (abs_pattern.startswith(path0 + '/')
                or path0.startswith(abs_pattern.rstrip('/') + '/')
                or abs_pattern == path0)
        ]

        # Opt-in native walker (see specs/tauri-native-app.md): a drop-in for the
        # `gfind` subprocess that emits the same `%y %b %T@ %p\0` stream, so the
        # `run_gfind` parser below consumes it unchanged. The gfind path stays the
        # default; `DISK_TREE_WALKER=<path-to-dt-walker>` swaps only the source cmd.
        walker = os.environ.get('DISK_TREE_WALKER')
        if walker:
            cmd = [walker, '--no-default-excludes']
            for abs_pattern in applicable_excludes:
                cmd.extend(['--exclude', abs_pattern])
            cmd.append(path0)
        else:
            cmd = [FIND, path0]
            for abs_pattern in applicable_excludes:
                cmd.extend(['-path', abs_pattern, '-prune', '-o'])
            # %b = 512-byte blocks actually allocated (handles sparse files correctly)
            cmd.extend(['-printf', r'%y %b %T@ %p\0'])
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
