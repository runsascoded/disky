"""Submit oversized deletions to AWS Batch (spec ``specs/staged-delete.md``, CP8).

The drainer deletes small runs inline (the "CFN" cell); a run whose scope exceeds
the Batch threshold is handed to a Batch job instead — a container that does the
recursive delete and writes the run's result back to D1. This is the submit half;
the container + the Batch compute/queue/job-definition are provisioned by the IaC
(``iac/aws/``). ``client`` is a boto3 ``batch`` client (or a fake in tests).
"""
from __future__ import annotations

from typing import Any


def submit_delete_job(
    client: Any,
    *,
    run_id: str,
    uris: list[str],
    job_queue: str,
    job_definition: str,
) -> str:
    """Submit a Batch delete job for ``run_id`` over ``uris``; return its job id.
    The container reads ``RUN_ID`` + ``URIS`` (newline-separated) from its env."""
    resp = client.submit_job(
        jobName=f"disk-tree-delete-{run_id}",
        jobQueue=job_queue,
        jobDefinition=job_definition,
        containerOverrides={
            "environment": [
                {"name": "RUN_ID", "value": run_id},
                {"name": "URIS", "value": "\n".join(uris)},
            ],
        },
    )
    return resp["jobId"]
