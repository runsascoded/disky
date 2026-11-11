"""`BatchSubmitCron`: Cloud Scheduler runs a Cloud Run job whose script submits the Batch spec (idempotently, with its
own retries and alert), so the cron's call is retried too, the task is not, and the spec is the stack's rendering."""
import json
import sys
from pathlib import Path

import pulumi
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gcp"))
from gcp_jobs import Adopt, BatchSubmitCron
from test_cron_labels import FULL, Mocks, submitter

SPEC = {"taskGroups": [], "allocationPolicy": {"serviceAccount": {"email": "job@p"}}}
LABELLED = {"taskGroups": [], "allocationPolicy": {"serviceAccount": {"email": "job@p"}, "labels": FULL}, "labels": FULL}
SCRIPT = "#!/usr/bin/env bash\ndt-cloud batch submit -t -e -r 1800 scan-%Y%m%d \"$spec\"\n"


def cron(tmp_path: Path, levels: list[str]) -> BatchSubmitCron:
    script = tmp_path / "job" / "scan-submit.sh"
    sub = submitter(tmp_path, SPEC, levels)
    script.write_text(SCRIPT)
    return BatchSubmitCron(
        "daily", project="p", region="us-east1", job="daily-submit", trigger="daily", schedule="0 7 * * *",
        submitter=sub, script=script, sa_email="job@p", image="img:latest", env={"SLACK_ALERT_CHANNEL": "C1"},
        secret_env={"SLACK_BOT_TOKEN": "bot-token"}, adopt=Adopt(False),
    )


@pulumi.runtime.test
def test_the_cron_runs_a_job_that_submits_the_rendered_spec(tmp_path, monkeypatch):
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="test", stack="test", preview=False)
    monkeypatch.setenv("DISKY_LABELS", "app=disky,deployment=gcs")
    c = cron(tmp_path, ["job", "allocation"])

    def check(_):
        typ, job = mocks.resources["daily-submit"]
        task = job["template"]["template"]
        assert (typ, task["maxRetries"], task["timeout"], task["serviceAccount"]) == ("gcp:cloudrunv2/job:Job", 0, "2400s", "job@p")
        assert task["containers"] == [{
            "image": "img:latest",
            "commands": ["bash", "-c", SCRIPT],
            "resources": {"limits": {"cpu": "1000m", "memory": "1Gi"}},
            "envs": [
                {"name": "BATCH_SPEC", "value": json.dumps(LABELLED, sort_keys=True)},
                {"name": "DISKY_LABELS", "value": "app=disky,deployment=gcs"},
                {"name": "SLACK_ALERT_CHANNEL", "value": "C1"},
                {"name": "SLACK_BOT_TOKEN", "valueSource": {"secretKeyRef": {"secret": "bot-token", "version": "latest"}}},
            ],
        }]
        typ, trigger = mocks.resources["daily"]
        assert (typ, trigger["httpTarget"]["uri"], trigger["httpTarget"].get("body"), trigger["retryConfig"]) == (
            "gcp:cloudscheduler/job:Job", "https://us-east1-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/p/jobs/daily-submit:run", None,
            {"retryCount": 5, "maxRetryDuration": "1800s", "minBackoffDuration": "30s", "maxBackoffDuration": "300s", "maxDoublings": 4},
        )

    return c.cron.name.apply(check)


@pytest.mark.parametrize("levels, where", [(["job"], "allocationPolicy.labels"), (["allocation"], "labels")])
def test_an_unlabelled_spec_is_refused_before_any_resource(tmp_path, monkeypatch, levels, where):
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="test", stack="test", preview=False)
    monkeypatch.setenv("DISKY_LABELS", "app=disky,deployment=gcs")
    with pytest.raises(ValueError) as e:
        cron(tmp_path, levels)
    assert str(e.value).split(" (")[0] == f"cron daily: its Batch job's {where} lack app, deployment, component"
    assert mocks.resources == {}
