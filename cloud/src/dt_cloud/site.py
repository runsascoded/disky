"""The dashboard's HTTP client — the one convention every verb that reads the
site shares: ``$SITE_TOKEN`` / ``$SITE_URL`` (or ``-t`` / ``-u``),
a Bearer header, and the server's own error string on a non-2xx.
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .deploy import site_token, site_url

# a real UA — CF edge-blocks bot UAs (1010), same as healthcheck.UA
UA = "gcs-usage-cli/1.0"


class SiteError(Exception):
    """A caller-fixable problem (no token, the server refused or is unreachable).

    ``status`` is the HTTP status of a refusal (``None``: unreachable), so a
    caller can tell a legitimate "not found" from an auth or server failure.
    """

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def creds(token: str | None, url: str | None) -> tuple[str, str | None]:
    """Resolve (base_url, token) from args then env, so every verb shares one
    convention: ``$SITE_TOKEN`` / ``$SITE_URL``."""
    return site_url(url), site_token(token)


def get_json(url: str, token: str, endpoint_path: str, params: dict | None = None, timeout: int = 30) -> dict | list:
    """GET ``<url><endpoint_path>?<params>`` as an authenticated JSON call.

    Surfaces the server's own error string on a non-2xx rather than a raw
    ``HTTPError``.
    """
    q = f"?{urlencode(params)}" if params else ""
    endpoint = f"{url.rstrip('/')}{endpoint_path}{q}"
    req = Request(endpoint, headers={"Authorization": f"Bearer {token}", "User-Agent": UA})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            msg = json.loads(body).get("error", body)
        except ValueError:
            msg = body
        raise SiteError(f"{endpoint} → HTTP {e.code}: {msg}", status=e.code) from e
    except URLError as e:
        raise SiteError(f"{endpoint} unreachable: {e.reason}") from e
