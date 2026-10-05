"""`BatchIngest`: an AWS Batch (Fargate Spot) job that ingests captures.

The laptop side only walks (`disk-tree capture` → layer-1 shards in an object
store); this component owns what runs after: an arm64 image built on CodeBuild
from the repo's sources (no local Docker), a Fargate Spot queue, the job
definition, and the IAM user a capture trigger submits jobs as.

Generic: every name derives from `prefix`, every size and env var is an input.
Secret *containers* are created empty from a `{env_var: secret_name}` map; the
values go in out of band (a deployment's `put-secrets`), so they never enter
Pulumi state. The trigger user gets no access key here for the same reason.

`moved_from_root=True` aliases each child to the same-named resource at the
stack root, so a stack that declared these resources inline adopts the
component with no replacements (`pulumi preview` shows 0 create / delete).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import pulumi
import pulumi_aws as aws
import pulumi_command as command

ECS_TASKS_TRUST = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "ecs-tasks.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
})
BUILD_IMAGE = Path(__file__).resolve().parent / "build-image"


@dataclass(frozen=True)
class IngestSpec:
    prefix: str                           # every resource name: `<prefix>`, `<prefix>-ingest`, …
    repo_root: Path                       # what `sources` / `dockerfile` are relative to
    sources: tuple[str, ...]              # files and dirs zipped into the image build
    dockerfile: str                       # relative to `repo_root`
    vcpu: str = "4"
    memory_mib: str = "16384"
    ephemeral_gib: int = 50
    max_vcpus: int = 16
    timeout_s: int = 2 * 3600
    env: dict[str, str] = field(default_factory=dict)       # plain job env
    secrets: dict[str, str] = field(default_factory=dict)   # job env var → Secrets Manager name
    keep_images: int = 5
    log_days: int = 30
    # The image build's shell command, given `<project> <bucket>/<key> <repo> <tag>`.
    # Default: this directory's `build-image`, by path relative to `repo_root`
    # (machine-independent, so state doesn't churn between hosts).
    build_cmd: str | None = None


def _files(spec: IngestSpec) -> list[Path]:
    files = []
    for rel in spec.sources:
        p = spec.repo_root / rel
        files += sorted(f for f in p.rglob("*") if f.is_file() and "__pycache__" not in f.parts) if p.is_dir() else [p]
    return files


def source_hash(root: Path, files: list[Path]) -> str:
    """Content hash of the build's inputs (paths + bytes): one image per hash."""
    h = hashlib.sha256()
    for f in files:
        h.update(str(f.relative_to(root)).encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:16]


