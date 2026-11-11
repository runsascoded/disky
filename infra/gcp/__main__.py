"""gcs's GCP stack: the scan job's account, secrets, crons and bucket grants.

Thin wiring over `gcp_jobs.py` (the shared components, from `cloud`). Each cron's
Batch spec comes from this branch's own `job/*-submit.sh` under `PIN=1 DRY=1`.

CI and agents run `pulumi preview` only; `up` is a human's call.
"""

from pathlib import Path

import pulumi
import pulumi_gcp as gcp

from gcp_jobs import Adopt, BatchCron, BatchSubmitCron, DeploymentImages, JobAccount, RunJobCron, Secrets, StorageBatchDelete, cost_labels, grant_bucket, labeled_provider
from task_logs import TaskLogBucket, TaskLogView

cfg = pulumi.Config()
gcp_cfg = pulumi.Config("gcp")
project = gcp_cfg.require("project")
region = gcp_cfg.require("region")
adopt = Adopt(cfg.get_bool("adopting") or False)
job_dir = Path(__file__).resolve().parents[2] / "job"

STACK = "gcs"
if pulumi.get_stack() != STACK:
    raise ValueError(f"this branch's gcp/ manages only the {STACK!r} stack, not {pulumi.get_stack()!r}")

# Cost attribution (specs/cost-labels.md): `$DISKY_LABELS` (app=disky,deployment=gcs, from the
# shell's `.envrc`) becomes every labelable resource's default labels.
labeled_provider("gcs-labeled", project=project, region=region, labels=cost_labels())

sa = lambda account_id: f"{account_id}@{project}.iam.gserviceaccount.com"  # noqa: E731

# The site's sweep console submits sweep-executor Batch jobs as this account
# (its key is the Pages secret `GCP_SA_KEY`; keys stay out of state).
dispatch = JobAccount(
    "gcs-usage-dispatch",
    project=project,
    account_id="gcs-usage-dispatch",
    display_name="gcs-usage sweep dispatch",
    description="Submits sweep-executor Batch jobs from the /sweep console; Batch-submit + actAs gcs-usage-job only",
    roles=["roles/batch.jobsEditor"],
    acts_as_self=False,
    adopt=adopt,
    existing=True,
)
task_logs = TaskLogView(
    "gcs-sweep-task-logs",
    project=project,
    location=cfg.get("taskLogsLocation") or "global",
    bucket=cfg.get("taskLogsBucket") or "_Default",
    view_id="gcs-sweep-task-logs",
    job_uid_prefix="gcs-sweep-",
    reader=dispatch.member,
)
pulumi.export("task_log_view", task_logs.path)
# Every Batch task's logs in the project (`log_id("batch_task_logs")`), kept 90 days beside `_Default`'s 30: Nextflow
# task jobs are named `nf-…`, so no deployment prefix tells them apart, and the project's one copy lives in this stack
task_log_bucket = TaskLogBucket("batch-task-logs", project=project, location="global", bucket_id="batch-task-logs", retention_days=90)
pulumi.export("task_log_bucket", task_log_bucket.path)
job = JobAccount(
    "gcs-usage-job",
    project=project,
    account_id="gcs-usage-job",
    display_name="Daily GCS-usage snapshot job",
    roles=[
        "roles/artifactregistry.reader",
        "roles/batch.agentReporter",
        "roles/batch.jobsEditor",
        "roles/logging.logWriter",
        # the `gcp-cost` step: buckets' bytes (Monitoring) and their soft-delete / lifecycle policies
        "roles/monitoring.viewer",
        "roles/storage.bucketViewer",
    ],
    actors=[sa("gcs-usage-dispatch")],
    adopt=adopt,
    existing=True,
)
# The site's `/v1/files` scan browser reads the buckets as this account (an
# HMAC key, also out of state).
browse = JobAccount(
    "gcs-usage-browse",
    project=project,
    account_id="gcs-usage-browse",
    display_name="gcs-usage scan browser (read-only proxy)",
    roles=[],
    acts_as_self=False,
    adopt=adopt,
    existing=True,
)

secrets = Secrets(
    "gcs-secrets",
    project=project,
    secrets={
        "cf-pages-token": None,
        "gcs-alert-slack-bot-token": None,
        "gcs-alert-slack-webhook": None,
        "gcs-sheet-sync-token": {"app": "gcs-usage", "role": "sheet-sync"},
        "gcs-usage-discord-webhook": None,
        "marin-discord-bot-token": None,
    },
    accessor=job.member,
    accessor_email=job.email_literal,
    adopt=adopt,
    existing=True,
)

