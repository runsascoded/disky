# Serving options for filters, diffs and history: a platform survey

Status: research (2026-10-03), plus benchmarks A′ and B measured the same day (§5, §6). It answers "could Lambda / Cloud Run / Modal / a real database replace the [serving box][filter-query-service] or the Worker?". Every price and limit below links to its source. Where a number isn't documented, the text says so instead of guessing. Estimates (marked *est.*) are mine and are what the proposed benchmark would check.

## Baseline (from our own measurements)

- **The Worker** (Pages Functions: 128 MB per isolate, 6 concurrent subrequests): 71 of 105 bench queries come back truncated, with a 6.9 s median.
- **The custom in-memory index on an 8-vCPU VM:** all 105 exact; p50 0.44 s, p90 2.1 s, max 5.2 s. It takes 41 GB of RAM per scan and 138 s to load from GCS.
- **DuckDB names-first over parquet** on the same VM: p50 2.0 s, max 91 s.
- **One scan:** 778M rows and 127M distinct names. The `path` column is 3.6 GiB compressed but 95 GiB decoded. The names file is 1.56 GB, and the vocabulary takes 6.6 GB as Arrow ([phase 0][p0]). Templating digit and hex runs collapses the 127M names to 2.16M templates ([phase 0][p0]).
- **Churn:** gcs's measured per-scan churn is about 0.1–0.2% of rows. A 16-scan SCD-2 consolidation holds 1.027 scan-equivalents of object rows and 1.057 of dir rows ([path-store §4.5][path-store]).

## Summary

Monthly costs assume 1k–5k queries a day ("moderate"). "Idle" means no queries all month. *Est.* marks a figure not yet benchmarked.

| option | broad-filter latency | cold start | $/mo idle | $/mo moderate | 1 scan / 2 scans / history | ops burden |
|---|---|---|---:|---:|---|---|
| Worker (status quo) | 6.9 s median, 71/105 truncated | none | ~0 | ~0 | yes / yes / yes (reads parquet) but truncated | low |
| Serving box, custom index, n2-highmem-8 on demand | 0.44 / 2.1 / 5.2 s (measured) | 138 s, only on restart | 383 | 383 | yes / no (2×41 GB > 64 GB) / no | medium |
| Same box, e2-highmem-8 with a 1-yr commitment | same *est.* (e2 is slower per core) | 138 s on restart | 190 | 190 | yes / no / no | medium |
| Same box on GCE **suspend/resume** | warm: same | resume time undocumented: **measure it** | memory + disk storage only | 0.36–0.52 $/active-h + storage | yes / no / no | medium |
| Same box on **Spot**, with the Worker as fallback | same while up | 138 s after each preemption | up to 91% off on-demand | same | yes / no / no | medium |
| Lambda, one 10 GB function (names-first, S3 range reads) | 2–5 s *est.* | 10–40 s to load the vocabulary; SnapStart restore at multi-GB undocumented | ~0 + GCS→S3 copy (70–150) | 20–60 + copy | vocabulary only; tree read from S3 per query | high (two clouds) |
| Lambda fan-out ("serverless MPP") brute-force `path` scan | 2–4 s *est.* | Python+DuckDB ~1.9 s per cold worker; Rust ~15 ms | ~0 + copy | 1,300–6,600 + copy | yes / yes / yes | high |
| Lambda fan-out, names-first shards | 2–4 s *est.* | as above | ~0 + copy | 90–450 + copy | yes / yes / yes | high |
| Lambda **MicroVMs** (≤ 16 vCPU / 32 GB, suspend/resume) | warm ≈ custom index if slimmed to ≤ ~28 GB *est.* | "within seconds", undocumented at 32 GB | snapshot storage ~3 + copy | 2.02 $/active-h; 120–360 + copy | only if slimmed / no / no | high (launched July 2026) |
| Cloud Run (≤ 8 vCPU / 32 GiB) | doesn't fit 41 GB | = index load (138 s); no snapshot | 0 (or 263 with 1 min instance) | 263+ with a min instance | only if slimmed / no / no | low |
| Cloud Run fan-out over GCS | ≈ Lambda fan-out *est.* | container start undocumented | 0 | ≈ Lambda fan-out, no egress | yes / yes / yes | medium-high |
| GKE Pod Snapshots (gVisor, lazy restore) | warm: same as box | undocumented for 40 GB of CPU memory (GPU examples: 15–37 s) | cluster fee + snapshot storage | node-hours | yes / no / no | high |
| Modal, memory snapshot | warm: same as box | ≥ 20 s for 40 GB at ≤ 2 GiB/s *est.*; documented snapshots go up to 10 GiB | 0 | 0.76 $/active-h × region/non-preemptible multipliers | yes (max memory undocumented) / ? / no | medium |
| Aurora Serverless v2 PG + `pg_trgm` | seconds to minutes *est.* | ~15 s resume (30 s+ after 24 h idle), then a cold cache | storage ~15–40 | 3.84 $/active-h at 32 ACU; ~460 at 4 h/day | yes / yes / yes (SCD-2) | medium |
| BigQuery on demand | 2–5 s *est.* | none | storage, a few $ | ~900–18,000 | yes / yes / yes | low |
| BigQuery editions (Standard, autoscaled) | 2–5 s *est.* | slots scale in seconds | storage only | ~100–500 *est.* | yes / yes / yes | low |
| ClickHouse, self-hosted, disk-backed VM | 0.5–3 s *est.* | none (always on) | 132–264 + disk | same | yes / yes / yes (SCD-2) | medium |
| ClickHouse Cloud (Scale) | as self-hosted once warm *est.* | wake undocumented, then a cold cache | storage | ~0.90 $/active-h for 24 GiB; ~110 at 4 h/day | yes / yes / yes | low |
| MotherDuck (Business) | ≈ DuckDB names-first (p50 2.0 s, max 91 s) | ~100 ms (Jumbo) to a few s (Mega/Giga) | 250 | 250 + 4.80 $/h (Jumbo) | yes / yes / yes | low |