class BatchIngest(pulumi.ComponentResource):
    queue: aws.batch.JobQueue
    job_def: aws.batch.JobDefinition
    image: pulumi.Output[str]
    log_group: aws.cloudwatch.LogGroup
    trigger_user: aws.iam.User

    def __init__(
        self,
        name: str,
        spec: IngestSpec,
        moved_from_root: bool = False,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("disky:aws:BatchIngest", name, None, opts)
        P = spec.prefix
        region = aws.config.region or "us-east-1"
        account_id = aws.get_caller_identity().account_id

        def o(old: str, **kw) -> pulumi.ResourceOptions:
            aliases = [pulumi.Alias(name=old, parent=pulumi.ROOT_STACK_RESOURCE)] if moved_from_root else None
            return pulumi.ResourceOptions(parent=self, aliases=aliases, **kw)

        def n(old: str) -> str:
            return f"{name}-{old}"

        vpc = aws.ec2.get_vpc(default=True)
        subnets = aws.ec2.get_subnets(filters=[aws.ec2.GetSubnetsFilterArgs(name="vpc-id", values=[vpc.id])])
        sg = aws.ec2.get_security_group(vpc_id=vpc.id, name="default")

        # Execution role: the ECS agent (image pull, logs, the injected secrets).
        execution_role = aws.iam.Role(n("execution-role"), name=f"{P}-batch-execution", assume_role_policy=ECS_TASKS_TRUST, opts=o("execution-role"))
        aws.iam.RolePolicyAttachment(
            n("execution-ecs-policy"),
            role=execution_role.name,
            policy_arn="arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy",
            opts=o("execution-ecs-policy"),
        )
        secret_arns: dict[str, pulumi.Output[str]] = {}
        for env_var, secret in spec.secrets.items():
            old = f"secret-{secret.replace('/', '-')}"
            secret_arns[env_var] = aws.secretsmanager.Secret(n(old), name=secret, recovery_window_in_days=0, opts=o(old)).arn
        if secret_arns:
            aws.iam.RolePolicy(
                n("execution-secrets-policy"),
                role=execution_role.id,
                policy=pulumi.Output.all(*secret_arns.values()).apply(lambda arns: json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{"Effect": "Allow", "Action": "secretsmanager:GetSecretValue", "Resource": list(arns)}],
                })),
                opts=o("execution-secrets-policy"),
            )
        # Task role: the container's identity. Its data store's keys come in as
        # secrets, so it gets no AWS grants.
        task_role = aws.iam.Role(n("task-role"), name=f"{P}-batch-task", assume_role_policy=ECS_TASKS_TRUST, opts=o("task-role"))

        repo = aws.ecr.Repository(
            n("repo"),
            name=P,
            image_tag_mutability="MUTABLE",
            force_delete=True,
            encryption_configurations=[aws.ecr.RepositoryEncryptionConfigurationArgs(encryption_type="AES256")],
            opts=o("repo"),
        )
        aws.ecr.LifecyclePolicy(
            n("repo-lifecycle"),
            repository=repo.name,
            policy=json.dumps({"rules": [{
                "rulePriority": 1,
                "description": f"keep the {spec.keep_images} newest images",
                "selection": {"tagStatus": "any", "countType": "imageCountMoreThan", "countNumber": spec.keep_images},
                "action": {"type": "expire"},
            }]}),
            opts=o("repo-lifecycle"),
        )

        # The image, built on CodeBuild: the sources are zipped (content-hashed)
        # and uploaded, and `build_cmd` runs one build per new hash, printing the
        # pinned `<repo>@sha256:…` ref on stdout.
        files = _files(spec)
        src_hash = source_hash(spec.repo_root, files)
        build_bucket = aws.s3.Bucket(n("build-src"), bucket=f"{P}-build-src-{account_id}", force_destroy=True, opts=o("build-src"))
        aws.s3.BucketLifecycleConfiguration(
            n("build-src-lifecycle"),
            bucket=build_bucket.id,
            rules=[aws.s3.BucketLifecycleConfigurationRuleArgs(
                id="expire-sources", status="Enabled",
                filter=aws.s3.BucketLifecycleConfigurationRuleFilterArgs(prefix="src/"),
                expiration=aws.s3.BucketLifecycleConfigurationRuleExpirationArgs(days=30),
            )],
            opts=o("build-src-lifecycle"),
        )
        src_key = f"src/{src_hash}.zip"
        src_obj = aws.s3.BucketObjectv2(
            n("build-src-zip"),
            bucket=build_bucket.id,
            key=src_key,
            source=pulumi.AssetArchive({str(f.relative_to(spec.repo_root)): pulumi.FileAsset(str(f)) for f in files}),
            opts=o("build-src-zip"),
        )
        codebuild_role = aws.iam.Role(n("codebuild-role"), name=f"{P}-codebuild", assume_role_policy=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Principal": {"Service": "codebuild.amazonaws.com"}, "Action": "sts:AssumeRole"}],
        }), opts=o("codebuild-role"))
        aws.iam.RolePolicy(
            n("codebuild-policy"),
            role=codebuild_role.id,
            policy=pulumi.Output.all(build_bucket.arn, repo.arn).apply(lambda a: json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], "Resource": "*"},
                    {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion"], "Resource": f"{a[0]}/*"},
                    {"Effect": "Allow", "Action": "ecr:GetAuthorizationToken", "Resource": "*"},
                    {"Effect": "Allow", "Action": [
                        "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
                        "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload", "ecr:PutImage", "ecr:UploadLayerPart",
                    ], "Resource": a[1]},
                ],
            })),
            opts=o("codebuild-policy"),
        )
        buildspec = f"""version: 0.2
phases:
  pre_build:
    commands:
      - aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin ${{REPO_URL%%/*}}
  build:
    commands:
      - docker build -f {spec.dockerfile} -t $REPO_URL:$TAG .
  post_build:
    commands:
      - docker push $REPO_URL:$TAG
"""
        codebuild_logs = aws.cloudwatch.LogGroup(n("codebuild-logs"), name=f"/{P}/codebuild", retention_in_days=spec.log_days, opts=o("codebuild-logs"))
        project = aws.codebuild.Project(
            n("image-build"),
            name=f"{P}-image",
            service_role=codebuild_role.arn,
            build_timeout=30,
            artifacts=aws.codebuild.ProjectArtifactsArgs(type="NO_ARTIFACTS"),
            environment=aws.codebuild.ProjectEnvironmentArgs(
                type="ARM_CONTAINER",
                image="aws/codebuild/amazonlinux-aarch64-standard:3.0",
                compute_type="BUILD_GENERAL1_SMALL",
                privileged_mode=True,
                environment_variables=[aws.codebuild.ProjectEnvironmentEnvironmentVariableArgs(name="REPO_URL", value=repo.repository_url)],
            ),
            source=aws.codebuild.ProjectSourceArgs(
                type="S3",
                location=pulumi.Output.concat(build_bucket.bucket, "/", src_key),
                buildspec=buildspec,
            ),
            logs_config=aws.codebuild.ProjectLogsConfigArgs(
                cloudwatch_logs=aws.codebuild.ProjectLogsConfigCloudwatchLogsArgs(group_name=codebuild_logs.name),
            ),
            opts=o("image-build"),
        )
        build_cmd = spec.build_cmd or f"python3 {os.path.relpath(BUILD_IMAGE, spec.repo_root)}"
        build = command.local.Command(
            n("image-build-run"),
            create=pulumi.Output.concat(build_cmd, " ", project.name, " ", build_bucket.bucket, "/", src_key, " ", repo.name, " ", src_hash),
            dir=str(spec.repo_root),
            environment={"AWS_REGION": region},
            triggers=[src_hash],
            opts=o("image-build-run", depends_on=[src_obj, project]),
        )
        self.image = build.stdout

        compute_env = aws.batch.ComputeEnvironment(
            n("spot-env"),
            name=f"{P}-spot",
            type="MANAGED",
            compute_resources=aws.batch.ComputeEnvironmentComputeResourcesArgs(
                type="FARGATE_SPOT",
                max_vcpus=spec.max_vcpus,
                subnets=subnets.ids,
                security_group_ids=[sg.id],
            ),
            opts=o("spot"),
        )
        self.queue = aws.batch.JobQueue(
            n("spot-queue"),
            name=P,
            state="ENABLED",
            priority=1,
            compute_environment_orders=[aws.batch.JobQueueComputeEnvironmentOrderArgs(order=1, compute_environment=compute_env.arn)],
            opts=o("spot"),
        )
        self.log_group = aws.cloudwatch.LogGroup(n("log-group"), name=f"/{P}-ingest/batch", retention_in_days=spec.log_days, opts=o("log-group"))

        def container_props(a: dict) -> str:
            return json.dumps({
                "image": a["image"],
                "runtimePlatform": {"operatingSystemFamily": "LINUX", "cpuArchitecture": "ARM64"},
                "resourceRequirements": [{"type": "VCPU", "value": spec.vcpu}, {"type": "MEMORY", "value": spec.memory_mib}],
                "executionRoleArn": a["exec_arn"],
                "jobRoleArn": a["task_arn"],
                "environment": [{"name": "PYTHONFAULTHANDLER", "value": "1"}, *({"name": k, "value": v} for k, v in spec.env.items())],
                "secrets": [{"name": k, "valueFrom": v} for k, v in a["secrets"].items()],
                "networkConfiguration": {"assignPublicIp": "ENABLED"},
                "fargatePlatformConfiguration": {"platformVersion": "LATEST"},
                "ephemeralStorage": {"sizeInGiB": spec.ephemeral_gib},
                "logConfiguration": {
                    "logDriver": "awslogs",
                    "options": {"awslogs-group": a["log_group"], "awslogs-region": region, "awslogs-stream-prefix": P},
                },
            })

        self.job_def = aws.batch.JobDefinition(
            n("ingest"),
            name=f"{P}-ingest",
            type="container",
            platform_capabilities=["FARGATE"],
            # Spot reclaims retry; a genuine failure exits.
            retry_strategy=aws.batch.JobDefinitionRetryStrategyArgs(
                attempts=3,
                evaluate_on_exits=[
                    aws.batch.JobDefinitionRetryStrategyEvaluateOnExitArgs(action="RETRY", on_status_reason="Your Spot Task*"),
                    aws.batch.JobDefinitionRetryStrategyEvaluateOnExitArgs(action="EXIT", on_reason="*"),
                ],
            ),
            timeout=aws.batch.JobDefinitionTimeoutArgs(attempt_duration_seconds=spec.timeout_s),
            container_properties=pulumi.Output.all(
                image=self.image,
                secrets=pulumi.Output.all(**secret_arns),
                log_group=self.log_group.name,
                exec_arn=execution_role.arn,
                task_arn=task_role.arn,
            ).apply(container_props),
            opts=o("ingest"),
        )

        # The capture trigger's identity: it may submit this one job definition
        # to this one queue, nothing else. Its access key is minted outside
        # Pulumi and piped straight into the trigger's secrets.
        self.trigger_user = aws.iam.User(n("capture-trigger"), name=f"{P}-capture-trigger", opts=o("capture-trigger"))
        aws.iam.UserPolicy(
            n("capture-trigger-submit"),
            user=self.trigger_user.name,
            policy=self.queue.arn.apply(lambda queue_arn: json.dumps({
                "Version": "2012-10-17",
                "Statement": [{
                    "Effect": "Allow",
                    "Action": "batch:SubmitJob",
                    "Resource": [
                        queue_arn,
                        f"arn:aws:batch:{region}:{account_id}:job-definition/{P}-ingest",
                        f"arn:aws:batch:{region}:{account_id}:job-definition/{P}-ingest:*",
                    ],
                }],
            })),
            opts=o("capture-trigger-submit"),
        )
        self.register_outputs({
            "queue": self.queue.name,
            "job_definition": self.job_def.name,
            "image": self.image,
            "log_group": self.log_group.name,
            "capture_trigger_user": self.trigger_user.name,
        })