# The static name index's R2 key (bucket `oa-gcs-usage-index` only, Object Read & Write): the daily
# static-names chain's `r2-copy` reads it on Batch (`secretVariables`, `job/static-daily.sh`).
static_secrets = Secrets(
    "gcs-static-secrets",
    project=project,
    secrets={
        "gcs-static-index-r2-key-id": {"app": "gcs-usage", "role": "static-index-r2"},
        "gcs-static-index-r2-secret": {"app": "gcs-usage", "role": "static-index-r2"},
    },
    accessor=job.member,
    accessor_email=job.email_literal,
    adopt=adopt,
)

# Cloudflare Account Analytics: Read (OA account), for `dt-cloud cf-usage` (specs/usage-metrics.md): the daily usage
# step's billed units and Analytics Engine attribution.
analytics_secret = Secrets(
    "gcs-cf-analytics-secret",
    project=project,
    secrets={"gcs-cf-analytics-token": {"app": "gcs-usage", "role": "cf-analytics-read"}},
    accessor=job.member,
    accessor_email=job.email_literal,
    adopt=adopt,
)

snapshot_secret = Secrets(
    "gcs-snapshot-secret",
    project=project,
    secrets={"gcs-snapshot-site-token": {"app": "gcs-usage", "role": "snapshot-read"}},
    accessor=job.member,
    accessor_email=job.email_literal,
    adopt=adopt,
)

# The daily scan: Cloud Scheduler → the Cloud Run job `gcs-usage-snapshot-submit` → `job/scan-submit.sh`, which submits
# `job/batch-submit.sh`'s PIN spec as `gcs-usage-snapshot-YYYYMMDD` (a 409 for that name = already submitted), retries
# transient failures for 30 min, and posts to #gcs-admin if it still can't. Scheduler posting the spec to Batch itself
# (`BatchCron`) can do neither: one POST, no job id, so a 502 is a silent missed scan (2026-11-08) and a retry a possible
# second one. The trigger keeps its name, adopted from its `BatchCron` (`cron_aliases`) and updated in place.
DAILY = "gcs-usage-snapshot-daily"
daily = BatchSubmitCron(
    DAILY,
    project=project,
    region=region,
    job="gcs-usage-snapshot-submit",
    trigger=DAILY,
    schedule="0 7 * * *",
    submitter=job_dir / "batch-submit.sh",
    script=job_dir / "scan-submit.sh",
    sa_email=job.email_literal,
    image=f"{region}-docker.pkg.dev/{project}/cloud-run-source-deploy/gcs-usage-snapshot:latest",
    env={"SLACK_ALERT_CHANNEL": "C0C579HNRFB"},  # #gcs-admin
    secret_env={"SLACK_BOT_TOKEN": "gcs-alert-slack-bot-token"},
    labels={"component": "scan-submit"},
    description="The daily scan: runs the submitter job, which names the day's Batch job, retries, and alerts",
    cron_aliases=[f"urn:pulumi:{STACK}::{pulumi.get_project()}::disky:gcp:BatchCron$gcp:cloudscheduler/job:Job::{DAILY}"],
    adopt=adopt,
    opts=pulumi.ResourceOptions(depends_on=[secrets, static_secrets, snapshot_secret]),
)

# The access-log poller (specs/access-log-freshness.md §A): GCS delivers each hour's usage CSVs ~9–16 h late, so
# ingest them hourly as they land instead of in the daily scan; the scan's `index` reads `access/agg` as it stands.
# It runs in the data bucket's region (us-east1), whose writes (~35 GiB a day of `access/agg`) would otherwise cross
# regions, and pulls the job image from that region's copy (`east_images`), which `job/build.sh` pushes beside `:latest`.
east_images = DeploymentImages("gcs", project=project, region="us-east1", adopt=adopt)
pulumi.export("image_us_east1", east_images.image)
access_ingest = BatchCron(
    "gcs-access-ingest-hourly",
    project=project,
    region="us-east1",
    schedule="20 * * * *",
    submitter=job_dir / "access-submit.sh",
    sa_email=job.email,
    adopt=adopt,
    depends_on=[secrets],
)

