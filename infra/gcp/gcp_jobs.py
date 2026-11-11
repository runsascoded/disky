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

Cost labels: `cost_labels()` reads `$DISKY_LABELS` (`k=v,…`, e.g.
`app=disky,deployment=gcs`; spec specs/done/cost-labels.md) into the provider's
`default_labels`, so every resource type that takes labels carries them; a
component passes its own `component` label where it bills on its own.

Adoption: the resources predate this code, so a stack first imports them. Each
component takes `existing=` (the resource is live, import it) and the stack's
`adopting` config gates every import id at once (`Adopt`). After the adopting
`up`, set `adopting: false`; the ids are then inert.
"""

import base64
import hashlib
import json
import os
import re
import shlex
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pulumi
import pulumi_gcp as gcp
from pulumi import ComponentResource, ResourceOptions
from pulumi_command import local

SCOPE = "https://www.googleapis.com/auth/cloud-platform"

LABELS_ENV = "DISKY_LABELS"
_KEY_RE = re.compile(r"[a-z][a-z0-9_-]{0,62}")
_VALUE_RE = re.compile(r"[a-z0-9_-]{0,63}")


def parse_labels(text: str) -> dict[str, str]:
    """`"k=v,k2=v2"` → `{"k": "v", "k2": "v2"}`; raises on anything GCP would refuse.

    The twin of `dt_cloud.cost_labels.parse_labels` (infra is its own uv project)."""
    out: dict[str, str] = {}
    for item in (i.strip() for i in text.split(",")):
        if not item:
            continue
        k, eq, v = (s.strip() for s in item.partition("="))
        if not eq or not _KEY_RE.fullmatch(k) or not _VALUE_RE.fullmatch(v) or k in out:
            raise ValueError(f"{LABELS_ENV}: bad or duplicate label {item!r}")
        out[k] = v
    return out


def cost_labels(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The deployment's cost labels, from `$DISKY_LABELS`.

    Required to be *set* (empty = deliberately none): run from a shell without it,
    an `up` would strip the labels off every live resource."""
    env = os.environ if environ is None else environ
    if LABELS_ENV not in env:
        raise ValueError(f"${LABELS_ENV} is unset: set it (e.g. app=disky,deployment=<stack>), or to '' to manage no labels")
    return parse_labels(env[LABELS_ENV])


def check_cron_labels(spec: dict, labels: dict[str, str], name: str) -> None:
    """Refuse a cron's Batch job `spec` unless its `labels` and its `allocationPolicy.labels` both carry the stack's cost
    `labels` (as valued) and a `component`.

    Cloud Scheduler posts the body straight to Batch, past `dt-cloud`'s own check (`batch.submit_job`), and Batch puts only
    `allocationPolicy.labels` on the VMs and disks a job creates: a body labelled on the job alone runs, and bills
    unlabelled. A stack with no labels (`$DISKY_LABELS` empty or unset) checks nothing."""
    if not labels:
        return
    for where, got in (("labels", spec.get("labels") or {}), ("allocationPolicy.labels", (spec.get("allocationPolicy") or {}).get("labels") or {})):
        if missing := [k for k, v in labels.items() if got.get(k) != v] + ([] if got.get("component") else ["component"]):
            raise ValueError(f"cron {name}: its Batch job's {where} lack {', '.join(missing)} (the stack's ${LABELS_ENV} + a component go on "
                             "the job's `labels` and on `allocationPolicy.labels`: its VMs and disks bill under the latter)")


def labeled_provider(name: str, *, project: str, region: str, labels: dict[str, str]) -> gcp.Provider:
    """The stack's GCP provider with `labels` as its `default_labels`, made the provider of every
    `gcp:` resource in the program (a stack transform), so no component needs threading."""
    provider = gcp.Provider(name, project=project, region=region, default_labels=labels or None)

    def transform(args: pulumi.ResourceTransformArgs) -> pulumi.ResourceTransformResult | None:
        if not args.type_.startswith("gcp:") or args.custom is False or args.opts.provider is not None:
            return None
        return pulumi.ResourceTransformResult(args.props, ResourceOptions.merge(args.opts, ResourceOptions(provider=provider)))

    pulumi.runtime.register_resource_transform(transform)
    return provider


