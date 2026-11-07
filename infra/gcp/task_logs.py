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


class TaskLogBucket(ComponentResource):
    """A log bucket keeping a project's Batch task logs for `retention_days`, fed by a sink.

    A copy beside `_Default` (whose 30 days, views and Logs Explorer links stay as they are): a project-scoped Logs
    Explorer query reads both buckets, so a run's task logs stay findable for the longer window. A sink into a log
    bucket in its own project needs no grant to its writer.
    """

    def __init__(
        self,
        name: str,
        *,
        project: str,
        location: str,
        bucket_id: str,
        retention_days: int,
        opts: ResourceOptions | None = None,
    ):
        if not 1 <= retention_days <= 3650:
            raise ValueError("retention_days must be 1–3650")
        super().__init__("disky:gcp:TaskLogBucket", name, None, opts)
        self.bucket = gcp.logging.ProjectBucketConfig(
            f"{name}-bucket",
            project=project,
            location=location,
            bucket_id=bucket_id,
            retention_days=retention_days,
            description=f"Batch task logs (container stdout/stderr), {retention_days} days",
            opts=ResourceOptions(parent=self),
        )
        self.sink = gcp.logging.ProjectSink(
            f"{name}-sink",
            project=project,
            name=bucket_id,
            destination=f"logging.googleapis.com/projects/{project}/locations/{location}/buckets/{bucket_id}",
            filter='log_id("batch_task_logs")',
            description=f"Batch task logs → {bucket_id}",
            unique_writer_identity=True,
            opts=ResourceOptions(parent=self, depends_on=[self.bucket]),
        )
        self.path = f"projects/{project}/locations/{location}/buckets/{bucket_id}"
        self.register_outputs({"path": self.path})
