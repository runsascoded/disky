"""Secrets from the environment, whitespace-stripped.

A secret lands in Secret Manager or a Pages secret via `echo "$X" | … --data-file=-`
often enough that a trailing newline is the normal failure mode — it broke the
Discord digest for two days in 2026-09 (`curl exit 3`: the webhook URL grew a
`\\n` before `/messages/<id>`). Batch injects secret versions into env
verbatim, so every read goes through here and strips. Ad-hoc `-t/--token`
values are stripped the same way (pasted with a newline as often as not).
"""
from __future__ import annotations

import os


def env_secret(name: str, default: str | None = None) -> str | None:
    """``$name`` with surrounding whitespace removed; ``default`` when unset."""
    v = os.environ.get(name)
    return v.strip() if v is not None else default


def secret(value: str | None, env_name: str) -> str | None:
    """An explicit ``value`` (stripped) if given, else ``env_secret(env_name)``."""
    return value.strip() if value else env_secret(env_name)


def parse_binding(spec: str, project: str) -> tuple[str, str]:
    """``ENV=SECRET[@VERSION]`` → (``ENV``, the version's resource name); ``SECRET`` may be a full
    ``projects/<p>/secrets/<s>[/versions/<v>]`` name, else it is ``project``'s, at ``latest``."""
    name, sep, ref = spec.partition("=")
    if not sep or not name or not ref:
        raise ValueError(f"secret binding must be ENV=SECRET[@VERSION]: {spec!r}")
    if ref.startswith("projects/"):
        return name, ref if "/versions/" in ref else f"{ref}/versions/latest"
    sec, _, ver = ref.partition("@")
    return name, f"projects/{project}/secrets/{sec}/versions/{ver or 'latest'}"


def access(resource: str, session) -> str:
    """One Secret Manager version's payload, whitespace-stripped (REST `:access`, ADC credentials)."""
    from base64 import b64decode

    r = session.get(f"https://secretmanager.googleapis.com/v1/{resource}:access")
    r.raise_for_status()
    return b64decode(r.json()["payload"]["data"]).decode().strip()


def resolve(bindings: list[str], project: str, session, environ: dict[str, str]) -> dict[str, str]:
    """The env additions for ``bindings``: each unset (or empty) ``ENV`` from Secret Manager; one already set
    (e.g. injected by Batch's `secretVariables`) is kept as is, so a job spec's own injection wins."""
    out = {}
    for spec in bindings:
        name, resource = parse_binding(spec, project)
        if not environ.get(name):
            out[name] = access(resource, session)
    return out
