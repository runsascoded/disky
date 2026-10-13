# OA blob-store inventory: what else could disky cover?

Read-only inventory taken 2026-10-10 (bucket metadata, Cloud Monitoring / R2 dashboard sizes, prefix names only; no object contents read). Recommendations at the end; every access/IAM change listed is for Ryan to approve, none were made.

## Correction to the starting assumption

Marin's scanned fleet does **not** live in `oa-internal-450019`. The six `marin-us-*` / `marin-eu-west4` buckets that gcs.oa.dev scans are in **`hai-gcp-models`** (the gcs stack's `fleetProject`), a Stanford-side project that also hosts unrelated CRFM/course buckets. `oa-internal-450019` holds only disky's own buckets and small infra buckets.

## Method

- GCP: `gcloud projects list` with the OA credentials (`direnv exec wt/gcs`) shows 3 projects: `oa-internal-450019`, `hai-gcp-models` and `oa-auth-509611` (no buckets). `gcloud storage buckets list` per project (needed `CLOUDSDK_PYTHON` pointed at Python 3.12; the default interpreter crashes on `ssl.PROTOCOL_TLSv1`). Sizes and object counts come from Cloud Monitoring `storage/total_bytes` and `storage/object_count` (latest, and 30 days earlier). Buckets with no series were spot-checked with a 3-key listing; all were empty.
- R2 (OA account): the bucket list comes from the API (`CF_IAC_TOKEN`; `CLOUDFLARE_API_TOKEN` and `CF_BATCH_TOKEN` lack R2 scope, and no token has R2 analytics scope). Sizes come from the dashboard in the OA Chrome profile.
- AWS / other S3: `~/.aws/config` profile names; read-only `list-buckets` where credentials work.
- CoreWeave: `list-buckets` on the `cw` profile.

## Inventory

Sizes are decimal TB. "30d" is the size 30 days earlier. Costs are rough list-price estimates (GCS regional Standard ≈ $20/TB-mo, US multi-region ≈ $26, R2 Standard $15).

### Already covered

| Store | Where | Size | Objects | 30d | Covered by |
|---|---|---|---|---|---|
| `marin-us-east5` | GCS `hai-gcp-models` | 844 TB | 48.1M | 905 | gcs (fleet) |
| `marin-us-central2` | 〃 | 818 TB | 86.7M | 1,028 | gcs |
| `marin-us-central1` | 〃 | 782 TB | 71.3M | 838 | gcs |
| `marin-eu-west4` | 〃 | 462 TB | 160.8M | 493 | gcs |
| `marin-us-west4` | 〃 | 180 TB | 13.7M | 148 | gcs |
| `marin-us-east1` | 〃 | 156 TB | 12.6M | 120 | gcs |
| 5 non-empty CAIOS buckets | CoreWeave | see `cw-zones-and-buckets` memory | | | cw-s3 |
| `oa-gcs-usage-dvx` | GCS `oa-internal-450019` | 8.2 TB | 0.11M | **3.0** | cw `/meta` |
| `oa-cw-s3-usage-index` | R2 (OA) | 284 GB | 5.5K | | cw `/meta` |

Fleet total: ≈3.24 PB / 393M objects, down ≈290 TB in 30 days (sweeps). `oa-gcs-usage-dvx` grew 2.8× in 30 days (+5.2 TB), which `/meta` already shows. Its growth is worth a separate look.

CoreWeave: the `cw` key sees exactly 6 buckets (`marin-us-east-02a`, `hero-checkpoints`, `rhoarnet-us-east-08a`, `marin-us-east-06a`, `marin-us-west-04a`, `marin-us-east-01a`). cw-s3 scans all 5 non-empty ones, and `01a` is still empty. There are **no CAIOS buckets beyond cw-s3's**, and no other CoreWeave org or key is evidenced locally.

### Not covered: candidates

**R2, OA account (4 buckets, ≈290 TB, ≈$450 billed in the first 4 days of the current period)**

