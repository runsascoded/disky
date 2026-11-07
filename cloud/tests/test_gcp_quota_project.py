"""`gcp.quota_creds`: only user credentials get the deployment's quota project; a Batch VM's service account goes out
without the `x-goog-user-project` header, which would demand `serviceusage.services.use` of it (gcs's `gcp-cost` 403,
2026-10-27)."""
from __future__ import annotations

from google.auth import compute_engine
from google.oauth2 import credentials

from dt_cloud import gcp


def test_user_credentials_get_the_deployment_project(monkeypatch):
    monkeypatch.setattr(gcp, "_project", "p")
    assert gcp.quota_creds(credentials.Credentials("tok")).quota_project_id == "p"


def test_user_credentials_keep_their_own_quota_project(monkeypatch):
    monkeypatch.setattr(gcp, "_project", "p")
    assert gcp.quota_creds(credentials.Credentials("tok", quota_project_id="q")).quota_project_id == "q"


def test_vm_service_account_goes_out_without_one(monkeypatch):
    monkeypatch.setattr(gcp, "_project", "p")
    vm = compute_engine.Credentials()
    out = gcp.quota_creds(vm)
    assert out is vm
    assert out.quota_project_id is None