@dataclass(frozen=True)
class Adopt:
    """Import ids for live resources, only while the stack is adopting."""

    on: bool

    def opts(self, existing: bool, import_id: pulumi.Input[str], **kw) -> ResourceOptions:
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


#: An image repo's cleanup (`DeploymentImages`): the deployment's tag kept, the newest builds kept, the rest deleted past
#: this age (probe and test tags `<purpose>-<sha>` included).
IMAGE_KEEP_TAGS = ("latest",)
IMAGE_KEEP_RECENT = 10
IMAGE_DELETE_AFTER_S = 14 * 86400


class DeploymentImages(ComponentResource):
    """A deployment's image repository, `<deployment>-usage` (Artifact Registry, Docker): one image (`scan`), the
    deployment's tag `:latest`, probe / test tags beside it, cleaned up (`IMAGE_KEEP_TAGS` and the newest
    `IMAGE_KEEP_RECENT` builds kept, the rest deleted after `IMAGE_DELETE_AFTER_S`). `readers` (the job account's
    member) pull, `writers` (a build account's) push. `repository_id` defaults to `<deployment>-usage`; a live repo is
    adopted under its own id (`existing`; a repo can't be renamed: a new name is a new repo, the old one deleted once
    nothing points at it). `logical`: the repo's resource name, so a stack moving it into this component keeps it
    (pass the name it had, and `aliases` its old parent, e.g. `[Alias(parent=ROOT_STACK_RESOURCE)]`)."""

    def __init__(
        self,
        deployment: str,
        *,
        project: str,
        region: str,
        readers: list[pulumi.Input[str]] = (),
        writers: list[pulumi.Input[str]] = (),
        repository_id: str | None = None,
        logical: str | None = None,
        description: str | None = None,
        adopt: Adopt,
        existing: bool = False,
        aliases: Sequence[pulumi.Alias] = (),
        opts: ResourceOptions | None = None,
    ):
        name = logical or f"{deployment}-usage-images"
        super().__init__("disky:gcp:DeploymentImages", name, None, opts)
        repo_id = repository_id or f"{deployment}-usage"
        A = gcp.artifactregistry
        self.repository = A.Repository(
            name,
            project=project,
            location=region,
            repository_id=repo_id,
            format="DOCKER",
            description=description or f"{deployment} deployment images (`scan`; `:latest` is the deployment's)",
            labels={"component": "images", "deployment": deployment},
            cleanup_policy_dry_run=False,
            cleanup_policies=[
                A.RepositoryCleanupPolicyArgs(id="keep-deployment-tags", action="KEEP",
                                              condition=A.RepositoryCleanupPolicyConditionArgs(tag_state="TAGGED", tag_prefixes=list(IMAGE_KEEP_TAGS))),
                A.RepositoryCleanupPolicyArgs(id="keep-recent", action="KEEP",
                                              most_recent_versions=A.RepositoryCleanupPolicyMostRecentVersionsArgs(keep_count=IMAGE_KEEP_RECENT)),
                A.RepositoryCleanupPolicyArgs(id="delete-old", action="DELETE",
                                              condition=A.RepositoryCleanupPolicyConditionArgs(tag_state="ANY", older_than=f"{IMAGE_DELETE_AFTER_S}s")),
            ],
            opts=adopt.opts(existing, f"projects/{project}/locations/{region}/repositories/{repo_id}", parent=self, protect=True, aliases=list(aliases)),
        )
        for role, members in (("reader", readers), ("writer", writers)):
            for i, member in enumerate(members):
                A.RepositoryIamMember(
                    f"{name}-{role}-{i}",
                    project=project,
                    location=self.repository.location,
                    repository=self.repository.repository_id,
                    role=f"roles/artifactregistry.{role}",
                    member=member,
                    opts=ResourceOptions(parent=self),
                )
        self.image = pulumi.Output.concat(region, "-docker.pkg.dev/", project, "/", self.repository.repository_id, "/scan")
        self.register_outputs({"image": self.image})


