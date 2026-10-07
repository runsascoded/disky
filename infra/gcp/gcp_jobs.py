"""Shared GCP components for a deployment's scheduled jobs.

The GCP twin of `../cf/cfn_dashboard.py`. A deployment's jobs need a service
account, its secret containers, its crons (Cloud Scheduler → a Batch job or a
Cloud Run job) and its bucket grants. This module carries the shapes and none
of the names: each deployment branch's `gcp/__main__.py` instantiates them.

Two rules hold everywhere:

- **Secret values are never managed.** Only the `Secret` container and its IAM;
  payloads stay out of code and out of state (`gcloud secrets versions add`).
- **Additive IAM only** (`IAMMember`, never `IAMPolicy` / `IAMBinding`), so a
  deployment's grants can't clobber anyone else's on a shared project or bucket.

Adoption: the resources predate this code, so a stack first imports them. Each
component takes `existing=` (the resource is live, import it) and the stack's
`adopting` config gates every import id at once (`Adopt`). After the adopting
`up`, set `adopting: false`; the ids are then inert.
"""

import base64
import json
import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pulumi
import pulumi_gcp as gcp
from pulumi import ComponentResource, ResourceOptions
from pulumi_command import local

SCOPE = "https://www.googleapis.com/auth/cloud-platform"


@dataclass(frozen=True)
class Adopt:
    """Import ids for live resources, only while the stack is adopting."""

    on: bool

    def opts(self, existing: bool, import_id: str, **kw) -> ResourceOptions:
        return ResourceOptions(import_=import_id if self.on and existing else None, **kw)


def sa_member(email: pulumi.Input[str]) -> pulumi.Output[str]:
    return pulumi.Output.from_input(email).apply(lambda e: f"serviceAccount:{e}")


class JobAccount(ComponentResource):
    """A service account, with its project roles.

    `actors` are account emails granted `serviceAccountUser` on it. A job account
    is its own actor (`acts_as_self`): Cloud Scheduler calls Batch with its token,
    and the Batch job that call submits runs as this same account. A dispatcher
    (e.g. the site's sweep console) is another; when this stack creates it, pass
    it in `actor_deps` so the binding waits for the account to exist.
    """

    def __init__(
        self,
        name: str,
        *,
        project: str,
        account_id: str,
        display_name: str,
        roles: list[str],
        actors: list[str] = (),
        actor_deps: list[pulumi.Resource] = (),
        description: str | None = None,
        acts_as_self: bool = True,
        adopt: Adopt,
        existing: bool = False,
        opts: ResourceOptions | None = None,
    ):
        super().__init__("disky:gcp:JobAccount", name, None, opts)
        email = f"{account_id}@{project}.iam.gserviceaccount.com"
        sa_path = f"projects/{project}/serviceAccounts/{email}"
        self.account = gcp.serviceaccount.Account(
            name,
            project=project,
            account_id=account_id,
            display_name=display_name,
            description=description,
            opts=adopt.opts(existing, sa_path, parent=self, protect=True),
        )
        self.email_literal = email
        self.email = self.account.email
        self.member = sa_member(self.email)
        for role in roles:
            gcp.projects.IAMMember(
                f"{name}-{role.split('/')[-1]}",
                project=project,
                role=role,
                member=self.member,
                opts=adopt.opts(existing, f"{project} {role} serviceAccount:{email}", parent=self),
            )
        act = "roles/iam.serviceAccountUser"
        for actor in [email, *actors] if acts_as_self else actors:
            gcp.serviceaccount.IAMMember(
                f"{name}-actor-{actor.split('@')[0]}",
                service_account_id=self.account.name,
                role=act,
                member=f"serviceAccount:{actor}",
                opts=adopt.opts(existing, f"{sa_path} {act} serviceAccount:{actor}", parent=self, depends_on=list(actor_deps)),
            )
        self.register_outputs({"email": self.email})


