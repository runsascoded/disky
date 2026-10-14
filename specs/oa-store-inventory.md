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
| **`marin-na`** | **289 TB** | **3.28M** | 2026-03-12 | WNAM, Standard, private. Top-level prefixes: `finelog/`, `iris/`, `marin/`, `ot-agent/`, `tmp/`, `users/`. Lifecycle: 9 `tmp/ttl={1..7,14,30}d/` expirations plus 7-day multipart abort, mirroring the CAIOS rules. ≈1.6M Class A operations/day, not necessarily writes: listings and multipart operations also count. Marin's `config/coreweave.yaml` registers it under regional alias `na`; North America is the likely expansion, not an explicitly documented name. Finelog archives use it; default CW task config can instead use CAIOS. Oct-11 dashboard: 289.29 TB / 3.28M objects, with a nearly flat recent storage trace. Flat bytes do not prove no writes or reads. ≈$4.3K/mo at Standard list prices. |
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

Correction from the Oct-11 class-level query: these are **not all Standard**. Bucket default storage class is not the class of every object. The non-fleet Marin/Levanter/data-values scope has about 148.893 TB Standard with Autoclass off, plus Autoclass-managed 25.044 TB Archive, 18.315 TB Nearline, 0.128 TB Coldline and 0.0095 TB Standard. Flat size can motivate an ownership/retention review, but neither flat size nor current class proves abandonment. The earlier uniform-Standard cost/tiering rationale must not be applied to the cold slices.

Also in `hai-gcp-models`:
- `marin-usage-logs`: 5.5 TB, 30K objects, **+2.0 TB in 30 days**. This is gcs's own access-log sink (`ACCESS_LOG_BUCKET`), so it's disky's storage but not in `/meta`. `marin-usage-logs-acl` has no size series.
- Non-Marin Stanford buckets (`crfm-helm-*`, `codalab-*`, `cs324`, `pubmed-mosaic`, `aw-proxy-evals`, `relbench-*`, `tracem`, Cloud Functions/Run/Build buckets): about 1.7 TB total. These belong to a different owner and audience.
- Empty (verified by listing): `marin-us-west1` (though it's in gcs's `ACCESS_BUCKETS`), `marin-asia-northeast-1`, `marin-marina`, `marin-artifact-registry`, `levanter-empty`, `pubmedgpt`, `model_spec`, `crfm-helm-dev`.