class Secrets(ComponentResource):
    """Secret containers (no values) and `secretAccessor` for one account each.

    `secrets` maps secret id → labels (or `None`). `existing` covers the
    containers; `grants_existing` the accessor bindings (default: the same), for
    a new account taking over live secrets. `grant_only` selects accessor grants
    without removing any secret containers; None preserves the all-secrets default.
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
        grant_only: set[str] | None = None,
        opts: ResourceOptions | None = None,
    ):
        if grant_only is not None and not grant_only.issubset(secrets):
            raise ValueError("grant_only must be a subset of the managed secret ids")
        super().__init__("disky:gcp:Secrets", name, None, opts)
        if grants_existing is None:
            grants_existing = existing
        self.secrets: dict[str, gcp.secretmanager.Secret] = {}
        self.grants: dict[str, gcp.secretmanager.SecretIamMember] = {}
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
            if grant_only is None or sid in grant_only:
                self.grants[sid] = grant_secret(
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
    bucket: pulumi.Input[str],
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
        opts=adopt.opts(existing, pulumi.Output.concat("b/", bucket, f" {role} serviceAccount:{member_email}"), parent=parent),
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
    the cron body can't drift from the tracked script. The one exception is
    `$DISKY_LABELS`, the stack's own cost labels, so the cron's jobs carry the
    same labels as the stack's resources.
    """
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PIN": "1", "DRY": "1"}
    if LABELS_ENV in os.environ:
        env[LABELS_ENV] = os.environ[LABELS_ENV]
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
# A cron whose target is safe to repeat (`BatchSubmitCron`: a second run of the submitter meets the period's job and
# stops): a failed or timed-out call is retried, 30 s doubling to 5 min, within 30 min of the scheduled time.
IDEMPOTENT_RETRY = gcp.cloudscheduler.JobRetryConfigArgs(
    retry_count=5,
    max_retry_duration="1800s",
    min_backoff_duration="30s",
    max_backoff_duration="300s",
    max_doublings=4,
)


def checked_spec(submitter: Path, name: str) -> dict:
    """`submitter_spec(submitter)`, refused (`check_cron_labels`) unless its job and allocation policy carry the stack's
    cost labels: a cron's job reaches Batch with what the stack rendered, so the check is here, before any resource."""
    spec = submitter_spec(submitter)
    check_cron_labels(spec, parse_labels(os.environ.get(LABELS_ENV, "")), name)
    return spec


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
    retry: gcp.cloudscheduler.JobRetryConfigArgs = RETRY,
    aliases: Sequence[pulumi.Input[str] | pulumi.Alias] = (),
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
        retry_config=retry,
        opts=adopt.opts(existing, f"projects/{project}/locations/{region}/jobs/{name}", parent=parent, depends_on=depends_on, aliases=list(aliases)),
    )