| Bucket | Size | Objects | Created | Notes |
|---|---|---|---|---|
| **`marin-na`** | **289 TB** | **3.28M** | 2026-03-12 | WNAM, Standard, private. Top-level prefixes: `finelog/`, `iris/`, `marin/`, `ot-agent/`, `tmp/`, `users/`. Lifecycle: 9 `tmp/ttl={1..7,14,30}d/` expirations plus 7-day multipart abort, mirroring the CAIOS rules. ≈1.6M Class A writes/day. In Marin's own config this is `MARIN_PREFIX` / Iris state / finelog for the `cw-us-east-02a` and `cw-us-east-08a` clusters (marin `infra/pulumi/README.md`: those clusters "share `marin-na` on R2"). Averages ≈40 TB/month since creation (the dashboard's history view wasn't readable, so recent growth is unknown). ≈$4.3K/mo. |
| `oa-gcs-usage-index` | 560 GB | 1.8K | 2026-10-08 | gcs's static name index (Worker-served). disky's own storage, but **not** in `/meta`. |
| `oa-cw-s3-usage-index` | 284 GB | 5.5K | 2026-09-25 | Already in `/meta`. |
| `marin-discord` | 23 MB | 1 | 2026-07-30 | Discord archive DB. Noise. |

No `dial*` buckets exist in this account.

**GCS `hai-gcp-models`: Marin/Levanter buckets outside the fleet (≈190 TB / 11.6M objects, flat over 30 days, ≈$4–5K/mo)**

| Bucket | Location | Size | Objects | 30d | Soft-delete |
|---|---|---|---|---|---|
| `marin-vlm` | US (multi) | 69.6 TB | 0.71M | 69.6 | 7d |
| `marin-vlm-eu` | EUROPE-WEST4 | 53.7 TB | 0.42M | 53.7 | 7d |
| `levanter-checkpoints` | NAM4 | 23.5 TB | 4.76M | 23.5 | off |
| `marin-dna-us-central1` | US-CENTRAL1 | 16.3 TB | 0.11M | 16.3 | off |
| `marin-ckpt-eu-w4` | EUROPE-WEST4 | 10.8 TB | 2.34M | 10.8 | 7d |
| `marin-ckpt-us-w4` | US-WEST4 | 6.6 TB | 0.83M | 6.6 | 7d |
| `marin-ckpt-us-c2` | US-CENTRAL2 | 4.9 TB | 1.55M | 4.9 | 7d |
| `levanter-data` | NAM4 | 1.7 TB | 0.13M | 1.7 | 7d |
| `data-values*` (3 buckets) | various | 1.7 TB | 0.11M | 1.7 | 7–14d |
| `levanter-eu` | EUROPE-WEST4 | 1.1 TB | 0.31M | 1.1 | 7d |
| `marin-dna-us-central2` | US-CENTRAL2 | 0.9 TB | 10K | 0.9 | off |
| `marin-models-us` / `-eu` | US / EU | 0.9 / 0.5 TB | 0.45M | same | 7d |
| `marin-eval-metadata`, `marin-iac-state`, `marin-data`, `marin-public` | | <0.03 TB each | | | |

All are Standard class and almost all have zero growth in 30 days. That makes them **cold-data / cleanup candidates** (abandoned checkpoints, or data to move to Nearline/Coldline or delete), which is exactly the question disky answers. None has labels, so ownership comes only from bucket names and path prefixes (e.g. VLM, DNA, Levanter-era checkpoints).

