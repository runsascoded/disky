# infra — a deployment's cloud resources as code

Pulumi components, one directory per provider, shared by every deployment. `cloud` carries only the components and their environment (`pyproject.toml`, `uv.lock`); each deployment branch adds its own stack programs next to them (`__main__.py`, `Pulumi.yaml`, `Pulumi.<stack>.yaml`). A program instantiates the components with its deployment's names, so the components carry no account ids, zone ids, buckets or secret names.

| dir | component | what it owns | who else writes there |
|---|---|---|---|
| `cf/` | `cfn_dashboard.py` (`CfnDashboard`) | the Cloudflare surface of a `site/` deployment: the Pages project shell, its custom domain + CNAME, preview-branch aliases, the D1 database, an optional `CACHE_KV` namespace, an optional R2 serving bucket, an optional Zero Trust Access gate | `wrangler pages deploy` fills each deploy's bindings and vars from the branch's `site/wrangler.toml` |
| `gcp/` | `gcp_jobs.py` (`JobAccount`, `Secrets`, `BatchCron`, `RunJobCron`, `grant_secret`, `grant_bucket`) | a deployment's scheduled jobs: their service accounts and project roles, Secret Manager containers and accessors, Cloud Scheduler crons (→ Batch, or → a Cloud Run job), bucket grants | the Batch spec is generated from the branch's `job/*-submit.sh` under `PIN=1 DRY=1`; a Cloud Run job's image and plain env are its deploy script's |

Two rules hold across `gcp/`: secret **values** are never managed (containers and IAM only; payloads go in with `gcloud secrets versions add`), and IAM is additive (`IAMMember`), so a stack can't clobber anyone else's grants on a shared project or bucket.

## Running a stack

```bash
cd infra/<cf|gcp>
pulumi preview -s <stack>   # read-only; `up` is a human's call
```

Pulumi runs the program under `uv`, from this directory's `pyproject.toml`. Each program checks that the selected stack is its branch's own.

## Adopting live resources

A deployment's resources mostly predate its stack, so a stack starts by importing them. Every component takes `existing=` per resource, and the stack's `adopting` config turns those into import ids all at once (`gcp_jobs.Adopt`). The first `up` with `adopting: "true"` imports; then set it to `"false"`. Read the adopting preview before running `up`: anything other than `= import`, or an `~ update` you can explain, means the code doesn't match what's live.