class BatchCron(ComponentResource):
    """A Cloud Scheduler cron that submits a Batch job.

    The body is `submitter_spec(submitter)`, so the job spec has one source of
    truth: the deployment's tracked `job/*-submit.sh`. The cron calls Batch as
    `sa_email`, which the spec's own `allocationPolicy.serviceAccount` should
    match. A body whose job or allocation policy lacks the stack's cost labels
    is refused (`check_cron_labels`), before any resource is declared.
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
        spec = checked_spec(submitter, name)
        super().__init__("disky:gcp:BatchCron", name, None, opts)
        self.spec = spec
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

    Pulumi owns the job's shell (account, limits, the secret env it reads, its
    `labels` on top of the provider's defaults, its `command`/`args` when given);
    the image and plain env are a deploy's (`gcloud run jobs update`), so they
    are `ignore_changes`, as `wrangler` owns a Pages deploy's contents under
    `cf/`. Given `env`, the plain env is the stack's instead (no longer ignored).
    `retry`: the cron's (default `RETRY`: none); `cron_aliases`: the cron's
    former URNs, for a cron moving here from another component.
    """

    TYPE = "disky:gcp:RunJobCron"

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
        labels: dict[str, str] | None = None,
        command: list[str] | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        retry: gcp.cloudscheduler.JobRetryConfigArgs = RETRY,
        description: str | None = None,
        cron_aliases: Sequence[pulumi.Input[str] | pulumi.Alias] = (),
        adopt: Adopt,
        existing: bool = False,
        opts: ResourceOptions | None = None,
    ):
        super().__init__(self.TYPE, name, None, opts)
        job_path = f"projects/{project}/locations/{region}/jobs/{job}"
        self.job = gcp.cloudrunv2.Job(
            job,
            project=project,
            location=region,
            name=job,
            deletion_protection=True,
            labels=labels,
            template=gcp.cloudrunv2.JobTemplateArgs(
                template=gcp.cloudrunv2.JobTemplateTemplateArgs(
                    service_account=sa_email,
                    timeout=timeout,
                    max_retries=max_retries,
                    containers=[
                        gcp.cloudrunv2.JobTemplateTemplateContainerArgs(
                            image=image,
                            commands=command,
                            args=args,
                            resources=gcp.cloudrunv2.JobTemplateTemplateContainerResourcesArgs(limits={"cpu": cpu, "memory": memory}),
                            envs=[
                                gcp.cloudrunv2.JobTemplateTemplateContainerEnvArgs(name=var, value=value)
                                for var, value in (env or {}).items()
                            ] + [
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
                ignore_changes=["template.template.containers[0].image", *([] if env is not None else ["template.template.containers[0].envs"]),
                                "client", "clientVersion", "annotations", "launchStage"],
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
            description=description,
            adopt=adopt,
            existing=existing,
            parent=self,
            depends_on=[self.job],
            retry=retry,
            aliases=cron_aliases,
        )
        self.register_outputs({"job": self.job.name, "cron": self.cron.name})


#: Where `BatchSubmitCron` hands its script the Batch job's spec (JSON).
SPEC_ENV = "BATCH_SPEC"
#: The role a `BatchSubmitCron` dispatcher holds on its Cloud Run job: run it, with overrides (`run.jobs.run`,
#: `run.jobs.runWithOverrides`, `run.executions.cancel`), nothing else; the narrowest predefined role that can override.
RUN_WITH_OVERRIDES = "roles/run.jobsExecutorWithOverrides"


def env_guard(env: dict[str, str], overridable: Sequence[str]) -> str:
    """Bash that refuses to go on unless each of `env`'s variables (the plain env the stack sets on the job) not named in
    `overridable` still holds the stack's value, compared by SHA-256 (`$BATCH_SPEC` is kilobytes): a run with
    overrides (Cloud Run's `containerOverrides[].env`, which no IAM condition can narrow to some variables) may change
    those, and only those. Exits 2 naming the variable. Secret-sourced variables aren't pinned (their values aren't the
    stack's to know)."""
    allowed = ", ".join(overridable) or "none"
    lines = [
        f"# The stack's env, pinned (`BatchSubmitCron`): a run may override only {allowed}.",
        '_pin() { [ "${!1+set}" = set ] && [ "$(printf %s "${!1}" | sha256sum | cut -d" " -f1)" = "$2" ] || '
        f"{{ echo \"refused: \\$$1 is not the stack's value (a run may override only {allowed})\" >&2; exit 2; }}; }}",
        *(f"_pin {var} {hashlib.sha256(value.encode()).hexdigest()}" for var, value in sorted(env.items()) if var not in overridable),
        "unset -f _pin",
    ]
    return "\n".join(lines) + "\n"


class BatchSubmitCron(RunJobCron):
    """A cron that submits a Batch job through a Cloud Run job, which retries and alerts, instead of Cloud Scheduler
    posting the body to Batch once (`BatchCron`), where a 5xx is a silently missed run and a retried POST a possible
    second job.

    The spec is `checked_spec(submitter)` as `BatchCron`'s (rendered and label-checked at `up`), handed to the job as
    `$BATCH_SPEC` beside `env`; the job runs `script` (a deployment's tracked shell script, its text passed as `bash -c`,
    so the job runs what the stack previewed, whatever the image's checkout). The script owns the submit: an
    idempotent one (`dt-cloud batch submit -t -e -r`: the job named for its period, a 409 counted as submitted, transient
    failures retried for a bounded window) and an alert if that still fails. Because a repeat is harmless, the cron
    retries its own call (`IDEMPOTENT_RETRY`), and the Cloud Run task is not retried (`max_retries=0`: the script's own
    window is the retry, and a second attempt would alert twice). `timeout` must outlast the script's window.

    A run by hand (e.g. a site's "run a scan now"): `dispatchers` (`{name: member}`) each get `RUN_WITH_OVERRIDES` on
    this one job, and `overridable` names the env variables such a run may set (`containerOverrides[].env`), which the
    script reads to vary its run (its job id, its trigger). With either, the script is prefixed by `env_guard`: every
    other plain variable the stack sets, the spec included, must still be the stack's, so an override can vary the run
    only as the script allows, never swap the spec. Not a boundary against the dispatcher itself (it could set a
    variable the stack doesn't, e.g. `BASH_ENV`): grant it only to an account already trusted to submit the job's Batch
    work as its account.
    """

    TYPE = "disky:gcp:BatchSubmitCron"

    def __init__(
        self,
        name: str,
        *,
        submitter: Path,
        script: Path,
        env: dict[str, str] | None = None,
        overridable: Sequence[str] = (),
        dispatchers: dict[str, pulumi.Input[str]] | None = None,
        memory: str = "1Gi",
        timeout: str = "2400s",
        retry: gcp.cloudscheduler.JobRetryConfigArgs = IDEMPOTENT_RETRY,
        **kw,
    ):
        self.spec = checked_spec(submitter, name)
        labels_env = {LABELS_ENV: os.environ[LABELS_ENV]} if LABELS_ENV in os.environ else {}
        job_env = {SPEC_ENV: json.dumps(self.spec, sort_keys=True), **labels_env, **(env or {})}
        guard = env_guard(job_env, overridable) if overridable or dispatchers else ""
        super().__init__(
            name,
            command=["bash", "-c", guard + script.read_text()],
            env=job_env,
            memory=memory,
            timeout=timeout,
            max_retries=0,
            retry=retry,
            **kw,
        )
        job = kw["job"]
        for who, member in (dispatchers or {}).items():
            gcp.cloudrunv2.JobIamMember(
                f"{job}-dispatch-{who}",
                project=kw["project"],
                location=kw["region"],
                name=job,
                role=RUN_WITH_OVERRIDES,
                member=member,
                opts=ResourceOptions(parent=self, depends_on=[self.job]),
            )


class CronAlerts(ComponentResource):
    """The backstop for a `BatchSubmitCron`'s failures its own script can't report, as Cloud Monitoring alert policies
    to `channels`, event-driven, no time windows. A channel is a notification channel's id
    (`projects/<project>/notificationChannels/<n>`): a `MonitoringRelay`'s `.channel` (the deployment's own bot, all in
    the stack), or one made by hand in the console (e.g. Monitoring's Slack app, whose OAuth the stack can't do).

    - `<job> failed`: a Cloud Run execution of `job` ended failed (`run.googleapis.com/job/completed_execution_count`,
      `result="failed"`): the container never started, ran out of memory, hit its timeout, or the script exited
      nonzero (which also posts its own, fuller alert).
    - `<trigger> couldn't start the job`: Cloud Scheduler logged a failed attempt to run it (its call to the job's
      `:run`; it retries per its retry config, so each failed attempt is one log entry), at most one notification an hour.

    Not covered: a trigger paused or deleted produces no event at all."""

    TYPE = "disky:gcp:CronAlerts"

    def __init__(self, name: str, *, project: str, job: str, trigger: str, channels: Sequence[pulumi.Input[str]], opts: ResourceOptions | None = None):
        if not channels:
            raise ValueError(f"CronAlerts {name}: no notification channels (policies nobody is told of)")
        super().__init__(self.TYPE, name, None, opts)
        M = gcp.monitoring
        child = ResourceOptions(parent=self)
        self.failed = M.AlertPolicy(
            f"{name}-failed",
            project=project,
            display_name=f"{job} failed",
            combiner="OR",
            conditions=[M.AlertPolicyConditionArgs(
                display_name="a failed execution",
                condition_threshold=M.AlertPolicyConditionConditionThresholdArgs(
                    filter=(f'resource.type = "cloud_run_job" AND resource.labels.job_name = "{job}" AND '
                            'metric.type = "run.googleapis.com/job/completed_execution_count" AND metric.labels.result = "failed"'),
                    comparison="COMPARISON_GT",
                    threshold_value=0,
                    duration="0s",
                    aggregations=[M.AlertPolicyConditionConditionThresholdAggregationArgs(alignment_period="300s", per_series_aligner="ALIGN_DELTA")],
                    trigger=M.AlertPolicyConditionConditionThresholdTriggerArgs(count=1),
                ),
            )],
            documentation=M.AlertPolicyDocumentationArgs(
                mime_type="text/markdown",
                content=(f"A run of the Cloud Run job `{job}` failed, so today's Batch job may not exist. Its logs say why; "
                         f"re-run it once fixed (a no-op if the job exists): `gcloud run jobs execute {job} --region <region> --project {project}`"),
            ),
            notification_channels=list(channels),
            opts=child,
        )
        self.unstarted = M.AlertPolicy(
            f"{name}-unstarted",
            project=project,
            display_name=f"{trigger} couldn't start {job}",
            combiner="OR",
            conditions=[M.AlertPolicyConditionArgs(
                display_name="a failed Scheduler attempt",
                condition_matched_log=M.AlertPolicyConditionConditionMatchedLogArgs(
                    filter=f'resource.type = "cloud_scheduler_job" AND resource.labels.job_id = "{trigger}" AND severity >= ERROR',
                ),
            )],
            alert_strategy=M.AlertPolicyAlertStrategyArgs(
                notification_rate_limit=M.AlertPolicyAlertStrategyNotificationRateLimitArgs(period="3600s"),
                auto_close="1800s",
            ),
            documentation=M.AlertPolicyDocumentationArgs(
                mime_type="text/markdown",
                content=f"Cloud Scheduler `{trigger}` failed to run the Cloud Run job `{job}` (after its retries, one entry each attempt).",
            ),
            notification_channels=list(channels),
            opts=child,
        )
        self.register_outputs({})


#: Who publishes a Pub/Sub notification channel's messages: the project's Monitoring notification service agent, made
#: when the project's first Pub/Sub channel is (so its grant on the topic follows the channel).
MONITORING_AGENT = "service-{number}@gcp-sa-monitoring-notification.iam.gserviceaccount.com"


class MonitoringRelay(ComponentResource):
    """Cloud Monitoring alerts to the deployment's own alert channel (its Slack bot, through `disk-tree notify`), with
    no console step: a Pub/Sub notification channel on a topic, and a push subscription delivering each incident to a
    Cloud Run service that runs `dt-cloud monitoring-relay serve` from the deployment's `image` (spec
    specs/monitoring-relay.md). `.channel` is the notification channel, for `CronAlerts` or any alert policy.

    - the topic, published to by the Monitoring notification agent (`MONITORING_AGENT`, granted once the channel exists);
    - the service, as its own account (`account_id`), which reads `secret_env`'s secrets (e.g. the bot token) and
      nothing else; `env` beside them (e.g. `SLACK_ALERT_CHANNEL`); scaled to zero between alerts, one instance at most;
    - the push, authenticated by an OIDC token as that same account (`run.invoker` on the service, which Cloud Run
      checks: the service admits no one else), minted by the Pub/Sub service agent (`serviceAccountTokenCreator` on
      the account). Undelivered (no channel took it: 500), a message is retried, 10 s backing off to 10 min, for a day.

    The image is the stack's as given: Cloud Run pins the digest `:latest` meant when the revision was made, so a
    rebuilt image reaches the relay by a new revision (`gcloud run deploy <name> --image <image>`), not by `up` alone."""

    TYPE = "disky:gcp:MonitoringRelay"

    def __init__(
        self,
        name: str,
        *,
        project: str,
        region: str,
        image: pulumi.Input[str],
        account_id: str,
        secret_env: dict[str, str],
        env: dict[str, str] | None = None,
        channels: str = "slack",
        memory: str = "1Gi",
        labels: dict[str, str] | None = None,
        display_name: str | None = None,
        opts: ResourceOptions | None = None,
    ):
        super().__init__(self.TYPE, name, None, opts)
        child = ResourceOptions(parent=self)
        labels = {"component": "alert-relay", **(labels or {})}
        api = gcp.projects.Service(
            f"{name}-pubsub-api",
            project=project,
            service="pubsub.googleapis.com",
            disable_on_destroy=False,
            deletion_policy="ABANDON",
            opts=child,
        )
        pubsub_agent = gcp.projects.ServiceIdentity(
            f"{name}-pubsub-agent",
            project=project,
            service="pubsub.googleapis.com",
            opts=ResourceOptions(parent=self, depends_on=[api]),
        )
        self.topic = gcp.pubsub.Topic(name, project=project, name=name, labels=labels, opts=ResourceOptions(parent=self, depends_on=[api]))
        self.account = gcp.serviceaccount.Account(
            f"{name}-sa",
            project=project,
            account_id=account_id,
            display_name=f"{name}: Monitoring incidents to the deployment's alert channel",
            opts=child,
        )
        member = sa_member(self.account.email)
        grants = [
            gcp.secretmanager.SecretIamMember(
                f"{name}-{sid}-accessor",
                project=project,
                secret_id=sid,
                role="roles/secretmanager.secretAccessor",
                member=member,
                opts=child,
            )
            for sid in sorted(set(secret_env.values()))
        ]
        self.service = gcp.cloudrunv2.Service(
            name,
            project=project,
            location=region,
            name=name,
            deletion_protection=False,
            ingress="INGRESS_TRAFFIC_ALL",
            labels=labels,
            template=gcp.cloudrunv2.ServiceTemplateArgs(
                service_account=self.account.email,
                timeout="60s",
                scaling=gcp.cloudrunv2.ServiceTemplateScalingArgs(min_instance_count=0, max_instance_count=1),
                containers=[gcp.cloudrunv2.ServiceTemplateContainerArgs(
                    image=image,
                    commands=["dt-cloud", "monitoring-relay", "serve", "-c", channels],
                    resources=gcp.cloudrunv2.ServiceTemplateContainerResourcesArgs(
                        limits={"cpu": "1000m", "memory": memory}, cpu_idle=True, startup_cpu_boost=True,
                    ),
                    envs=[
                        gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(name=var, value=value)
                        for var, value in (env or {}).items()
                    ] + [
                        gcp.cloudrunv2.ServiceTemplateContainerEnvArgs(
                            name=var,
                            value_source=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceArgs(
                                secret_key_ref=gcp.cloudrunv2.ServiceTemplateContainerEnvValueSourceSecretKeyRefArgs(secret=sid, version="latest"),
                            ),
                        )
                        for var, sid in secret_env.items()
                    ],
                )],
            ),
            opts=ResourceOptions(parent=self, depends_on=grants, ignore_changes=["client", "clientVersion"]),
        )
        invoker = gcp.cloudrunv2.ServiceIamMember(
            f"{name}-invoker",
            project=project,
            location=region,
            name=self.service.name,
            role="roles/run.invoker",
            member=member,
            opts=child,
        )
        token = gcp.serviceaccount.IAMMember(
            f"{name}-pubsub-token",
            service_account_id=self.account.name,
            role="roles/iam.serviceAccountTokenCreator",
            member=pubsub_agent.member,
            opts=child,
        )
        self.subscription = gcp.pubsub.Subscription(
            name,
            project=project,
            name=name,
            topic=self.topic.id,
            labels=labels,
            ack_deadline_seconds=60,
            message_retention_duration="86400s",
            retry_policy=gcp.pubsub.SubscriptionRetryPolicyArgs(minimum_backoff="10s", maximum_backoff="600s"),
            expiration_policy=gcp.pubsub.SubscriptionExpirationPolicyArgs(ttl=""),
            push_config=gcp.pubsub.SubscriptionPushConfigArgs(
                push_endpoint=self.service.uri,
                oidc_token=gcp.pubsub.SubscriptionPushConfigOidcTokenArgs(service_account_email=self.account.email, audience=self.service.uri),
            ),
            opts=ResourceOptions(parent=self, depends_on=[invoker, token]),
        )
        self.notification_channel = gcp.monitoring.NotificationChannel(
            name,
            project=project,
            type="pubsub",
            display_name=display_name or f"{name} (Pub/Sub, relayed to {channels})",
            labels={"topic": self.topic.id},
            opts=ResourceOptions(parent=self, depends_on=[self.subscription]),
        )
        # Monitoring's notification agent (`MONITORING_AGENT`) isn't made by the project's first Pub/Sub channel (gcs,
        # 2026-11-11: the grant below failed, "does not exist"): created explicitly, as the Pub/Sub agent is.
        monitoring_agent = gcp.projects.ServiceIdentity(
            f"{name}-monitoring-agent",
            project=project,
            service="monitoring.googleapis.com",
            opts=child,
        )
        number = gcp.organizations.get_project_output(project_id=project).number
        gcp.pubsub.TopicIAMMember(
            f"{name}-monitoring-publisher",
            project=project,
            topic=self.topic.name,
            role="roles/pubsub.publisher",
            member=number.apply(lambda n: "serviceAccount:" + MONITORING_AGENT.format(number=n)),
            opts=ResourceOptions(parent=self, depends_on=[self.notification_channel, monitoring_agent]),
        )
        self.channel: pulumi.Output[str] = self.notification_channel.name
        self.register_outputs({"channel": self.channel, "topic": self.topic.id, "service": self.service.uri})