Also in `hai-gcp-models`:
- `marin-usage-logs`: 5.5 TB, 30K objects, **+2.0 TB in 30 days**. This is gcs's own access-log sink (`ACCESS_LOG_BUCKET`), so it's disky's storage but not in `/meta`. `marin-usage-logs-acl` has no size series.
- Non-Marin Stanford buckets (`crfm-helm-*`, `codalab-*`, `cs324`, `pubmed-mosaic`, `aw-proxy-evals`, `relbench-*`, `tracem`, Cloud Functions/Run/Build buckets): about 1.7 TB total. These belong to a different owner and audience.
- Empty (verified by listing): `marin-us-west1` (though it's in gcs's `ACCESS_BUCKETS`), `marin-asia-northeast-1`, `marin-marina`, `marin-artifact-registry`, `levanter-empty`, `pubmedgpt`, `model_spec`, `crfm-helm-dev`.

**GCS `oa-internal-450019`**: `oa-gcs-usage-scratch` (0.44 TB, 40K objects, disky's scratch, not in `/meta`), `oa-transfer` (0.08 TB), `oa-pulumi` and the appspot/cloudbuild/run-sources buckets (≈0). `oa-marin-dna-us-central1` is empty.

**GCS `oa-auth-509611`**: no buckets.

**AWS (OA)**: `~/.aws/config` has an `openathena` SSO session with profiles `oa-management`, `oa-ci-dev`, `oa-ci-prod`, `general-dev` and `fomo-prod` (an AWS Organization). The SSO token has expired, so **no bucket inventory was possible**: logging in is interactive and was not attempted. To finish this section, Ryan runs `aws sso login --sso-session openathena` and then `aws s3api list-buckets --profile <p>` for each profile. Sizes come from CloudWatch `AWS/S3 BucketSizeBytes` / `NumberOfObjects`, which is cheap.

**Other S3-compatible**: profile `cfo` points at an R2 endpoint in a different Cloudflare account from the OA dashboard account. `ListBuckets` is denied, which suggests a bucket-scoped key. Its owner and purpose are unknown. No other OA S3 endpoints are evidenced.

## Recommendations

### 1. (a) Add `marin-na` as a secondary store on cw-s3 (highest value)

At 289 TB it is larger than `marin-us-west4` and `marin-us-east1` combined, costs about $4.3K/mo, and has no visibility today. It is the R2 half of the CoreWeave clusters' storage, so it serves the same Marin audience and the same questions as cw-s3: `users/` ownership, `tmp/ttl=*` churn, and growth. 3.3M objects lists in minutes.

- **Why cw-s3 and not a new deployment:** same audience. cw-s3 already has the generic multi-store machinery (`STORES_EXTRA` / `STORES_JSON`, `specs/multi-store.md`) and an S3-generic job chain that already lists `r2://` roots (`cw-meta-run.sh`). Folding it into cw's *primary* store would put an R2 bucket under the CoreWeave provider root and break the per-zone quota/digest semantics, so use a secondary store (e.g. `na` → `cw-s3.oa.dev/na`, viewer scope rather than admin). A future alternative is to rename the primary store's root to "Marin on CoreWeave + R2". That is a UX decision for Ryan.
- **Work (≈1–2 days, mostly config):**
  - Run the meta job's chain with `STORE=na`, `META_ROOTS=r2://marin-na` (generalize `cw-meta-run.sh` / `cw-meta-submit.sh` names to a per-store job, or add env defaults).
  - Add a Batch schedule (daily, or 12 h like cw).
  - Register the store the way `migrations/cw/0006` did for `meta`, and add a `STORES_JSON.na` entry (`STORE_BUCKET=oa-cw-s3-usage-index`, prefixes `na-l2/,snapshots/na/`).
  - Optionally snapshot `marin-na`'s lifecycle rules, as cw-run does for CAIOS. Check that R2's S3 `GetBucketLifecycleConfiguration` works with an object-read token.
- **Access (Ryan):**
  - Create a new R2 API token in the OA account: **Object Read only, scoped to `marin-na`** (one token per consumer, per the `r2-demo-tokens` lesson).
  - Store it in Secret Manager for the cw job's service account (`cw-s3-job`).
  - The site needs no `marin-na` credentials, because it serves the derived outputs from `oa-cw-s3-usage-index`.
  - Marin owns the bucket, so tell them it's being scanned (it's metadata only).

