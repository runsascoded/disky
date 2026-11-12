"""Provision one independently revocable read-only job grant into Secret Manager.

Secret containers and accessor IAM belong to the deployment's Pulumi stack.
Only the token hash reaches D1; raw tokens travel on stdin, never argv/logs.
Retries reuse the stored token, including after a partial D1 failure.
"""
from base64 import urlsafe_b64encode
from hashlib import sha256
from json import dumps, loads
import re
from secrets import token_urlsafe
from subprocess import run
from time import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from click import argument, command, option

from .index_footer import _creds, _d1_query, _q


def token_hash(token: str) -> str:
    return urlsafe_b64encode(sha256(token.encode()).digest()).decode().rstrip("=")


#: A store's `series_scope` (`site/functions/_lib/stores.ts` `SERIES_SCOPE_RE`): a bare scope name, never `admin`.
SERIES_SCOPE_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


def verify_identity(
    url: str,
    token: str,
    scope: str,
    series_scopes: tuple[str, ...] = (),
) -> None:
    req = Request(f"{url.rstrip('/')}/api/me", headers={"Authorization": f"Bearer {token}", "User-Agent": "dt-cloud/job-token"})
    try:
        with urlopen(req, timeout=30) as response:
            identity = loads(response.read(1_048_576))
    except HTTPError as exc:
        # Classify edge failures without dumping a response or credential.
        raise ValueError(f"job-token: live verification HTTP {exc.code}; content-type={exc.headers.get('content-type')}; cf-ray={exc.headers.get('cf-ray')}") from None
    if sorted(identity.get("scopes") or []) != sorted([scope, *series_scopes]) or identity.get("admin") is not False:
        raise ValueError("job-token: live identity does not have exactly the expected read-only scope")


def provision(
    project: str,
    secret_id: str,
    name: str,
    scope: str,
    url: str | None = None,
    expires_at: int | None = None,
    note: str = "Read-only job grant",
    series_scopes: tuple[str, ...] = (),
) -> str:
    """`series_scopes`: store `series_scope` names granted beside `scope` (each opens only that store's root
    `/api/series`, e.g. the fleet dashboard's `fleet`); the grant's scopes are `scope` then them, space-separated."""
    if not scope.endswith(":read") or any(c.isspace() for c in scope):
        raise ValueError("job-token: scope must be one read-only deployment scope (e.g. gcs:read)")
    bad = [x for x in series_scopes if not SERIES_SCOPE_RE.fullmatch(x) or x == "admin"]
    if bad:
        raise ValueError(f"job-token: series scopes must be store `series_scope` names (not admin): {', '.join(bad)}")
    scopes = " ".join([scope, *series_scopes])
    if expires_at is not None and expires_at <= time():
        raise ValueError("job-token: expiry must be in the future")
    cmd = ["gcloud", "secrets", "versions"]
    versions = run([*cmd, "list", secret_id, "--project", project, "--format=json"], check=True, capture_output=True, text=True)
    if loads(versions.stdout):
        token = run([*cmd, "access", "latest", "--secret", secret_id, "--project", project], check=True, capture_output=True, text=True).stdout.strip()
    else:
        token = token_urlsafe(24)
        run([*cmd, "add", secret_id, "--project", project, "--data-file=-"], input=token, check=True, capture_output=True, text=True)
    hashed = token_hash(token)
    # Stable for this secret/token pair: a retry cannot invent another grant.
    grant_id = token_hash(f"{project}/{secret_id}/{hashed}")[:12]
    api_token, account = _creds()
    rows = _d1_query(f"SELECT id, name, scopes, revoked_at, disabled_at, expires_at FROM grants WHERE token_hash={_q(hashed)}", account, api_token)
    expected = {"id": grant_id, "name": name, "scopes": scopes, "revoked_at": None, "disabled_at": None, "expires_at": expires_at}
    if rows:
        if rows != [expected]:
            raise ValueError("job-token: stored secret belongs to a different, expired or revoked grant; refusing to widen or reactivate it")
    else:
        columns = "id, token_hash, name, note, subject_json, scopes, redeems, created_at, created_by, expiry_ends_sessions"
        values = [grant_id, hashed, name, note, dumps({"name": name}), scopes]
        extra_column = ", expires_at" if expires_at is not None else ""
        extra_value = f", {expires_at}" if expires_at is not None else ""
        sql = f"INSERT INTO grants ({columns}{extra_column}) VALUES ({', '.join(_q(v) for v in values)}, 0, {int(time())}, 'job-token provisioning', 1{extra_value})"
        _d1_query(sql, account, api_token)
        rows = _d1_query(f"SELECT id, name, scopes, revoked_at, disabled_at, expires_at FROM grants WHERE token_hash={_q(hashed)}", account, api_token)
        if rows != [expected]:
            raise ValueError("job-token: grant verification failed")
    if url:
        verify_identity(url, token, scope, series_scopes)
    return grant_id


@command("job-token")
@option("-c", "--scope", required=True, help="Read-only deployment scope, e.g. gcs:read")
@option("-n", "--name", required=True, help="Named grant label (no email required)")
@option("-p", "--project", required=True, help="GCP project holding the existing secret container")
@option("-s", "--series-scope", "series_scopes", multiple=True, help="Also a store's `series_scope` (repeatable): its root-level /api/series only, e.g. fleet")
@option("-u", "--url", help="Also verify the token's exact read-only scope against this site's /api/me")
@argument("secret_id")
def cli(
    scope: str,
    name: str,
    project: str,
    series_scopes: tuple[str, ...],
    url: str | None,
    secret_id: str,
) -> None:
    """Store a job read token and register its hash in this deployment's D1.

    Requires $D1_DB_ID + CF credentials. Pulumi must create SECRET_ID and
    its job-account accessor first. Prints the non-secret grant ID only.
    """
    from click import echo

    grant_id = provision(project, secret_id, name, scope, url, series_scopes=series_scopes)
    echo(f"job-token: {name}: grant {grant_id} verified; token stored in {project}/{secret_id}")
