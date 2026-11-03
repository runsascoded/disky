"""Cost-attribution labels: `$DISKY_LABELS` + a job's `component`, on every Batch spec dt-cloud builds, required
(specs/done/cost-labels-required.md)."""

import ast
import json
import re
from pathlib import Path

import pytest
from click.testing import CliRunner

from dt_cloud.batch import listing_job_spec
from dt_cloud.cli import main
from dt_cloud.cost_labels import MissingCostLabels, audit_lines, audit_rows, audit_sql, check_spec, cost_labels, label_batch_spec, parse_labels
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


def labels_of(spec: dict) -> tuple[dict, dict]:
    return spec.get("labels"), spec["allocationPolicy"].get("labels")


HINT = "($DISKY_LABELS carries app=…,deployment=…; each job names its component)"


@pytest.mark.parametrize("environ, component, missing", [
    ({}, "scan", "app, deployment"),
    ({"DISKY_LABELS": ""}, "scan", "app, deployment"),
    ({"DISKY_LABELS": "app=disky"}, "scan", "deployment"),
    ({"DISKY_LABELS": "deployment=gcs,team=x"}, "scan", "app"),
    ({"DISKY_LABELS": "app=disky,deployment="}, "scan", "deployment"),
    ({"DISKY_LABELS": "app=disky,deployment=gcs"}, None, "component"),
    ({"DISKY_LABELS": "app=disky,deployment=gcs"}, "", "component"),
    ({"SCAN_RUN_ID": "root-1", "SCAN_ID": "2026-10-13T1300"}, "scan", "app, deployment"),
])
def test_missing_labels_refused(environ, component, missing):
    """No unset-means-unlabelled path: every way a label can be missing raises, naming the missing keys."""
    spec = {"labels": {"purpose": "x"}, "allocationPolicy": {}, "taskGroups": [{}]}
    for call in (lambda: cost_labels(component, environ), lambda: label_batch_spec(spec, component, environ)):
        with pytest.raises(MissingCostLabels) as e:
            call()
        assert str(e.value) == f"cost labels: missing {missing} {HINT}"
    assert spec == {"labels": {"purpose": "x"}, "allocationPolicy": {}, "taskGroups": [{}]}


def test_missing_labels_refused_from_the_process_env(monkeypatch):
    monkeypatch.delenv("DISKY_LABELS")
    with pytest.raises(MissingCostLabels) as e:
        label_batch_spec({}, "scan")
    assert (e.value.missing, e.value.where) == (["app", "deployment"], "cost labels")


FULL = {"app": "disky", "deployment": "gcs", "component": "race"}


@pytest.mark.parametrize("spec, error", [
    ({}, f"Batch job labels: missing app, deployment, component {HINT}"),
    ({"labels": FULL}, f"Batch job allocationPolicy.labels: missing app, deployment, component {HINT}"),
    ({"labels": FULL, "allocationPolicy": {"labels": {**FULL, "deployment": ""}}}, f"Batch job allocationPolicy.labels: missing deployment {HINT}"),
    ({"labels": {"app": "disky", "deployment": "gcs"}, "allocationPolicy": {"labels": FULL}}, f"Batch job labels: missing component {HINT}"),
])
def test_check_spec_refuses(spec, error):
    with pytest.raises(MissingCostLabels) as e:
        check_spec(spec)
    assert str(e.value) == error


def test_check_spec_passes_a_labelled_spec():
    assert check_spec({"labels": FULL, "allocationPolicy": {"labels": FULL}}) is None


def test_submit_job_refuses_before_the_network(monkeypatch):
    """The choke point: `submit_job` checks the final spec; an unlabelled one never reaches a session."""
    from dt_cloud import batch, gcp

    def no_network():
        raise AssertionError("submit_job reached the network")

    monkeypatch.setattr(gcp, "session", no_network)
    monkeypatch.setattr(batch, "session", no_network)
    with pytest.raises(MissingCostLabels) as e:
        batch.submit_job({"labels": FULL, "allocationPolicy": {}}, "race-1", "us-east1", "p")
    assert str(e.value) == f"Batch job race-1 allocationPolicy.labels: missing app, deployment, component {HINT}"


