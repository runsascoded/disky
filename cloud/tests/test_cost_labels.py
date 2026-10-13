"""Cost-attribution labels: `$DISKY_LABELS` + a job's `component`, on every Batch spec dt-cloud builds."""

import json

import pytest
from click.testing import CliRunner

from dt_cloud.batch import listing_job_spec
from dt_cloud.cli import main
from dt_cloud.cost_labels import cost_labels, label_batch_spec, parse_labels
from dt_cloud.static_profile import Profile
from dt_cloud.static_runner import job_spec
from dt_cloud.sweep_job import reviewed_job_spec

DEPLOY = {"app": "disky", "deployment": "gcs"}


@pytest.fixture
def labeled(monkeypatch):
    monkeypatch.setenv("DISKY_LABELS", "app=disky, deployment=gcs")


def test_parse_labels():
    assert parse_labels("") == {}
    assert parse_labels(" app=disky,,deployment=gcs ,team=") == {"app": "disky", "deployment": "gcs", "team": ""}


@pytest.mark.parametrize("text, error", [
    ("app", "DISKY_LABELS: 'app' is not k=v"),
    ("App=disky", "label key 'App': must match [a-z][a-z0-9_-]{0,62}"),
    ("app=Disky", "label app='Disky': value must match [a-z0-9_-]{0,63}"),
    ("app=a,app=b", "DISKY_LABELS: duplicate key 'app'"),
])
def test_parse_labels_refuses(text, error):
    with pytest.raises(ValueError) as e:
        parse_labels(text)
    assert str(e.value) == error


def test_unset_is_opt_out():
    spec = {"labels": {"purpose": "x"}, "allocationPolicy": {}}
    assert cost_labels("scan", {}) == {}
    assert label_batch_spec(spec, "scan", {}) is spec


def test_label_batch_spec_merges_job_and_allocation():
    spec = {"labels": {"purpose": "static-names", "app": "old"}, "allocationPolicy": {"labels": {"x": "1"}, "serviceAccount": {"email": "sa"}}}
    out = label_batch_spec(spec, "drill", {"DISKY_LABELS": "app=disky,deployment=gcs"})
    assert out == {
        "labels": {"purpose": "static-names", "app": "disky", "deployment": "gcs", "component": "drill"},
        "allocationPolicy": {"labels": {"x": "1", "app": "disky", "deployment": "gcs", "component": "drill"}, "serviceAccount": {"email": "sa"}},
    }
    assert spec["labels"] == {"purpose": "static-names", "app": "old"}


def labels_of(spec: dict) -> tuple[dict, dict]:
    return spec.get("labels"), spec["allocationPolicy"].get("labels")


def test_listing_spec(labeled, monkeypatch):
    monkeypatch.setenv("JOB_SA", "job@p.iam.gserviceaccount.com")
    monkeypatch.setenv("JOB_IMAGE", "img")
    want = {**DEPLOY, "component": "listing"}
    assert labels_of(listing_job_spec("2026-10-09", ["b1"])) == (want, want)


@pytest.mark.parametrize("stage, component", [("append", "static-names"), ("r2", "static-names"), ("drill", "drill"), ("anchors", "anchors")])
def test_static_spec(labeled, stage, component):
    cfg = Profile(name="t", gen="2026-10-08c", project="p", region="us-east1", image="img", sa="sa@p", bucket="data", scratch="scr")
    want = {**DEPLOY, "component": component}
    assert labels_of(job_spec(cfg, "sn-x", 1, ["true"], stage=stage)) == ({"purpose": "static-names", "stage": stage, "gen": "2026-10-08c", **want}, want)


def test_reviewed_sweep_spec(labeled):
    template = {
        "taskGroups": [{"taskSpec": {
            "environment": {"variables": {"DATA_BUCKET": "d", "PLAN_ID": "1", "PLAN_DIGEST": "g", "D1_DB_ID": "db", "SITE_URL": "https://s"}},
            "computeResource": {"cpuMilli": 1000, "memoryMib": 1000},
        }}],
        "allocationPolicy": {"instances": [{"policy": {}}], "serviceAccount": {"email": "sa"}, "location": {"allowedLocations": ["regions/us-east1"]}},
    }
    want = {**DEPLOY, "component": "sweep"}
    assert labels_of(reviewed_job_spec(template, "gs://d/dry", "gs://d/p.json", "gs://d/o", "img@sha256:" + "a" * 64)) == (want, want)


def test_cli(labeled):
    run = CliRunner().invoke
    assert run(main, ["cost-labels", "-c", "sheet-sync"]).output == "app=disky,deployment=gcs,component=sheet-sync\n"
    assert json.loads(run(main, ["cost-labels", "-j"]).output) == DEPLOY


def test_cli_unset():
    assert CliRunner().invoke(main, ["cost-labels", "-c", "scan"]).output == "\n"
