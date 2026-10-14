from pathlib import Path
import sys

import pulumi
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gcp"))
from task_logs import TaskLogView


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