class Secrets(ComponentResource):
    """Secret containers (no values) and `secretAccessor` for one account each.

    `secrets` maps secret id → labels (or `None`). `existing` covers the
    containers; `grants_existing` the accessor bindings (default: the same), for
    a new account taking over live secrets.
    """

    def __init__(
        self,
        name: str,
        *,
        project: str,
        secrets: dict[str, dict[str, str] | None],
        accessor: pulumi.Input[str],
        accessor_email: str,
        adopt: Adopt,
        existing: bool = False,
        grants_existing: bool | None = None,
        opts: ResourceOptions | None = None,
    ):
        super().__init__("disky:gcp:Secrets", name, None, opts)
        if grants_existing is None:
            grants_existing = existing
        self.secrets: dict[str, gcp.secretmanager.Secret] = {}
        for sid, labels in secrets.items():
            path = f"projects/{project}/secrets/{sid}"
            secret = gcp.secretmanager.Secret(
                sid,
                project=project,
                secret_id=sid,
                labels=labels,
                replication=gcp.secretmanager.SecretReplicationArgs(auto=gcp.secretmanager.SecretReplicationAutoArgs()),
                opts=adopt.opts(existing, path, parent=self, protect=True),
            )
            self.secrets[sid] = secret
            grant_secret(
                f"{sid}-accessor",
                project=project,
                secret_id=sid,
                member=accessor,
                member_email=accessor_email,
                adopt=adopt,
                existing=grants_existing,
                parent=self,
                depends_on=[secret],
            )
        self.register_outputs({})


def grant_secret(
    name: str,
    *,
    project: str,
    secret_id: str,
    member: pulumi.Input[str],
    member_email: str,
    adopt: Adopt,
    existing: bool = False,
    parent: pulumi.Resource | None = None,
    depends_on: list[pulumi.Resource] | None = None,
) -> gcp.secretmanager.SecretIamMember:
    """`secretAccessor` on one secret, which may be another stack's."""
    role = "roles/secretmanager.secretAccessor"
    path = f"projects/{project}/secrets/{secret_id}"
    return gcp.secretmanager.SecretIamMember(
        name,
        project=project,
        secret_id=secret_id,
        role=role,
        member=member,
        opts=adopt.opts(existing, f"{path} {role} serviceAccount:{member_email}", parent=parent, depends_on=depends_on),
    )


def grant_bucket(
    name: str,
    *,
    bucket: str,
    role: str,
    member: pulumi.Input[str],
    member_email: str,
    adopt: Adopt,
    existing: bool = False,
    parent: pulumi.Resource | None = None,
) -> gcp.storage.BucketIAMMember:
    """One additive role on a bucket, which may live in another project."""
    return gcp.storage.BucketIAMMember(
        name,
        bucket=bucket,
        role=role,
        member=member,
        opts=adopt.opts(existing, f"b/{bucket} {role} serviceAccount:{member_email}", parent=parent),
    )