**GCS `oa-internal-450019`**: `oa-gcs-usage-scratch` (0.44 TB, 40K objects, disky's scratch, not in `/meta`), `oa-transfer` (0.08 TB), `oa-pulumi` and the appspot/cloudbuild/run-sources buckets (≈0). `oa-marin-dna-us-central1` is empty.

**GCS `oa-auth-509611`**: no buckets.

**AWS (OA)**: `~/.aws/config` has an `openathena` SSO session with profiles `oa-management`, `oa-ci-dev`, `oa-ci-prod`, `general-dev` and `fomo-prod` (an AWS Organization). The SSO token has expired, so **no bucket inventory was possible**: logging in is interactive and was not attempted. To finish this section, Ryan runs `aws sso login --sso-session openathena` and then `aws s3api list-buckets --profile <p>` for each profile. Sizes come from CloudWatch `AWS/S3 BucketSizeBytes` / `NumberOfObjects`, which is cheap.

**Other S3-compatible**: profile `cfo` points at an R2 endpoint in a different Cloudflare account from the OA dashboard account. `ListBuckets` is denied, which suggests a bucket-scoped key. Its owner and purpose are unknown. No other OA S3 endpoints are evidenced.

## Recommendations

### 1. (a) Prepare an independent OA R2 deployment (highest value)

The Oct-10 secondary-store-on-CW recommendation is superseded by Ryan's Oct-11 account-scoped direction: `r2.oa.dev`, separate from CW and GCS, with shared engine/site code. The OA account's Sep-7–Oct-6 completed bill was $4,565.99: storage $4,340.75, Class A $216, Class B $9 and Workers CPU $0.24. These are account totals, not a per-bucket bill, though `marin-na` accounts for almost all observed storage. It exceeds either `marin-us-west4` or `marin-us-east1` alone, not their combined size.

- **Deployment boundary:** own deployment branches, IaC stack, D1, derived-output R2 bucket, credential roles, schedule and cost attribution. Reuse shared readers, listing/import code and generic job plumbing rather than maintaining an R2-specific feature fork. Ryan clarified that CW is OA-only, so reusing that viewer policy is a reasonable proposed default; the separate deployment still needs its policy explicitly configured.
- **Store/view boundary:** a deployment is the host/auth/infrastructure boundary; a store is an indexed root and its scan history; a view is a query/lens over a store. Index the OA account's four approved buckets, keep `/` as all buckets and link people directly to `/marin-na`; no alias redirects. Multiple stores within this deployment may share its D1 and derived bucket with namespaced prefixes. Its `/meta` should describe this pipeline's own footprint, separately from the OA account-wide source-storage/billing view.
- **First milestone:** one metadata-only remote scan, existing treemap/drill/diff machinery and a last-modified histogram. R2 listing supplies `LastModified`; the S3 adapter stores it in the canonical `created` column, but it is not a true object birth time. Validate listing/build/publication totals and failures before enabling a daily schedule. At 3.28M objects and 1,000 keys/page, a simple daily listing is about 3,280 Class A requests ($0.015 at Standard list prices), excluding adaptive discovery, index IO and compute.
- **Access:** dedicated source object-read credentials for the approved buckets; separate derived-index publisher credentials. No source credentials in the site. Choose remote job placement explicitly rather than inheriting CW's GCP service account or adding unrelated access scopes. Lifecycle inspection may need additional permissions; do not broaden the scan key just to render lifecycle rules.
- **Access logging:** [R2 Data Access Logs] now cover supported S3/API/Workers/public object operations. They are not S3 `PutBucketLogging` and do not require proxying existing S3 clients. Logs include only status codes below 400, delivery is asynchronous/best-effort, retention is seven days, and enabling does not backfill history. Actor/access-key identity is useful attribution but shared keys are not individual users. No logging setting was changed. Verify an export/query ingestion path before promising a persistent last-read index; absent events must never be interpreted as proof of no reads.
- **Logging cost gate:** [Observability pricing] begins December 1, 2026, with Paid account allowances shared across supported datasets: 50 GB ingestion/cycle and 10 GB-month storage, then $0.25/GB and $0.10/GB-month. Measure actual event bytes before projecting costs. A pilot on the personal R2 demo is lower risk, followed by an explicitly approved `marin-na` pilot.

### 2. (a) Add the ~190 TB of non-fleet Marin/Levanter GCS buckets to gcs

These are the same project and provider as the fleet; confirm ownership and intended viewer access before adding them. The earlier $4–5K/mo estimate assumed uniform Standard class and is not established by actual class-level reporting. Their flat footprint motivates a retention review, not a conclusion that they are inactive. The Oct-10 inventory counted roughly 11.6M objects, about 3% of the fleet's listing volume.

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

- The original `storage/total_bytes` census was live-only. The Oct-11 `storage/v2/total_bytes` / `total_count` query separates live, soft-deleted, noncurrent and multipart storage. Across the six GCS fleet buckets it reports about 750.524 TB / 200.600M soft-deleted objects, in addition to live bytes; multipart series report at least 51.7 GB (one bucket's byte series is missing). These are latest daily gauges, not an instantaneous exact object census; see `tmp/gcs-tier-summary.json` and `tmp/oa-storage-report-2026-10-11.md`.
- Normal GCS/S3/R2 listings index live objects, not soft-deleted generations, noncurrent versions or incomplete MPU parts. The existing lifecycle-rule display and GCS sweep recovery tooling do not constitute an index of those billable states. Start with provider aggregate gauges, then add separate state-aware inventories with generation/version/upload identities; do not silently mix retained bytes into the live treemap. MPU support needs provider-specific listing/part-size APIs, and an in-progress upload is not necessarily abandoned.
- `marin-na`'s flat storage trace does not measure churn or access. A first listing establishes current last-modified distribution; subsequent scans and access logs answer different questions.
- GCS cost estimates remain list-price approximations. The completed OA R2 billing cycle above is actual account billing supplied by Ryan, not an exact attribution to `marin-na`.

[R2 Data Access Logs]: https://developers.cloudflare.com/r2/buckets/data-access-logs/
[Observability pricing]: https://developers.cloudflare.com/observability/pricing/
