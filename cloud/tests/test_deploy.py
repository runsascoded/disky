import pytest

from dt_cloud import deploy
from dt_cloud.access import access_buckets, access_log_bucket
from dt_cloud.deploy import data_bucket, env, site_token, site_url
from dt_cloud.gcp import sii_project
from dt_cloud.sweep import sweep_bucket, sweep_endpoint


def test_set(monkeypatch):
    monkeypatch.setenv("DATA_BUCKET", " my-data ")
    assert (data_bucket(), site_url(), site_url("https://other.example/")) == ("my-data", "https://site.example.org", "https://other.example")


def test_unset_is_an_error_naming_the_variable(monkeypatch):
    for v in ("DATA_BUCKET", "SITE_URL", "GCS_USAGE_URL"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(SystemExit) as e:
        data_bucket()
    assert str(e.value) == "DATA_BUCKET is unset: export the deployment's data bucket (snapshots, index tiers, sweep state)"
    with pytest.raises(SystemExit) as e:
        site_url()
    assert str(e.value) == "no site URL: pass -u or export SITE_URL"


@pytest.fixture
def fresh_notes(monkeypatch):
    monkeypatch.setattr(deploy, "_NOTED", set())


def test_a_renamed_var_falls_back_to_its_old_name_with_one_note(monkeypatch, capsys, fresh_notes):
    for v in ("SITE_URL", "SITE_TOKEN", "SWEEP_BUCKET", "SWEEP_S3_ENDPOINT"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("GCS_USAGE_URL", "https://legacy.example.org")
    monkeypatch.setenv("GCS_USAGE_TOKEN", " tok \n")
    monkeypatch.setenv("CW_BUCKET", "b1")
    monkeypatch.setenv("CW_ENDPOINT", "https://s3.example.com")
    assert (site_url(), site_url(), site_token(), sweep_bucket(), sweep_endpoint()) == (
        "https://legacy.example.org", "https://legacy.example.org", "tok", "b1", "https://s3.example.com",
    )
    assert capsys.readouterr().err.splitlines() == [
        "note: $GCS_USAGE_URL is deprecated; set $SITE_URL",
        "note: $GCS_USAGE_TOKEN is deprecated; set $SITE_TOKEN",
        "note: $CW_BUCKET is deprecated; set $SWEEP_BUCKET",
        "note: $CW_ENDPOINT is deprecated; set $SWEEP_S3_ENDPOINT",
    ]


def test_the_new_name_wins_silently(monkeypatch, capsys, fresh_notes):
    monkeypatch.setenv("SWEEP_BUCKET", "new")
    monkeypatch.setenv("CW_BUCKET", "old")
    monkeypatch.setenv("SITE_TOKEN", "t2")
    assert (sweep_bucket(), site_token(), site_token(" explicit "), env("NOT_SET_ANYWHERE")) == ("new", "t2", "explicit", None)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("read, var, what", [
    (sweep_bucket, "SWEEP_BUCKET", "the bucket a sweep deletes from"),
    (sweep_endpoint, "SWEEP_S3_ENDPOINT", "the S3 endpoint a sweep deletes through"),
    (access_log_bucket, "ACCESS_LOG_BUCKET", "the bucket the usage logs are delivered to"),
    (access_buckets, "ACCESS_BUCKETS", "the buckets whose usage logs to ingest (or pass -b)"),
    (sii_project, "SII_PROJECT", "the GCP project holding the buckets' Storage Insights report configs"),
])
def test_required_deployment_config(monkeypatch, read, var, what):
    for v in ("SWEEP_BUCKET", "CW_BUCKET", "SWEEP_S3_ENDPOINT", "CW_ENDPOINT", "ACCESS_LOG_BUCKET", "ACCESS_BUCKETS", "SII_PROJECT"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(SystemExit) as e:
        read()
    assert str(e.value) == f"{var} is unset: export {what}"


def test_access_buckets_split_on_whitespace(monkeypatch):
    monkeypatch.setenv("ACCESS_BUCKETS", " b1  b2\tb3 ")
    monkeypatch.setenv("ACCESS_LOG_BUCKET", "logs")
    assert (access_buckets(), access_log_bucket()) == (["b1", "b2", "b3"], "logs")