class StorageBatchDelete(ComponentResource):
    """Opt-in prerequisites for generation-pinned Storage Batch Operations.

    Storage Intelligence is a paid fleet subscription (``TRIAL`` rolls into
    ``STANDARD`` after 30 days), so callers must choose the edition
    explicitly. The config is bucket-filtered and managed through ``gcloud``
    because pulumi-gcp has no IntelligenceConfig resource yet.
    """

    def __init__(
        self,
        name: str,
        *,
        control_project: str,
        fleet_project: str,
        buckets: list[str],
        manifest_bucket: str,
        submitter: pulumi.Input[str],
        edition: str,
        opts: ResourceOptions | None = None,
    ):
        super().__init__("disky:gcp:StorageBatchDelete", name, None, opts)
        edition = edition.upper()
        if edition not in {"TRIAL", "STANDARD"}:
            raise ValueError(f"Storage Intelligence edition must be TRIAL or STANDARD, got {edition!r}")
        if not buckets:
            raise ValueError("Storage Batch Operations requires at least one target bucket")

        self.api = gcp.projects.Service(
            f"{name}-api",
            project=control_project,
            service="storagebatchoperations.googleapis.com",
            disable_on_destroy=False,
            deletion_policy="ABANDON",
            opts=ResourceOptions(parent=self),
        )
        self.agent = gcp.projects.ServiceIdentity(
            f"{name}-agent",
            project=control_project,
            service="storagebatchoperations.googleapis.com",
            opts=ResourceOptions(parent=self, depends_on=[self.api]),
        )
        gcp.projects.IAMMember(
            f"{name}-submitter",
            project=control_project,
            role="roles/storagebatchoperations.admin",
            member=submitter,
            opts=ResourceOptions(parent=self, depends_on=[self.api]),
        )
        gcp.storage.BucketIAMMember(
            f"{name}-manifest-reader",
            bucket=manifest_bucket,
            role="roles/storage.objectViewer",
            member=self.agent.member,
            opts=ResourceOptions(parent=self, depends_on=[self.agent]),
        )
        for bucket in buckets:
            gcp.storage.BucketIAMMember(
                f"{name}-{bucket}-object-user",
                bucket=bucket,
                role="roles/storage.objectUser",
                member=self.agent.member,
                opts=ResourceOptions(parent=self, depends_on=[self.agent]),
            )

        patterns = ",".join(f"^{re_escape(bucket)}$" for bucket in buckets)
        base = ["gcloud", "storage", "intelligence-configs"]
        scope = f"--project={fleet_project}"
        filters = f"--include-bucket-id-regexes={patterns}"
        edition_flag = ["--trial-edition"] if edition == "TRIAL" else []

        def command(verb: str, args: list[str]) -> str:
            return " ".join(map(shlex.quote, [*base, verb, scope, filters, *args, "--quiet"]))

        # Protecting this resource makes ending a trial/subscription an
        # explicit two-step operation. Once unprotected and removed, Pulumi
        # disables the project-level config rather than leaving billing behind.
        self.intelligence = local.Command(
            f"{name}-intelligence",
            create=command("enable", edition_flag),
            update=command("update", edition_flag),
            delete=" ".join(map(shlex.quote, [*base, "disable", scope, "--quiet"])),
            triggers=[edition, fleet_project, patterns],
            opts=ResourceOptions(parent=self, protect=True),
        )
        self.register_outputs({"service_agent": self.agent.email, "edition": edition})


def re_escape(value: str) -> str:
    """RE2-safe exact literal for a GCS bucket id."""
    import re

    return re.escape(value)


