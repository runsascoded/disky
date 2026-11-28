"""`job_metadata`: a local job's own GCE metadata server, as Google's client libraries read it."""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from dt_cloud.job_metadata import MetadataServer

SA = "job@p.iam.gserviceaccount.com"
SCOPE = "https://www.googleapis.com/auth/cloud-platform"
FLAVOR = {"metadata-flavor": "Google"}


class Clock:
    def __init__(self, t: float = 1000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


def server(clock: Clock | None = None, allow: str = "10.1.0.0/16", fail: bool = False) -> tuple[MetadataServer, list[int]]:
    minted: list[int] = []

    def mint(lifetime: int) -> str:
        if fail:
            raise PermissionError("403")
        minted.append(lifetime)
        return f"tok-{len(minted)}"

    return MetadataServer(SA, "p", mint, bind="127.0.0.1", allow=allow, lifetime_s=3600, clock=clock or Clock(),
                          log=lambda s: None), minted


@pytest.mark.parametrize("path, answer", [
    ("/", (200, "application/text", "computeMetadata/\n")),
    ("/computeMetadata/v1/project/project-id", (200, "application/text", "p")),
    ("/computeMetadata/v1/universe/universe-domain", (200, "application/text", "googleapis.com")),
    ("/computeMetadata/v1/instance/service-accounts/", (200, "application/text", f"default/\n{SA}/\n")),
    ("/computeMetadata/v1/instance/service-accounts/default/?recursive=true",
     (200, "application/json", json.dumps({"aliases": ["default"], "email": SA, "scopes": [SCOPE]}))),
    (f"/computeMetadata/v1/instance/service-accounts/{SA}/email", (200, "application/text", SA)),
    ("/computeMetadata/v1/instance/service-accounts/default/scopes", (200, "application/text", f"{SCOPE}\n")),
    ("/computeMetadata/v1/instance/service-accounts/default/token?scopes=x",
     (200, "application/json", json.dumps({"access_token": "tok-1", "expires_in": 3600, "token_type": "Bearer"}))),
    ("/computeMetadata/v1/instance/service-accounts/other@p.iam.gserviceaccount.com/token", (404, "text/plain", "not found\n")),
    ("/computeMetadata/v1/instance/zone", (404, "text/plain", "not found\n")),
])
def test_it_answers_what_the_clients_ask(path, answer):
    s, _ = server()
    assert s.answer("10.1.2.3", path, FLAVOR) == answer


def test_it_refuses_other_networks_and_requests_without_the_flavor_header():
    s, minted = server()
    path = "/computeMetadata/v1/instance/service-accounts/default/token"
    assert (s.answer("10.2.0.5", path, FLAVOR), s.answer("10.1.0.5", path, {}), minted) == (
        (403, "text/plain", "forbidden\n"), (403, "text/plain", "Missing Metadata-Flavor:Google header.\n"), [])


def test_a_token_is_reused_until_it_nears_its_expiry():
    clock = Clock()
    s, minted = server(clock)
    path = "/computeMetadata/v1/instance/service-accounts/default/token"
    seen = []
    for dt in (0, 3000, 301):  # 3,000 s in: 600 s left, reused; 3,301 s in: 299 s left, under the 300 s mark
        clock.t += dt
        seen.append(json.loads(s.answer("10.1.0.5", path, FLAVOR)[2]))
    assert (seen, minted) == ([
        {"access_token": "tok-1", "expires_in": 3600, "token_type": "Bearer"},
        {"access_token": "tok-1", "expires_in": 600, "token_type": "Bearer"},
        {"access_token": "tok-2", "expires_in": 3600, "token_type": "Bearer"},
    ], [3600, 3600])


def test_a_token_that_cannot_be_minted_is_a_500():
    s, _ = server(fail=True)
    assert s.answer("10.1.0.5", "/computeMetadata/v1/instance/service-accounts/default/token", FLAVOR) == (
        500, "text/plain", f"token for {SA} unavailable\n")


def test_google_auth_finds_and_uses_it(tmp_path):
    """In a fresh interpreter pointed at the server (the variables a job's containers get), `google.auth.default()`
    takes the GCE path: the server's project, and credentials whose refresh gets the server's token."""
    s, minted = server(allow="127.0.0.0/8")
    address = s.start()
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GOOGLE_", "GCLOUD_", "GCE_", "CLOUDSDK_"))}
        env.update(HOME=str(tmp_path), GCE_METADATA_HOST=address, GCE_METADATA_IP=address, GCE_METADATA_ROOT=address)
        code = ("import google.auth, google.auth.transport.requests as r\n"
                "c, p = google.auth.default()\n"
                "c.refresh(r.Request())\n"
                "print(type(c).__module__, p, c.token, c.service_account_email)\n")
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    finally:
        s.stop()
    assert (out.returncode, out.stdout, minted) == (0, f"google.auth.compute_engine.credentials p tok-1 {SA}\n", [3600]), out.stderr
