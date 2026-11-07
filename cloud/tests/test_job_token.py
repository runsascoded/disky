from json import dumps
from io import BytesIO
from subprocess import CompletedProcess
from urllib.error import HTTPError

import pytest

from dt_cloud import job_token as J


@pytest.mark.parametrize("existing", [False, True])
def test_provision_is_read_only_idempotent_and_never_puts_token_in_argv(monkeypatch, existing):
    token = "test-token-not-for-logs"
    hashed = J.token_hash(token)
    grant_id = J.token_hash(f"p/s/{hashed}")[:12]
    expected = {"id": grant_id, "name": "snapshot", "scopes": "gcs:read", "revoked_at": None, "disabled_at": None, "expires_at": None}
    calls = []
    sql = []
    monkeypatch.setattr(J, "token_urlsafe", lambda n: token)
    monkeypatch.setattr(J, "time", lambda: 123)
    monkeypatch.setattr(J, "_creds", lambda: ("api", "account"))

    def command(args, **kw):
        calls.append((args, kw.get("input")))
        out = dumps([{"name": "versions/1"}]) if existing else "[]"
        if args[3] == "access":
            out = token
        return CompletedProcess(args, 0, stdout=out, stderr="")

    def query(statement, account, api):
        sql.append(statement)
        return [expected] if existing or len(sql) == 3 else []

    monkeypatch.setattr(J, "run", command)
    monkeypatch.setattr(J, "_d1_query", query)
    assert J.provision("p", "s", "snapshot", "gcs:read") == grant_id
    assert calls == [
        (["gcloud", "secrets", "versions", "list", "s", "--project", "p", "--format=json"], None),
        (["gcloud", "secrets", "versions", "access", "latest", "--secret", "s", "--project", "p"], None) if existing else
        (["gcloud", "secrets", "versions", "add", "s", "--project", "p", "--data-file=-"], token),
    ]
    select = f"SELECT id, name, scopes, revoked_at, disabled_at, expires_at FROM grants WHERE token_hash='{hashed}'"
    assert sql == ([select] if existing else [select,
        f'INSERT INTO grants (id, token_hash, name, note, subject_json, scopes, redeems, created_at, created_by, expiry_ends_sessions) VALUES (\'{grant_id}\', \'{hashed}\', \'snapshot\', \'Read-only job grant\', \'{{"name": "snapshot"}}\', \'gcs:read\', 0, 123, \'job-token provisioning\', 1)',
        select,
    ])


def test_provision_refuses_write_scopes_before_external_actions():
    with pytest.raises(ValueError) as exc:
        J.provision("p", "s", "snapshot", "gcs admin")
    assert str(exc.value) == "job-token: scope must be one read-only deployment scope (e.g. gcs:read)"


def test_expiring_grant_is_exact_and_refuses_expiry_changes(monkeypatch):
    token = "test-token"
    hashed = J.token_hash(token)
    grant_id = J.token_hash(f"p/s/{hashed}")[:12]
    sql = []
    expected = {"id": grant_id, "name": "monitor", "scopes": "gcs:session-monitor:read", "revoked_at": None, "disabled_at": None, "expires_at": 1792540800}
    monkeypatch.setattr(J, "time", lambda: 123)
    monkeypatch.setattr(J, "token_urlsafe", lambda n: token)
    monkeypatch.setattr(J, "_creds", lambda: ("api", "account"))
    monkeypatch.setattr(J, "run", lambda args, **kw: CompletedProcess(args, 0, stdout="[]", stderr=""))

    def query(statement, account, api):
        sql.append(statement)
        return [expected] if len(sql) >= 3 else []

    monkeypatch.setattr(J, "_d1_query", query)
    assert J.provision("p", "s", "monitor", "gcs:session-monitor:read", expires_at=1792540800, note="Monitor only") == grant_id
    assert sql[1] == f'INSERT INTO grants (id, token_hash, name, note, subject_json, scopes, redeems, created_at, created_by, expiry_ends_sessions, expires_at) VALUES (\'{grant_id}\', \'{hashed}\', \'monitor\', \'Monitor only\', \'{{"name": "monitor"}}\', \'gcs:session-monitor:read\', 0, 123, \'job-token provisioning\', 1, 1792540800)'
    with pytest.raises(ValueError) as exc:
        J.provision("p", "s", "monitor", "gcs:session-monitor:read", expires_at=1792540801)
    assert str(exc.value) == "job-token: stored secret belongs to a different, expired or revoked grant; refusing to widen or reactivate it"


@pytest.mark.parametrize("identity", [
    {"scopes": ["gcs:read"], "admin": False},
    {"scopes": ["gcs:read", "admin"], "admin": True},
    {"scopes": ["gcs"], "admin": False},
])
def test_live_identity_verification_is_exact_and_identifies_the_client(monkeypatch, identity):
    calls = []

    def open_request(request, timeout):
        calls.append((request.full_url, dict(request.header_items()), timeout))
        return BytesIO(dumps(identity).encode())

    monkeypatch.setattr(J, "urlopen", open_request)
    if identity == {"scopes": ["gcs:read"], "admin": False}:
        assert J.verify_identity("https://site.test/", "test-token", "gcs:read") is None
    else:
        with pytest.raises(ValueError) as exc:
            J.verify_identity("https://site.test/", "test-token", "gcs:read")
        assert str(exc.value) == "job-token: live identity does not have exactly the expected read-only scope"
    assert calls == [("https://site.test/api/me", {"Authorization": "Bearer test-token", "User-agent": "dt-cloud/job-token"}, 30)]


def test_live_verification_error_does_not_dump_response_or_token(monkeypatch):
    def rejected(request, timeout):
        raise HTTPError(request.full_url, 403, "Forbidden", {"content-type": "text/html", "cf-ray": "test-ray"}, BytesIO(b"private response"))

    monkeypatch.setattr(J, "urlopen", rejected)
    with pytest.raises(ValueError) as exc:
        J.verify_identity("https://site.test", "test-token", "gcs:read")
    assert str(exc.value) == "job-token: live verification HTTP 403; content-type=text/html; cf-ray=test-ray"
