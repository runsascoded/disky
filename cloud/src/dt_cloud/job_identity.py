"""A local job's identity inside its container (specs/vm-pilot.md "JobBackend"): the access token the local backend
minted for the spec's service account arrives as `$GOOGLE_OAUTH_ACCESS_TOKEN` (`job_backend.TOKEN_ENV`), and every
Google client in this process uses it instead of whatever ADC would find (the host's metadata server is closed to the
job's containers, so ADC would find nothing).

`install()` runs at `dt_cloud`'s import and does nothing without the variable (Batch, a laptop), so `google.auth` isn't
imported then. With it, `google.auth.default` returns those credentials with `$GOOGLE_CLOUD_PROJECT` as the project:
gcsfs, `google-cloud-storage` and `gcp.session` all resolve credentials through it. The token isn't refreshed: a run
longer than its lifetime (`$DISKY_LOCAL_TOKEN_LIFETIME_S`, default an hour) sees its calls refused after that.
"""
from __future__ import annotations

import os

TOKEN_ENV = "GOOGLE_OAUTH_ACCESS_TOKEN"


def install(environ: dict[str, str] | None = None) -> bool:
    """Route `google.auth.default` to `$GOOGLE_OAUTH_ACCESS_TOKEN`'s credentials; whether it did."""
    env = os.environ if environ is None else environ
    token = (env.get(TOKEN_ENV) or "").strip()
    if not token:
        return False
    import google.auth
    from google.oauth2.credentials import Credentials

    project = env.get("GOOGLE_CLOUD_PROJECT") or None

    def default(*args, **kwargs):
        return Credentials(token), project

    google.auth.default = default
    return True
