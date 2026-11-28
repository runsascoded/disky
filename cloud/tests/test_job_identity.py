"""`job_identity`: a local job's minted token (`$GOOGLE_OAUTH_ACCESS_TOKEN`) as every Google client's credentials."""
import google.auth
from google.oauth2.credentials import Credentials

from dt_cloud import job_identity as J


def test_without_a_token_nothing_changes(monkeypatch):
    before = google.auth.default
    assert (J.install({}), J.install({"GOOGLE_OAUTH_ACCESS_TOKEN": " "}), google.auth.default) == (False, False, before)


def test_a_token_becomes_the_default_credentials(monkeypatch):
    monkeypatch.setattr(google.auth, "default", google.auth.default)  # restored after the test
    assert J.install({"GOOGLE_OAUTH_ACCESS_TOKEN": "tok\n", "GOOGLE_CLOUD_PROJECT": "p"}) is True
    creds, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    assert (type(creds), creds.token, project) == (Credentials, "tok", "p")