def test_submit_job_posts_a_labelled_spec(monkeypatch):
    from dt_cloud import batch

    posted = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"name": "projects/p/locations/us-east1/jobs/race-1"}

    class Session:
        def post(self, url, json, params):
            posted.append((url, json, params))
            return Response()

    monkeypatch.setattr(batch, "session", Session)
    spec = {"labels": FULL, "allocationPolicy": {"labels": FULL}}
    assert batch.submit_job(spec, "race-1", "us-east1", "p") == "race-1"
    assert posted == [("https://batch.googleapis.com/v1/projects/p/locations/us-east1/jobs", spec, {"job_id": "race-1"})]


SRC = Path(__file__).parents[1] / "src" / "dt_cloud"
#: What creating a Batch job looks like, other than `batch.submit_job`'s POST: the client library, or `gcloud`.
OTHER_CREATE = re.compile(r"batch_v1|google\.cloud\.batch|gcloud\W+(?:\w+\W+)?batch\W+jobs\W+submit|[\"']batch[\"'],\s*[\"']jobs[\"'],\s*[\"']submit")
POST = re.compile(r"\.post\(|\b_post\(")


def test_one_batch_create_call_site():
    """Every Batch job dt-cloud creates goes through `batch.submit_job` (the label check): no function but it both names
    the Batch API and POSTs, and no module creates jobs by the client library or `gcloud batch jobs submit`."""
    posters, others = [], []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text()
        others.extend(f"{path.relative_to(SRC)}:{text.count(chr(10), 0, m.start()) + 1}" for m in OTHER_CREATE.finditer(text))
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = ast.get_source_segment(text, node) or ""
                if "batch.googleapis.com" in body and POST.search(body):
                    posters.append(f"{path.relative_to(SRC)}:{node.name}")
    assert (posters, others) == (["batch.py:submit_job"], [])


def test_grandchildren_inherit_without_scan_context():
    """A job that submits helpers (a race head, a compaction, a fan-out) hands them `$DISKY_LABELS` in its tasks' env,
    scan or not: each generation labels identically, its `component` its own."""
    env = {"DISKY_LABELS": "app=disky,deployment=gcs"}
    child = label_batch_spec({"taskGroups": [{"taskSpec": {}}, {}]}, "compact-probe", env)
    want = {"app": "disky", "deployment": "gcs", "component": "compact-probe"}
    assert labels_of(child) == (want, want)
    assert [g["taskSpec"]["environment"] for g in child["taskGroups"]] == [{"variables": env}, {"variables": env}]
    # the child VM's env is all the grandchild's submitter sees
    grandchild = label_batch_spec({"labels": {"stage": "measure"}, "taskGroups": [{}]}, "drill",
                                  child["taskGroups"][0]["taskSpec"]["environment"]["variables"])
    want = {"app": "disky", "deployment": "gcs", "component": "drill"}
    assert labels_of(grandchild) == ({"stage": "measure", **want}, want)
    assert grandchild["taskGroups"] == [{"taskSpec": {"environment": {"variables": env}}}]


def test_label_batch_spec_merges_job_and_allocation():
    spec = {"labels": {"purpose": "static-names", "app": "old"}, "allocationPolicy": {"labels": {"x": "1"}, "serviceAccount": {"email": "sa"}}}
    out = label_batch_spec(spec, "drill", {"DISKY_LABELS": "app=disky,deployment=gcs"})
    assert out == {
        "labels": {"purpose": "static-names", "app": "disky", "deployment": "gcs", "component": "drill"},
        "allocationPolicy": {"labels": {"x": "1", "app": "disky", "deployment": "gcs", "component": "drill"}, "serviceAccount": {"email": "sa"}},
    }
    assert spec["labels"] == {"purpose": "static-names", "app": "old"}


def test_listing_spec(labeled, monkeypatch):
    monkeypatch.setenv("JOB_SA", "job@p.iam.gserviceaccount.com")
    monkeypatch.setenv("JOB_IMAGE", "img")
    want = {**DEPLOY, "component": "listing", "scan_id": "2026-10-09"}
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
    assert json.loads(run(main, ["cost-labels", "-j", "-c", "scan"]).output) == {**DEPLOY, "component": "scan"}


@pytest.mark.parametrize("args, labels, missing", [
    (["-c", "scan"], None, "app, deployment"),
    (["-c", "scan"], "app=disky", "deployment"),
    (["-j"], "app=disky,deployment=gcs", "component"),
])
def test_cli_refuses(monkeypatch, args, labels, missing):
    if labels is None:
        monkeypatch.delenv("DISKY_LABELS")
    else:
        monkeypatch.setenv("DISKY_LABELS", labels)
    r = CliRunner().invoke(main, ["cost-labels", *args])
    assert (r.exit_code, type(r.exception), str(r.exception), r.output) == (1, MissingCostLabels, f"cost labels: missing {missing} {HINT}", "")


