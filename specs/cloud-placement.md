# Cloud placement: where each step runs, what it reads, and what crosses providers

**Status:** audit, 2026-10-10. Read-only. Code read from `cloud` (root checkout), `gcs` @ `4c2661f5`, `cw-s3` @ `4de39d7d` and `m3` via `git show`. No bucket, D1, KV or billing data was read: the billing export dataset has no tables yet (`bq ls` is empty; enabling it needs the billing account's Costs Manager, `cost-labels.md` §"Billing export to BigQuery"), and bucket-location lookups failed from this sandbox. So every dollar figure below is an **estimate** from list prices and the sizes recorded in specs. File refs are to `cloud` unless prefixed `gcs:`, `cw:` or `m3:`.

Builds on: [`index-landscape.md`] §1 (what each index is and where it lives), [`interval-store.md`], [`static-append.md`] ("Egress: merges re-upload"), [`latency-observability.md`] §3.5, `cw:specs/storage-consolidation.md`, [`done/r2-serving-migration.md`], and the `diff-perf` / `path-index-footer-in-d1` memories.

## Principles, and one correction

- **Workers read R2.** R2 has no egress fee and sits next to the Worker. A Worker reading GCS pays GCS internet egress and a cross-provider round trip on every read.
- **Jobs run where their input lives.** Ingress is free everywhere. The bill is for bytes *leaving* a provider (or a GCP region).
- **Correction to `interval-store.md:329`** ("Writing the runs to R2 directly from the job would avoid the egress"): it wouldn't. GCP bills internet egress on every byte that leaves GCP, whether a Batch VM uploads it to R2 directly or a later step copies it out of GCS. `static-append.md` already corrected this for the static index. A direct write saves the GCS copy (storage) and one hop, not the egress. **Only compute outside GCP saves egress on bytes headed for R2**, and only if its own provider doesn't bill egress (CoreWeave and GitHub-hosted runners don't; AWS does, $0.09/GB).

Prices used (list, verify against the export's SKUs once it exists):

| Flow | $/GB | Note |
|---|---:|---|
| GCS / GCE → internet (incl. R2, Cloudflare Workers), North America, first TiB | 0.12 | premium tier. Cloudflare is a GCP CDN Interconnect partner; if traffic to it is billed at the interconnect rate (~$0.04–0.05), every egress figure here is ~2.5–3× too high. Only the export's SKU names settle it |
| GCP inter-region, within North America | 0.02 | us-central1 ↔ us-east1 (VM ↔ bucket counts) |
| GCP inter-region, Europe ↔ North America | 0.05 | europe-west4 listing node → us-east1 bucket |
| CoreWeave CAIOS egress | 0 | per `static-append.md`'s lever (a) ("CoreWeave doesn't bill internet egress"); not checked against the contract |
| R2 egress | 0 | Class A/B operations only |
| AWS → internet | 0.09 | first 100 GB/month per account free |

## 0. Placement at a glance

| | gcs (gcs.oa.dev) | cw (cw-s3.oa.dev) | r2 (r2.rbw.sh) | m3 (laptop) |
|---|---|---|---|---|
| Scanned data | GCS: 6 Marin buckets (us-central2/central1/east1/east5/west4, eu-west4) | CoreWeave CAIOS: 5 buckets (US-EAST-02A/06A/08A, US-WEST-04A) | R2: 3 public buckets (2 accounts) | laptop disk |
| Job compute | GCP Batch, **us-central1** (listing: us-central1, eu-west4's in europe-west4); static + interval appends: Batch **us-east1** | GCP Batch **us-central1**; static appends Batch us-east1 | GitHub Actions | laptop capture → AWS Batch Fargate Spot, us-east-1 |
| Job data bucket | GCS `oa-gcs-usage-dvx`, **US-EAST1** (`gcs:infra/gcp/__main__.py`) | the same GCS bucket (`cw-l2/`, `snapshots/cw/`) | R2 `disk-tree-demo` | R2 `disk-tree` |
| Worker per-scan store (`STORE_*` seam) | **GCS** via S3-compat + HMAC | R2 via its S3 endpoint | R2 via S3 endpoint | R2 via S3 endpoint |
| `INDEX_R2` binding | `oa-gcs-usage-index`: interval store + static names | `oa-cw-s3-usage-index`: static names | — | — |
| D1 / KV | D1 + `CACHE_KV` | D1 + `CACHE_KV` | D1 | D1 |

So **gcs is the only deployment whose Workers still read GCS**. cw moved its whole served subset to R2 on 2026-09-28; r2 and m3 never touched GCS.

## 1. Serving: every Pages Functions read, per deployment

Seams (`_lib/index.ts`): `makeStore` / `storeTarget` / `storeCreds` → `STORE_ENDPOINT` (unset = `https://storage.googleapis.com`) + `STORE_BUCKET` + `STORE_*` creds (fallback `GCS_HMAC_*`). `INDEX_R2` is the R2 binding. D1 is `DB`, KV is `CACHE_KV`.

| Read | Code | gcs | cw | r2 | m3 |
|---|---|---|---|---|---|
| `/data/scans.json` (scan picker; **every page load, then a 5-min poll**): a LIST of `snapshots/` ∩ D1 `pathScans` | `data/[[path]].ts:52-75` | **GCS LIST** + D1 | R2 LIST + D1 | R2 + D1 | R2 + D1 |
| `/data/<scan>/meta.json`, `lifecycle.json` (home page; `useScanTimes` for same-day scans) | `data/[[path]].ts`, `src/scan.ts:219`, `LifecycleFold.tsx:26` | **GCS** | R2 | R2 | R2 |
| `rules.json` → identity registry (lens / pool views, OG, `/me`, estate; 10-min isolate memo) | `identity.ts:27` | **GCS** | R2 (none published) | R2 (none) | R2 (none) |
| `scanTimes` (`d=` prefix on a day with >1 scan; OG) | `scanTimes.ts:16`, `scanArg.ts:28` | **GCS** meta.json | R2 | R2 | R2 |
| Path store: subtree / diff / series / owners / prefixes, for scans the interval store holds | `openInterval` `index.ts:662`, `openIvRun` `:713` | **R2 binding** (`PATH_STORE=intervals`, gen `2026-10-09b` + runs) | — | — | — |
| Path store, per-scan (scans the iv store doesn't hold; every read where it's off) | `openIndex` `index.ts:397` → `fileFor` `:369` | **GCS**, fallback only: a scan published but not yet appended (the appends run before `index-sync`, so normally none), any sort the gen lacks (`age-*`, `over-time`, unsliced `user`) | R2 (S3 endpoint) | R2 | R2 |
| Footers | D1 `index_row_groups`; cold `.groups.parquet` `index.ts:933`; iv footer `ivFooter` `:486` | D1 (30-scan hot window) / **GCS** cold footers (fallback only) / R2 (iv) | D1 | D1 | D1 |
| Extras `attr.tsv` + `.idx.json` (provenance), **awaited beside `readView` in every `buildView`** | `extras.ts:40`, `view.ts:1695` | **GCS**: ~3.15 MB per bucket view, the remaining **~400 ms** of a warm bucket view (`interval-store.md:423`) | R2 404 probe, 60 s miss memo | same | same |
| Search sidecars probe (`perScan()`; filter fallback chain) | `search.ts:96`, `view.ts:842,894` | **GCS** 404 probe per scan per 60 s (gcs builds none) | R2 404 probe | R2 (built) | R2 404 probe |
| Series: `unindexedScans` LIST + `meta.json` points | `api/series.ts:37-62` | **GCS** LIST + GETs (edge-cached result) | R2 | R2 | R2 |
| Over-time groups | `overTime.ts:67` | — | R2 | — | — |
| Age pyramid | `api/age-pyramid.ts:52` | not built (D1 "not synced", no read) | R2 (~0.06 GB/scan, 8 bins) | — | — |
| Read lens `last_read` | iv `path` column; `ivLastRead` `index.ts:733` only when `path` lacks it | R2 (folded into the gen) | — | — | — |
| Static name index (`?f=`, `/names`) | `staticFilter.ts:257`, `nameSummaryStatic.ts:144` | R2 binding | R2 binding | — | — |
| `/api/path-index` (raw index download) | `api/path-index.ts:53` | **GCS** | R2 | R2 | R2 |
| `/v1/files` (data-bucket browser) | `v1/files/[[path]].ts:35` | **GCS** (`listing/`, `snapshots/`, `sweep/`) | R2 (`listing/`, `sweep/` 404: not published) | R2 | R2 |
| `/v1/objects` (scanned-bucket browser) | `v1/objects/[[path]].ts:35` | **GCS** Marin buckets: inherent (it browses the scanned data) | — | — | — |
| OG cards | `og/data.ts:67` (+ subtree reads) | **GCS** snapshot JSON + registry; data via iv/R2 | R2 | R2 | R2 |
| `/staged`, plans, `owner_totals`, `scan_runs`, ledger, auth | `staged.ts`, `api/plans/*`, `ownerTotals.ts` | D1 (owner totals read the iv slice sorts on R2) | D1 | D1 | D1 |
| Plan / sweep dispatch | `planDispatch.ts:52`, `sweepDispatch.ts:225`, `gcp.ts` | GCS **write** (plan JSON, Batch input; ingress, free) + Batch API | GCP Batch API (`cwBatch.ts`) | — | — |
| Sweep progress / logs | `runReflect.ts:43`, `api/sweep/jobs.ts:112`, `api/sweep/logs.ts:28` | **GCS** JSON API (`sweep/runs/`), Cloud Logging API; admin-only, KB | GCS JSON API, Cloud Logging | — | — |
| Edge cache / KV | `edgeCache.ts` | colo + KV | colo + KV | colo | colo |

**What gcs still reads from GCS on a Worker**, now that path reads are on the interval store:
1. `scans.json` (a GCS LIST) on every page load and every 5-minute poll: **on the critical path of first paint**, since no view request can be built before the scan id is known.
2. `meta.json` (+ `lifecycle.json`, `rules.json` for lens views): the home page's header and the lens registry.
3. **Extras (`attr.tsv`) on every subtree**, awaited in parallel with the view: the biggest remaining latency term on warm bucket views (~400 ms, 3.15 MB).
4. The per-scan fallback (scans not yet appended, sorts the gen lacks, `/api/path-index`) and its cold footers: rare in steady state.
5. Search-sidecar 404 probes (cheap, but a cross-provider round trip per scan per isolate per minute on filtered views).
6. Series' unindexed-scan LIST and `meta.json` points; OG cards' snapshot JSON.
7. Admin/control plane (sweep records, Batch, Logging, plan writes): GCP by nature, KB-scale, fine where it is.

**cw**: no GCS reads on the data path. Only the admin control plane (Batch API, sweep reflect from GCS, Logging). Its per-scan reads go through R2's S3 endpoint rather than the binding; that is same-provider (no egress), slightly more latency than the binding (SigV4, public endpoint). Not worth changing until per-scan stores are retired there.

**r2, m3**: R2 and D1 only.

## 2. Pipelines: every job stage, per deployment

"X-region" = GCP inter-region; "X-provider" = bytes leave a provider. GB are per scan unless noted.

### 2.1 gcs (daily, 07:00 UTC; `gcs:job/run.sh`)

| Step | Runs in | Reads from | Writes to | Cross? | Est. GB |
|---|---|---|---|---|---:|
| Access-log ingest (`dt-cloud access ingest`, overlapped with listing) | snapshot VM, us-central1 | `gs://marin-usage-logs` (Stanford's project; **location unknown**), ~240 GB CSV/day | `access/raw` (~34 GB/day) + `access/agg` (~25 MiB) in us-east1 | X-region write (34 GB); the CSV read is X-region too **if** the log bucket isn't in us-central1 | 34 (+240?) |
| Listing (`job submit-listing`, one Batch task per bucket) | us-central1 (central2's nearest: Batch has no us-central2); eu-west4 in europe-west4 | the 6 fleet buckets (list API, ~600M objects) | `listing/<scan>/<bucket>/` in us-east1 | X-region writes (10.7 GB, part EU→NA); **list responses from us-central2/east1/east5/west4 to us-central1 are cross-region and unmeasured** | 10.7 (+ list bytes) |
| Stage (`dt-cloud stage`) | snapshot VM, us-central1 | listings + attribution + `access/agg` (us-east1) | local SSD | X-region read | ~11 |
| `path-index` + upload | us-central1 | local SSD | `listing/<scan>/index/<gen>/` (21.7 GB) + `dir-cache/` (19.1 GB, via gcsfuse) in us-east1 | X-region writes | 40.8 |
| Publish snapshot JSONs, `lifecycle pull`, `rules.json` | us-central1 | — | `snapshots/<scan>/` (us-east1) | X-region (MB) | ~0 |
| Static names `runs add -c` | Batch us-east1 (profile `region`) | the published `path` sort (in-region) | runs on GCS, then copied to R2 `oa-gcs-usage-index` | **X-provider** (GCS → R2) | run 3.25 (1.1 sx + 2.15 drill), **×~3 with merges**, + compaction |
| Interval store `append -c` | Batch us-east1 | published `path` (in-region) | runs + manifest on GCS → R2 | **X-provider** | 0.66 run, + binary-counter carries (pairwise, inline) |
| `index-sync` (footers → D1 over the HTTP API) | us-central1 | gcsfuse (us-east1) | D1 | X-provider (~218 MB of rows as JSON, est. 0.3–0.5 GB) | ~0.4 |
| Healthcheck, `warm-cache`, `probe -c` | us-central1 | the site (Workers) | probe records → GCS | X-provider (responses, MB) | ~0 |
| Slack + Discord digests | us-central1 | `snapshots/` | Slack, Discord, `wrangler pages deploy` of icons | X-provider (KB–MB) | ~0 |
| `index-gc -r 30` | us-central1 | D1 | D1 | — | 0 |
| Sheet mirror (hourly Cloud Run job) | us-central1 | the site's API | Google Sheets | X-provider (KB) | ~0 |
| Sweeps / executor (on demand from `/staged`) | Batch in the target bucket's region (us-central1 for central2) | manifest from `oa-gcs-usage-dvx` (central2's 2.31 GiB) | deletes on the fleet bucket; run records → GCS | X-region manifest read | occasional |

Per scan: **~97 GB crosses us-central1 ↔ us-east1** (34 access + 10.7 listing + 11 stage + 40.8 path-index/dir-cache), plus the unmeasured list-response and log-CSV terms. **~4–30 GB leaves GCP for R2** (below).

### 2.2 cw (every 6 h; `cw:job/cw-run.sh`; meta self-scan daily 09:00)

| Step | Runs in | Reads from | Writes to | Cross? | Est. GB |
|---|---|---|---|---|---:|
| CAIOS bucket inventory check | Batch us-central1 (n2-standard-8) | CAIOS list | — | X-provider (CoreWeave → GCP, unbilled) | ~0 |
| `bulk-list` | us-central1 | CAIOS (5 buckets, ~92M objects; 26 min) | local disk (~10 GB shards) | X-provider listing, every page pays CoreWeave-east ↔ us-central1 RTT | list bytes |
| `import` + webdata + `index-write` | us-central1 | local | local | — | 0 |
| Publish snapshots + `cw-l2/<scan>/` (rollup 1.3 GB, `index/<gen>/` 2.7 GB) | us-central1 | local | GCS us-east1 | X-region | ~4.1 |
| `index-sync`, `index-gc` | us-central1 | gcsfuse | D1 | X-provider (small) | ~0.05 |
| `publish-r2 -L` (served subset) | us-central1 | GCS us-east1 | R2 `oa-cw-s3-usage-index` | X-region read **and** X-provider | 2.7 |
| Over-time groups (every 16th scan) | us-central1 | 16 scans' `path-index` from GCS (~21 GB) | group on GCS → R2 (`publish-r2 -L` since the storage-consolidation fix) | X-region + X-provider | ~1.3 X-region, ~0.05 X-provider per scan, amortized |
| `index-gc -R -F` (GCS + R2 file GC) | us-central1 | D1, both buckets | deletes | — | 0 |
| Static names (`STATIC_NAMES=1` in the cron body, `cw:job/cw-batch-submit.sh:78`) | Batch us-east1 | `cw-l2/<scan>/` `path` | runs on GCS → R2 | **X-provider** | 0.1–0.8 sx (post hex rule) + drill (unmeasured), ×~3 with merges, + compaction |
| `warm-cache`, `probe -c`, Slack digest | us-central1 | site; `snapshots/cw/` | Slack, icons Pages, `probes/cw/` on GCS | small | ~0 |
| Sweeps (`cw-sweep`) | Batch us-central1 | CAIOS | CAIOS deletes, records on GCS | X-provider (requests) | small |
| Meta self-scan (daily, n2-standard-4) | us-central1 | lists `oa-gcs-usage-dvx` (us-east1) + R2 `oa-cw-s3-usage-index` | `meta-l2/` on GCS → R2 | X-region list, X-provider (small) | <1/day |

**The cw data path crosses three providers for no reason**: CAIOS → GCP us-central1 → GCS us-east1 → R2. Its input isn't on GCP and its output isn't served from GCP. GCP is just where the job image, service account and Batch scheduler already were (`cw-run.sh:9-12`).

### 2.3 r2 demo (daily, 07:15 UTC; `.github/workflows/daily-ingest.yml`)

GitHub Actions (Azure, US) lists 3 R2 buckets (~1.03M objects, two Cloudflare accounts) → `dt-cloud path-index -S` → uploads ~35 MB (`path` 12 MiB + search sidecars 21 MiB + `bysize`) to R2 `disk-tree-demo` → `index-sync` to D1. **No billed egress anywhere** (R2 egress is free; GitHub doesn't bill runner egress). Nothing to fix.

### 2.4 m3 (per capture; `m3:infra/aws/ingest.sh`)

The laptop uploads a capture's shards to R2 `disk-tree` → a Cloudflare Worker (`capture-trigger`) submits AWS Batch (Fargate Spot, us-east-1) → it downloads the shards from R2 (free), runs `path-index -g`, uploads index + snapshot to R2, and syncs D1. The only billed flow is the **AWS → R2 upload** at $0.09/GB, past AWS's 100 GB/month free tier. A laptop capture's index is ~0.1–1 GB, so it stays inside the free tier unless captures are frequent. Not worth moving (GitHub's public runners would expose laptop paths in public logs).

## 3. Costs

### 3.1 Cross-provider egress (estimates, $/month)

| Flow | Volume | $/month at $0.12/GB | Basis |
|---|---|---:|---|
| **gcs static names → R2**: runs + merge re-uploads (deferred N-way carries, ~35% fewer than pairwise) | ~10 GB/day now; +~14 GB/day amortized once compaction runs on a schedule | **~35 now, ~85 with compaction** | `static-append.md` "Egress: merges re-upload" |
| **cw static names → R2** (4 scans/day, hex rule on) | ~5–14 GB/day + drill | **~20–50** + drill | same table, ×0.27 for the hex rule (`static-hex-runs.md`) |
| **cw `publish-r2`** | 2.7 GB × 4/day ≈ 324 GB/month | **~39** | `cw:specs/storage-consolidation.md` |
| gcs interval store → R2 | 0.66 GB/scan run; pairwise carries make it ~2–5× | ~2.5–12 | `interval-store.md:388` |
| gcs Workers → GCS reads (extras, snapshot JSON, LISTs, fallback, probes) | extras ~3 MB × a few hundred cold views/day, plus KB-scale JSON | ~1–10 | `interval-store.md:423`; traffic volume unmeasured |
| `index-sync` → D1 HTTP API (gcs + cw) | ~0.4 GB/scan gcs, small cw | ~1.5–2 | `path-index-footer-in-d1` |
| Latency bench (proposed, not built) per-scan cells | 1–2 GB/day | 4–7 | `latency-observability.md` §3.7; goes to ~0 once gcs per-scan reads are on R2 |
| One-offs: new static gens, re-cuts | 617.7 GB for `2026-10-08c` (≈ $74); cw `2026-10-10cw` 73.6 GB (≈ $9); an iv L4 re-cut ≈ $5; a names-into-`sx` regen ≈ $16 | per event | `ch-store-retirement.md:153`, `static-hex-runs.md:127` |
| m3 AWS → R2 | within AWS's free 100 GB/month | ~0 | — |
| **Total recurring** | | **≈ $100–200/month** | ≈ $35–85 of it gcs static, ≈ $60–90 cw |

CAIOS → GCP (cw listing and sweeps) is unbilled by CoreWeave per the specs; GCP ingress is free.

### 3.2 GCP inter-region (us-central1 ↔ us-east1), $/month

| Flow | GB/month | $/month at $0.02 |
|---|---:|---:|
| gcs snapshot job: access/raw write, listing writes, stage reads, path-index + dir-cache writes (~97 GB/scan) | ~2,900 | **~58** |
| gcs list responses (fleet buckets → us-central1) and the access-log CSV read (~240 GB/day, if the log bucket isn't in us-central1) | unknown | **0 to ~145** |
| cw: publish to GCS (4.1) + `publish-r2` read back (2.7) + over-time reads (1.3), ×4/day | ~1,000 | **~20** |

The inter-region line is the one nobody has looked at: the data bucket was put in US-EAST1 (next to the static/interval Batch work), but both crons and the listing nodes run in us-central1 (`Pulumi.{gcs,cw-s3}.yaml`: `gcp:region: us-central1`).

### 3.3 Latency (measured in the specs)

| Term | Measured | Source |
|---|---|---|
| Fleet-root diff walk (~10 serial dependent rounds, 44 range GETs), uncached | **R2 89–120 ms vs GCS 4.1–4.9 s (~40×)** | `diff-perf` memory 9/25; `done/r2-serving-migration.md` |
| Single coarse tier read | R2 160–175 ms vs GCS 180–340 ms | same |
| Point lookups | R2 ~30% faster | same |
| Raw 16 MB range GET from GCS | ~0.6 s | `diff-perf` 9/23 |
| gcs warm bucket view, remaining term | ~400 ms = `extrasFor` 3.15 MB from GCS | `interval-store.md:423` |
| gcs first paint: `scans.json` GCS LIST + `meta.json` GET before/beside the first view | unmeasured; one cross-provider round trip each (est. 100–300 ms) | — |
| A/B confound: per-scan (GCS) vs interval (R2) timings mix layout and provider | — | `latency-observability.md` §3.5 |

The dollar cost of the Workers' GCS reads is small. Their cost is latency: the extras read alone is ~400 ms on every warm bucket view, and the `scans.json` LIST gates every first paint.

## 4. Recommendations (ranked by value ÷ work)

| # | Change | Saves | Work | Who | Ryan's approval |
|---:|---|---|---|---|---|
| 1 | **gcs: get the last data-path GCS reads off the Worker.** (a) `scans.json` from D1 alone: the Function already intersects the LIST with D1's `pathScans`; make D1 the list and drop the LIST. (b) Publish `snapshots/<scan>/*.json`, `snapshots/rules.json` and the gen dir's `attr.tsv{,.idx.json}` to `oa-gcs-usage-index` (`dt-cloud publish-r2 -p …` from the job, a few MB/scan, < $0.01), and read them through an R2 seam: either gcs's `STORE_*` pointed at R2 as cw did (then the per-scan fallback must also be on R2, or be retired, see #6), or a second, R2-only snapshot/extras seam. (c) Or **drop extras from the default view**: make `extrasFor` lazy (only for a lens or provenance tooltip) instead of awaited on every `buildView` | ~400 ms on warm bucket views; one cross-provider round trip off every first paint; ~$1–10/month | S–M. (a) and (c) are code on `cloud`; (b) adds a job step and wrangler config on gcs | root (`cloud`: D1-only `scans.json`, lazy extras, the seam); gcs session (job step, `wrangler.toml`) | gcs deploy |
| 2 | **Colocate the gcs and cw crons with the data bucket.** Move the snapshot jobs (and cw's listing, which is nearer CoreWeave's US-East zones from us-east1 anyway) to us-east1, or move the bucket. **First check `marin-usage-logs`' location**: if it's us-central1, keep the access ingest there (or move just its output to a us-central1 bucket). Listing of central2 stays in us-central1 (Batch has no us-central2), writing its shards cross-region either way | ~$58 + ~$20/month inter-region, and possibly the unknown ~$0–145 | S: `gcp:region` in `infra/gcp/Pulumi.*.yaml`, `REGION=` in the submit scripts, Batch quota for n2-highmem-16 + local SSD in us-east1, image repo region (pull cost is small) | gcs and cw sessions (`infra/gcp`, `job/`) | yes (prod cron moves) |
| 3 | **Move cw's whole pipeline off GCP, onto CoreWeave.** List CAIOS from a node in its own zone, import/index locally, write the served subset straight to R2, and keep the rollups in R2 (or CAIOS) instead of GCS. This also takes cw's static-name merges off GCP (lever (a) in `static-append.md`) | ~$39 (`publish-r2`) + ~$20–50 (static names) + ~$20 inter-region ≈ **$80–110/month**; shorter listings (in-zone RTT); retires cw's GCS footprint (~2.75 GB/scan of duplicate tiers) | L: a CoreWeave node or k8s CronJob instead of Batch, secrets (R2 RW, D1 token, CAIOS key, Slack), scan-run records, alerting. `blobfs` already speaks `r2://`, and the meta scan already lists R2 | cw session (job + infra); root only for generic pieces (an R2-native layer-2 target, no GCS assumptions in `publish`/`over-time-groups`) | yes: CoreWeave compute, credentials, ops ownership |
| 4 | **gcs static names: stop re-uploading merges from GCP.** Either (a) run the merge/compaction stages on non-GCP compute reading runs from R2 (a CoreWeave node, as #3; **not** GitHub's public runners, whose logs are public and would expose OA path names), or (b) a base-4 counter plus bounded compaction (`static-append.md` levers (b) and recommendation) | ~$20–60/month (merges and compaction re-uploads; first uploads stay) | M: (b) is runner config/code on `cloud`; (a) needs #3's node | root (`cloud`: runner) | (a) yes; (b) no, beyond the usual go |
| 5 | **Interval store: same lever as #4**, at its unification with the static runner (`index-landscape.md` recommendation 1): deferred N-way carries instead of pairwise inline ones | ~$2–10/month | S once the shared runner lands | root | no |
| 6 | **Retire gcs per-scan GCS serving** for scans the interval store holds (`index-landscape.md` §5 removal list: scan existence from the store's `scans.json`, search off `perScan()`), keeping the newest few scans as fallback (`interval-store.md:205`) | removes the GCS fallback reads and probes; ~13 GB/scan of per-scan `bysize` storage; makes #1(b)'s "point `STORE_*` at R2" trivial | M | root (`cloud`), then gcs config | yes (deletes data) |
| 7 | **cw quick fixes already specified** (`cw:specs/storage-consolidation.md` phase 1): `publish-r2` only the D1-pointed generation; delete superseded generations in both buckets; delete GCS tiers after a verified R2 publish (once over-time reads R2) | storage (~26 GB × 2 once, ~2.75 GB/scan), a little egress | S | cw session (and root for `publish`/`index-gc` code) | yes (deletes) |
| 8 | **Turn on the billing export** (Costs Manager action, `cost-labels.md`) and re-cost this table from SKUs: inter-region vs internet vs CDN Interconnect egress, and the two unknowns (list responses, log CSVs) | settles which of the above are real | XS for the admin | Ryan forwards the request | yes |

Not recommended:
- **Writing GCP job outputs straight to R2 "to avoid egress"**: it saves GCS storage and a hop, not egress (see the correction above). Worth it only as part of #3/#4 (compute outside GCP), or to drop a GCS copy nobody reads.
- **Moving gcs's listing or aggregation off GCP**: its input is on GCS. Listing ~600M objects from outside GCP would turn free in-GCP list traffic into egress, and the aggregation reads ~11 GB of listings per scan.
- **cw per-scan reads through an R2 binding instead of the S3 endpoint**: same provider, small latency gain; moot once per-scan stores are retired.

## 5. Open questions (need live state or Ryan)

- `marin-usage-logs`' location (decides #2's shape) and the fleet buckets' list-response volume.
- Whether GCP bills egress to Cloudflare at the CDN Interconnect rate (would cut §3.1 by ~2.5–3×).
- CoreWeave's actual CAIOS egress terms on OA's contract (assumed $0, per the specs).
- Whether `PATH_STORE=intervals` is deployed on gcs prod (the branch config says so, `gcs` `1db67e1a`; `index-landscape.md` didn't check the deploy either).

[`index-landscape.md`]: index-landscape.md
[`interval-store.md`]: interval-store.md
[`static-append.md`]: done/static-append.md
[`latency-observability.md`]: latency-observability.md
[`done/r2-serving-migration.md`]: done/r2-serving-migration.md
