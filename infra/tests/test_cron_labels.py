"""A cron's Batch body is posted by Cloud Scheduler, past `dt-cloud`'s own label check: `BatchCron` refuses one whose job
or allocation policy lacks the stack's cost labels (the allocation policy's are what its VMs and disks bill under)."""
import base64
import json
import sys
from pathlib import Path

import pulumi
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gcp"))
from gcp_jobs import Adopt, BatchCron, check_cron_labels

STACK = {"app": "disky", "deployment": "gcs"}
FULL = {**STACK, "component": "scan"}


@pytest.mark.parametrize("spec, error", [
    ({}, "cron daily: its Batch job's labels lack app, deployment, component"),
    ({"labels": FULL}, "cron daily: its Batch job's allocationPolicy.labels lack app, deployment, component"),
    ({"labels": FULL, "allocationPolicy": {}}, "cron daily: its Batch job's allocationPolicy.labels lack app, deployment, component"),
    ({"labels": FULL, "allocationPolicy": {"labels": STACK}}, "cron daily: its Batch job's allocationPolicy.labels lack component"),
    ({"labels": FULL, "allocationPolicy": {"labels": {**FULL, "deployment": "cw"}}}, "cron daily: its Batch job's allocationPolicy.labels lack deployment"),
    ({"labels": {"app": "disky", "component": "scan"}, "allocationPolicy": {"labels": FULL}}, "cron daily: its Batch job's labels lack deployment"),
])
def test_a_cron_body_missing_a_stack_label_on_either_level_is_refused(spec, error):
    with pytest.raises(ValueError) as e:
        check_cron_labels(spec, STACK, "daily")
    assert str(e.value) == f"{error} (the stack's $DISKY_LABELS + a component go on the job's `labels` and on `allocationPolicy.labels`: its VMs and disks bill under the latter)"


def test_a_cron_body_labelled_on_both_levels_passes():
    assert check_cron_labels({"labels": {**FULL, "purpose": "x"}, "allocationPolicy": {"labels": {**FULL, "scan_id": "s"}}}, STACK, "daily") is None


def test_a_stack_with_no_labels_checks_nothing():
    """`DISKY_LABELS=''` (deliberately none) or unset: the body is taken as the submitter prints it."""
    assert check_cron_labels({}, {}, "daily") is None
    assert check_cron_labels({"labels": {"purpose": "x"}, "allocationPolicy": {}}, {}, "daily") is None


class Mocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources: dict[str, tuple[str, dict]] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[args.name] = (args.typ, args.inputs)
        return args.name, args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


def submitter(tmp_path: Path, spec: dict, levels: list[str]) -> Path:
    """A `job/<name>-submit.sh` printing `spec` with `$DISKY_LABELS` + `component: scan` on `levels` (`job`, `allocation`)."""
    job = tmp_path / "job"
    job.mkdir(exist_ok=True)
    sub = job / "submit.sh"
    sub.write_text(f"""python3 - <<'PY'
import json, os
spec = json.loads({json.dumps(json.dumps(spec))})
labels = dict(i.split("=", 1) for i in os.environ.get("DISKY_LABELS", "").split(",") if i)
if labels:
    labels["component"] = "scan"
    for level in {json.dumps(levels)}:
        (spec if level == "job" else spec["allocationPolicy"])["labels"] = labels
print(json.dumps(spec))
PY
""")
    return sub


def cron(tmp_path: Path, levels: list[str]) -> BatchCron:
    sub = submitter(tmp_path, {"taskGroups": [], "allocationPolicy": {"serviceAccount": {"email": "job@p"}}}, levels)
    return BatchCron("daily", project="p", region="us-east1", schedule="0 7 * * *", submitter=sub, sa_email="job@p", adopt=Adopt(False))


@pulumi.runtime.test
def test_batch_cron_body_carries_the_stacks_labels_on_the_job_and_its_allocation(tmp_path, monkeypatch):
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="test", stack="test", preview=False)
    monkeypatch.setenv("DISKY_LABELS", "app=disky,deployment=gcs")
    c = cron(tmp_path, ["job", "allocation"])

    def check(_):
        typ, inputs = mocks.resources["daily"]
        body = json.loads(base64.b64decode(inputs["httpTarget"]["body"]))
        assert (typ, inputs["httpTarget"]["uri"], body) == (
            "gcp:cloudscheduler/job:Job", "https://batch.googleapis.com/v1/projects/p/locations/us-east1/jobs",
            {"taskGroups": [], "allocationPolicy": {"serviceAccount": {"email": "job@p"}, "labels": FULL}, "labels": FULL},
        )

    return c.job.name.apply(check)


@pytest.mark.parametrize("levels, where", [(["job"], "allocationPolicy.labels"), (["allocation"], "labels"), ([], "labels")])
def test_batch_cron_refuses_a_submitter_that_labels_one_level(tmp_path, monkeypatch, levels, where):
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="test", stack="test", preview=False)
    monkeypatch.setenv("DISKY_LABELS", "app=disky,deployment=gcs")
    with pytest.raises(ValueError) as e:
        cron(tmp_path, levels)
    assert str(e.value).split(" (")[0] == f"cron daily: its Batch job's {where} lack app, deployment, component"
    assert [n for n, (typ, _) in mocks.resources.items() if typ == "gcp:cloudscheduler/job:Job"] == []


@pulumi.runtime.test
def test_batch_cron_without_stack_labels_is_unchanged(tmp_path, monkeypatch):
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="test", stack="test", preview=False)
    monkeypatch.delenv("DISKY_LABELS", raising=False)
    c = cron(tmp_path, ["job"])

    def check(_):
        body = json.loads(base64.b64decode(mocks.resources["daily"][1]["httpTarget"]["body"]))
        assert body == {"taskGroups": [], "allocationPolicy": {"serviceAccount": {"email": "job@p"}}}

    return c.job.name.apply(check)