# The "data behind this site" store (`/meta`, `job/profiles/gcs-meta.yml`): the data bucket and the R2 serving
# bucket, scanned daily after the main scan (07:00, ~2–4 h with its retire) so each meta scan sees its result.
meta = BatchCron(
    "gcs-meta-snapshot-daily",
    project=project,
    region=region,
    schedule="0 12 * * *",
    submitter=job_dir / "meta-submit.sh",
    sa_email=job.email,
    adopt=adopt,
    depends_on=[secrets, static_secrets],
)

# The hourly D1 → Google Sheet mirror (deploy/sheet-mirror/): `deploy.sh` ships
# the image and the rendered config; this owns the job, its trigger and IAM.
sheet_sync = RunJobCron(
    "gcs-sheet-sync",
    project=project,
    region=region,
    job="gcs-sheet-sync",
    trigger="gcs-sheet-sync-hourly",
    schedule="5 * * * *",
    time_zone="UTC",
    sa_email=job.email_literal,
    image=f"{region}-docker.pkg.dev/{project}/cloud-run-source-deploy/gcs-sheet-sync:latest",
    secret_env={"SITE_TOKEN": "gcs-sheet-sync-token"},
    labels={"component": "sheet-sync"},
    adopt=adopt,
    existing=True,
    opts=pulumi.ResourceOptions(depends_on=[secrets]),
)

# The scans' own bucket: snapshots, path indexes, access logs. The raw access
# logs (`access/raw/`) are deleted at 7 days: nothing reads them after ingest
# (`access/agg` + `access/sizes` are cut from the same chunk), so they are only a
# window to re-aggregate in. Nextflow's task dirs (`nf/work/`) are deleted at 14:
# a failed scan's `-resume` is the only reader.
DATA_BUCKET = "oa-gcs-usage-dvx"
gcp.storage.Bucket(
    DATA_BUCKET,
    project=project,
    name=DATA_BUCKET,
    location="US-EAST1",
    storage_class="STANDARD",
    uniform_bucket_level_access=True,
    # gcs's alone, so the stack's own `deployment` (the provider's default labels): cw's `cw-l2/` and static
    # names left for its own bucket at its isolation (2026-10-13), and only gcs's accounts hold grants here
    labels={"component": "data"},
    # Soft delete off (2026-10-25): `retire` deletes each old scan's sorts and listings daily, and a 7-day
    # window billed them all twice. Everything here is rebuildable from the scans; deletes are final.
    soft_delete_policy=gcp.storage.BucketSoftDeletePolicyArgs(retention_duration_seconds=0),
    lifecycle_rules=[
        gcp.storage.BucketLifecycleRuleArgs(
            action=gcp.storage.BucketLifecycleRuleActionArgs(type="Delete"),
            condition=gcp.storage.BucketLifecycleRuleConditionArgs(age=7, matches_prefixes=["access/raw/"]),
        ),
        gcp.storage.BucketLifecycleRuleArgs(
            action=gcp.storage.BucketLifecycleRuleActionArgs(type="Delete"),
            condition=gcp.storage.BucketLifecycleRuleConditionArgs(age=14, matches_prefixes=["nf/work/"]),
        ),
    ],
    opts=adopt.opts(True, f"{project}/{DATA_BUCKET}", protect=True),
)
grant_bucket(f"{DATA_BUCKET}-job", bucket=DATA_BUCKET, role="roles/storage.objectAdmin", member=job.member, member_email=job.email_literal, adopt=adopt, existing=True)
grant_bucket(f"{DATA_BUCKET}-browse", bucket=DATA_BUCKET, role="roles/storage.objectViewer", member=browse.member, member_email=browse.email_literal, adopt=adopt, existing=True)
# The site's dispatch drops each run's `plan.json` into `sweep/runs/<job>/` before
# submitting the job (`_lib/sweepDispatch.ts`): create-only, no read/overwrite/delete.
grant_bucket(f"{DATA_BUCKET}-dispatch", bucket=DATA_BUCKET, role="roles/storage.objectCreator", member=dispatch.member, member_email=dispatch.email_literal, adopt=adopt)

