# Cost labels: attributing disky's GCP spend

disky's GCP resources (Batch jobs, Cloud Run, Scheduler, Secret Manager, buckets) share a project with other OA work. To report disky's spend, everything disky creates carries cost-attribution labels, and the project's billing account exports detailed usage cost to BigQuery, where a query groups the labeled rows by deployment and component.

## The setting

One env var, generic, with no deployment defaults:

```bash
export DISKY_LABELS=app=disky,deployment=gcs     # wt/gcs/.envrc (untracked)
export DISKY_LABELS=app=disky,deployment=cw-s3   # wt/cw-s3/.envrc
```

Each job adds its own `component`. Label rules are GCP's: keys `[a-z][a-z0-9_-]{0,62}`, values `[a-z0-9_-]{0,63}`, ≤64 labels. A malformed value fails loudly at parse time, not at the API.

| component | what | labeled by |
|---|---|---|
| `scan` | the daily/sub-daily snapshot job (its VM also runs the digest, index-sync, path-index) | the deployment's `job/*batch-submit.sh` |
| `meta-scan` | cw-s3's meta self-scan | `job/cw-meta-submit.sh` |
| `listing` | the DIY fleet-listing fan-out (`dt-cloud job submit-listing`) | `batch.listing_job_spec` |
| `static-names` | the static name index chain (append, shards, catalog, publish, verify, r2) | `static_runner.job_spec`; manual `job/static-names.sh` |
| `drill`, `anchors` | the static chain's drill and anchored-search stages | `static_runner.job_spec` (`STAGE_COMPONENTS`) |
| `sweep` | sweep executors (site dispatch, `job submit-reviewed`, cw plan-sweep) | `sweep_job.reviewed_job_spec`, `sweepJobSpec`, `sweepBatchSpec` |
| `sweep-undo`, `sweep-purge` | sweep undo / cw purge jobs | `sweepUndo.ts`, `plan-sweep/{undo,purge}.ts` |
| `sheet-sync` | the D1 → Google Sheet Cloud Run job | `RunJobCron(labels=…)`, `deploy/sheet-mirror/deploy.sh` |
| `data`, `scratch` | `oa-gcs-usage-dvx`, `oa-gcs-usage-scratch` (with `deployment=shared`) | gcs's Pulumi program |

`interval-store` and `digest` have no Batch jobs of their own yet: their compute runs inside the `scan` VM. A future standalone job takes its component from `label_batch_spec(spec, "<component>")`.

## Where it is read

**Python (`dt_cloud/cost_labels.py`):** `cost_labels(component)` = `$DISKY_LABELS` + `component`; `label_batch_spec(spec, component)` sets them on both the job's `labels` and `allocationPolicy.labels`. Unset `DISKY_LABELS` is opt-out: no labels at all, specs byte-identical to before. `dt-cloud cost-labels [-c COMPONENT] [-j]` prints them for shell callers (`k=v,…` for gcloud's `--labels` / `--update-labels`, or JSON).

**Batch label propagation.** Batch's [labels doc] is explicit: job `labels` "are only applied to the job"; `allocationPolicy.labels` are "applied to the job, as well as to each GPU (if any), persistent disk (all boot disks and any new storage volumes), and VM created for the job". The Compute cost lines in the billing export carry the VM's and disk's labels, so **`allocationPolicy.labels` is the field that matters**; dt-cloud sets both, so the job list filters by the same labels.

**The scan VM's children.** The scan job runs `dt-cloud job submit-listing` and `static-names runs add` from inside its VM, so the submitter forwards `DISKY_LABELS` into the scan container's env: the children inherit the deployment's labels and add their own component.

