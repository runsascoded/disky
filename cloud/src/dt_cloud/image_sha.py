"""The running image's source revision: what `job/build.sh` stamps into it (`DISKY_GIT_SHA`, `DISKY_GIT_DIRTY`, and the
OCI labels beside them), read back so a job or benchmark can say, and check, which code it runs.

`dt-cloud image-sha` prints it; `--expect SHA` (and a harness's `-E/--expect-sha`) refuses a different, dirty or
unstamped image, so a measurement can't silently run on the wrong build."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from functools import wraps
from pathlib import Path

from click import BadParameter, command, option


def err(*args) -> None:
    """stderr, looked up at call time (a test's captured stream, not the one at import)."""
    print(*args, file=sys.stderr, flush=True)


SHA_ENV = "DISKY_GIT_SHA"
DIRTY_ENV = "DISKY_GIT_DIRTY"
MIN_PREFIX = 7
HEX = re.compile(r"[0-9a-f]{7,40}")


@dataclass(frozen=True)
class Stamp:
    sha: str | None
    dirty: bool | None  # None: the image carries a sha but no dirty state

    def __str__(self) -> str:
        if self.sha is None:
            return "unstamped"
        return self.sha + {False: "", True: " dirty", None: " dirty-unknown"}[self.dirty]


def stamp(env: Mapping[str, str] = os.environ) -> Stamp:
    """The image's stamp from its env (`ENV` lines the Dockerfile sets from `build.sh`'s build args)."""
    sha = env.get(SHA_ENV, "").strip().lower() or None
    dirty = {"0": False, "1": True}.get(env.get(DIRTY_ENV, "").strip())
    return Stamp(sha, dirty if sha else None)


def expected(sha: str) -> str:
    """A `--expect` value, lowercased: 7–40 hex chars."""
    s = sha.strip().lower()
    if not HEX.fullmatch(s):
        raise BadParameter(f"{sha!r}: expected a git sha or a prefix of at least {MIN_PREFIX} hex chars")
    return s


def expect_option(ctx, param, value: str | None) -> str | None:
    """Click callback: validate a `--expect` value at parse time."""
    return None if value is None else expected(value)


def mismatch(st: Stamp, expect: str, *, allow_dirty: bool = False) -> str | None:
    """Why the image `st` isn't the build of `expect` (a validated sha or prefix), or None when it is."""
    if st.sha is None:
        return f"image is unstamped (no ${SHA_ENV}); expected {expect}: rebuild it with job/build.sh"
    if not st.sha.startswith(expect):
        return f"image is {st.sha}, expected {expect}"
    if st.dirty and not allow_dirty:
        return f"image {st.sha} is dirty: built from uncommitted changes, not exactly {expect} (--allow-dirty to accept)"
    if st.dirty is None and not allow_dirty:
        return f"image {st.sha} carries no ${DIRTY_ENV}: it may hold uncommitted changes (--allow-dirty to accept)"
    return None


def code_dir() -> str:
    """Where `dt_cloud` was imported from: the image's install, unless a stage's `PYTHONPATH` overlay replaced it."""
    return str(Path(__file__).resolve().parent)


_logged = False


def log_stamp(label: str, env: Mapping[str, str] = os.environ) -> Stamp:
    """Log the image's stamp to stderr once per process (`label: image <sha> (dt_cloud: <dir>)`)."""
    global _logged
    st = stamp(env)
    if not _logged:
        _logged = True
        err(f"{label}: image {st} (dt_cloud: {code_dir()})")
    return st


def in_job(env: Mapping[str, str] = os.environ) -> bool:
    """Running in a job image (its Dockerfile sets `$DISKY_GIT_SHA`, empty when unstamped) or a Batch task."""
    return SHA_ENV in env or "BATCH_TASK_INDEX" in env


def log_job_stamp(label: str, env: Mapping[str, str] = os.environ) -> None:
    """`log_stamp` in a job (a stage task, a scan step, the head); silent elsewhere (tests, a laptop)."""
    if in_job(env):
        log_stamp(label, env)


def require(label: str, expect: str | None, *, allow_dirty: bool = False, env: Mapping[str, str] = os.environ) -> Stamp:
    """Log the stamp; with `expect` (a validated sha or prefix), refuse (exit 1) to go on when the image isn't that
    build."""
    st = log_stamp(label, env)
    if expect is not None and (why := mismatch(st, expect, allow_dirty=allow_dirty)):
        raise SystemExit(f"{label}: refusing to start: {why}")
    return st


def expect_sha_options(label: str):
    """A harness command's `-E/--expect-sha` + `--allow-dirty`: its image stamp is logged at start, and with `-E` it
    refuses to start on a different, dirty or unstamped image."""
    def deco(f):
        @wraps(f)
        def run(*args, expect_sha: str | None, allow_dirty: bool, **kw):
            require(label, expect_sha, allow_dirty=allow_dirty)
            return f(*args, **kw)

        run = option("--allow-dirty", is_flag=True, help="With -E: accept an image built from a dirty tree")(run)
        return option("-E", "--expect-sha", callback=expect_option, help="Refuse to start unless the image is this build (`$DISKY_GIT_SHA`, a sha or ≥7-char prefix; clean unless --allow-dirty)")(run)
    return deco


@command("image-sha")
@option("-a", "--allow-dirty", is_flag=True, help="With -e: accept an image built from a dirty tree")
@option("-e", "--expect", callback=expect_option, help="Exit 1 unless the image is this build (a sha or ≥7-char prefix), clean, and stamped")
@option("-j", "--json", "as_json", is_flag=True, help='Print `{"sha", "dirty"}` (nulls when unstamped)')
def image_sha_cmd(allow_dirty: bool, expect: str | None, as_json: bool) -> None:
    """Print the running image's source revision (`$DISKY_GIT_SHA`, `job/build.sh`'s stamp): `<sha>`, `<sha> dirty`,
    or `unstamped`. With `-e`, check it."""
    st = stamp()
    print(json.dumps({"sha": st.sha, "dirty": st.dirty}) if as_json else str(st))
    if expect is not None and (why := mismatch(st, expect, allow_dirty=allow_dirty)):
        raise SystemExit(f"image-sha: {why}")