**Short version:** no serverless platform makes the custom index cheaper and as fast. The platforms that can hold it warm (Modal, Lambda MicroVMs, GKE Pod Snapshots, GCE suspend) all pay for a cold restore proportional to its 30–40 GB. None documents that restore at this size, and at about 2 GiB/s it is 15–20 s or more. Fan-out beats the Worker but not the in-memory index, and pays per byte it rescans. The bigger wins are design-side: shrink the index (templates, ids), and store history as SCD-2 intervals. Then a disk-backed database (ClickHouse) or the box on a cheaper footing (a commitment, Spot, or suspend) covers it.

## 1. AWS Lambda instead of Workers

### Limits

- **Memory and CPU:** 128–10,240 MB. CPU scales with memory: one vCPU at 1,769 MB, so about 5.8 vCPU at 10,240 MB ([quotas][lambda-quotas], [memory][lambda-mem]).
- **`/tmp`:** 512–10,240 MB ([quotas][lambda-quotas]).
- **Timeout:** 900 s ([quotas][lambda-quotas]).
- **Payloads:** 6 MB each way for a synchronous call. A streamed response can be 200 MB, uncapped bandwidth for the first 6 MB, then 2 MBps ([quotas][lambda-quotas]). Our `TreeNode` responses (1–2 MB) fit the plain 6 MB limit.
- **Network:** 625 Mbps per execution environment. Functions outside a VPC can request more, scaling with memory up to 3,000 Mbps at 10,240 MB ([quotas][lambda-quotas]). Lambada measured S3 scans saturating at about 90 MiB/s per worker ([Lambada][lambada]).
- **Concurrency:** 1,000 by default. Each function can add 1,000 execution environments every 10 s ([quotas][lambda-quotas]).
- **SnapStart:**
  - Supported on Python 3.12 and later.
  - Not with provisioned concurrency, EFS, or ephemeral storage above 512 MB ([SnapStart][snapstart]).
  - AWS promises startup "as low as sub-second", and warns that infrequently invoked functions "might not experience the same performance improvements" ([SnapStart][snapstart]).
  - No published restore time for a multi-GB snapshot. Community reports are of small functions going from seconds to under one second ([dev.to][snapstart-py]).
  - Price: a cache charge of $0.0000015046/GB-s (minimum 3 h) and $0.0001397998 per GB restored ([pricing][lambda-pricing]). A 10 GB snapshot costs about $39/month to keep cached per published version (one version a day) and $0.0014 per restore.
- **Cold starts:**
  - Python + DuckDB: one benchmark measured 1.85 s cold, including the `httpfs` install, against 0.45 s warm ([dev.to][duckdb-lambda]).
  - Rust (`provided.al2023`): community benchmarks put its init at roughly 15 ms p50, against about 90–170 ms for Python 3.13 ([edjgeek][rust-cold], [nandann][rust-cold2]).

**Does it help?** Against the Worker, yes: 80× the memory, about 6 vCPUs, 15 minutes and ~75 MiB/s from S3. One 10 GB function could pin the names vocabulary (6.6 GB as Arrow, less if templated) and run phase 0's names-first plan. It can't hold the custom index: 41 GB is four times the 10 GB cap.

### Fan-out ("serverless MPP")

**Published systems:**

- **[Lambada][lambada]:**
  - Answers TPC-H queries over S3 at interactive latency (under 10 s, hot or cold) with hundreds to thousands of workers.
  - Invoking 1,000 workers from one driver takes 3.4–4.4 s. Two-level invocation, where workers invoke workers, gets the last one started after about 2.5 s.
- **[Starling][starling]:** cheaper than provisioned systems only when queries arrive 30–60 s or more apart. Its fixed cost is a small coordinator.
- **[Boxer][boxer]:** adds direct function-to-function TCP (NAT punching): up to 11× TPC-H speedups, 621 Mbit/s and under 1 ms RTT between functions.
- **[Skyrise][skyrise]:** comparable to Lambada on scan-heavy queries. A review notes it reports no cold-start numbers.

**Applied to us:**

- **Brute-force `path` scan:**
  - ~110 workers each read about 75 MB of the 8.3 GB `path` file in about 1 s and decode about 0.9 GiB, which phase 0's per-thread rate puts at about 1 s on 6 vCPU.
  - With invocation overhead (0.4–2.5 s), that's 2–4 s *est.*
  - Cost: 110 × 3 s × 10 GB = 3,300 GB-s ≈ $0.044 at the arm rate of $0.0000133334/GB-s ([pricing][lambda-pricing]).
  - That's **$1,300–6,600/month at 1k–5k queries a day**, three to seventeen times the serving box.
- **Names-first fan-out:**
  - About 16 workers each scan a vocabulary shard (~100 MB), then fetch only the matched row groups (0.23–2.36M rows in [phase 0][p0]).
  - About $0.001–0.005 per query, so $90–450/month *est.*
  - Every invocation re-reads its shard unless the environment is warm, so p50 is about 2–4 s *est.*: like DuckDB names-first, not like the in-memory index.

**Verdict:** fan-out turns "truncated" into "exact in a few seconds", which the Worker can't do. It doesn't beat a warm in-memory index, and its cost grows with the bytes each query rescans. It is also a new distributed system: driver, shard plan, stragglers, retries.

### Lambda MicroVMs (new, July 2026)

- **What they are:** Firecracker VMs built from a Dockerfile. Lambda snapshots memory and disk after a `/ready` hook. Each VM gets its own HTTPS endpoint, auto-suspends when idle and auto-resumes on traffic ([concepts][microvm-concepts], [launch blog][microvm-blog]).
- **Limits:** the largest configuration is 16 vCPU / 32 GB ([quotas][lambda-quotas]). A VM runs at most 8 h ([quotas][lambda-quotas]). Arm (Graviton) only. The launch blog names us-east-1 as a region.
- **Pricing:** $0.0000276944 per vCPU-second, $0.0000036667 per GB-second, snapshot storage $0.08/GB-month, $0.00155/GB per snapshot read, $0.0038/GB per write ([pricing][lambda-pricing]).
  - At 16 vCPU / 32 GB that's **$2.02 per running hour**, about four times an n2-highmem-8.
  - Each resume reads about $0.05 of snapshot, and each suspend writes about $0.12.
- **Fit:** this is the closest AWS shape to "the box, scaled to zero". It holds one scan only if the index slims to about 28 GB (the [serving-box spec][filter-query-service] estimates 25–30 GB for the id arrays alone).
- **Unknowns:** resume latency ("within seconds", no figure at 32 GB) and whether memory restores lazily are both undocumented.