# Pipeline intermediates (e.g. the static name-search shuffle): no soft delete,
# so a deleted intermediate stops billing at once, and anything left behind is
# deleted after 7 days. Nothing here is a source of truth.
SCRATCH_BUCKET = "oa-gcs-usage-scratch"
scratch = gcp.storage.Bucket(
    SCRATCH_BUCKET,
    project=project,
    name=SCRATCH_BUCKET,
    location="US-EAST1",
    storage_class="STANDARD",
    uniform_bucket_level_access=True,
    labels={"component": "scratch"},
    soft_delete_policy=gcp.storage.BucketSoftDeletePolicyArgs(retention_duration_seconds=0),
    lifecycle_rules=[
        gcp.storage.BucketLifecycleRuleArgs(
            action=gcp.storage.BucketLifecycleRuleActionArgs(type="Delete"),
            condition=gcp.storage.BucketLifecycleRuleConditionArgs(age=7),
        ),
        # Experiments' outputs (races, benches, sims) go under `tmp/`: gone in 3 days. The rest of the bucket keeps
        # 7, since the interval store's open-version state lives here and an append needs the previous scan's.
        gcp.storage.BucketLifecycleRuleArgs(
            action=gcp.storage.BucketLifecycleRuleActionArgs(type="Delete"),
            condition=gcp.storage.BucketLifecycleRuleConditionArgs(age=3, matches_prefixes=["tmp/"]),
        ),
    ],
)
grant_bucket(f"{SCRATCH_BUCKET}-job", bucket=scratch.name, role="roles/storage.objectAdmin", member=job.member, member_email=job.email_literal, adopt=adopt)

# What can't be regenerated (D1 exports taken before a destructive migration, a
# `scan retire -b` target): soft delete kept (7 days), public access off. The job
# may add and read backups, never overwrite or delete them (`objectCreator` +
# `objectViewer`); deleting one is a human's call.
BACKUPS_BUCKET = "oa-gcs-usage-backups"
backups = gcp.storage.Bucket(
    BACKUPS_BUCKET,
    project=project,
    name=BACKUPS_BUCKET,
    location="US-EAST1",
    storage_class="STANDARD",
    uniform_bucket_level_access=True,
    public_access_prevention="enforced",
    labels={"component": "backups"},
    soft_delete_policy=gcp.storage.BucketSoftDeletePolicyArgs(retention_duration_seconds=604800),
    opts=pulumi.ResourceOptions(protect=True),
)
for role in ["roles/storage.objectCreator", "roles/storage.objectViewer"]:
    grant_bucket(f"{BACKUPS_BUCKET}-job-{role.split('.')[-1]}", bucket=backups.name, role=role, member=job.member, member_email=job.email_literal, adopt=adopt)

# The scanned fleet (Marin's buckets, another project): the job lists and reads
# every bucket and deletes from the swept ones (`objectUser`); the browser reads.
FLEET = {
    "marin-us-east1": True,
    "marin-us-east5": True,
    "marin-us-central1": True,
    "marin-us-central2": True,
    "marin-eu-west4": True,
    "marin-us-west4": True,
}
for bucket, swept in FLEET.items():
    roles = ["roles/storage.legacyBucketReader", "roles/storage.objectViewer"] + (["roles/storage.objectUser"] if swept else [])
    for role in roles:
        grant_bucket(f"{bucket}-job-{role.split('.')[-1]}", bucket=bucket, role=role, member=job.member, member_email=job.email_literal, adopt=adopt, existing=True)
    grant_bucket(f"{bucket}-browse", bucket=bucket, role="roles/storage.objectViewer", member=browse.member, member_email=browse.email_literal, adopt=adopt, existing=True)

# Managed object deletion is deliberately opt-in: Storage Intelligence is
# billed per managed object after its one-time 30-day trial. When enabled, the
# shared component owns the API, service identity, least-scope bucket grants,
# and bucket-filtered Intelligence config. The actual jobs use the reviewed
# DR's generation-pinned CSV manifests, never prefix selection.
batch_delete_edition = (cfg.get("batchDeleteEdition") or "DISABLED").upper()
if batch_delete_edition != "DISABLED":
    batch_delete = StorageBatchDelete(
        "gcs-sweep-batch-delete",
        control_project=project,
        fleet_project=cfg.require("fleetProject"),
        buckets=[bucket for bucket, swept in FLEET.items() if swept],
        manifest_bucket=DATA_BUCKET,
        submitter=dispatch.member,
        edition=batch_delete_edition,
    )
    pulumi.export("storage_batch_service_agent", batch_delete.agent.email)

pulumi.export("job_account", job.email)
pulumi.export("crons", [daily.cron.name, meta.job.name, sheet_sync.cron.name])
