"""Shared `gfind`-output parser used by LocalBackend and SshBackend."""

import re
import subprocess
import time as time_module
from os.path import dirname
from subprocess import PIPE
from typing import Callable, Iterator

from tqdm import tqdm
from utz import err, o

from disk_tree import time

from .base import ErrorCollector, ProgressCallback


# gfind / find (and the native dt-walker) emit permission errors with straight
# or Unicode curly quotes (' ')
PERMISSION_DENIED_RE = re.compile(r"^(?:g?find|dt-walker): ['\u2018]([^'\u2019]+)['\u2019]: Permission denied$")


def run_gfind(
    cmd: list[str],
    root_path: str,
    *,
    uri_for: Callable[[str], str],
    errors: ErrorCollector | None = None,
    progress_callback: ProgressCallback | None = None,
    progress_interval: float = 1.0,
    progress: bool = True,
) -> Iterator[dict]:
    """Spawn `cmd` (expected to produce null-terminated `gfind -printf '%y %b %T@ %p\\0'`
    output), parse entries, yield dicts.

    `root_path` is the absolute path on the target filesystem that `gfind` was asked
    to traverse; `uri_for(remote_abs_path) -> uri_string` constructs the per-entry URI.
    """
    proc = subprocess.Popen(cmd, stdout=PIPE, stderr=PIPE)

    def read_records():
        buffer = b''
        for chunk in iter(lambda: proc.stdout.read(65536), b''):
            buffer += chunk
            while b'\0' in buffer:
                record, buffer = buffer.split(b'\0', 1)
                yield record

    last_progress_time = time_module.time()
    start_time = last_progress_time
    items_count = 0

    with time("gfind lines"):
        for record in tqdm(read_records(), disable=not progress):
            try:
                line = record.decode('utf-8', errors='replace')
            except UnicodeDecodeError:
                continue
            strs = line.split(' ', 3)
            if len(strs) < 4:
                continue
            kind = 'file' if strs[0] == 'f' else 'dir' if strs[0] == 'd' else strs[0]
            try:
                size = int(strs[1]) * 512
                mtime = int(float(strs[2]))
            except ValueError:
                continue
            filepath = strs[3]
            prefix = '/' if root_path == '/' else f'{root_path}/'
            if not filepath.startswith(prefix) and filepath != root_path:
                continue
            uri = uri_for(filepath)
            if filepath == root_path:
                filepath = ''
            elif root_path == '/':
                filepath = filepath[1:]
            else:
                filepath = filepath[len(root_path) + 1:]
            yield o(
                path=filepath,
                size=size,
                mtime=mtime,
                kind=kind,
                parent=None if filepath == '' else dirname(filepath),
                uri=uri,
            )

            items_count += 1
            now = time_module.time()
            if progress_callback and (now - last_progress_time) >= progress_interval:
                elapsed = now - start_time
                items_per_sec = items_count / elapsed if elapsed > 0 else None
                err_count = errors.count if errors else 0
                progress_callback(items_count, items_per_sec, err_count)
                last_progress_time = now

    stderr_bytes = proc.stderr.read()
    if stderr_bytes:
        stderr_output = stderr_bytes.decode('utf-8', errors='replace')
        for line in stderr_output.strip().split('\n'):
            if not line:
                continue
            match = PERMISSION_DENIED_RE.match(line)
            if match and errors is not None:
                errors.add(match.group(1))
            else:
                err(line)

    code = proc.wait()
    if code != 0 and (errors is None or errors.count == 0):
        err(f"gfind process exited with return code {code}")
