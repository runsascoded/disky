"""`dt-cloud access jobs`: the deployment's job-table image submitted for the days not yet written."""

import datetime as dt

from dt_cloud.access_jobs import batch_problems, job_id, job_spec, plan, submit, submit_args

ENV = {"DISKY_LABELS": "app=disky,deployment=d"}


def write_day(root, day, *names):
    (root / day).mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / day / n).write_bytes(b"")


def test_plan_counts_a_day_written_only_with_both_tables(tmp_path):
    write_day(tmp_path, "2026-11-16", "workers.parquet", "tasks.parquet")
    write_day(tmp_path, "2026-11-17", "workers.parquet")  # its job died between the two
    days = ["2026-11-16", "2026-11-17", "2026-11-18"]
    assert plan(str(tmp_path), days) == (["2026-11-17", "2026-11-18"], ["2026-11-17"])
    assert plan(str(tmp_path), ["2026-11-18"]) == (["2026-11-18"], [])


def test_job_id_is_one_per_set_of_days_per_hour():
    at = dt.datetime(2026, 11, 19, 7, 12, tzinfo=dt.timezone.utc)
    a = job_id("gcs", ["2026-11-17", "2026-11-18"], at)
    assert (a[:27], a[-7:], len(a)) == ("access-jobs-gcs-2026-11-18-", "-111907", 42)
    assert [a == job_id(*args) for args in (
        ("gcs", ["2026-11-18", "2026-11-17"], at.replace(minute=59)),  # a retried step: the same job
        ("gcs", ["2026-11-18"], at),  # another set of days
        ("gcs", ["2026-11-17", "2026-11-18"], at.replace(hour=8)),  # a later run, after that job failed
    )] == [True, False, False]


def test_job_spec_runs_the_image_entrypoint_with_root_and_days():
    spec = job_spec("gs://b/access/jobs", ["2026-11-18", "2026-11-17"], image="r/img:latest", service_account="sa@p",
                    region="us-central1", project="p", env={"SOURCE_BUDGET_GB": "5"}, secrets={"T": "tok"},
                    environ=ENV)
    task = spec["taskGroups"][0]["taskSpec"]
    assert (task["runnables"], task["environment"]["secretVariables"], task["computeResource"], task["maxRetryCount"]) == (
        [{"container": {"imageUri": "r/img:latest", "commands": ["-o", "gs://b/access/jobs", "2026-11-17", "2026-11-18"]}}],
        {"T": "projects/p/secrets/tok/versions/latest"},
        {"cpuMilli": 4000, "memoryMib": 14800},
        2,
    )
    assert task["environment"]["variables"]["SOURCE_BUDGET_GB"] == "5"
    policy = spec["allocationPolicy"]
    assert (policy["instances"], policy["serviceAccount"], policy["location"], policy["labels"]["component"]) == (
        [{"policy": {"machineType": "e2-standard-4", "provisioningModel": "SPOT"}}], {"email": "sa@p"},
        {"allowedLocations": ["regions/us-central1"]}, "access-jobs")


def test_submit_names_no_job_when_every_day_is_written(tmp_path):
    for d in ("2026-11-17", "2026-11-18"):
        write_day(tmp_path, d, "workers.parquet", "tasks.parquet")
    assert submit(str(tmp_path), ["2026-11-17", "2026-11-18"], image="i", service_account="sa", region="r", project="p",
                  environ=ENV) == {"missing": [], "overdue": []}


def test_submit_dry_run_names_the_missing_days_job(tmp_path):
    write_day(tmp_path, "2026-11-17", "workers.parquet", "tasks.parquet")
    out = submit(str(tmp_path), ["2026-11-17", "2026-11-18"], image="i", service_account="sa", region="r", project="p",
                 dry_run=True, environ=ENV, now=dt.datetime(2026, 11, 19, 7, tzinfo=dt.timezone.utc))
    assert (out["missing"], out["overdue"], out["job"][-7:], out["spec"]["taskGroups"][0]["taskSpec"]["runnables"][0]["container"]["commands"]) == (
        ["2026-11-18"], [], "-111907", ["-o", str(tmp_path), "2026-11-18"])


def test_submit_args_and_problems():
    batch = {"image": "i", "region": "us-central1", "spot": False, "cross_region": True, "env": {"B": "2", "A": "1"}}
    assert submit_args(batch) == ["-i", "i", "-L", "us-central1", "-S", "-X", "-e", "A=1", "-e", "B=2"]
    assert batch_problems({"region": 3, "ssd_gb": 1}) == [
        "options.access-jobs.batch.ssd_gb: unknown key (known: image, service_account, region, project, machine, spot, env, secrets, cross_region)",
        "options.access-jobs.batch.region: a string",
        "options.access-jobs.batch.image: required (the deployment's job-table image)",
    ]
    assert batch_problems(batch) == []
