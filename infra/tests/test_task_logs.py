from pathlib import Path
import sys

import pulumi
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gcp"))
from task_logs import TaskLogBucket, TaskLogView


class Mocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources: dict[str, tuple[str, dict]] = {}

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[args.name] = (args.typ, args.inputs)
        return args.name, args.inputs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {}


@pulumi.runtime.test
def test_task_logs_reader_is_view_scoped_and_deployment_filtered():
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="test", stack="test", preview=False)
    logs = TaskLogView(
        "logs",
        project="deployment",
        location="global",
        bucket="_Default",
        view_id="task-logs",
        job_uid_prefix="sweep-",
        reader="serviceAccount:site@deployment.iam.gserviceaccount.com",
    )

    def check(_):
        assert logs.path == "projects/deployment/locations/global/buckets/_Default/views/task-logs"
        assert mocks.resources == {
            "logs": ("disky:gcp:TaskLogView", {}),
            "logs-view": ("gcp:logging/logView:LogView", {
                "parent": "projects/deployment",
                "location": "global",
                "bucket": "_Default",
                "name": "task-logs",
                "description": "Deployment Batch task logs only; no agent, audit, or unrelated job logs",
                "filter": 'log_id("batch_task_logs") AND labels.job_uid =~ "^sweep-"',
            }),
            "logs-reader": ("gcp:logging/logViewIamMember:LogViewIamMember", {
                "parent": "projects/deployment",
                "location": "global",
                "bucket": "_Default",
                "name": "task-logs",
                "role": "roles/logging.viewAccessor",
                "member": "serviceAccount:site@deployment.iam.gserviceaccount.com",
            }),
        }

    return pulumi.Output.all(logs.view.id, logs.reader.id).apply(check)


@pytest.mark.parametrize("prefix", ["", 'sweep-" OR true', "sweep-.*"])
def test_task_log_prefix_must_be_literal(prefix: str):
    with pytest.raises(ValueError, match="^job_uid_prefix must be a nonempty literal job-name prefix$"):
        TaskLogView("logs", project="deployment", location="global", bucket="_Default", view_id="v", job_uid_prefix=prefix, reader="reader")


@pulumi.runtime.test
def test_task_log_bucket_keeps_batch_task_logs_beside_default():
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="test", stack="test", preview=False)
    logs = TaskLogBucket("tl", project="deployment", location="global", bucket_id="batch-task-logs", retention_days=90)

    def check(_):
        assert logs.path == "projects/deployment/locations/global/buckets/batch-task-logs"
        assert mocks.resources == {
            "tl": ("disky:gcp:TaskLogBucket", {}),
            "tl-bucket": ("gcp:logging/projectBucketConfig:ProjectBucketConfig", {
                "project": "deployment",
                "location": "global",
                "bucketId": "batch-task-logs",
                "retentionDays": 90,
                "description": "Batch task logs (container stdout/stderr), 90 days",
            }),
            "tl-sink": ("gcp:logging/projectSink:ProjectSink", {
                "project": "deployment",
                "name": "batch-task-logs",
                "destination": "logging.googleapis.com/projects/deployment/locations/global/buckets/batch-task-logs",
                "filter": 'log_id("batch_task_logs")',
                "description": "Batch task logs → batch-task-logs",
                "uniqueWriterIdentity": True,
            }),
        }

    return pulumi.Output.all(logs.bucket.id, logs.sink.id).apply(check)


@pytest.mark.parametrize("days", [0, 3651])
def test_task_log_bucket_retention_bounds(days: int):
    with pytest.raises(ValueError, match="^retention_days must be 1–3650$"):
        TaskLogBucket("tl", project="p", location="global", bucket_id="b", retention_days=days)


@pulumi.runtime.test
def test_a_deployments_bucket_and_view_take_only_its_labelled_task_logs():
    mocks = Mocks()
    pulumi.runtime.set_mocks(mocks, project="test", stack="test", preview=False)
    logs = TaskLogBucket("cw-logs", project="p", location="global", bucket_id="cw-task-logs", retention_days=365, deployment="cw")
    view = TaskLogView("cw-view", project="p", location="global", bucket="cw-task-logs", view_id="cw", deployment="cw",
                       reader="serviceAccount:job@p.iam.gserviceaccount.com")
    only_cw = 'log_id("batch_task_logs") AND labels.deployment = "cw"'

    def check(_):
        assert (mocks.resources["cw-logs-sink"][1]["filter"], mocks.resources["cw-logs-sink"][1]["description"]) == (
            only_cw, "Batch task logs of cw → cw-task-logs")
        assert mocks.resources["cw-logs-bucket"][1]["retentionDays"] == 365
        assert mocks.resources["cw-view-view"][1]["filter"] == only_cw
        assert view.path == "projects/p/locations/global/buckets/cw-task-logs/views/cw"

    return pulumi.Output.all(logs.sink.id, view.view.id).apply(check)


@pytest.mark.parametrize("kw", [{}, {"job_uid_prefix": "sn-", "deployment": "cw"}])
def test_a_view_filters_by_exactly_one_of_prefix_and_deployment(kw: dict):
    with pytest.raises(ValueError, match="^exactly one of job_uid_prefix and deployment$"):
        TaskLogView("v", project="p", location="global", bucket="b", view_id="v", reader="r", **kw)


@pytest.mark.parametrize("dep", ["", 'cw" OR true', "CW"])
def test_a_deployment_filter_takes_only_a_label_value(dep: str):
    with pytest.raises(ValueError, match="^deployment must be a label value"):
        TaskLogBucket("tl", project="p", location="global", bucket_id="b", retention_days=30, deployment=dep)
