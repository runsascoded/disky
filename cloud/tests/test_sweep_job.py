"""Reviewed dispatch keeps deployment credentials, pins code and disables retries."""

import json

import pytest

from dt_cloud.sweep_job import reviewed_job_spec, wait_drained


IMAGE = "registry/image@sha256:" + "a" * 64


@pytest.fixture
def template():
    return {
        "name": "old-job", "uid": "old-uid",
        "taskGroups": [{"taskSpec": {
            "environment": {"variables": {"DATA_BUCKET": "data", "PLAN_ID": "1", "PLAN_DIGEST": "digest", "D1_DB_ID": "db", "SITE_URL": "https://site.test"}, "secretVariables": {"SITE_TOKEN": "secret-ref"}},
            "computeResource": {"cpuMilli": 32000, "memoryMib": 250000},
        }}],
        "allocationPolicy": {
            "instances": [{"policy": {"machineType": "n2-highmem-32"}}],
            "serviceAccount": {"email": "job@example.test"},
            "location": {"allowedLocations": ["regions/us-central1", "zones/us-central1-a"]},
            "labels": {"batch-job-id": "old-job"},
        },
    }


def test_reviewed_job_spec(template):
    result = reviewed_job_spec(template, "gs://data/dry", "gs://data/plan.json", "gs://data/new", IMAGE, for_real=True, buckets=("b1",))
    assert result == {
        "taskGroups": [{"taskCount": 1, "taskSpec": {
            "runnables": [{"container": {"imageUri": IMAGE, "entrypoint": "/bin/bash", "commands": ["-c", '\n'.join([
                "set -euo pipefail",
                'trap \'curl -fsS -m 60 -o /dev/null -H "Authorization: Bearer $SITE_TOKEN" "$SITE_URL/api/sweep/jobs" || true\' EXIT',
                "dt-cloud sweep execute-reviewed -e xml -j 32 -r 8000 -p gs://data/plan.json -o gs://data/new -w /work/reviewed -B 6 -c guided -b b1 --for-real gs://data/dry",
            ])]}}],
            "computeResource": {"cpuMilli": 32000, "memoryMib": 250000},
            "maxRetryCount": 0, "maxRunDuration": "259200s",
            "environment": template["taskGroups"][0]["taskSpec"]["environment"],
        }}],
        "allocationPolicy": {
            "instances": [{"policy": {"machineType": "n2-highmem-32"}}],
            "serviceAccount": {"email": "job@example.test"},
            "location": {"allowedLocations": ["regions/us-central1"]},
        },
        "logsPolicy": {"destination": "CLOUD_LOGGING"},
    }


def test_dry_dispatch_and_shell_quoting(template):
    result = reviewed_job_spec(template, "gs://data/dry space", "plan", "out", IMAGE)
    script = result["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1]
    assert script.splitlines()[-1] == "dt-cloud sweep execute-reviewed -e xml -j 32 -r 8000 -p plan -o out -w /work/reviewed -B 6 -c guided 'gs://data/dry space'"


def test_refuses_unpinned_image(template):
    with pytest.raises(ValueError, match="^reviewed job image must be pinned by sha256 digest$"):
        reviewed_job_spec(template, "source", "plan", "out", "registry/image:latest")


def test_requires_plan_identity(template):
    del template["taskGroups"][0]["taskSpec"]["environment"]["variables"]["PLAN_DIGEST"]
    with pytest.raises(ValueError, match="^reviewed job template lacks PLAN_DIGEST$"):
        reviewed_job_spec(template, "source", "plan", "out", IMAGE)


def test_wait_barrier_is_before_any_execute(template):
    result = reviewed_job_spec(template, "source", "plan", "out", IMAGE, after_run="gs://data/old")
    script = result["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1]
    assert script.splitlines()[2:] == [
        "dt-cloud sweep wait-drained gs://data/old",
        "dt-cloud sweep execute-reviewed -e xml -j 32 -r 8000 -p plan -o out -w /work/reviewed -B 6 -c guided source",
    ]


def test_bucket_concurrency_uses_one_explicit_shared_worker_pool(template):
    result = reviewed_job_spec(template, "source", "plan", "out", IMAGE, workers=32, bucket_workers=4, pacing="adaptive")
    script = result["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1]
    assert script.splitlines()[-1] == "dt-cloud sweep execute-reviewed -e xml -j 32 -r 8000 -p plan -o out -w /work/reviewed -B 4 -c adaptive source"


def test_resume_exclusions_are_after_the_drain_barrier(template):
    result = reviewed_job_spec(template, "source", "plan", "out", IMAGE, after_run="gs://data/old", bucket_workers=4, exclude_runs=("gs://data/old", "gs://data/older"))
    script = result["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"][1]
    assert script.splitlines()[2:] == [
        "dt-cloud sweep wait-drained gs://data/old",
        "dt-cloud sweep execute-reviewed -e xml -j 32 -r 8000 -p plan -o out -w /work/reviewed -B 4 -c guided -x gs://data/old -x gs://data/older source",
    ]


def test_wait_drained_requires_stop_and_valid_summary(tmp_path):
    run = str(tmp_path)
    with pytest.raises(ValueError, match="^handoff requires the old run's STOP marker$"):
        wait_drained(run, timeout=0)
    (tmp_path / "STOP").touch()
    with pytest.raises(TimeoutError, match="^old executor did not drain before the handoff timeout; no XML deletions started$"):
        wait_drained(run, timeout=0)
    (tmp_path / "deleted-summary.json").write_text(json.dumps({"plan": run, "for_real": False}))
    with pytest.raises(ValueError, match="^old run drain summary does not identify the real executor$"):
        wait_drained(run, timeout=0)
    expected = {"plan": run, "for_real": True, "buckets": {"b1": {"decisions": {"delete": 5}}}}
    (tmp_path / "deleted-summary.json").write_text(json.dumps(expected))
    assert wait_drained(run, timeout=0) == expected
