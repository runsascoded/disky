"""`BatchIngest` / `CaptureTrigger` under Pulumi's mocks: every resource each
declares (type, logical name, physical name), the job's container spec and the
image build command — no cloud account needed.

    uv run --project infra --with pytest pytest infra/tests
"""
import json
import sys
from pathlib import Path

import pulumi
import pytest

INFRA = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(INFRA / "aws"), str(INFRA / "cf")]


class Mocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.created: list[tuple[str, str, dict]] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.created.append((args.typ, args.name, args.inputs))
        outs = {**args.inputs, "arn": f"arn:{args.name}", "repository_url": f"acct.dkr.ecr/{args.inputs.get('name')}", "queue_id": f"qid-{args.name}"}
        if args.typ == "command:local:Command":
            outs["stdout"] = "acct.dkr.ecr/ingest@sha256:abc"
        return f"{args.name}_id", outs

    def call(self, args: pulumi.runtime.MockCallArgs):
        return {
            "aws:index/getCallerIdentity:getCallerIdentity": {"accountId": "123456789012", "arn": "a", "userId": "u", "id": "123456789012"},
            "aws:ec2/getVpc:getVpc": {"id": "vpc-1"},
            "aws:ec2/getSubnets:getSubnets": {"ids": ["subnet-a", "subnet-b"], "id": "s"},
            "aws:ec2/getSecurityGroup:getSecurityGroup": {"id": "sg-1"},
        }.get(args.token, {})


@pytest.fixture
def mocks():
    m = Mocks()
    pulumi.runtime.set_mocks(m, project="t", stack="t", preview=False)
    return m


def physical(typ: str, inputs: dict) -> str | None:
    """The resource's own name in the cloud, where the program sets one."""
    key = {"aws:s3/bucket:Bucket": "bucket", "aws:s3/bucketObjectv2:BucketObjectv2": "key"}.get(typ, "name")
    return inputs.get(key)


@pulumi.runtime.test
def test_batch_ingest(mocks, tmp_path):
    from batch_ingest import BatchIngest, IngestSpec

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    spec = IngestSpec(
        prefix="dt-x",
        repo_root=tmp_path,
        sources=("src", "Dockerfile"),
        dockerfile="Dockerfile",
        vcpu="1",
        memory_mib="8192",
        ephemeral_gib=30,
        env={"INDEX_VARIANTS": "path"},
        secrets={"AWS_ACCESS_KEY_ID": "dt-x/r2-key-id"},
        build_cmd="python3 build-image",
    )
    ing = BatchIngest("ingest", spec)

    def check(args):
        image, props = args
        assert image == "acct.dkr.ecr/ingest@sha256:abc"
        rows = sorted((t, n, physical(t, i)) for t, n, i in mocks.created if t != "disky:aws:BatchIngest")
        assert rows == sorted([
            ("aws:iam/role:Role", "ingest-execution-role", "dt-x-batch-execution"),
            ("aws:iam/rolePolicyAttachment:RolePolicyAttachment", "ingest-execution-ecs-policy", None),
            ("aws:secretsmanager/secret:Secret", "ingest-secret-dt-x-r2-key-id", "dt-x/r2-key-id"),
            ("aws:iam/rolePolicy:RolePolicy", "ingest-execution-secrets-policy", None),
            ("aws:iam/role:Role", "ingest-task-role", "dt-x-batch-task"),
            ("aws:ecr/repository:Repository", "ingest-repo", "dt-x"),
            ("aws:ecr/lifecyclePolicy:LifecyclePolicy", "ingest-repo-lifecycle", None),
            ("aws:s3/bucket:Bucket", "ingest-build-src", "dt-x-build-src-123456789012"),
            ("aws:s3/bucketLifecycleConfiguration:BucketLifecycleConfiguration", "ingest-build-src-lifecycle", None),
            ("aws:s3/bucketObjectv2:BucketObjectv2", "ingest-build-src-zip", next(i["key"] for t, n, i in mocks.created if n == "ingest-build-src-zip")),
            ("aws:iam/role:Role", "ingest-codebuild-role", "dt-x-codebuild"),
            ("aws:iam/rolePolicy:RolePolicy", "ingest-codebuild-policy", None),
            ("aws:cloudwatch/logGroup:LogGroup", "ingest-codebuild-logs", "/dt-x/codebuild"),
            ("aws:codebuild/project:Project", "ingest-image-build", "dt-x-image"),
            ("command:local:Command", "ingest-image-build-run", None),
            ("aws:batch/computeEnvironment:ComputeEnvironment", "ingest-spot-env", "dt-x-spot"),
            ("aws:batch/jobQueue:JobQueue", "ingest-spot-queue", "dt-x"),
            ("aws:cloudwatch/logGroup:LogGroup", "ingest-log-group", "/dt-x-ingest/batch"),
            ("aws:batch/jobDefinition:JobDefinition", "ingest-ingest", "dt-x-ingest"),
            ("aws:iam/user:User", "ingest-capture-trigger", "dt-x-capture-trigger"),
            ("aws:iam/userPolicy:UserPolicy", "ingest-capture-trigger-submit", None),
        ])
        cmd = next(i for t, n, i in mocks.created if t == "command:local:Command")
        assert cmd["create"].split(" ")[:3] == ["python3", "build-image", "dt-x-image"]
        assert cmd["environment"] == {"AWS_REGION": "us-east-1"}
        assert json.loads(props) == {
            "image": "acct.dkr.ecr/ingest@sha256:abc",
            "runtimePlatform": {"operatingSystemFamily": "LINUX", "cpuArchitecture": "ARM64"},
            "resourceRequirements": [{"type": "VCPU", "value": "1"}, {"type": "MEMORY", "value": "8192"}],
            "executionRoleArn": "arn:ingest-execution-role",
            "jobRoleArn": "arn:ingest-task-role",
            "environment": [{"name": "PYTHONFAULTHANDLER", "value": "1"}, {"name": "INDEX_VARIANTS", "value": "path"}],
            "secrets": [{"name": "AWS_ACCESS_KEY_ID", "valueFrom": "arn:ingest-secret-dt-x-r2-key-id"}],
            "networkConfiguration": {"assignPublicIp": "ENABLED"},
            "fargatePlatformConfiguration": {"platformVersion": "LATEST"},
            "ephemeralStorage": {"sizeInGiB": 30},
            "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "/dt-x-ingest/batch", "awslogs-region": "us-east-1", "awslogs-stream-prefix": "dt-x"}},
        }

    return pulumi.Output.all(ing.image, ing.job_def.container_properties).apply(check)


@pulumi.runtime.test
def test_capture_trigger(mocks):
    from capture_trigger import CaptureTrigger

    ct = CaptureTrigger("captures", account_id="acct", bucket="dt-bucket", queue_name="dt-captures")

    def check(_):
        rows = sorted((t, n) for t, n, i in mocks.created if t != "disky:cf:CaptureTrigger")
        assert rows == [
            ("cloudflare:index/queue:Queue", "captures-queue"),
            ("cloudflare:index/r2BucketEventNotification:R2BucketEventNotification", "captures-notification"),
        ]
        note = next(i for t, n, i in mocks.created if n == "captures-notification")
        assert (note["bucketName"], note["queueId"], note["rules"]) == ("dt-bucket", "qid-captures-queue", [{
            "actions": ["PutObject", "CompleteMultipartUpload", "CopyObject"],
            "prefix": "captures/",
            "suffix": "_SUCCESS.json",
            "description": "a finished capture → ingest",
        }])

    return ct.notification.id.apply(check)
