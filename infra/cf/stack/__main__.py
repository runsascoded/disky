"""A deployment's Cloudflare stack, from config alone.

Every deployment runs this same program; only its `Pulumi.<stack>.yaml` differs
(`Pulumi.stack.example.yaml` lists every key). A deployment's `infra/cf/Pulumi.yaml`
points here with `main: stack/` (`Pulumi.yaml.example`).

- `CfnDashboard`: the Pages project, custom domain + CNAME, D1, and optionally
  preview-branch aliases, a cache KV, an R2 serving bucket + token, an Access gate.
- Optionally a second Pages project on the same D1 (`devProject` / `devDomain`):
  a separate dev site, as opposed to a preview-branch alias of the production one.
- Optionally the capture trigger (`capturesBucket` / `capturesQueue`): a finished
  laptop capture in R2 → a queue → the `capture-trigger` Worker → the ingest.

`pulumi preview` is read-only; `up` is a human's call.
"""
import hashlib
import sys
from pathlib import Path

import pulumi
import pulumi_cloudflare as cloudflare

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from capture_trigger import CaptureTrigger  # noqa: E402
from cfn_dashboard import AccessApp, BranchAlias, CfnDashboard, Store  # noqa: E402

cfg = pulumi.Config()
account_id = cfg.require("accountId")
zone_id = cfg.require("zoneId")


def access_app(c: dict | None) -> AccessApp | None:
    if not c:
        return None
    return AccessApp(
        name=c["name"],
        uris=tuple(c["uris"]),
        include_everyone=c.get("includeEveryone", False),
        email_domains=tuple(c.get("emailDomains", ())),
        session_duration=c.get("sessionDuration", "24h"),
        app_launcher_visible=c.get("appLauncherVisible"),
        policy_name=c.get("policyName"),
    )


store = Store(
    pages_project=cfg.require("pagesProject"),
    production_branch=cfg.get("productionBranch") or "main",
    domain=cfg.require("domain"),
    d1_name=cfg.require("d1Name"),
    branch_aliases=tuple(BranchAlias(domain=a["domain"], branch=a["branch"]) for a in cfg.get_object("branchAliases") or []),
    access=access_app(cfg.get_object("access")),
    kv_name=cfg.get("kvName"),
    r2_bucket=cfg.get("r2Bucket"),
    r2_location=cfg.get("r2Location") or "enam",
    service_token=cfg.get("serviceToken"),
)
dash = CfnDashboard(pulumi.get_stack(), account_id=account_id, zone_id=zone_id, store=store, import_ids=cfg.get_object("importIds") or None)

pulumi.export("pages_project", dash.pages.name)
pulumi.export("custom_domain", dash.domain.name)
for branch, dom in dash.branch_domains.items():
    pulumi.export(f"{branch}_domain", dom.name)
pulumi.export("d1_database", dash.d1.name)
pulumi.export("d1_database_id", dash.d1.id)   # → site/wrangler.toml `database_id`
if dash.kv is not None:
    pulumi.export("cache_kv", dash.kv.title)
    pulumi.export("cache_kv_id", dash.kv.id)   # → site/wrangler.toml `[[kv_namespaces]] id`
if dash.r2 is not None and dash.r2_token is not None:
    pulumi.export("r2_bucket", dash.r2.name)
    pulumi.export("r2_s3_access_key_id", pulumi.Output.secret(dash.r2_token.id))
    # R2's S3 secret is the SHA-256 of the token value.
    pulumi.export("r2_s3_secret_access_key", pulumi.Output.secret(dash.r2_token.value.apply(lambda v: hashlib.sha256(v.encode()).hexdigest())))

# A separate dev site: its own Pages project, domain and CNAME, bound to the same
# D1 by its wrangler config (not a second CfnDashboard, which would make its own D1).
dev_project, dev_domain = cfg.get("devProject"), cfg.get("devDomain")
if bool(dev_project) != bool(dev_domain):
    raise pulumi.RunError("devProject and devDomain go together")
if dev_project:
    dev_pages = cloudflare.PagesProject(
        "dev-pages",
        account_id=account_id,
        name=dev_project,
        production_branch="main",
        build_config=cloudflare.PagesProjectBuildConfigArgs(destination_dir="dist"),
        opts=pulumi.ResourceOptions(ignore_changes=["deployment_configs"]),   # wrangler's
    )
    dev_cname = cloudflare.DnsRecord(
        "dev-cname",
        zone_id=zone_id,
        name=dev_domain,
        type="CNAME",
        content=f"{dev_project}.pages.dev",
        ttl=1,
        proxied=True,
        opts=pulumi.ResourceOptions(ignore_changes=["comment"]),
    )
    dev = cloudflare.PagesDomain(
        "dev-domain",
        account_id=account_id,
        project_name=dev_pages.name,
        name=dev_domain,
        opts=pulumi.ResourceOptions(depends_on=[dev_cname]),
    )
    pulumi.export("dev_pages_project", dev_pages.name)
    pulumi.export("dev_domain", dev.name)

captures_bucket, captures_queue = cfg.get("capturesBucket"), cfg.get("capturesQueue")
if bool(captures_bucket) != bool(captures_queue):
    raise pulumi.RunError("capturesBucket and capturesQueue go together")
if captures_bucket:
    trigger = CaptureTrigger(
        "captures",
        account_id=account_id,
        bucket=captures_bucket,
        queue_name=captures_queue,
        prefix=cfg.get("capturesPrefix") or "captures/",
        suffix=cfg.get("capturesSuffix") or "_SUCCESS.json",
        moved_from_root=cfg.get_bool("capturesMovedFromRoot") or False,
    )
    pulumi.export("captures_queue", trigger.queue.queue_name)