**Site (`site/functions/_lib/costLabels.ts`):** the Pages var `DISKY_LABELS` (in `batchConfig`'s `BatchEnv`, parsed into `BatchConfig.labels`); `labelBatchSpec` applies them in `sweepJobSpec` (gcs executors) and `sweepBatchSpec` (cw plan-sweep). The deployment sets the var in its `site/wrangler.toml` `[vars]`.

**Pulumi (`infra/gcp/gcp_jobs.py`, on `cloud`):**
- `cost_labels()` reads `$DISKY_LABELS`; **unset is an error** (empty = deliberately none), because an `up` from a shell without it would strip the labels off every live resource.
- `labeled_provider(name, project=, region=, labels=)` makes an explicit `gcp.Provider` with `default_labels` and registers a resource transform that makes it the provider of every `gcp:` resource, so no component needs threading. Every label-capable resource (buckets, secrets, the Cloud Run job) gets the defaults; a resource's own `labels` override them per key.
- `RunJobCron(labels=…)`: the Cloud Run job's own labels. `labels` is no longer in its `ignore_changes`; pulumi-gcp ≥5's `labels` is non-authoritative (system labels such as `cloud.googleapis.com/location` live in `effectiveLabels`), so it doesn't fight `gcloud run jobs deploy`.
- `submitter_spec` passes `$DISKY_LABELS` through its scrubbed env, so each `BatchCron` body carries the same labels as the stack.

Moving resources from the default provider to the explicit one is an **update, not a replace**: the previews below show IAM members and service accounts unchanged, and only label-capable resources diffing.

**Not labelable:**
- Cloud Scheduler jobs have no labels field. They cost about $0.10 per job per month.
- Cloud Build builds have `tags`, not labels, and build minutes reach the export without resource labels. That covers `job/build.sh` and `deploy/sheet-mirror/build.sh`.
- Service accounts, IAM bindings and logging views have no labels and no cost.

## Deployment rollout (gcs / cw-s3 sessions)

The deployment-only parts are patches for each deployment session: `wt/gcs/specs/cost-labels-gcs.patch` and `wt/cw-s3/specs/cost-labels-cw-s3.patch`, each applying over the deployment branch after it merges `cloud`.
- **`infra/gcp/__main__.py`:** a `labeled_provider(...)` call. gcs also adds `labels={"component": "sheet-sync"}` on the `RunJobCron`, and `{"deployment": "shared", "component": "data" | "scratch"}` on the two buckets, which both deployments use (cw-s3's `cw-l2/` and static names live there too).
- **`job/batch-submit.sh` (gcs) and `job/cw-{batch,meta}-submit.sh` (cw-s3):** a `labels()` helper; `"labels"` on the job and its `allocationPolicy`; `DISKY_LABELS` in the container env.
- **gcs's `job/static-names.sh`:** labels on its manual-submit spec.
- **`site/wrangler.toml`:** `DISKY_LABELS = "app=disky,deployment=<name>"` in `[vars]` (and the preview env's vars).
- **`.envrc` (untracked):** `export DISKY_LABELS=app=disky,deployment=<name>`.

Then `pulumi preview` (and `up` on Ryan's go), a `site/deploy`, and a job image rebuild (`job/build.sh`) so the scan VM's `dt-cloud` labels its children.

### Previews (2026-10-09, with the patches applied, not `up`'d)

gcs: `+ 1 to create` (the `gcs-labeled` provider), `~ 12 to update`, 59 unchanged, no replaces.
- The two buckets get `labels` (`deployment: shared`, `component: data` / `scratch`, plus the defaults).
- Eight secrets diff only in computed `effectiveLabels` / `pulumiLabels`, which are the defaults. Three already carry `app=gcs-usage`; a resource's own key wins, so they keep it.
- The sheet-sync Cloud Run job gets `component: sheet-sync`.
- The `gcs-usage-snapshot-daily` cron body adds `labels` and `allocationPolicy.labels` (`app=disky, deployment=gcs, component=scan`) and the env var `DISKY_LABELS`. Nothing else in the body changes.

cw-s3: `+ 1 to create`, `~ 9 to update`, no replaces. Seven secrets, plus the two cron bodies (`component=scan`, `meta-scan`), carrying the same three additions.

## Existing buckets (one-off, until the gcs stack's `up`)

The gcs stack manages both buckets, so its `up` sets these labels. The commands below set them now, to the same values the stack will (an `up` then finds no diff):

```bash
gcloud storage buckets update gs://oa-gcs-usage-dvx --project=oa-internal-450019 \
  --update-labels=app=disky,deployment=shared,component=data
gcloud storage buckets update gs://oa-gcs-usage-scratch --project=oa-internal-450019 \
  --update-labels=app=disky,deployment=shared,component=scratch
```

The project's other buckets are someone else's or shared: `oa-pulumi` holds many teams' state, and `*_cloudbuild` and `run-sources-*` belong to the platform. They stay unlabeled. The Artifact Registry repo `cloud-run-source-deploy` holds disky's images (`gcs-usage-snapshot`, `gcs-sheet-sync`) and `pqtk`. Labeling it is optional and approximate:

```bash
gcloud artifacts repositories update cloud-run-source-deploy --project=oa-internal-450019 \
  --location=us-central1 --update-labels=app=disky,deployment=shared,component=images
```

## Billing export to BigQuery

**Console only.** There is no gcloud command (`gcloud billing` has only `accounts`, `budgets`, `projects`), no Cloud Billing API method and no Terraform/Pulumi resource for the export setting. The [setup doc] documents only console steps, and Google's forum confirms there is no public API ([feature request], [issue 504194143]).

**Dataset:** `oa-internal-450019:billing_export`, US multi-region, created 2026-10-09 with `bq --location=US mk --dataset`. A multi-region usage-cost dataset also backfills the previous month, but those rows carry no disky labels.

**Who can enable it:** "Detailed usage cost" needs **Billing Account Costs Manager or Billing Account Administrator** on the billing account, plus **BigQuery User** on `oa-internal-450019`.
- Ryan's account has `billing.accounts.get` and `getIamPolicy` on the billing account, but not `updateUsageExportSpec`.
- The billing account's own policy has a single binding, `roles/billing.costsManager`, held by one OA admin.
- Administrators may also hold roles at the organization level, which Ryan can't read.
- So **that Costs Manager** (or an org billing admin) has to enable it. The project's owners (the `eng-all` group, and others) already hold BigQuery access.

**Steps** (for the account with that role):
1. Open `https://console.cloud.google.com/billing/<BILLING_ACCOUNT_ID>/export/bigquery`, or [the export page], then choose "OA Billing Account".
2. On the **BigQuery export** tab, under **Detailed usage cost**, click **Edit settings**.
3. **Project:** `oa-internal-450019`. **Dataset:** `billing_export`. Click **Save**.
4. Within a few hours, the table `gcp_billing_export_resource_v1_<BILLING_ACCOUNT_ID>` appears in the dataset, and rows flow from then on.

Leave "Standard usage cost" alone. Detailed is a superset at resource granularity.

## The query

Detailed export table names end in the billing account id; the wildcard keeps it out of the query. `labels` holds the resource's own labels: the VM's and disk's for Batch, the bucket's, and the Cloud Run job's.

```sql
-- disky's GCP spend by month × deployment × component × service (gross; credits shown separately)
SELECT
  invoice.month AS month,
  (SELECT value FROM UNNEST(labels) WHERE key = 'deployment') AS deployment,
  (SELECT value FROM UNNEST(labels) WHERE key = 'component') AS component,
  service.description AS service,
  ROUND(SUM(cost), 2) AS gross,
  ROUND(SUM(IFNULL((SELECT SUM(c.amount) FROM UNNEST(credits) c), 0)), 2) AS credits,
  ANY_VALUE(currency) AS currency
FROM `oa-internal-450019.billing_export.gcp_billing_export_resource_v1_*`
WHERE project.id = 'oa-internal-450019'
  AND EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'app' AND value = 'disky')
  AND DATE(_PARTITIONTIME) >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
GROUP BY month, deployment, component, service
ORDER BY month DESC, gross DESC;
```

The remainder is the project's spend without `app=disky`, by service. It catches what labels can't reach (Cloud Build, Scheduler, untagged egress) and anything mislabeled:

```sql
SELECT invoice.month AS month, service.description AS service, sku.description AS sku, ROUND(SUM(cost), 2) AS gross
FROM `oa-internal-450019.billing_export.gcp_billing_export_resource_v1_*`
WHERE project.id = 'oa-internal-450019'
  AND NOT EXISTS (SELECT 1 FROM UNNEST(labels) WHERE key = 'app' AND value = 'disky')
  AND DATE(_PARTITIONTIME) >= DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY)
GROUP BY month, service, sku
HAVING gross > 1
ORDER BY month DESC, gross DESC;
```

Once rows exist, check that Batch VM lines carry the labels: `SELECT DISTINCT l.key FROM … , UNNEST(labels) l WHERE service.description = 'Compute Engine'`. Rows from before the labels shipped are unlabeled. Batch's own predefined VM labels (`batch-node`, and any job-id label) may still attribute them by job-name prefix (`gcs-usage-snapshot-`, `sn-`, `gcs-sweep-`, `cw-`).

**Out of scope:** reads of the scanned fleet's buckets (Marin's, in another project) bill that project, not this one.

[labels doc]: https://docs.cloud.google.com/batch/docs/organize-resources-using-labels
[setup doc]: https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery-setup
[feature request]: https://discuss.google.dev/t/feature-request-gcloud-cli-support-for-configuring-cloud-billing-export-to-bigquery/351785
[issue 504194143]: https://issuetracker.google.com/issues/504194143
[the export page]: https://console.cloud.google.com/billing/export
