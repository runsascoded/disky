"""Every test runs as a deployment with a neutral data bucket and site
(`dt_cloud.deploy`): dt-cloud has no default of its own. A test of the unset
case removes them with `monkeypatch.delenv`."""
import pytest


@pytest.fixture(autouse=True)
def _deployment_env(monkeypatch):
    monkeypatch.setenv("DATA_BUCKET", "my-data")
    monkeypatch.setenv("SITE_URL", "https://site.example.org")
