"""A run's jobs on one image: a registry tag resolved to its digest once, where the run starts.

A coordinator (the scan head, `static-names runs add | merge`, the interval store's chain) submits Batch jobs over hours;
naming them by a moving tag (`scan:latest`) lets a promote mid-run split one run across two code versions. On
2026-11-26 cw's level-5 fold started on one image, a promote moved `:latest`, and its `fold-shard` jobs, submitted
later by tag, refused to fold for code that wasn't theirs: the fold failed. So the coordinator resolves its image's
tag to `repo@sha256:…` before its first job and every job it submits (and their helpers, through `STATIC_NAMES_IMAGE`,
`INTERVAL_STORE_IMAGE`, `JOB_IMAGE`) runs that digest. A tag that can't be resolved fails the run: never a silent
fallback to the tag.

Only Artifact Registry references (`<region>-docker.pkg.dev/<project>/<repo>/<name>:<tag>`) are resolved; a digest is
kept; anything else (a local image, a test's stand-in) is left as given.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

#: An Artifact Registry tag reference: host, path (`project/repo/name…`), tag.
AR_TAG = re.compile(r"(?P<host>[a-z0-9-]+-docker\.pkg\.dev)/(?P<path>[a-z0-9._/-]+):(?P<tag>[\w][\w.-]{0,127})")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
#: The env vars a run's jobs read their image from: a coordinator's helpers (`static_profile.ENV`, `interval_append.ENV`)
#: and the scan steps' own submits (`batch.job_image`, `vm-pack`).
IMAGE_ENVS = ("JOB_IMAGE", "STATIC_NAMES_IMAGE", "INTERVAL_STORE_IMAGE")
#: The manifest kinds a tag may name (a single image, a multi-arch list, their OCI forms).
ACCEPT = ", ".join((
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
))


def is_tag(image: str | None) -> bool:
    return bool(image) and AR_TAG.fullmatch(image) is not None


def _registry_digest(url: str) -> str:
    """The `Docker-Content-Digest` of a registry manifest URL, as the ambient Google credentials (one HEAD request)."""
    from .gcp import session

    r = session().head(url, headers={"Accept": ACCEPT})
    r.raise_for_status()
    return r.headers.get("Docker-Content-Digest", "")


def resolve(image: str, digest_of: Callable[[str], str] | None = None) -> str:
    """`image` as `repo@sha256:…`: an Artifact Registry tag resolved to the digest it names now; a digest, or a
    reference that isn't an Artifact Registry tag, as given. Raises when the registry doesn't answer with a digest."""
    m = AR_TAG.fullmatch(image)
    if not m:
        return image
    url = f"https://{m['host']}/v2/{m['path']}/manifests/{m['tag']}"
    got = (digest_of or _registry_digest)(url)
    if not DIGEST.fullmatch(got or ""):
        raise RuntimeError(f"{image}: the registry gave no digest ({got!r}); not running on a moving tag")
    return f"{m['host']}/{m['path']}@{got}"


def pin_profile(p: Any, digest_of: Callable[[str], str] | None = None, log: Callable[[str], None] | None = None) -> Any:
    """A static-names / interval-store profile (`static_profile.Profile`) with its `image` resolved (`resolve`)."""
    image = getattr(p, "image", None)
    if not is_tag(image):
        return p
    pinned = resolve(image, digest_of)
    if log:
        log(f"image: {image} → {pinned} (every job this run submits)")
    return replace(p, image=pinned)


def pinned_env(env: Mapping[str, str], image: str) -> dict[str, str]:
    """`env` with `IMAGE_ENVS` set to `image` (a digest), for every job a run's tasks submit."""
    return {**env, **{k: image for k in IMAGE_ENVS}}