def submitter_spec(submitter: Path) -> dict:
    """The Batch job spec a `job/*-submit.sh` prints under `PIN=1 DRY=1`.

    `PIN=1` makes the submitter ignore every ambient override, and the process
    runs under a scrubbed env, so the spec is a function of the submitter alone:
    the cron body can't drift from the tracked script.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PIN": "1", "DRY": "1"}
    out = subprocess.run(
        ["bash", str(submitter)],
        cwd=submitter.parent.parent,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return json.loads(out)


def b64_json(spec: dict) -> str:
    return base64.b64encode(json.dumps(spec, indent=2, sort_keys=True).encode()).decode()


# The live crons' server defaults, set explicitly so an import doesn't diff on them.
RETRY = gcp.cloudscheduler.JobRetryConfigArgs(
    retry_count=0,
    max_retry_duration="0s",
    min_backoff_duration="5s",
    max_backoff_duration="3600s",
    max_doublings=5,
)


def _cron(
    name: str,
    *,
    project: str,
    region: str,
    schedule: str,
    uri: str,
    body: str | None,
    sa_email: pulumi.Input[str],
    time_zone: str,
    description: str | None,
    adopt: Adopt,
    existing: bool,
    parent: pulumi.Resource,
    depends_on: list[pulumi.Resource] | None = None,
) -> gcp.cloudscheduler.Job:
    return gcp.cloudscheduler.Job(
        name,
        project=project,
        region=region,
        name=name,
        description=description,
        schedule=schedule,
        time_zone=time_zone,
        attempt_deadline="180s",
        http_target=gcp.cloudscheduler.JobHttpTargetArgs(
            uri=uri,
            http_method="POST",
            headers={"Content-Type": "application/json"} if body is not None else None,
            body=body,
            oauth_token=gcp.cloudscheduler.JobHttpTargetOauthTokenArgs(service_account_email=sa_email, scope=SCOPE),
        ),
        retry_config=RETRY,
        opts=adopt.opts(existing, f"projects/{project}/locations/{region}/jobs/{name}", parent=parent, depends_on=depends_on),
    )


class BatchCron(ComponentResource):
    """A Cloud Scheduler cron that submits a Batch job.

    The body is `submitter_spec(submitter)`, so the job spec has one source of
    truth: the deployment's tracked `job/*-submit.sh`. The cron calls Batch as
    `sa_email`, which the spec's own `allocationPolicy.serviceAccount` should
    match.
    """

    def __init__(
        self,
        name: str,
        *,
        project: str,
        region: str,
        schedule: str,
        submitter: Path,
        sa_email: pulumi.Input[str],
        time_zone: str = "Etc/UTC",
        description: str | None = None,
        adopt: Adopt,
        existing: bool = False,
        depends_on: list[pulumi.Resource] | None = None,
        opts: ResourceOptions | None = None,
    ):
        super().__init__("disky:gcp:BatchCron", name, None, opts)
        self.spec = submitter_spec(submitter)
        self.job = _cron(
            name,
            project=project,
            region=region,
            schedule=schedule,
            uri=f"https://batch.googleapis.com/v1/projects/{project}/locations/{region}/jobs",
            body=b64_json(self.spec),
            sa_email=sa_email,
            time_zone=time_zone,
            description=description,
            adopt=adopt,
            existing=existing,
            parent=self,
            depends_on=depends_on,
        )
        self.register_outputs({"name": self.job.name})


class RunJobCron(ComponentResource):
    """A Cloud Run job and the Cloud Scheduler cron that runs it, both as `sa`.

    Pulumi owns the job's shell (account, limits, the secret env it reads); the
    image and plain env are a deploy's (`gcloud run jobs update`), so they are
    `ignore_changes`, as `wrangler` owns a Pages deploy's contents under `cf/`.
    """

    def __init__(
        self,
        name: str,
        *,
        project: str,
        region: str,
        job: str,
        schedule: str,
        sa_email: str,
        image: str,
        secret_env: dict[str, str],
        cpu: str = "1000m",
        memory: str = "512Mi",
        timeout: str = "600s",
        max_retries: int = 1,
        time_zone: str = "Etc/UTC",
        trigger: str | None = None,
        adopt: Adopt,
        existing: bool = False,
        opts: ResourceOptions | None = None,
    ):
        super().__init__("disky:gcp:RunJobCron", name, None, opts)
        job_path = f"projects/{project}/locations/{region}/jobs/{job}"
        self.job = gcp.cloudrunv2.Job(
            job,
            project=project,
            location=region,
            name=job,
            deletion_protection=True,
            template=gcp.cloudrunv2.JobTemplateArgs(
                template=gcp.cloudrunv2.JobTemplateTemplateArgs(
                    service_account=sa_email,
                    timeout=timeout,
                    max_retries=max_retries,
                    containers=[
                        gcp.cloudrunv2.JobTemplateTemplateContainerArgs(
                            image=image,
                            resources=gcp.cloudrunv2.JobTemplateTemplateContainerResourcesArgs(limits={"cpu": cpu, "memory": memory}),
                            envs=[
                                gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs(
                                    name=var,
                                    value_source=gcp.cloudrunv2.JobTemplateTemplateContainerEnvValueSourceArgs(
                                        secret_key_ref=gcp.cloudrunv2.JobTemplateTemplateContainerEnvValueSourceSecretKeyRefArgs(secret=sid, version="latest"),
                                    ),
                                )
                                for var, sid in secret_env.items()
                            ],
                        )
                    ],
                ),
            ),
            opts=adopt.opts(
                existing,
                job_path,
                parent=self,
                ignore_changes=["template.template.containers[0].image", "template.template.containers[0].envs", "client", "clientVersion", "labels", "annotations", "launchStage"],
            ),
        )
        invoker = "roles/run.invoker"
        gcp.cloudrunv2.JobIamMember(
            f"{job}-invoker",
            project=project,
            location=region,
            name=job,
            role=invoker,
            member=f"serviceAccount:{sa_email}",
            opts=adopt.opts(existing, f"{job_path} {invoker} serviceAccount:{sa_email}", parent=self, depends_on=[self.job]),
        )
        self.cron = _cron(
            trigger or f"{job}-cron",
            project=project,
            region=region,
            schedule=schedule,
            uri=f"https://{region}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/{project}/jobs/{job}:run",
            body=None,
            sa_email=sa_email,
            time_zone=time_zone,
            description=None,
            adopt=adopt,
            existing=existing,
            parent=self,
            depends_on=[self.job],
        )
        self.register_outputs({"job": self.job.name, "cron": self.cron.name})