### Cost, egress and integration

- **Cost per query and month** (summary table): one 10 GB function at 3k queries a day × 2 s is 1.8M GB-s/month, about $19 after the free tier ([pricing][lambda-pricing]). Fan-out is $90–6,600, depending on the bytes rescanned.
- **The index has to move to S3.** GCS's general data transfer out of Google Cloud is **$0.12/GiB** for the first 10 TiB a month ([GCS pricing][gcs-pricing]). At 20–40 GiB per scan, copied daily, that's **$72–144/month**, plus S3 storage at $0.023/GB-month ([cloudzero][s3-price]): about $14–28/month for 30 days of scans. Copying only an SCD-2 delta (§4.6) would bring the egress to near zero, but then the index has to be rebuilt on AWS.
- **Integration:** the Worker stays the front door and calls a Function URL with a shared secret or SigV4. S3, IAM and CloudWatch are first-class. Everything else (the daily job, D1, the auth gate) stays on GCP or Cloudflare, so we would operate two clouds. AWS internet egress on the responses applies too; its rate isn't checked here.

## 2. GCP's equivalents

- **Cloud Run** (Cloud Run functions deploy onto it):
  - Limits: up to 8 vCPU and 32 GiB per instance, a 4-minute startup timeout and 60-minute requests ([quotas][run-quotas], [memory][run-mem]). A plain HTTP/1 response is capped at 32 MiB; chunked or streamed responses aren't ([quotas][run-quotas]).
  - Request-based billing: active time is $0.000024/vCPU-s and $0.0000025/GiB-s, and **idle minimum instances** are $0.0000025/vCPU-s and $0.0000025/GiB-s ([pricing][run-pricing]). Instance-based billing is $0.000018/vCPU-s and $0.000002/GiB-s ([pricing][run-pricing]).
  - So one warm 8 vCPU / 32 GiB minimum instance costs **about $263/month** idle and $0.98 per active hour.
- **Startup CPU boost:** extra CPU during startup and for 10 s after it, billed while it runs. At 8 vCPU it adds nothing (8 → 8) ([CPU limits][run-cpu]).
- **Snapshot/restore:** Cloud Run has none. The GCP features that do are:
  - **GKE Pod Snapshots**, GA in May 2026 ([docs][gke-snap]): it requires GKE Sandbox (gVisor) and no E2 machines, and restores memory lazily, page-faulting what the app touches first while streaming the rest in the background ([docs][gke-snap], [InfoQ][gke-infoq]). The published examples are GPU models: an 8B model in 15 s and a 70B in 37 s. There's no figure for 40 GB of CPU memory.
  - **GCE suspend/resume**: works for VMs up to 208 GB of memory. The memory is kept in persistent storage, and you pay for storage instead of cores and memory while suspended, for up to 60 days ([suspend][gce-suspend], [overview][gce-suspend-ov]). Resume time isn't documented.