def _submit_cli(monkeypatch, tmp_path, *args):
    from dt_cloud import batch

    posted = []
    monkeypatch.setattr(batch, "submit_job", lambda spec, job_id, region, project: posted.append((spec, job_id, region, project)) or job_id)
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({
        "taskGroups": [{"taskSpec": {"runnables": [{"container": {"imageUri": "img", "commands": ["true"]}}],
                                     "environment": {"variables": {"X": "1"}}}}],
        "allocationPolicy": {"instances": [{"policy": {"machineType": "n2-standard-4"}}], "labels": {"purpose": "race"}},
        "labels": {"purpose": "race"},
    }))
    return CliRunner().invoke(main, ["batch", "submit", *args, "sn-race-1", str(spec)]), posted


def test_batch_submit_cli_round_trip(labeled, monkeypatch, tmp_path):
    """`dt-cloud batch submit`: the shell's submit, labelled from `$DISKY_LABELS` + `-c` and handed to `submit_job`."""
    monkeypatch.setenv("SCAN_ID", "2026-10-25T0700")
    r, posted = _submit_cli(monkeypatch, tmp_path, "-c", "race", "-L", "us-east1", "-p", "proj")
    want = {**DEPLOY, "component": "race", "scan_id": "2026-10-25t0700"}
    assert (r.exit_code, r.output) == (0, "sn-race-1\n")
    assert posted == [({
        "taskGroups": [{"taskSpec": {"runnables": [{"container": {"imageUri": "img", "commands": ["true"]}}],
                                     "environment": {"variables": {"X": "1", "DISKY_LABELS": "app=disky, deployment=gcs", "SCAN_ID": "2026-10-25T0700"}}}}],
        "allocationPolicy": {"instances": [{"policy": {"machineType": "n2-standard-4"}}], "labels": {"purpose": "race", **want}},
        "labels": {"purpose": "race", **want},
    }, "sn-race-1", "us-east1", "proj")]


def test_batch_submit_cli_defaults_region(labeled, monkeypatch, tmp_path):
    from dt_cloud.gcp import REGION

    r, posted = _submit_cli(monkeypatch, tmp_path, "-c", "analysis")
    assert (r.exit_code, [(job_id, region, project) for _, job_id, region, project in posted]) == (0, [("sn-race-1", REGION, None)])


def test_batch_submit_cli_refuses_unlabelled(monkeypatch, tmp_path):
    monkeypatch.delenv("DISKY_LABELS")
    r, posted = _submit_cli(monkeypatch, tmp_path, "-c", "race")
    assert (r.exit_code, type(r.exception), str(r.exception), posted) == (1, MissingCostLabels, f"cost labels: missing app, deployment {HINT}", [])


def test_audit_sql():
    assert audit_sql("billing-proj.billing.gcp_billing_export_v1_0000", 2) == """SELECT
  (SELECT value FROM UNNEST(labels) WHERE key = 'batch-job-id') AS job_id,
  MIN(DATE(usage_start_time)) AS first_day,
  SUM(cost) + SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) AS c), 0)) AS net
FROM `billing-proj.billing.gcp_billing_export_v1_0000`
WHERE _PARTITIONTIME >= TIMESTAMP_TRUNC(TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY), DAY)
  AND usage_start_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 2 DAY)
  AND service.description = 'Compute Engine'
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'batch-job-id')
  AND NOT EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'deployment')
GROUP BY job_id
ORDER BY net DESC, job_id"""


@pytest.mark.parametrize("table, days, error", [
    ("billing", 2, "billing table 'billing': expected project.dataset.table"),
    ("p.d.t`; DROP", 2, "billing table 'p.d.t`; DROP': expected project.dataset.table"),
    ("p.d.t", 0, "days: 0 < 1"),
])
def test_audit_sql_refuses(table, days, error):
    with pytest.raises(ValueError) as e:
        audit_sql(table, days)
    assert str(e.value) == error


AUDIT_PAGE = {"jobComplete": True, "rows": [
    {"f": [{"v": "sn-compact-probe-1"}, {"v": "2026-10-19"}, {"v": "41.237"}]},
    {"f": [{"v": "gcs-iv-repair-2"}, {"v": "2026-10-21"}, {"v": "1.5"}]},
]}
AUDIT_ROWS = [("sn-compact-probe-1", "2026-10-19", 41.237), ("gcs-iv-repair-2", "2026-10-21", 1.5)]


