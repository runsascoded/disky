# infra — a deployment's cloud resources as code

Pulumi components, one directory per provider, shared by every deployment, plus a shared stack program per provider that builds a deployment from config alone. A deployment branch adds only its `Pulumi.yaml` (pointing at the shared program) and `Pulumi.<stack>.yaml`. The components and programs carry no account ids, zone ids, buckets or secret names; those are the config's.

| dir | component | what it owns | who else writes there |
|---|---|---|---|
| `cf/` | `cfn_dashboard.py` (`CfnDashboard`); the program `stack/` | the Cloudflare surface of a `site/` deployment: the Pages project shell, its custom domain + CNAME, preview-branch aliases, the D1 database, an optional `CACHE_KV` namespace, an optional R2 serving bucket, an optional Zero Trust Access gate | `wrangler pages deploy` fills each deploy's bindings and vars from the branch's `site/wrangler.toml` |
| `cf/` | `capture_trigger.py` (`CaptureTrigger`) | a laptop deployment's capture trigger: the queue, and the R2 event notification that publishes a finished capture's marker (`captures/…/_SUCCESS.json`) to it | the consumer Worker (`capture-trigger/`, on `local`) is wrangler's |
| `aws/` (`local`) | `batch_ingest.py` (`BatchIngest`); the program `stack/` | a laptop deployment's ingest: the image (built on CodeBuild by `build-image`, no local Docker), ECR repo, Fargate Spot queue, job definition, its roles and Secrets Manager containers, and the IAM user the capture trigger submits jobs as | secret values and the trigger user's access key go in out of band (the deployment's `put-secrets`) |
| `cf/` | `cron-dispatch/` (a Worker, deployed by wrangler) | GitHub Actions workflows started on Cloudflare's cron (`workflow_dispatch`), since GitHub's own `schedule:` runs hours late; configuration only (`wrangler.example.toml`), reusable for any repo; r2's is `wrangler.r2.toml` | the `GITHUB_TOKEN` secret (fine-grained, Actions read/write) goes in with `wrangler secret put` |
| `gcp/` | `gcp_jobs.py` (`JobAccount`, `Secrets`, `BatchCron`, `RunJobCron`, `grant_secret`, `grant_bucket`) | a deployment's scheduled jobs: their service accounts and project roles, Secret Manager containers and accessors, Cloud Scheduler crons (→ Batch, or → a Cloud Run job), bucket grants | the Batch spec is generated from the branch's `job/*-submit.sh` under `PIN=1 DRY=1`; a Cloud Run job's image and plain env are its deploy script's |

Two rules hold across `gcp/`: secret **values** are never managed (containers and IAM only; payloads go in with `gcloud secrets versions add`), and IAM is additive (`IAMMember`), so a stack can't clobber anyone else's grants on a shared project or bucket. `aws/` keeps the first rule the same way (Secrets Manager containers only).

## Running a stack

```bash
cd infra/<cf|gcp>
pulumi preview -s <stack>   # read-only; `up` is a human's call
```

Pulumi runs the program under `uv`, from `infra/pyproject.toml`. Give it its own environment (`UV_PROJECT_ENVIRONMENT=infra/.venv`): in a shell where the repo's `.venv` is active, `uv` would sync infra's dependencies into the engine's venv.

Every deployment's Cloudflare stack runs the shared `cf/stack/` program (gcs and cw-s3 moved onto it on 2026-10-03, `specs/iac-templates.md` phase 4, previews unchanged). Their GCP stacks still have their own `infra/gcp/__main__.py`.

## Tests

The components' and programs' tests run under Pulumi's mocks, with no cloud account: every resource a config declares, by type and name.

```bash
UV_PROJECT_ENVIRONMENT=infra/.venv uv run --project infra --with pytest pytest infra/tests
```

## Starting a deployment

A deployment is a branch off `cloud` (or off `local`, for a laptop deployment) that adds config and nothing else. It serves a Map of one or more buckets' usage at a domain you own, from an index you build on a schedule.

**You need:**
- a Cloudflare account with a zone for the site's domain, and an API token for the Cloudflare stack (its permissions are listed in `cf/stack/Pulumi.stack.example.yaml`);
- a bucket for the index (R2, S3 or GCS) with a read-only key pair for the site and a read-write one for the indexing job;
- read access to the buckets you'll scan;
- the Pulumi CLI, `uv`, `pnpm`, and wrangler (from `site/`'s dependencies).

**1. The Cloudflare stack.** Copy `cf/stack/Pulumi.yaml.example` to `cf/Pulumi.yaml` (rename the project, choose a state backend) and `cf/stack/Pulumi.stack.example.yaml` to `cf/Pulumi.<stack>.yaml`, and fill in the ids and names. Then:

```bash
cd infra/cf
pulumi stack init <stack>
pulumi preview      # read it: everything should be a create
pulumi up
pulumi stack output d1_database_id
```

That makes the Pages project, the custom domain and its CNAME, and the D1 database, plus the optional blocks you uncommented.

**2. The site's config.** Copy `site/wrangler.example.toml` to `site/wrangler.toml` and fill it in: the Pages project, the D1 id from step 1, the index bucket (`STORE_*`), and the vars for the features you want. Each key has a one-line comment; optional ones stay commented out. Add a matching entry to the store registry in `site/src/stores.ts`, under the key `STORE` names.

**3. The database schema.** Apply the migrations your features need:

```bash
cd site && pnpm exec wrangler d1 migrations apply <d1Name> --remote
```

`migrations_dir` in `wrangler.toml` picks the lineage. r2.rbw.sh (public, index only) uses `migrations/cw`. A deployment with sign-in, plans or the ownership ledger needs those tables too; today they live in the gcs and cw lineages on their branches.

**4. Secrets.** Set each `# secret` line of `wrangler.example.toml` that applies, for example:

```bash
pnpm exec wrangler pages secret put STORE_ACCESS_KEY_ID --project-name <pagesProject>
```

`SESSION_SECRET` is required wherever anyone signs in. Values never go in git or in Pulumi state.

**5. Build and deploy.**

```bash
cd site && pnpm build && pnpm exec wrangler pages deploy dist --project-name <pagesProject> --branch main
```

The build reads `STORE` and `AUTH_MODE` from `wrangler.toml`. `.github/workflows/deploy-r2.yml` is r2.rbw.sh's version of this step.

**6. The index.** The site shows nothing until a scan is indexed and its footer is in D1:

```bash
disk-tree bulk-list <scheme>://<bucket> -o work/listing/<bucket>    # once per bucket
cd cloud && dt-cloud path-index -S -d <date> -l 'work/listing/*/*.parquet' -P work/index/path-index.parquet -o work/snap
# upload work/index/ → <index bucket>/listing/<date>/index/<gen>/ and work/snap/ → snapshots/<sub>/<date>/
dt-cloud index-sync <date> -g <gen> -b <index bucket> -k listing/<date>/index/<gen> -d work/index -v path -v bysize
```

`index-sync` needs `CLOUDFLARE_API_TOKEN` (D1 edit), `CLOUDFLARE_ACCOUNT_ID`, `D1_DB_ID` and `D1_DB_NAME`. `.github/workflows/daily-ingest.yml` is r2.rbw.sh's daily version, and a template for a scheduled one.

**A laptop deployment** (this branch, `local`) adds three steps after step 1. A laptop's scans are uploaded to R2 as captures (`disk-tree capture`), and AWS Batch turns each into an index:

**L1. The AWS ingest.** Copy `aws/stack/Pulumi.yaml.example` to `aws/Pulumi.yaml` and `aws/stack/Pulumi.stack.example.yaml` to `aws/Pulumi.<stack>.yaml`, fill them in (the `env` block takes the D1 id from step 1), and `pulumi up` in `infra/aws`. It builds the ingest image on CodeBuild, so no local Docker. Then fill the Secrets Manager containers its `secrets` map names (`aws secretsmanager put-secret-value`).

**L2. The capture trigger.** Set `capturesBucket` / `capturesQueue` in the Cloudflare stack's config and `pulumi up` again. Copy `cf/capture-trigger/wrangler.example.toml` to `wrangler.toml`, fill it in from both stacks' outputs, and deploy it (`pnpm -C infra/cf/capture-trigger run deploy`). Then mint its AWS key straight into the Worker's secrets: `infra/cf/capture-trigger/put-secrets $(pulumi -C infra/aws stack output capture_trigger_user)`. That IAM user may only submit the ingest job, and the key is never printed or stored. Re-running it rotates the key.

**L3. Captures.** On the laptop: `disk-tree capture <path> -t r2://<capturesBucket>/captures`. The trigger submits one ingest per finished capture.

**Optional:**
- **Scheduled jobs on GCP** (scans on Batch, a Cloud Run job): `gcp/gcp_jobs.py`'s components, used by the gcs and cw-s3 branches' `gcp/__main__.py`.


## Moving inline resources into a component

A stack that declared resources inline adopts a component with `moved_from_root=True` (`BatchIngest`; `CaptureTrigger` via the Cloudflare program's `capturesMovedFromRoot`): each child is aliased to the same-named resource at the stack root, so its URN moves with no replacement. The bar is a `pulumi preview` with 0 to create, replace or delete.

## Adopting live resources

A deployment's resources mostly predate its stack, so a stack starts by importing them. Every component takes `existing=` per resource, and the stack's `adopting` config turns those into import ids all at once (`gcp_jobs.Adopt`). The first `up` with `adopting: "true"` imports; then set it to `"false"`. Read the adopting preview before running `up`: anything other than `= import`, or an `~ update` you can explain, means the code doesn't match what's live.
