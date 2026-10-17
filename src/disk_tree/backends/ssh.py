"""A filesystem on another machine, walked by its own GNU `find` over ssh.

The remote side needs only `find` with `-printf` (Linux; e.g. an Unraid NAS,
which has no Python): the same `%y %b %T@ %p\\0` stream `LocalBackend` reads
comes back over the ssh connection and is parsed here, so nothing is installed
on the remote. Paths are the remote machine's absolute paths.
"""
import os
import shlex
from typing import Iterator

from disk_tree.env import get as env_get, name as env_name
from .base import Backend, ErrorCollector, ProgressCallback
from .gfind import run_gfind

#: The ssh client argv prefix (`DISKY_SSH`, else `DISK_TREE_SSH`; shell-split; default `ssh`).
#: Tests point it at a stand-in that runs the remote command locally.
SSH_VAR = env_name('SSH')


def ssh_cmd(host: str, remote: list[str]) -> list[str]:
    """`ssh` argv running `remote` (quoted for the remote shell) on `host`."""
    client = shlex.split(env_get('SSH') or 'ssh')
    return [*client, '-o', 'BatchMode=yes', host, shlex.join(remote)]


class SshBackend(Backend):
    """`host`'s filesystem, listed by its `find` over ssh."""

    scheme = 'file'

    def __init__(self, host: str):
        self.host = host

    @property
    def is_local(self) -> bool:
        return False

    @property
    def supports_sudo(self) -> bool:
        return False

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
        if sudo:
            raise ValueError('ssh walks run as the ssh user (no sudo)')
        root = url.rstrip('/') or '/'
        if not root.startswith('/'):
            raise ValueError(f'ssh walk root must be absolute: {url!r}')
        fmt = r'%y %b %T@ %p\0'
        remote = ['find', root]
        if one_fs:
            # Linux has no firmlinks, so `-xdev` is exactly "this filesystem only".
            remote.append('-xdev')
        for pattern in excludes or []:
            remote.extend(['-path', pattern, '-printf', fmt, '-prune', '-o'])
        remote.extend(['-printf', fmt])
        yield from run_gfind(
            ssh_cmd(self.host, remote),
            root,
            uri_for=lambda p: p,
            errors=errors,
            progress_callback=progress_callback,
            progress_interval=progress_interval,
            progress=progress,
            # ssh's own failure (unreachable host, auth): never an empty capture.
            fatal_codes=frozenset({255}),
        )

    def delete(self, url: str) -> None:
        raise NotImplementedError('ssh backend is read-only')

    def exists(self, url: str) -> bool:
        raise NotImplementedError('ssh backend is read-only')
