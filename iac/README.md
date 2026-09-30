# `@disk-tree/iac` — the CFN reference deploy, as code

The staged-delete executors (spec [`specs/staged-delete.md`](../specs/staged-delete.md)) need per-deployment infrastructure. This package holds the **`CfnDashboard`** Pulumi component — "the Vite + Cloudflare-Pages-Functions reference deploy" the R2 / S3 / GCS deployments share — and the repo ships a generator (`disk-tree iac`) that derives its config from `buckets.yml`, so setup is one source of truth rather than hand-maintained parallel config.

Design rationale + the broader inventory: marin-gcs-usage `specs/cf-iac.md` (Pulumi, one stack per deployment branch).

## The two axes it provisions (spec CP6–CP8)

Deletion has two orthogonal axes; IaC provisions the **executor** axis:

| executor | small scope | large scope |
|---|---|---|
| **R2** | `CfnDashboard` binds `R2_<bucket>` → the edge Worker deletes inline (**CP7, done**) | Worker queue / Durable-Object chain |
| **S3** | drainer inline `aws s3 rm` (**CP4, done**) | **AWS Batch (`iac/aws/`, done)** — the drainer submits oversized runs |
| **GCS** | Cloud Function | GCP Batch (what gcs runs today) |

The R2 small + S3 large cells are live. The drainer (`disk-tree dispatch --serve`) deletes small runs inline and, past `delete.batch.threshold`, submits the run to AWS Batch (`disk_tree/batch.py`) — the run keeps `batch_job` set (unfinished, but not re-submitted) until the job's container (`iac/aws/delete-job/`) deletes the objects and writes the result back to D1.

## AWS Batch (`iac/aws/`)

Terraform for the Fargate compute environment + job queue + job definition, with an IAM role scoped to your S3 buckets, and the delete-job container. Fill `terraform.tfvars` from `buckets.yml`:

```bash
disk-tree iac aws-batch > iac/aws/terraform.tfvars   # project/region/s3_buckets
# add image (the pushed delete-job container), d1_database_id, subnets, security_group_ids
cd iac/aws && terraform init && terraform apply
```

Then point the drainer at it — a `delete.batch` block in `buckets.yml`:

```yaml
delete:
  batch: { provider: aws, job_queue: disk-tree-delete, job_definition: disk-tree-delete, threshold: 10000 }
```

## `CfnDashboard` (`index.ts`)

One component stands up: the **Pages project** (with `DB` D1 + `SCANS` R2 + `R2_<bucket>` executor bindings + `SCANS_PREFIX`/`DELETE_THRESHOLD` vars), the **D1 auth database**, the **R2 data bucket**, and one **R2 bucket per CP7 executor binding**. Identity-bearing config (`SESSION_SECRET`, `ALLOWED_EMAILS`, `ACCESS_*`) stays Pulumi secrets set per stack — not in the component.

```ts
new CfnDashboard('demo', {
  accountId: cfAccount,
  ...JSON.parse(iacConfigJson),   // `disk-tree iac config`
})
```

## The generator (`disk-tree iac`, tested in `tests/test_iac.py`)

```bash
disk-tree iac r2-bindings   # [[r2_buckets]] wrangler.toml blocks — activates the CP7 CFN
disk-tree iac config        # the CfnDashboard component config (JSON) for this deployment
```

`iac r2-bindings` is immediately usable: paste its output into `ui/wrangler.toml` and redeploy to bind your R2 buckets (each `r2://<bucket>` in `buckets.yml` → an `R2_<bucket>` binding), which turns small R2 dispatches from drainer-deferred into edge-inline.

## Applying

Not applied from this repo (which ships no `@pulumi` deps). In an environment with the Pulumi SDK + CF credentials (e.g. OA's `~/c/oa/ops`):

```bash
pnpm install && pnpm typecheck   # provider fields track @pulumi/cloudflare v5
pulumi up                        # import existing resources first, so the first up is a no-op diff
```

Deployments themselves (`wrangler pages deploy`) and D1 migrations (`wrangler d1 migrations apply`) stay with the app — Pulumi owns the resources, not the container or the schema.
