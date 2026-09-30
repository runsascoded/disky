import re
import subprocess
from datetime import timezone
from dateutil.parser import parse
from os.path import dirname
from subprocess import PIPE
from typing import Iterator
from urllib.parse import urlparse

from tqdm import tqdm
from utz import o

from disk_tree import time

from .base import Backend, ErrorCollector


WS = re.compile(r'\s+')


class S3Backend(Backend):
    """S3 (or S3-compatible) bucket, scanned via `aws s3 ls --recursive`.

    `scheme` is the URL scheme this instance answers for: `s3` (the default),
    or an S3-compatible store addressed by its own scheme — `r2://bucket` is
    listed through `endpoint_url` (the bucket's Cloudflare endpoint, resolved
    by `backend_for`), and every emitted `uri` keeps the caller's scheme so a
    scan's rows say `r2://…`, not `s3://…`. The aws CLI only speaks `s3://`,
    so URLs are rewritten to that on the way out (`_s3_url`).
    """

    scheme = 's3'  # the instance's `scheme` may shadow this (`r2`)

    def __init__(self, endpoint_url: str | None = None, profile: str | None = None, scheme: str = 's3'):
        self.endpoint_url = endpoint_url
        self.profile = profile
        self.scheme = scheme

    def _aws_cmd(self, subcmd: list[str]) -> list[str]:
        if self.scheme != 's3' and not self.endpoint_url:
            raise RuntimeError(
                f"{self.scheme}:// needs an S3 endpoint — set DISK_TREE_R2_ENDPOINT_URL, or give the bucket an "
                "`endpoint_url` in buckets.yml"
            )
        cmd = ['aws']
        if self.profile:
            cmd.extend(['--profile', self.profile])
        if self.endpoint_url:
            cmd.extend(['--endpoint-url', self.endpoint_url])
        cmd.extend(subcmd)
        return cmd

    @staticmethod
    def _s3_url(url: str) -> str:
        """`r2://bucket/prefix` → `s3://bucket/prefix` (identity for `s3://`)."""
        p = urlparse(url)
        return f's3://{p.netloc}{p.path}'

    def list(
        self,
        url: str,
        *,
        errors: ErrorCollector | None = None,
        excludes: list[str] | None = None,
        sudo: bool = False,
        progress: bool = True,
    ) -> Iterator[dict]:
        cmd = self._aws_cmd(['s3', 'ls', '--recursive', self._s3_url(url)])
        proc = subprocess.Popen(cmd, stdout=PIPE, stderr=PIPE, text=True)
        parsed = urlparse(url)
        scheme = self.scheme
        bkt = parsed.netloc
        key0 = parsed.path.lstrip('/')
        dirs = set()
        with time("s3_files_iter lines"):
            for line in tqdm(proc.stdout, disable=not progress):
                strs = WS.split(line.rstrip('\n'), 3)
                mtime_str = f'{strs[0]} {strs[1]}'
                mtime = int(parse(mtime_str).replace(tzinfo=timezone.utc).timestamp())
                size = int(strs[2])
                key = strs[3]
                if key0:
                    if not key.startswith(f'{key0}/') and key != key0:
                        raise ValueError(f"{url}: unexpected {key=}")
                    relpath = key[len(key0) + 1:] if key != key0 else ''
                else:
                    relpath = key
                cur = relpath
                if not cur and not relpath:
                    continue
                new_dirs = []
                while True:
                    try:
                        idx = cur.rindex('/')
                    except ValueError:
                        idx = 0
                    cur = cur[:idx]
                    seen = cur in dirs
                    if not seen:
                        dirs.add(cur)
                        new_dirs.append(cur)
                    if not cur or seen:
                        break

                for d in reversed(new_dirs):
                    yield o(
                        path=d,
                        size=0,
                        mtime=0,
                        kind='dir',
                        parent=dirname(d) if d else None,
                        uri=f'{scheme}://{bkt}/{key0}/{d}' if key0 else f'{scheme}://{bkt}/{d}',
                    )
                yield o(
                    path=relpath,
                    size=size,
                    mtime=mtime,
                    kind='file',
                    parent=dirname(relpath),
                    uri=f'{scheme}://{bkt}/{key}',
                )

    def delete(self, url: str) -> None:
        cmd = self._aws_cmd(['s3', 'rm', '--recursive', self._s3_url(url)])
        subprocess.run(cmd, check=True)

    def restore(self, url: str) -> int:
        """Undo a delete on a versioned bucket by removing the latest
        delete-markers under `url`'s prefix (spec `staged-delete.md` CP5). Each
        removal reveals the object's prior version. Returns the count restored;
        0 if the bucket isn't versioned (no delete-markers to remove)."""
        import json

        p = urlparse(url)
        bucket, prefix = p.netloc, p.path.lstrip('/')
        listed = subprocess.run(
            self._aws_cmd(['s3api', 'list-object-versions', '--bucket', bucket, '--prefix', prefix, '--output', 'json']),
            check=True, stdout=PIPE, text=True,
        )
        markers = [m for m in (json.loads(listed.stdout or '{}').get('DeleteMarkers') or []) if m.get('IsLatest')]
        for m in markers:
            subprocess.run(
                self._aws_cmd(['s3api', 'delete-object', '--bucket', bucket, '--key', m['Key'], '--version-id', m['VersionId']]),
                check=True, stdout=PIPE,
            )
        return len(markers)

    def exists(self, url: str) -> bool:
        cmd = self._aws_cmd(['s3', 'ls', self._s3_url(url)])
        return subprocess.run(cmd, capture_output=True).returncode == 0
