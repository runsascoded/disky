"""Read-only Batch task logs, isolated from a project's other log streams."""

from json import dumps
from re import fullmatch

import pulumi
import pulumi_gcp as gcp
from pulumi import ComponentResource, ResourceOptions


class TaskLogView(ComponentResource):
    """An additive reader grant on a deployment-filtered view, not the project."""

    def __init__(
        self,
        name: str,
        *,
        project: str,
        location: str,
        bucket: str,
        view_id: str,
        job_uid_prefix: str,
        reader: pulumi.Input[str],
        opts: ResourceOptions | None = None,
    ):
        if not fullmatch(r"[a-zA-Z0-9_-]+", job_uid_prefix):
            raise ValueError("job_uid_prefix must be a nonempty literal job-name prefix")
        super().__init__("disky:gcp:TaskLogView", name, None, opts)
        parent = f"projects/{project}"
        self.path = f"{parent}/locations/{location}/buckets/{bucket}/views/{view_id}"
        self.view = gcp.logging.LogView(
            f"{name}-view",
            parent=parent,
            location=location,
            bucket=bucket,
            name=view_id,
            description="Deployment Batch task logs only; no agent, audit, or unrelated job logs",
            filter=f'log_id("batch_task_logs") AND labels.job_uid =~ {dumps("^" + job_uid_prefix)}',
            opts=ResourceOptions(parent=self),
        )
        self.reader = gcp.logging.LogViewIamMember(
            f"{name}-reader",
            parent=parent,
            location=location,
            bucket=bucket,
            name=self.view.name,
            role="roles/logging.viewAccessor",
            member=reader,
            opts=ResourceOptions(parent=self),
        )
        self.register_outputs({"path": self.path})