def test_audit_rows_and_lines():
    assert audit_rows(AUDIT_PAGE) == AUDIT_ROWS
    assert audit_rows({"jobComplete": True}) == []
    assert audit_lines(AUDIT_ROWS) == ["sn-compact-probe-1\t2026-10-19\t$41.24", "gcs-iv-repair-2\t2026-10-21\t$1.50", "total\t2 jobs\t$42.74"]
    assert audit_lines([]) == []


@pytest.mark.parametrize("rows, code, output", [
    (AUDIT_ROWS, 1, "sn-compact-probe-1\t2026-10-19\t$41.24\ngcs-iv-repair-2\t2026-10-21\t$1.50\ntotal\t2 jobs\t$42.74\n"),
    ([], 0, ""),
])
def test_audit_cli(monkeypatch, rows, code, output):
    from dt_cloud import cost_labels

    queries = []
    monkeypatch.setattr(cost_labels, "query_rows", lambda sql, project: queries.append((sql, project)) or rows)
    r = CliRunner().invoke(main, ["cost-labels", "audit", "-d", "2", "-p", "billing-proj", "-t", "billing-proj.billing.export"])
    assert (r.exit_code, r.stdout, queries) == (code, output, [(audit_sql("billing-proj.billing.export", 2), "billing-proj")])


def test_scan_context_reaches_allocations_and_grandchildren():
    env = {"DISKY_LABELS": "app=disky,deployment=gcs", "SCAN_RUN_ID": "root-1", "SCAN_ID": "2026-10-13T1300"}
    spec = {"labels": {"stage": "merge"}, "taskGroups": [{"taskSpec": {"environment": {"variables": {"OTHER": "keep"}}}}]}
    labeled = label_batch_spec(spec, "static-names", env, scan_id="2026-10-12T2006")
    want = {**DEPLOY, "component": "static-names", "scan_run": "root-1", "scan_id": "2026-10-12t2006", "stage": "merge"}
    assert labels_of(labeled) == (want, want)
    variables = labeled["taskGroups"][0]["taskSpec"]["environment"]["variables"]
    assert variables == {"OTHER": "keep", "DISKY_LABELS": "app=disky,deployment=gcs", "SCAN_RUN_ID": "root-1", "SCAN_ID": "2026-10-12T2006"}
    # A descendant VM has a different automatic UID, but keeps the root.
    assert cost_labels("anchors", {**variables, "BATCH_JOB_UID": "child-2"}) == {
        **DEPLOY, "component": "anchors", "scan_run": "root-1", "scan_id": "2026-10-12t2006",
    }
    assert spec == {"labels": {"stage": "merge"}, "taskGroups": [{"taskSpec": {"environment": {"variables": {"OTHER": "keep"}}}}]}


def test_bad_context_refuses():
    with pytest.raises(ValueError) as e:
        cost_labels("scan", {"DISKY_LABELS": "app=disky,deployment=gcs", "SCAN_RUN_ID": "x/y"})
    assert str(e.value) == "label scan_run='x/y': value must match [a-z0-9_-]{0,63}"


def test_append_job_uses_actual_catch_up_scan_not_parent_scan(labeled, monkeypatch):
    from dt_cloud.static_runner import Runner

    monkeypatch.setenv("SCAN_RUN_ID", "root-uid")
    monkeypatch.setenv("SCAN_ID", "2026-10-13T1300")
    cfg = Profile(name="t", gen="g1", project="p", region="us-east1", image="img", sa="sa@p", bucket="data", scratch="scr")
    runner = Runner(cfg=cfg, exists=lambda _: False, count=lambda *_: 0, read_json=lambda _: {},
                    published=lambda *_: [], run_job=lambda *_: None, prepare=lambda _: None,
                    prune=lambda _: None, publish=lambda _: None)
    _, spec = runner.job("anchors", "2026-10-12T2006", 1, "static_anchors", ["run"])
    want = {**DEPLOY, "component": "anchors", "scan_run": "root-uid", "scan_id": "2026-10-12t2006", "stage": "anchors"}
    assert spec["allocationPolicy"]["labels"] == want
    assert spec["taskGroups"][0]["taskSpec"]["environment"]["variables"] == {
        "STATIC_NAMES_BUCKET": "data", "STATIC_NAMES_SCRATCH": "scr",
        "DISKY_LABELS": "app=disky, deployment=gcs", "SCAN_RUN_ID": "root-uid", "SCAN_ID": "2026-10-12T2006",
    }