- **Fan-out over GCS:** the same plan works on Cloud Run (concurrency 1, N instances), with free in-region GCS reads ([GCS pricing][gcs-pricing]) and no copy to S3. One vCPU with 2 GiB costs $0.000029/s on Cloud Run, against about $0.0000236/s for 1.77 GB on Lambda arm ([run][run-pricing], [lambda][lambda-pricing]). Per-instance GCS throughput isn't documented.
- **Honest comparison:**
  - Lambda has the finer billing (1 ms, against Cloud Run's 100 ms ([run pricing][run-pricing])), SnapStart and MicroVMs.
  - Cloud Run has 3× the memory, sits next to our data with no egress, and has idle minimum instances priced at about a tenth of the active rate.
  - Neither holds 41 GB. For our data, Cloud Run plus GCE suspend is the more coherent pair. Lambda only wins if MicroVM resume turns out to be fast, which would have to be measured on AWS.

## 3. Modal

- **Pricing:** $0.0000131 per physical core per second (one core = 2 vCPUs) and $0.00000222/GiB-s. Region multipliers are 1.15–1.75×, and non-preemptible runs cost 3× ([pricing][modal-pricing]). An 8-core / 48 GiB container is **$0.76/h** before multipliers.
- **Size limits:** the docs say a maximum is enforced but don't give the number for CPU or memory ([resources][modal-res]). Disk goes up to 3.0 TiB ([resources][modal-res]).
- **Cold start:** "containers boot in about one second" ([cold start][modal-cold]). The idle window (`scaledown_window`) runs from 2 s to 20 minutes ([cold start][modal-cold]).
- **Memory snapshots:**
  - Restore is lazy: gVisor serves pages in the background, prioritizing the ones a process blocks on ([blog][modal-snap-blog]).
  - Snapshots are typically 100 MiB–10 GiB ([blog][modal-snap-blog]). Hosts are expected to download about 2 GiB/s, with "much less effective throughput" in the tail ([blog][modal-snap-blog]).
  - The published examples are 1–3.5 s restores of small snapshots ([blog][modal-snap-blog]).
  - GPU snapshots are alpha ([docs][modal-snap]).
- **Can 20–40 GB restore in seconds?** No: even at the full 2 GiB/s it's 10–20 s, and a broad filter touches most of the index, so lazy restore doesn't save much. Snapshots that size are beyond what Modal documents. **The earlier conclusion still holds:** a cold first query of about 20 s or more, then fast while warm.

## 4. "A proper DB"

Common shape for every engine:

- A `names` table (127M rows, or 2.16M templates plus residuals).
- A `nodes` table (778M rows of ids: `parent`, `name_id`, `size`, counts, `usr`).
- An ancestor or ordinal encoding so "group by child of the view root" is a range or array lookup, not a string prefix.

Querying full `path` strings is out on any engine: 95 GiB decoded per scan.

### 4.1 RDS / Aurora Postgres with `pg_trgm`

- **What the index can do:** GIN/GiST trigram indexes accelerate `LIKE`, `ILIKE`, `~` and `~*`. But "a pattern with no extractable trigrams will degenerate to a full-index scan" ([pg_trgm][pgtrgm]), which is what `step-\d+`-style regexes are.
- **Latency *est.*:** the `names` lookup is fine. The expensive part is the join into 778M `nodes` and the tree rollup. Postgres runs one query on a few cores, so broad filters land in seconds to minutes.
- **Scale to zero:** Aurora Serverless v2 can pause at 0 ACU. Resume is typically about 15 s, and 30 s or more after 24 h paused ([auto-pause][aurora-pause]). It resumes at low capacity and with a cold buffer cache ([auto-pause][aurora-pause]).
- **Pricing:** $0.12 per ACU-hour; 1 ACU ≈ 2 GiB ([Aurora pricing][aurora-pricing]). A cache-resident working set (about 64 GiB) needs about 32 ACU = **$3.84/h**. Storage is $0.10/GB-month ([Aurora pricing][aurora-pricing]).
- **History:** handles it well as an SCD-2 table with range types.
- **Verdict:** poor latency per dollar for this workload.

### 4.2 BigQuery

- **On demand:**
  - $6.25/TiB after the first free TiB a month, with a 10 MB minimum per table and per query ([pricing][bq-pricing]).
  - A names-first query reads the vocabulary (a few GB) plus the matched `nodes` blocks; clustering on `name_id` prunes the rest. That's 5–20 GB *est.*, about **$0.03–0.12 per query** and **$0.9k–18k/month** at 1k–5k queries a day.
- **Editions:**
  - Slots cost $0.04/slot-hour in Standard, $0.06 in Enterprise and $0.10 in Enterprise Plus ([pricing][bq-pricing]).
  - The autoscaler adds slots in steps of 50, keeps them for at least 60 s, and bills a one-minute minimum. Opting in to "fluid scaling" bills per second with no minimum ([autoscaling][bq-autoscale]).
  - At about 200 slot-seconds per query *est.*, 3k queries a day is about $200/month.
- **Search indexes** don't help: they serve `=`, `IN`, `LIKE 'prefix%'`, `STARTS_WITH` and `ENDS_WITH`, "designed for point lookups", with no wildcards ([search][bq-search], [intro][bq-search-intro]). Infix substrings and regexes stay full scans of the names table, which is fine at 2–3 GB.
- **Latency *est.*:** about 1–2 s of job overhead plus execution, so 2–5 s. Not measured; Google doesn't document an interactive latency floor.
- **Storage:** active logical storage costs $0.000031507/GiB-hour, about $0.023/GiB-month ([pricing][bq-pricing]). A decade of SCD-2 history costs a few dollars.
- **Verdict:** zero ops and great for history, but on-demand pricing is ruinous for an interactive UI. Editions make it affordable, at a latency that's probably DuckDB-like.

### 4.3 ClickHouse

**Self-hosted:**

- **Indexes:** the inverted **text index** is GA from ClickHouse 26.2, with `ngrams`, `sparseGrams` and `splitByNonAlpha` tokenizers, and works with `LIKE` and `match` ([text indexes][ch-text]). `ngrambf_v1` and `tokenbf_v1` are now deprecated in its favour ([skip indexes][ch-skip]). Its limits:
  - It can't be materialized on parts over about 4.3B rows.
  - It takes memory to build.
  - The page we fetched listed it as not supported in ClickHouse Cloud; that needs checking against current Cloud releases ([text indexes][ch-text]).
- **Latency *est.*:** a vectorized scan of the names table (a few GB) on 8 cores is well under a second. The `name_id IN (…)` filter and the rollup over `nodes` run multi-core. So 0.5–3 s, with a tail on regexes over the whole vocabulary.
- **Machines:** it's disk-backed, so it doesn't need 41 GB of RAM:
  - e2-highmem-4 (32 GiB) at $0.181/h ≈ $132/month.
  - e2-highmem-8 (64 GiB) at $0.362/h ≈ $264/month, or $0.260/h ≈ $190 with a 1-year commitment ([GCE pricing][gce-gp]).
  - Disk at about $0.10/GiB-month for pd-balanced ([disk pricing][gce-disk]).
- **History:** handles it well, in one SCD-2 `nodes_hist` table.

**ClickHouse Cloud:**

- **Compute** is billed per minute in units of 8 GiB + 2 vCPU and scales to zero when idle ([pricing][ch-pricing]). Scale tier is about $0.2985 per unit-hour on AWS us-east according to third-party teardowns ([getbeton][ch-scale]). ClickHouse's own pricing page showed only $0.32–0.51 for Enterprise ([pricing][ch-pricing]).
- **Idling:** compute isn't billed while paused, storage is. Wake time isn't documented, connections "can time out while it is paused", and it isn't recommended for frequently accessed customer-facing services ([idling][ch-idle]).

### 4.4 MotherDuck

- **Pricing:** Business is $250/org/month plus usage. Pulse is $0.60/CU-hour; Standard $2.40/h, Jumbo $4.80/h, Mega $12/h and Giga $24/h. Storage is $0.04/GB-month ([pricing][md-pricing]).
- **Billing:** wall clock with a 1-minute minimum. The cooldown is configurable from 1 minute to 24 h ([sizes][md-sizes]).
- **Startup:** about 100 ms for Pulse, Standard and Jumbo; a few seconds for Mega and Giga ([sizes][md-sizes]). Mega is 64 cores / 256 GB and Giga 192 cores / 1.5 TB ([blog][md-blog]). Sizes for the smaller tiers weren't found.
- **Latency:** it is DuckDB, so expect our names-first numbers (p50 2.0 s, max 91 s), better on Mega.
- **Verdict:** no gain over self-hosted DuckDB except ops.

### 4.5 Anything better suited

- **Keep the custom index, change its footing.** It's the fastest engine we have, and the box is already a soft dependency: the Worker answers flagged when the box is down ([serving-box spec][filter-query-service]). So:
  - **Spot** is safe: up to 91% off on-demand, with prices changing at most monthly ([GCE pricing][gce-vm]).
  - A **1-year commitment** on e2-highmem-8 costs $190/month ([GCE pricing][gce-gp]).
  - **GCE suspend/resume** keeps the loaded index in suspended memory and charges only storage when idle ([overview][gce-suspend-ov]).
- **Shrink the index.** Templating (127M → 2.16M names, [phase 0][p0]) plus id arrays could bring a scan to ≤ 28–32 GB. That unlocks Cloud Run, Lambda MicroVMs, two scans in a 64 GB box (diffs), or a 32 GB e2-highmem-4.

### 4.6 History: 1e12 rows is the wrong framing

- **The design point:** store history as an interval (SCD-2) table, `(path_id, valid_from, valid_to, size, n_files, usr, …)`, with one row per *version* of a path, not per scan.
  - A scan at time *t* is `valid_from ≤ t < valid_to`.
  - A diff between scans *a* and *b* reads only rows whose interval starts or ends in `(a, b]`, so its work scales with churn, not with table size.
- **Estimate:** rows ≈ N · (1 + c · (S − 1)), for N = 778M rows per scan, S scans and per-scan churn c (new versions as a fraction of rows). Dir rows are included in c because they change whenever a descendant does.
  - **Measured:** gcs c ≈ 0.1–0.2% (median about 0.1% for objects, 0.04% for dirs; two attribution-change days excepted) ([path-store §4.5][path-store]).
  - **1,000 daily scans:** c = 0.1% gives **~1.6B rows**, c = 0.2% gives **~2.3B**. A pessimistic c = 1% still gives only ~8.6B. The naive layout would be 7.8e11.
  - **One year:** about 1.7 scans' worth, ~1.3B rows ([path-store][path-store]).
  - **Caveat:** our listings carry no `updated`, so changes to metadata alone are invisible to this count.
- **What it means:** history is a 2–3× single-scan problem, not a 1,000× one. Every database above holds it on disk. Even the custom index could hold "latest + SCD-2 deltas" instead of two full scans for diffs.

## Recommendation

1. **Don't move to AWS or Modal now.**
   - Lambda helps only through fan-out, which is slower than the custom index and costs more per query, plus $70–150/month of GCS egress and a second cloud.
   - Lambda MicroVMs and Modal can't restore a 30–40 GB index interactively, by their own documented numbers.
   - Revisit MicroVMs only if the index slims to ≤ ~28 GB and AWS publishes resume times.
2. **Keep the custom index and fix its cost:** a 1-year commitment ($190), Spot, or suspend/resume, with the Worker as the fallback that already exists.
3. **Prototype a disk-backed database as the history and diff engine.**

### Proposed benchmark: two candidates against the 105-query bench

**A. Custom index on GCE with suspend/resume, and on Spot**

- Same build as the phase 2 bake-off.
- Measure, at 41 GB loaded:
  - suspend time, resume time and first-query latency after resume (p50 over about 10 cycles, including one after 24 h);
  - the 105-query latencies on e2-highmem-8 against n2-highmem-8;
  - on Spot, preemptions over a week.
- **Cost:** about 10 VM-hours at $0.36–0.52/h, plus a week of Spot e2-highmem-8 and suspended storage. **≈ $15–40.**

**B. ClickHouse 26.2+ self-hosted, on an e2-highmem-8 with pd-balanced**

- Tables: `names` (with a text index, `ngrams`), `nodes` (ids, `ORDER BY (depth, parent, …)`), and `nodes_hist` (SCD-2 over 16 existing scans).
- Run the 105 queries plus a diff set: two adjacent scans, and two scans 15 days apart.
- Pass bar: within 2× of the custom index's p50/p90, with max ≤ 10 s, exact on all 105, and diffs reading only the delta.
- **Cost:** about 20 VM-hours at $0.36/h plus 200 GB of disk for a week. **≈ $10–15.**

**Optional C (≈ $15):** load the same `names`/`nodes` into BigQuery and run the 105 queries on demand. 105 × ~$0.1 is mostly covered by the free TiB. The result is a real latency floor for BQ, to settle §4.2's estimate.

**Decision rule:**

- If A's resume is ≤ ~10 s, run the box suspended (or on Spot) and keep it.
- If B lands within 2×, move diffs and history to ClickHouse and keep the custom index only for filters, or drop it if B is within 1.5×.
- If neither, buy the 1-year commitment and stop.

## 5. Measured: A′, the custom index on GCE suspend/resume (2026-10-03)

Setup: one n2-highmem-8 (8 vCPU, 64 GB) in us-east1-c, Debian 12, a 500 GB pd-ssd boot disk, labelled `purpose=serving-exp`. It ran `dt-cloud bench-serve -e mem` (new: the bench engine loaded once and answering `GET /run` over HTTP) in the gcs job image, holding phase 2's `mem` index for 2026-10-01 (a copy, `scratch/bench/serving-exp/mem-index/2026-10-01/`). `job/serving-exp-suspend.sh` drove it from a laptop, so every time below runs from the `gcloud` call, not from inside the VM. Records: `tmp/serving-exp/a-*.jsonl` (not committed).

**Before any suspend:** the cold load from GCS took 140 s (119 s to copy 40.4 GB, 21 s to load), 40.7 GB resident. Two full runs: 105/105 exact, p50 0.44 / 0.45 s, p90 2.21 / 2.19 s, max 5.97 / 5.94 s, matching phase 2 (§6.2 of the serving-box spec). The first-query probe below (`safetensors`, its 3 views) took 0.30–0.37 s per view.

### 5.1 Measured: suspend and resume, 3 cycles

| suspended for | `suspend` call | `resume` call | → `RUNNING` | → `/health` answers | → first exact answer (3 views) | first view after / before | all 105 after: p50 / p90 / max |
|---|---:|---:|---:|---:|---:|---:|---|
| 60 s | 62.7 s | 12.8 s | 13.5 s | 17.5 s | 29.9 s | 7.7 s / 0.34 s | 0.47 / 2.54 / 6.85 s |
| 5 min | 100.6 s | 13.5 s | 14.3 s | 18.9 s | 31.2 s | 7.9 s / 0.37 s | 0.51 / 2.22 / 5.88 s |
| 32 min | 61.8 s | 13.1 s | 13.8 s | 17.6 s | 31.5 s | 8.5 s / 0.33 s | 0.66 / 2.20 / 5.80 s |

All 105 answers stayed exact after every resume.

- **Resume is about 13 s of API time plus 4 s until the process answers.** It didn't depend on how long the VM had been suspended (1 min to 32 min). The 24 h case wasn't run.
- **Memory comes back lazily.** The first query after a resume took 7.7–8.5 s against 0.34 s before: its vocabulary scan touches the whole 6.6 GB lowercase blob, which pages in on first touch. The second view of the same query took 0.7 s, the third 0.35 s. A full run straight after a resume was 5–50% slower at the median and back to normal at p90 and max.
- **So the first exact answer arrives about 30 s after the resume call** (26 s for one view, plus the probe's other two views and ssh). That is well past the ~10 s the decision rule asked for.
- **Suspending took 62–101 s** (writing 64 GB of memory out), so a box suspended after each burst of use can't take a new query for a minute or two.

### 5.2 Measured: stop/start with a reload from the PD

| `stop` call | `start` call | → ssh | → `/health` (index reloaded) | → first exact answer | first view | all 105 after |
|---:|---:|---:|---:|---:|---:|---|
| 28.1 s | 8.9 s | 25.5 s | 138.1 s | 141.8 s | 1.27 s | 0.44 / 2.19 / 5.83 s |

The reload read 40.4 GB from the 500 GB pd-ssd in 103 s (about 390 MB/s) into fully resident arrays, so the answers after it are as fast as ever. A stop/start costs about 140 s to the first answer against a resume's 30 s, and phase 2's gcsfuse cache (89 s from GCS to RAM) would be close to the same.

### 5.3 Measured: what suspension costs

- **No vCPU or memory charges while suspended.** The preserved memory and device state are billed at $0.000232877 per GiB-hour ([pricing][gce-suspend-price]), about $0.17 per GiB-month, the pd-ssd rate. 64 GB of memory is about **$11 a month**, plus the disks: $85 a month for this 500 GB pd-ssd, or about $10 for a 100 GB pd-balanced plus the index in GCS.
- **Suspension is limited to 60 days**, after which the VM is stopped ([suspend][gce-suspend]).
- **Running cost:** $0.52 an hour on demand for n2-highmem-8. A box awake 4 hours a day costs about $64 a month plus storage, against $383 always on.

**Verdict for A′:** suspend/resume works and is exact, but at gcs's size the first answer comes about 30 s after the resume call, three times the decision rule's ~10 s, and suspending takes a minute or more. It fits "warm standby, woken by a cron or a first visitor who sees a 30 s spinner", not a box woken per request. A stop/start costs 140 s to first answer and is no cheaper to hold.

## 6. Measured: B, self-hosted ClickHouse with SCD-2 history (2026-10-03)

Setup: one n2-standard-8 (8 vCPU, 32 GB) in us-east1-c with a 500 GB pd-ssd boot disk, labelled `purpose=serving-exp`, running ClickHouse 26.9.8 (the stable channel, text index GA). `job/serving-exp-ch.sh` drove it; its SQL and scripts are in `job/serving-exp-ch/`. The bench client is `dt-cloud bench-engine -e ch` (new, `dt_cloud.bench.ch`) in the gcs job image on the same VM, so its latencies include the HTTP round trips to ClickHouse but no network hop.

### 6.1 Measured: the latest scan's tables and the 105-query bench

**Layout.** `dt-cloud bench-ch-export` turns phase 2's `mem` index plus its generation's `path` sort into interval-encoded nodes: each node gets `pre`, its rank in a depth-first order, and `post` = `pre` + its subtree's node count − 1. "Under X" is then `X.pre < pre ≤ X.post`, and "has an ancestor in this set" is a running max of `post` in `pre` order: integer work, never a path string. The `pre`/`post` pass over 778M nodes took 53 s in numpy, and the export 846 s on a Batch n2-highmem-16. Tables:

- `names (nid, l)` `ORDER BY l`, with a text index `text(tokenizer = ngrams(3))` on `l`: 127M lowercase names;
- `nodes_by_name (nid, pre, post, depth, b, o, path)` `ORDER BY (nid, pre)`: a name's nodes are one primary-key range;
- `nodes (pre, post, depth, nid, b, o, path)` `ORDER BY pre`: a subtree is one primary-key range (the children of a trailing-`/` term's stems).

**Query plan** (`ChIndex.evaluate`, `truth.view_truth`'s semantics, as the DuckDB engine): the names passing each term's segment test (`LIKE` / `startsWith` / `endsWith` / `match`) → their nodes strictly under the view → full-path `p`/`n` flags on `lowerUTF8(path)` → roots = outermost of `p ∧ ¬n` and exclusions = outermost of `n` (one window pass each), each exclusion charged to the root holding it (one window pass over both). Each answer is a handful of statements on one HTTP session with temporary tables.

**Load** (from local parquet on the VM): `names` 147 s, including the text index; `nodes` 266 s; `nodes_by_name` 375 s; merging `names` into one part another 373 s. About 19 minutes, plus the 14-minute export.

| | on disk | uncompressed |
|---|---:|---:|
| `names` (2.57 GB of it the text index) | 3.6 GB | 6.6 GB |
| `nodes` | 8.5 GB | 119 GB |
| `nodes_by_name` | 11.0 GB | 119 GB |
| **one scan, total** | **23.1 GB** | |

**Results:** 105/105 exact on every pass. Warm is the second of two back-to-back passes; cold drops ClickHouse's caches and the OS page cache before every answer, so each one reads from the pd-ssd.

| | p50 | p90 | max | over 10 s |
|---|---:|---:|---:|---:|
| `mem` on n2-highmem-8 (§6.2 of the serving-box spec) | 0.44 s | 2.1 s | 5.2 s | 0 |
| pass bar (2× `mem`, max ≤ 10 s) | 0.88 s | 4.2 s | 10 s | 0 |
| ClickHouse warm | **0.20 s** | **1.46 s** | **4.33 s** | 0 |
| ClickHouse cold | 1.03 s | 4.67 s | 6.25 s | 0 |

- **Warm, ClickHouse beats the in-memory index** at the median and the tail, holding none of it in process memory. **Cold, it misses the 2× bar slightly** at p50 (1.03 s against 0.88 s) and p90 (4.67 s against 4.2 s), with no answer over 10 s.
- **The slowest answers are at the store root:** `step` (18.7M roots) 4.3 s warm / 6.2 s cold; `ext-npy` (20.4M roots) 2.7 / 3.1 s; dense substrings and AND/NOT queries (`eval`, `qwen sft`, `moe 1e23`, `nemotron -dclm|dolma`, `ckpt -eval`) 2.2–2.7 s warm and 4.7–6.1 s cold. Cold regexes pay most (`re-grug-moe` 0.67 → 6.25 s, `re-tokenizer-json` 0.04 → 2.90 s): `match` over the names can't use the text index, so it reads the whole `names` table (3.6 GB) from disk.
- **One rewrite mattered.** The first version deduplicated candidates with a `GROUP BY` and carried path strings through the window passes: `step` took 12.4 s and `ext-npy` 11.4 s warm. Window passes over `(pre, post, b, o)` only, with the root paths joined back only for listing, brought them to 4.3 s and 2.7 s.
- **Memory:** the server holds 1.6–1.9 GB resident between queries, the largest bench query peaked at 2.8 GB, and a full warm pass from a dropped page cache pulled 5.9 GB into it. The working set is about 11 GB, so **16 GB would very likely do** for the latest scan (not run). The other 16 GB of this box only helped the bulk history build (§6.2).

### 6.2 Measured: SCD-2 history over 7 daily scans

**Sources:** per scan (2026-09-27 … 10-03), the per-object listings (`listing/<date>/<bucket>/shard-*.parquet`, about 10 GB a day) and `dir-cache/dir-stats.parquet`. dir-stats holds each dir's *direct* objects only (12.5 GB a day, 178M dirs), and dirs with no direct objects are absent, so the dir rollups had to be built. Copying a day's files from GCS to the VM took 45–77 s.

**Build:**

1. Raw rows, per scan: `obj_raw (path, s, size, created, sc)` and `dir_raw` (direct stats), both `ORDER BY (path, s)`. Loading took 263–328 s for a day's objects (557–564M rows) and 116–151 s for its dir-stats, about 7 minutes a scan.
2. Dir rollups, bottom-up one depth at a time: rollup(x) = direct(x) + Σ rollup(children) (`dir-rollup.sh`). This took 78 minutes for all 7 scans, about 11 minutes a scan. The first attempt summed every row into each of its ancestors (about 10B rows) and ran out of memory on one huge top-level dir. The rollups match the 2026-10-01 path index exactly on every bucket's bytes and objects, and 217M dirs + 561M objects = its 778M nodes.
3. SCD-2 intervals (`hist-build.sql`): one pass per table in primary-key order (`optimize_aggregation_in_order`). Each path's scans become runs of identical, consecutive presence, and each run becomes one `[vf, vt)` row, `ORDER BY (depth, path, vf)`. This took 835 s for objects and 365 s for dirs.

All 7 scans took about 2.4 hours on 8 vCPU. A daily increment would be about 20 minutes: 7 for the raw rows, 11 for the rollups, plus extending the intervals, which wasn't built here.

**Churn per scan:** "opened" counts new and changed paths, "closed" counts deleted and changed ones, each as a fraction of the rows present at that scan.

| scan | objects present | opened | closed | dirs present | opened | closed |
|---|---:|---:|---:|---:|---:|---:|
| 09-28 | 558.1M | 0.115% | 0.054% | 217.1M | 0.065% | 0.051% |
| 09-29 | 558.6M | 0.146% | 0.057% | 217.2M | 0.040% | 0.021% |
| 09-30 | 559.9M | 0.452% | 0.219% | 217.2M | 0.107% | 0.088% |
| 10-01 | 561.1M | 0.426% | 0.206% | 217.2M | 0.126% | 0.116% |
| 10-02 | 562.8M | 0.459% | 0.168% | 217.3M | 0.147% | 0.106% |
| 10-03 | 564.2M | 0.416% | 0.164% | 217.3M | 0.041% | 0.052% |

- **Seven scans in SCD-2 hold 1.009 scan-equivalents of objects and 1.004 of dirs:** 569M object versions (4.2 GB on disk) and 218M dir versions (2.4 GB), against 3.9B and 1.5B raw rows (19 GB).
- **Object churn ran at 0.12–0.46% a scan**, two to four times §4.6's 0.1–0.2%, and it rose from 09-30 on. **Dir churn ran at 0.04–0.15%.** Per scan of all 778M rows, that's about 2.1M new versions, roughly 0.27%. At that rate 1,000 daily scans come to about 2.9B rows, roughly 20 GB at these compression rates: still a few scans' worth, as §4.6 predicted.

**History queries:** each was timed cold (ClickHouse's caches and the OS page cache dropped first) and warm (best of 2), through `clickhouse-client` (whose ~0.05 s start-up is included). Scans are 0 = 09-27 … 6 = 10-03, and the deep dir is `marin-us-central2/grug`.

| query | cold | warm | rows read |
|---|---:|---:|---:|
| as-of 09-28: the store root's buckets | 0.32 s | 0.10 s | 8K |
| as-of 09-28: a bucket's children (dirs + direct objects) | 0.32 s | 0.11 s | 25K |
| as-of 09-28: a deep dir's children | 0.30 s | 0.10 s | small |
| diff at the root, adjacent scans | 0.26 s | 0.10 s | 8K |
| diff at the root, 6 days apart | 0.27 s | 0.10 s | 8K |
| diff at a bucket's children, adjacent scans | 0.28 s | 0.11 s | 25K |
| diff at a bucket's children, 6 days apart | 0.31 s | 0.10 s | 25K |
| every changed object under a bucket, 6 days, rolled up to its children | 1.02 s | 0.50 s | 259M |
| filtered diff at the root (`checkpoints`), 6 days, per bucket | 1.35 s | 0.21 s | 525M |
| filtered diff at a bucket (`step-`), adjacent scans, per child | 0.66 s | 0.14 s | 259M |
| bytes over time for one dir, every scan | 0.26 s | 0.10 s | small |
| bytes over time for every bucket | 0.25 s | 0.10 s | 8K |

- **As-of views, view-level diffs and series are primary-key ranges** on `(depth, path, vf)`: a few granules, about 0.1 s warm and 0.3 s cold, whether the scans compared are 1 day or 6 days apart.
- **Object-level diffs and filtered diffs don't yet read only the delta.** `vf`/`vt` aren't in the sort key, so the query scans every version under the prefix (259M rows for `marin-us-central2`, 525M for the whole store). That still takes 0.14–0.5 s warm and 0.66–1.35 s cold. A projection or a changes table keyed by `vf` and `vt` would make them proportional to churn (not built).
- Dir rollups can't be filtered, since the filter applies to object paths, so a filtered diff sums object versions up to the view's children (exact, as above).

### 6.3 Measured: verdict and the always-on box

- **Pass bar:** exact on all 105; warm p50/p90/max 0.20/1.46/4.33 s, inside 2× of `mem` (and faster); cold 1.03/4.67/6.25 s, just outside 2× at p50 and p90; nothing over 10 s. History (as-of, diffs, series) answers in 0.1–0.5 s warm and 0.25–1.35 s cold.
- **Smallest always-on box that would serve it:**
  - Measured: an n2-standard-8 (8 vCPU, 32 GB) at $0.389/h, about **$284 a month** on demand.
  - Not run but supported by the measured ~11 GB working set: 8 vCPU with 16 GB, e.g. an e2 custom 8 vCPU / 16 GB at about $160 a month, or e2-standard-8 (32 GB) at about $196.
  - Disk: about 30 GB of serving data (23 GB for the latest scan, 6.6 GB of history) plus the raw staging the builds need. Cold latency depends on the disk; these runs used a 500 GB pd-ssd.
  - Fewer vCPUs (n2-standard-4, $142) would slow every multi-threaded scan; not measured.
- **Against the `mem` box:** it serves filters as fast warm, holds no 41 GB in RAM (a 16–32 GB box instead of 64 GB), keeps months of history in a few more GB, and answers diffs and as-of views over any two scans. That is §4.6's design point, measured. Per the decision rule, B lands within 2× (warm well inside, cold at the edge), so diffs and history move to ClickHouse, and the custom index is no longer needed for filters on a warm box.

**Cost of both experiments:** about $4 of the $40 budget (VMs about 6 h, Batch about 0.5 h of n2-highmem-16). Both VMs, their disks and the Batch job records were deleted and checked gone. The index copies and exports under `scratch/bench/serving-exp/` were deleted, except the run records under `runs/`.

[filter-query-service]: done/filter-query-service.md
[p0]: done/filter-query-service-p0.md
[path-store]: path-store.md
[lambda-quotas]: https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html
[lambda-mem]: https://docs.aws.amazon.com/lambda/latest/dg/configuration-memory.html
[lambda-pricing]: https://aws.amazon.com/lambda/pricing/
[snapstart]: https://docs.aws.amazon.com/lambda/latest/dg/snapstart.html
[snapstart-py]: https://dev.to/aws-builders/from-seconds-to-milliseconds-fixing-python-cold-starts-with-snapstart-59mn
[duckdb-lambda]: https://dev.to/aws-builders/serverless-analytics-on-nas-data-for-000001query-duckdb-lambda-x-fsx-for-ontap-2o5o
[rust-cold]: https://edjgeek.com/blog/lambda-cold-starts-dead/
[rust-cold2]: https://www.nandann.com/blog/rust-aws-lambda-production-guide
[lambada]: https://arxiv.org/pdf/1912.00937
[starling]: https://arxiv.org/abs/1911.11727
[boxer]: https://www.cidrdb.org/cidr2021/papers/cidr2021_paper12.pdf
[skyrise]: https://pith.science/paper/2501.08479
[microvm-concepts]: https://docs.aws.amazon.com/lambda/latest/dg/microvms-how-it-works.html
[microvm-blog]: https://aws.amazon.com/blogs/compute/announcing-lambda-microvms-serverless-compute-environments-with-vm-level-isolation-and-near-instant-startup/
[gcs-pricing]: https://cloud.google.com/storage/pricing
[s3-price]: https://www.cloudzero.com/blog/s3-pricing/
[run-quotas]: https://cloud.google.com/run/quotas
[run-mem]: https://cloud.google.com/run/docs/configuring/services/memory-limits
[run-cpu]: https://docs.cloud.google.com/run/docs/configuring/services/cpu
[run-pricing]: https://cloud.google.com/run/pricing
[gke-snap]: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/pod-snapshots
[gke-infoq]: https://www.infoq.com/news/2026/09/gke-pod-snapshots-benchmarks/
[gce-suspend]: https://cloud.google.com/compute/docs/instances/suspend-resume-instance
[gce-suspend-ov]: https://cloud.google.com/compute/docs/instances/suspend-stop-reset-instances-overview
[gce-gp]: https://cloud.google.com/products/compute/pricing/general-purpose
[gce-vm]: https://cloud.google.com/compute/vm-instance-pricing
[gce-disk]: https://cloud.google.com/compute/disks-image-pricing
[modal-pricing]: https://modal.com/pricing
[modal-res]: https://modal.com/docs/guide/resources
[modal-cold]: https://modal.com/docs/guide/cold-start
[modal-snap]: https://modal.com/docs/guide/memory-snapshot
[modal-snap-blog]: https://modal.com/blog/mem-snapshots
[pgtrgm]: https://www.postgresql.org/docs/current/pgtrgm.html
[aurora-pause]: https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/aurora-serverless-v2-auto-pause.html
[aurora-pricing]: https://aws.amazon.com/rds/aurora/pricing/
[bq-pricing]: https://cloud.google.com/bigquery/pricing
[bq-autoscale]: https://cloud.google.com/bigquery/docs/slots-autoscaling-intro
[bq-search]: https://cloud.google.com/bigquery/docs/search
[bq-search-intro]: https://cloud.google.com/bigquery/docs/search-intro
[ch-text]: https://clickhouse.com/docs/engines/table-engines/mergetree-family/textindexes
[ch-skip]: https://clickhouse.com/docs/optimize/skipping-indexes
[ch-pricing]: https://clickhouse.com/pricing
[ch-scale]: https://www.getbeton.ai/blog/clickhouse-pricing-teardown/
[ch-idle]: https://clickhouse.com/docs/cloud/features/autoscaling/idling
[md-pricing]: https://motherduck.com/product/pricing/
[md-sizes]: https://motherduck.com/docs/about-motherduck/billing/duckling-sizes/
[md-blog]: https://motherduck.com/blog/scaling-duckdb-with-ducklings/
[gce-suspend-price]: https://cloud.google.com/compute/vm-instance-pricing#suspended_vm_instances
