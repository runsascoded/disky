"""Every test runs as a deployment with a neutral data bucket and site
(`dt_cloud.deploy`): dt-cloud has no default of its own. A test of the unset
case removes them with `monkeypatch.delenv`. The layer-2 template and the
static-names profile start unset. No cost labels (`DISKY_LABELS`)
leak in from the shell: a test that wants them sets them."""
import pytest


@pytest.fixture(autouse=True)
def _deployment_env(monkeypatch):
    monkeypatch.setenv("DATA_BUCKET", "my-data")
    monkeypatch.setenv("SITE_URL", "https://site.example.org")
    monkeypatch.delenv("DISKY_LABELS", raising=False)
    # Deployment config the generic code requires (no defaults): unset unless a test sets it.
    for v in ("LAYER2_PREFIX", "STATIC_NAMES_PROFILE", "STATIC_NAMES_GEN", "SCAN_RUN_ID", "SCAN_ID"):
        monkeypatch.delenv(v, raising=False)
