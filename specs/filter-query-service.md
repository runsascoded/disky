# Serving box: a stateful query backend behind the Worker

Status: proposed (2026-10-02). Supersedes this spec's earlier scale-to-zero Cloud Run design (after [phase 0]). Reviewed by the root session. Owner: the gcs session. Code lands on `cloud` (shared); a deployment opts in with its own VM.

**Scope:** the box is one serving tier among several, not the default. Its cost floor (~$260+/month) pays only at fleet scale (gcs: 778M rows). `cloud` keeps a range of tuned configs, and each deployment picks one (§8). Every engine is scored by the same harness, so the tiers stay comparable.

## 1. Why

The site's reads run in a Cloudflare Worker: ~128 MB of memory, limited CPU, 6 concurrent subrequests, and no state between requests. Every request re-reads and re-decompresses parquet from GCS. That is why gcs's filters cut out `partial`, and why plain diffs and the series take seconds. The fight is with the serverless setup, not the problem size. On gcs's 2026-10-01 scan ([phase 0]):

- **The data fits on one machine:** 778M rows (objects + dirs), 127M distinct names (about 3 GB as text), and a 7.7 GiB `path` sort.
- **The search is fast when the data is held:** names-first matching takes 0.6–2.4 s end to end in plain DuckDB with the vocabulary in memory, and root sets are identical to a full scan.
- **The full path scan is not an option:** the `path` column is 95 GiB decoded, so a scan takes 34 s warm and 219–288 s cold. Full paths are never scanned; the box keeps the tree as ids.
- **Memory decides the platform:** the vocabulary takes 10–19 GB per generation in DuckDB, and a diff needs two generations, which exceeds Cloud Run's 32 GiB maximum.

## 2. What

An always-on VM, the **serving box**, that acts as a cascading cache in front of the data:

- **Memory:** the hot generations (the latest two, for diffs) in RAM: vocabulary, name → rows map, and per-dir aggregates.
- **Local NVMe:** recently used generations' files, kept under LRU, at block granularity where the cache layer allows (§4.3).
- **GCS:** the parquet files of record, unchanged.

The daily job warms it after each publish. Otherwise, each layer evicts least-recently-used data.