### 2. (a) Add the ~190 TB of non-fleet Marin/Levanter GCS buckets to gcs

These are the same project, provider and audience as the fleet. Their 30-day flatness and Standard class make them the clearest cleanup/tiering lead: ≈$4–5K/mo with apparently no active writers. Together they hold 11.6M objects, about 3% of the fleet's listing volume.

- **Work (≈0.5–1 day):**
  - Append the buckets to `FLEET` in `job/run.sh`. Check what else keys off `FLEET`: the lifecycle snapshot, `WARM_PATHS`, `ACCESS_BUCKETS`, sweeps (`PROTECTED_BUCKETS=marin-*` already covers them).
  - Add them to `infra/gcp/__main__.py` `FLEET` with `swept=False`, so they get **read-only** grants.
  - Plan the UX of the first scan: each bucket appears in diffs as "+N TB added" on that day. Announce it, or start them in a separate root group (e.g. "other Marin buckets").
  - Consider whether `levanter-*` and `data-values*` belong (likely Marin/CRFM, unlabeled). At minimum include `marin-vlm`, `marin-vlm-eu`, `levanter-checkpoints`, `marin-dna-*`, `marin-ckpt-*` and `marin-models-*`.
- **Access (Ryan):** a Pulumi `up` on the gcs GCP stack, adding per-bucket `legacyBucketReader` + `objectViewer` for the job service account and `objectViewer` for the browse service account in `hai-gcp-models`. This needs bucket-IAM admin in that Stanford-side project, which the existing fleet grants imply Ryan has. Per-bucket grants keep the job off the non-Marin buckets.

### 3. (a) Complete the `/meta` store with disky's own storage

`/meta` claims to show "data behind this site" but misses about 6.5 TB of it, including the fastest-growing piece:
- `marin-usage-logs`: 5.5 TB, +2 TB/month.
- `oa-gcs-usage-index` (R2): 560 GB.
- `oa-gcs-usage-scratch`: 0.44 TB.

**Work (≈2–4 h, config only):** extend `META_ROOTS` and `STORES_JSON.meta.vars.STORE_BUCKETS` / `ROOT_TITLE`.

**Access (Ryan):**
- Meta job service account: `objectViewer` + `legacyBucketReader` on `marin-usage-logs` (`hai-gcp-models`) and on `oa-gcs-usage-scratch`.
- R2 read on `oa-gcs-usage-index` for the meta job's key. Either widen the existing token (today it's presumably scoped to `oa-cw-s3-usage-index`) or mint a separate read-only token.

### Open: (b) AWS, pending inventory

OA has five AWS accounts behind SSO, and their S3 holdings are unknown. Any substantial S3 there would likely be a **new deployment**, because the audience is OA engineering/product (e.g. `fomo-prod`) rather than Marin. The `cloud` base's S3 lister and per-bucket `profile:` (cross-account `buckets.yml`) already support it. The cost would be a new site/D1/Batch stack, cloned from cw-s3's `cf/` + `infra/` (≈2–3 days), plus a read-only IAM role per account that the scan job can assume. First step: Ryan logs in to SSO and lists the buckets.

### (c) Skip

- `marin-discord`: 23 MB.
- The non-Marin Stanford buckets in `hai-gcp-models`: ≈1.7 TB, a different owner and audience, and the job's grants should stay off them.
- `oa-transfer`, `oa-pulumi` and the appspot/cloudbuild/run-sources buckets: ≈0.
- The empty buckets listed above.
- `oa-auth-509611`: has no buckets.
- The `cfo` R2 account: unknown owner, and no listing access.

## Caveats

- `total_bytes` is live data only. The 7-day soft-delete retention on most buckets bills extra bytes after deletes, which matters most for the swept fleet.
- `marin-na`'s growth rate is an average since creation, not a measured trend. Its first scan establishes the real number.
- GCS and R2 costs are list-price approximations, not billing data.
