"""Private R2 archive + collector. Preview first; human-only apply.

Wrangler is the non-secret config/build source, Pulumi owns deployed resources,
and the runtime query secret is installed separately (never in Pulumi state).
"""

import json
import re
from pathlib import Path

import pulumi
import pulumi_cloudflare as cf


here = Path(__file__).resolve().parent
config = pulumi.Config()
worker_config = Path(config.get("workerConfig") or here / "wrangler.json")
definition = json.loads(worker_config.read_text())
account = definition["account_id"]
variables = definition["vars"]
bucket_name = definition["r2_buckets"][0]["bucket_name"]
enabled = config.get_bool("enabled") or False
configured_retention = config.get_int("retentionDays")
retention = 30 if configured_retention is None else configured_retention
if not re.fullmatch(r"[a-f0-9]{32}", account) or variables["ACCOUNT_ID"] != account:
    raise pulumi.RunError("Worker account and ACCOUNT_ID must match")
if variables["ARCHIVE_BUCKET_NAME"] != bucket_name or bucket_name in json.loads(variables["SOURCE_BUCKETS"]):
    raise pulumi.RunError("Archive binding must match its name and must not be a source")
if definition["workers_dev"] or definition["preview_urls"]:
    raise pulumi.RunError("Collector must not have public Worker URLs")
if definition["triggers"]["crons"]:
    raise pulumi.RunError("Pulumi owns schedules; Wrangler crons must remain empty")
if not 7 <= retention <= 90:
    raise pulumi.RunError("retentionDays must be between 7 and 90")
if enabled and not config.get_bool("secretReady"):
    raise pulumi.RunError("Install the runtime secret first, then set secretReady before enabling cron")
bundle = Path(config.get("bundleFile") or here.parents[2] / "tmp" / "r2-access-build" / "index.js")
if not bundle.is_file():
    raise pulumi.RunError("Build the collector first: pnpm --dir infra/cf/r2-access build")

archive = cf.R2Bucket(
    "archive",
    account_id=account,
    name=bucket_name,
    location="enam",
    storage_class="Standard",
    opts=pulumi.ResourceOptions(protect=True),
)
private_archive = cf.R2ManagedDomain(
    "archive-private",
    account_id=account,
    bucket_name=archive.name,
    enabled=False,
)
lifecycle = cf.R2BucketLifecycle(
    "archive-retention",
    account_id=account,
    bucket_name=archive.name,
    rules=[{
        "id": "raw-runs-retention", "enabled": True, "conditions": {"prefix": "runs/"},
        "delete_objects_transition": {"condition": {"type": "Age", "max_age": retention * 86400}},
    }],
)
collector = cf.WorkersScript(
    "collector",
    account_id=account,
    script_name=definition["name"],
    content=bundle.read_text(),
    main_module="index.js",
    compatibility_date=definition["compatibility_date"],
    compatibility_flags=definition["compatibility_flags"],
    bindings=[
        {"name": "ARCHIVE", "type": "r2_bucket", "bucket_name": archive.name},
        *[{"name": key, "type": "plain_text", "text": value} for key, value in sorted(variables.items())],
    ],
    keep_bindings=["secret_text"],
    observability=definition["observability"],
    limits=definition["limits"],
    opts=pulumi.ResourceOptions(depends_on=[private_archive, lifecycle]),
)
private_worker = cf.WorkersScriptSubdomain(
    "collector-private",
    account_id=account,
    script_name=collector.script_name,
    enabled=False,
    previews_enabled=False,
)
schedules = cf.WorkersCronTrigger(
    "collector-cron",
    account_id=account,
    script_name=collector.script_name,
    schedules=[{"cron": variables[key]} for key in ("HOURLY_CRON", "DAILY_CRON")] if enabled else [],
    opts=pulumi.ResourceOptions(depends_on=[private_worker]),
)

pulumi.export("accountId", account)
pulumi.export("archiveBucket", archive.name)
pulumi.export("collectorName", collector.script_name)
pulumi.export("enabled", enabled)
pulumi.export("retentionDays", retention)