The Worker stays the front door: sign-in (the app's own Google OIDC and email-code sessions in D1, not Cloudflare Access), the edge cache, and every read it already serves well. It forwards to the box only what the box serves better. Phase 1 forwards path filters (`/api/subtree?q=` and `/api/diff?q=`); plain views, diffs and the series move later, only if the bake-off and probes show a gain (§6). The box is a soft dependency: if it's down, the Worker answers as today, with filters flagged `partial` or `approximate`. Never an outage, and never silently short.

## 3. Request flow (filters)

1. The client calls `/api/subtree?q=…` as today.
2. The Worker parses `q` (the shared parser; syntax errors stay 400s). It forwards to the box when the box is configured and the query is heavy: more than ~2K trigram candidates, NOT-only, file-extension-shaped terms, the `regex` syntax on a large store, or a generation without search sidecars. Otherwise it answers itself, and forwards anyway if its answer comes out `partial` or `approximate`.
3. The box returns the same `TreeNode` / `DiffRow` shapes, exact. The Worker caches the answer like any view, keyed by the canonicalised AST (operands sorted, case folded) plus generation, view root, canvas and `minArea`.
4. If the box is down or over its deadline, the Worker returns its own flagged answer.

**Engine override:** `qe=auto|box|worker` on the page URL, passed through to the API (default `auto`). `qe=box` always forwards, for verification; `qe=worker` never does. Every filtered response names the engine that answered and its time.

## 4. The box

### 4.1 Evaluation

- **Roots first:** the search finds only the outermost matching paths; anything under a matching dir is that root's contents. Candidates are checked shallowest-first, and one under an already-found root is skipped before its rows are read.
- **Names-first:**
  - The vocabulary (with a precomputed lowercase column; never `ILIKE`, 6.3 s vs 0.3 s in [phase 0]) is pinned per hot generation.
  - Segment terms are matched against it: substring, `*` within a segment, `^`/`$` anchors, `regex`.
  - Matched name ids map to rows via a pinned name → row-groups map (v1 `rgs`).
  - Roots are an anti-join against matched ancestors, and totals come from the dir aggregates.
- **Drawing:** only what's inside the roots above the view's threshold, level by level (children ordered by size), never whole subtrees.
- **Diff:** both generations in one query.
- **User lens with claims:** the Worker passes the claim regions it already computes.

### 4.2 Engines to bake off

1. **DuckDB names-first** over the generation's files on local NVMe: the [phase 0] approach, served.
2. **Custom in-memory index:** the tree as id arrays (parent, name id, size, counts; ~25–30 GB for 778M rows), the vocabulary as one concatenated byte blob scanned with SIMD `memmem` across cores (~0.1–0.3 s for 3 GB), and children sorted by size.

The bake-off (§6) picks between them, or a hybrid.

### 4.3 Cache layers to bake off

- **gcsfuse's file cache:** whole-file, LRU by size.
- **`rclone mount --vfs-cache-mode full`:** byte ranges, sparse, LRU by size. This is the block-granular option.
- **DuckDB `cache_httpfs`:** a block cache under DuckDB itself.

The deciding questions: time to a hot generation, and how much NVMe a month of generations needs at the access pattern we actually see.

### 4.4 Operations

- **Machine:** a 64 GB VM (n2-highmem-8 or e2-highmem-8; price both, since the work is decode- and string-bound and tolerates e2) with a local SSD, in us-east1 beside the data bucket.
  - On demand for the first month to measure use, then a 1-year commitment.
  - It runs 24/7. A schedule doesn't pay: a commitment bills while stopped, and on demand 16 h a day costs about what a commitment does 24/7.
- **Reachability:** `cloudflared` on the VM, a Cloudflare Tunnel to a hostname gated by a Cloudflare Access service token the Worker presents. No public IP, no load balancer. That token authenticates the Worker to the box only; it has nothing to do with user sign-in.
- **Shape:**
  - A managed instance group of size 1, with autohealing on `/healthz`.
  - A container built from `cloud/` (`dt-cloud serve-query`).
  - A startup script that copies the latest two generations' files to local SSD and loads them. Phase 0 measured copy-then-load at about 8–10 s for the vocabulary.
  - The daily job calls `/reload?gen=…` after publish.
- **IaC:** a `QueryBox` component in `cloud`'s `infra/gcp/` beside `gcp_jobs.py` (generic: its SA with `objectViewer` on the data bucket, the MIG, the tunnel token secret, the reload hook). gcs's `infra/gcp/__main__.py` only instantiates it with its bucket, region and size, so no separate stack.
- **Admin status:** an admin panel, read through the Worker from the box's `/status` and the VM's monitoring metrics: up/down, loaded generations, memory, recent query latencies by engine. It has a "reload latest" button.

## 5. Costs

At list prices, approximate:

| Option | Cost per month |
|---|---|
| n2-highmem-8, on demand | ~$380 |
| n2-highmem-8, 1-year commitment | ~$240 |
| e2-highmem-8, on demand | ~$260 |
| Local SSD 375 GB | ~$30 |

There's no egress cost, since the box is in the same region as the bucket. For scale, that's about 1% of the GCS storage bill.

The Worker's search sidecars become an optional fast path: selective queries stay in the Worker, everything else goes to the box. Templating hash-like names (74× fewer trigram postings, [phase 0]) and skewing the index toward drawable or shallow names are ways to keep that fast path cheap enough to build daily.

## 6. Phases

1. **Ground truth and the bake-off harness.**
   - A query set of ~30 queries across shapes: selective and broad terms, `^`/`$` anchors, `-x`, file extensions, digit-heavy names, AND/OR, and drilled view roots.
   - Exact answers computed once from a full scan on Batch (root sets and totals), stored under `gs://oa-gcs-usage-dvx/scratch/bench/`.
   - A `dt-cloud probe` query-set mode that scores any engine on latency and exactness against them.
2. **Prototype the engines** (§4.2) on a Batch or dev VM against the harness: the Worker index (v1, v2, templated), DuckDB names-first, and the custom in-memory index; cache layers per §4.3. Pick.
3. **`dt-cloud serve-query`**, with parity tests (the shared AST case table, run against the vitest fixtures).
4. **Worker hand-off** behind `QUERY_BOX_URL` (absent = today's behaviour), the `qe=` override, and probe scenarios.
5. **Infra:** the `QueryBox` component plus the tunnel, on demand. Then measure for a month, then commit.
6. **Then, if the probes say so:** plain views, diffs and the series through the box.

## 7. Code layout

Per the branch rule, everything generic is `[cloud]`: `dt-cloud serve-query`, the `QueryBox` component, the Worker hand-off, the probe query-set mode and the ground-truth job. Deployment-specific pieces are data or config on the deployment branch: gcs's query set, its `QueryBox(...)` instantiation, and `QUERY_BOX_URL`.

## 8. Serving tiers

The box is the top of a ladder. Each rung should be thoroughly tuned on its own, not left as a degraded fallback:

| Tier | Fits | Search |
|---|---|---|
| Local (`disk-tree` on one machine, the app) | a disk or a small bucket | in process, the whole index in memory |
| Worker only | up to ~tens of millions of rows | the Worker's sidecars (v2 rows-search, templated); exact at this size |
| Worker + box | gcs scale | §2–4 |

The bake-off scores all rungs on the same query set, so each deployment's choice is a measurement. The Worker-only tier stays the default in `cloud`; a deployment adds the box only when its probes say the Worker can't serve it exactly within budget.

## 9. Open questions

- **AST compiler:** where it lives. TS (Worker) and Python or a compiled language (box) both need it. Keep one AST JSON schema with a shared case table.
- **Retention of the Worker's sidecars**, if any are built daily: last ~7 scans plus pinned dates.
- **Anchors in the simple syntax:** `^tomat` / `tomat$` as sugar. `/tomat` and `tomat/` already work as segment-start and segment-end matches.

[phase 0]: filter-query-service-p0.md
