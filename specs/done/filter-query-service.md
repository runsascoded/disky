# Serving box: a stateful query backend behind the Worker

Status: **retired 2026-10-10** with the ClickHouse store ([`ch-store.md`](ch-store.md)): the serving box (`dt_cloud/box/`, `serve-query`, the Worker's `QUERY_BOX_URL` hand-off) left `cloud` at tag `ch-store-final`. The filter bench's ground truth and scoring (`dt_cloud/bench/{queryset,truth,score}.py`, `bench-truth`, `probe -Q`) stay.

Status: proposed (2026-10-02); phases 1–3 done 2026-10-03 (§6.1–6.3). Supersedes this spec's earlier scale-to-zero Cloud Run design (after [phase 0]). Reviewed by the root session. Owner: the gcs session. Code lands on `cloud` (shared); a deployment opts in with its own VM.

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

1. **Ground truth and the bake-off harness.** Built 2026-10-02 (`dt_cloud.bench`; results in §6.1).
   - **Query set** (data, per deployment): gcs's is `job/bench-queries.yml`, 35 queries × 3 view roots (`""`, `marin-us-central2`, `marin-us-central2/grug`; `tomat` swaps the last for `marin-us-east5`). Phase 0's four; anchors as they work today (`/tomat`, `tomat/`, `checkpoints/step-`); selective, broad, digit-heavy and hex terms; NOT (including NOT-only), AND, OR; extensions; four `regex` queries. Each entry records why it's in the set; terms were sampled from the 2026-10-01 vocabulary.
   - **Ground truth:** `dt-cloud bench-truth <queries> <gen> -o <truth>` (Batch, n2-highmem-16). Names-first (phase 0's method, v1 `rgs`) wherever the index's lemma bounds the candidates; a one-pass `path` scan for the rest (a term ending in `/`, `regex`), keeping only rows where a half of the predicate becomes true (a regex: every row it holds on). `-c <id>` computes a query both ways and compares. Per query × view: the match roots (count, md5 of the sorted list, the list with each root's net totals when ≤ 50k), the outermost excluded paths, and net totals, with the Worker's semantics (root hit, NOT subtraction; specs/path-store-search.md §1). gcs 2026-10-01: `gs://oa-gcs-usage-dvx/scratch/bench/truth/2026-10-01/`.
   - **Scoring:** `dt-cloud probe -Q <queries> -T <truth> [-c] [-r N] [-x qe=…] [-u <base>]` sends every query × view to `/api/subtree?q=&qs=&full=1`, one at a time, and scores it: `exact` (root set and net totals match, unflagged), `exact*` (exact but flagged), `flagged` (inexact, flagged `partial` / `approximate`), **`FAIL` (inexact and unflagged: a correctness bug)**, `refused` (a 413), `error`. Wall and `server-timing` latency per request; a JSON record per run (`-o …/runs/`). The engine is pluggable (`bench.score.Engine`): the deployed Worker, a local `wrangler` stack with another index variant, the box, or a `qe=` override are all the same `SubtreeEngine` at another base URL or with another param.
2. **Prototype the engines** (§4.2) on a Batch or dev VM against the harness: the Worker index (v1, v2, templated), DuckDB names-first, and the custom in-memory index; cache layers per §4.3. Pick. Done 2026-10-03 for the box's two engines and two cache layers (results and the pick in §6.2); the Worker index variants weren't re-run.
3. **`dt-cloud serve-query`**, with parity tests (the shared AST case table, run against the vitest fixtures). Done 2026-10-03, with the index slimmed first (§6.3).
4. **Worker hand-off** behind `QUERY_BOX_URL` (absent = today's behaviour), the `qe=` override, and probe scenarios.
   - **The tier selector is only `QUERY_BOX_URL`**, set or unset in a deployment's `[vars]`. Unset means Worker-only, the `cloud` default. There is no tier enum; the local tier is the app build.
   - **Every filtered response names its engine and time** in a header, Worker-only ones included (e.g. `x-query-engine: worker;dur=1234`, `box;dur=…`), so `dt-cloud probe` compares tiers across deployments with no config.
   - Ground truth stays the phase-1 set; the box must score 100% exact against it (phase 2 does, §6.2).
5. **Infra:** the `QueryBox` component plus the tunnel, on demand. Then measure for a month, then commit.
6. **Then, if the probes say so:** plain views, diffs and the series through the box.

### 6.1 Phase 1 results: today's Worker on gcs 2026-10-01 (v1 sidecars)

Truth: 2 Batch jobs (1,213 s + 53 s for an appended query; a 128 s vocabulary-sampling job picked the terms). Names-first and the full scan agree on all three checked queries (`ckpt -eval`, `/tomat`, `model-*-of-00004`), and `tomat` / `grug` / `safetensors` reproduce phase 0's root sets (same md5s). Under full-path semantics `ckpt -eval` has 165 roots and 2 excluded paths at the root (phase 0 tested the last segment only: 170).

Baseline: `dt-cloud probe -Q job/bench-queries.yml -T …/truth/2026-10-01/ -c -r 2`, record `scratch/bench/runs/2026-10-02T19:32:47Z.json`. Of 105 query × view cases: **0 FAIL**, 9 `exact`, 24 `exact*`, 71 `flagged`, 1 `refused` (413, `tensorstore` at the root; on an earlier run it was `ckpt -eval`, so it depends on load). Wall time median 6.9 s, p90 12.1 s, max 16.0 s; `server-timing` median 4.9 s.

Each cell: verdict, roots returned / true roots, flags (P = `partial`, A = `approximate`), median wall time of 2 cold requests.

| query | store root | `marin-us-central2` | `marin-us-central2/grug` |
|---|---|---|---|
| `tomat` | flagged 4/21739 P 9.4s | flagged 0/4 P 7.5s | flagged 1/9 P 15.4s (`marin-us-east5`) |
| `grug` | flagged 13/7724 P 7.4s | flagged 7/5703 P 11.8s | exact* 1/1 A 3.7s |
| `safetensors` | flagged 0/89122 P 5.1s | flagged 0/22899 P 4.5s | flagged 80/240 P 9.1s |
| `ckpt -eval` | flagged 0/165 P 8.3s | flagged 18/22 P 8.2s | exact 0/0 4.3s |
| `/tomat` | flagged 4/400 P 12.1s | exact* 0/0 P 5.9s | exact* 0/0 P 5.4s |
| `tomat/` | flagged 9/22 A 7.8s | exact* 0/0 A 2.2s | exact* 0/0 A 2.7s |
| `checkpoints/step-` | flagged 181/204279 P 6.4s | flagged 581/102269 P 7.6s | flagged 22895/47476 P 9.8s |
| `tensorstore` | refused 10.4s | exact* 1/1 P 12.2s | exact 0/0 4.2s |
| `opt_state` | flagged 0/48615 P 3.9s | flagged 36/39710 P 7.6s | exact* 0/0 P 4.5s |
| `config.json` | flagged 0/527874 P 1.9s | flagged 0/88330 P 4.0s | flagged 3/11 P 10.9s |
| `isoflop-3e+20` | flagged 64/676 P 8.9s | flagged 97/186 P 9.5s | exact 0/0 4.3s |
| `27f2fb` | flagged 25/42 P 12.3s | flagged 1/4 P 8.2s | exact* 0/0 P 7.5s |
| `781bc3291c81ce28` | flagged 0/3 P 4.9s | exact* 0/0 P 5.9s | exact 0/0 1.6s |
| `step-10000` | flagged 3/25666 P 7.6s | flagged 22/9806 P 9.8s | flagged 405/1087 P 13.0s |
| `00001-of` | flagged 0/33355 P 4.8s | flagged 0/8397 P 6.1s | flagged 1/5 P 9.3s |
| `model-*-of-00004` | flagged 0/7138 P 4.4s | flagged 0/2715 P 6.2s | exact* 0/0 P 5.3s |
| `shard_00` | flagged 0/1488639 P 7.3s | flagged 0/226814 P 4.4s | exact* 0/0 P 6.2s |
| `eval` | flagged 12/154194 P 16.0s | flagged 7/61971 P 10.6s | exact* 63/63 P 12.4s |
| `checkpoint` | flagged 147/182512 P 7.4s | flagged 1289/30630 P 8.9s | exact* 1890/1890 P 7.9s |
| `step-` | flagged 80/18695353 P 8.6s | flagged 636/174601 P 7.5s | flagged 22900/47481 P 6.9s |
| `podcast` | exact* 7/7 P 5.3s | exact* 0/0 P 4.4s | exact* 0/0 P 4.5s |
| `grug -checkpoints` | flagged 13/2619 P 9.4s | flagged 6/1451 P 12.5s | exact* 1/1 P 8.0s |
| `-checkpoints` | flagged 1/1 P 3.1s | flagged 1/1 P 3.0s | exact* 1/1 P 9.0s |
| `-tensorstore` | flagged 1/1 P 3.2s | exact 1/1 4.1s | exact 1/1 5.9s |
| `nemotron -dclm\|dolma` | flagged 231/76015 P 9.8s | flagged 103/3036 P 14.6s | exact* 13/13 P 11.6s |
| `qwen sft` | flagged 41/21124 P 13.5s | flagged 12/2834 P 9.3s | exact* 0/0 P 7.5s |
| `tootsie\|isoflop` | flagged 55/8533 P 13.0s | flagged 81/1277 P 11.0s | exact* 541/541 P 8.4s |
| `moe 1e23` | flagged 12/409 P 8.1s | flagged 13/374 P 9.8s | flagged 39/373 P 8.4s |
| `*.pt` | flagged 158/91346 P 8.0s | flagged 0/60292 P 5.7s | exact* 0/0 P 4.9s |
| `.npy` | flagged 0/20392645 P 5.5s | flagged 0/377 P 7.7s | exact* 0/0 P 6.4s |
| `.wav` | exact 128/128 6.9s | exact 0/0 1.4s | exact 0/0 1.7s |
| `\.safetensors$` (regex) | flagged 0/76381 A 2.9s | flagged 0/18223 A 1.6s | flagged 79/235 A 6.2s |
| `/step-[0-9]+$` (regex) | flagged 77/317306 A 6.5s | flagged 607/144822 A 4.6s | flagged 22899/47480 A 5.2s |
| `^marin-us-central2/grug/[^/]*moe[^/]*$` (regex) | flagged 84/1003 A 3.7s | flagged 169/1003 A 4.1s | exact* 1003/1003 A 3.1s |
| `tokenizer\.json$` (regex) | flagged 0/51539 A 2.0s | flagged 0/9832 A 2.2s | flagged 1/5 A 6.2s |

Reading it:

- **The flags hold:** every inexact answer was flagged. Exact answers come from selective terms (`.wav`; `podcast`'s 7 roots, flagged anyway) or small drilled views; at the store root and at the bucket, almost every query is cut by a budget (name groups, path groups or the 1.5 s search time) and returns a small fraction of the roots.
- **A NOT-only query at the store root subtracts nothing.** `-checkpoints` and `-tensorstore` at `""` return exactly the store's total (bytes and objects), while the same queries at `marin-us-central2` subtract. `view.ts` keeps an excluded path only `if (rootFor(e))`, and when the view root is the store root `rootFor` returns `''`, which is falsy, so every exclusion is dropped (the phase-2 loop's `if (!r) continue` likewise drops the drawn rows). Today the `partial` flag from the exclusion search's budget happens to cover it; with a search that completes, the answer would be unflagged and wrong. Fix: compare `rootFor(e) !== null`.
- **`partial` doesn't say which way.** With exclusions cut short, totals come out *over* the truth (`grug -checkpoints`: +4 % bytes at the root, +27 % at `marin-us-central2`; `-checkpoints` at the root: +57 %), while a cut positive search comes out under. A flag that says only "some matches may be missing" understates this.
- **Latency is flat, and slow:** 2–16 s whether the answer is exact or not, because the budgets stop the work. That is the box's target (§1).

### 6.2 Phase 2 results: the box's engines and cache layers on gcs 2026-10-01

Both engines plan every query in the set from the vocabulary, and both score **105 of 105 `exact`** against the phase-1 truth. The custom in-memory index never touches a `path` string. DuckDB decodes the `path` row groups holding the matched names, and falls back to a full `path` scan past 30% of the file (`config.json` and `.npy` at the store root). The custom index is 4.5× faster at the median and 6–17× at the tail, but holds 41 GB per generation against DuckDB's 12 GB.

**Code** (`dt_cloud.bench`, on `cloud`; gcs's Batch wrappers are `job/serving-box-{submit,bench}.sh`):

- `dt-cloud bench-index GEN -o DIR [-u gs://…]` builds the in-memory index from a generation's `path` sort and v1 names file.
- `dt-cloud bench-engine -e mem|duckdb -Q queries -T truth GEN` loads an engine and scores it in-process with `probe -Q`'s verdicts. It records load time, resident memory, and each answer's compute time separately from the time to list its root paths. Only the bench lists every root; a response draws the tree.
- `terms` decomposes each matcher over path segments, so every query resolves from the vocabulary:
  - a substring or glob becomes a test on the last segment's name, plus, for a term spanning `k` slashes, a check of the `k` ancestors' names;
  - a trailing `/` holds strictly below a matching dir;
  - a `$`-anchored regex whose tail can't cross a `/` becomes a name filter (all four `regex` queries in the set qualify). Any other regex needs a full scan: `mem` refuses it, and `duck` scans.

**Jobs** (us-east1, outputs and run records under `gs://oa-gcs-usage-dvx/scratch/bench/serving-box/out/<job>/`). The final runs:

| job | machine | what | run time |
|---|---|---|---:|
| `sb-build-20261003-014239` | n2-highmem-32, 4 local SSDs | build the index (21 min) and upload it to `…/serving-box/index/2026-10-01/` (39.4 GB, plus 1 GB of sort orders added by `sb-py-aug-20261003a`) | 1,245 s |
| `sb-target-mem-20261003d` | n2-highmem-8, 1 local SSD | `mem`, cold from GCS (all 105) and warm from NVMe | 517 s |
| `sb-target-20261003-020552` | n2-highmem-8, 1 local SSD | `duckdb` on NVMe (all 105), then both engines through gcsfuse and rclone mounts | 2,059 s |
| `sb-target-mounts-20261003c` | n2-highmem-8, 1 local SSD | the mounts again, with allocated (not apparent) NVMe footprints | 844 s |

The other `sb-*` jobs were debugging runs: three failed builds, two more deleted mid-run (a hung download and a nested-loop join), a debug script, and two intermediate `mem` runs. All jobs together came to about 1.8 h of n2-highmem-32 and 1.5 h of n2-highmem-8, **roughly $5**.

#### Engines on the box's size (n2-highmem-8: 8 vCPU, 64 GB)

| | `mem` (custom index) | `duckdb` (names-first) |
|---|---|---|
| exact | **105 / 105** | **105 / 105** |
| latency p50 / p90 / max (roots + totals) | **0.44 s / 2.1 s / 5.2 s** | 2.0 s / 12.1 s / 91 s |
| plus listing every root path (bench only) | p50 0.67 s; max 62 s (`step-`: 18.7M roots) | p50 2.2 s; max 128 s |
| resident after load / peak | 40.6 GB / 49.4 GB | 11.8 GB / 39.7 GB (DuckDB `memory_limit` 44 GB) |
| cold load (GCS → NVMe → RAM) | 138 s: 120 s copy (40.4 GB, one file at a time) + 18 s load | 26 s: 14 s copy (9.9 GB) + 2 s footer + 10 s vocabulary |
| warm load (from NVMe) | 55 s (one local SSD, page cache dropped) | 12 s (files already local) |
| build per generation | 21 min on n2-highmem-32 (DuckDB peaked at 175 GB RSS) | none (reads the published files) |

What the `mem` index holds per generation, for 778,372,702 nodes and 127,349,049 names:

| arrays | GB |
|---|---:|
| `parent`, `nid`, `o` (int32 each) | 9.3 |
| `b` (int64) | 6.2 |
| `depth` (uint8) | 0.8 |
| name → nodes (`name_nodes` + `name_off`) | 3.6 |
| node → children by size (`child` + `child_off`) | 6.2 |
| vocabulary: `name` (original case) + `l` (lowercase), one Arrow blob each | 13.2 |
| vocabulary sort orders (`vfwd`, `vrev`) | 1.0 |
| **total** | **40.4** |

Each cell below reads: roots · `mem` seconds / `duckdb` seconds. The third column is `marin-us-central2/grug` (`marin-us-east5` for `tomat`).

| query | store root | `marin-us-central2` | third view |
|---|---|---|---|
| `tomat` | 21,739 · 2.25 / 2.80 | 4 · 1.70 / 0.64 | 9 · 1.69 / 0.64 |
| `grug` | 7,724 · 0.66 / 0.91 | 5,703 · 0.67 / 0.65 | 1 · 1.35 / 5.55 |
| `safetensors` | 89,122 · 0.32 / 3.04 | 22,899 · 0.30 / 1.15 | 240 · 0.29 / 0.67 |
| `ckpt -eval` | 165 · 2.14 / 8.03 | 22 · 1.95 / 2.66 | 0 · 1.91 / 1.44 |
| `/tomat` | 400 · 0.01 / 0.49 | 0 · 0.00 / 0.42 | 0 · 0.00 / 0.43 |
| `tomat/` | 22 · 0.00 / 12.34 | 0 · 0.00 / 12.39 | 0 · 0.00 / 12.08 |
| `checkpoints/step-` | 204,279 · 0.64 / 21.80 | 102,269 · 0.64 / 2.02 | 47,476 · 0.61 / 1.24 |
| `tensorstore` | 38 · 0.25 / 0.67 | 1 · 0.25 / 0.59 | 0 · 0.25 / 0.64 |
| `opt_state` | 48,615 · 0.32 / 1.34 | 39,710 · 0.31 / 1.14 | 0 · 0.31 / 0.61 |
| `config.json` | 527,874 · 0.39 / 67.72 | 88,330 · 0.34 / 28.95 | 11 · 0.32 / 3.78 |
| `isoflop-3e+20` | 676 · 0.23 / 0.70 | 186 · 0.23 / 0.61 | 0 · 0.23 / 0.58 |
| `27f2fb` | 42 · 0.45 / 1.01 | 4 · 0.44 / 0.83 | 0 · 0.44 / 0.79 |
| `781bc3291c81ce28` | 3 · 0.20 / 0.64 | 0 · 0.19 / 0.62 | 0 · 0.19 / 0.61 |
| `step-10000` | 25,666 · 0.32 / 1.60 | 9,806 · 0.30 / 0.90 | 1,087 · 0.30 / 0.70 |
| `00001-of` | 33,355 · 0.42 / 11.24 | 8,397 · 0.40 / 4.76 | 5 · 0.39 / 0.86 |
| `model-*-of-00004` | 7,138 · 0.34 / 3.17 | 2,715 · 0.32 / 2.98 | 0 · 0.32 / 2.89 |
| `shard_00` | 1,488,639 · 1.83 / 4.87 | 226,814 · 1.72 / 2.02 | 0 · 1.70 / 0.73 |
| `eval` | 154,194 · 1.29 / 7.45 | 61,971 · 1.28 / 2.12 | 63 · 1.27 / 0.89 |
| `checkpoint` | 182,512 · 0.37 / 4.28 | 30,630 · 0.34 / 1.36 | 1,890 · 0.33 / 0.74 |
| `step-` | 18,695,353 · 5.24 / 42.09 | 174,601 · 3.16 / 3.45 | 47,481 · 2.96 / 1.58 |
| `podcast` | 7 · 1.96 / 14.40 | 0 · 1.95 / 0.74 | 0 · 1.92 / 0.74 |
| `grug -checkpoints` | 2,619 · 1.09 / 4.95 | 1,451 · 0.98 / 1.87 | 1 · 0.98 / 1.09 |
| `-checkpoints` | 1 · 0.34 / 7.37 | 1 · 0.31 / 4.36 | 1 · 0.30 / 0.71 |
| `-tensorstore` | 1 · 0.25 / 0.68 | 1 · 0.25 / 0.62 | 1 · 0.25 / 0.65 |
| `nemotron -dclm\|dolma` | 76,015 · 2.18 / 4.44 | 3,036 · 2.12 / 2.61 | 13 · 2.08 / 1.78 |
| `qwen sft` | 21,124 · 1.85 / 5.39 | 2,834 · 1.78 / 2.06 | 0 · 1.77 / 1.24 |
| `tootsie\|isoflop` | 8,533 · 0.80 / 1.75 | 1,277 · 0.78 / 1.19 | 541 · 0.78 / 1.19 |
| `moe 1e23` | 409 · 1.65 / 32.26 | 374 · 1.67 / 4.99 | 373 · 1.67 / 2.17 |
| `*.pt` | 91,346 · 1.00 / 5.52 | 60,292 · 1.01 / 5.15 | 0 · 0.98 / 4.78 |
| `.npy` | 20,392,645 · 4.95 / 91.23 | 377 · 2.85 / 30.59 | 0 · 2.69 / 4.42 |
| `.wav` | 128 · 0.68 / 0.71 | 0 · 0.67 / 0.70 | 0 · 0.68 / 0.66 |
| `\.safetensors$` (regex) | 76,381 · 0.07 / 4.84 | 18,223 · 0.05 / 3.29 | 235 · 0.04 / 2.93 |
| `/step-[0-9]+$` (regex) | 317,306 · 0.26 / 5.10 | 144,822 · 0.24 / 3.45 | 47,480 · 0.23 / 2.85 |
| `^marin-us-central2/grug/[^/]*moe[^/]*$` (regex) | 1,003 · 0.97 / 6.83 | 1,003 · 0.97 / 5.39 | 1,003 · 0.97 / 5.30 |
| `tokenizer\.json$` (regex) | 51,539 · 0.06 / 4.57 | 9,832 · 0.04 / 3.23 | 5 · 0.03 / 2.98 |

Reading it:

- **`mem`'s time is the vocabulary scan.** Each answer records per-step times. Everything after the scan (names → nodes, the ancestor walks, roots, totals) takes milliseconds, except the two 20M-root sets (`step-`, `.npy`), where finding the outermost roots takes 3 s. How each kind of name test is answered:
  - anchored tests (`/tomat`, `tomat/`, `^step-`, `….json$`) are binary searches over the vocabulary sorted forward and by reversed bytes: milliseconds;
  - `contains` walks the 6.6 GB lowercase blob with libc `memmem`, from threads (a foreign call releases the GIL) when hits are sparse: 0.2–0.4 s. When hits are dense (`podcast` is in 811K names; `eval`, `qwen`, `shard_00`), a vectorized numpy pass takes over, which is memory-bound at about 1.2 s on 8 vCPU;
  - regexes run RE2 only over the names that hold their longest literal run.

  The remaining lever is a compiled single-pass substring scan (Rust or numba), which would bring the dense case near 0.3 s. More vCPUs should also help, since the pass is bandwidth-bound; that wasn't measured.
- **DuckDB's time is decoding `path` row groups**, phase 0's floor. Many matched names (`podcast`: 811K; `moe 1e23`: 4,415 row groups) or a broad term (`config.json`, `.npy`: past 30% of the file, so a full scan) cost 14–91 s. A trailing `/` costs 12 s: one pushdown read per matching dir.
- **Watch DuckDB's `hash()` when keying paths.** The first build keyed nodes by DuckDB's 64-bit `hash(path)` and silently merged about 690K distinct paths (sibling `model-0000{1,3}-of-00004.safetensors` files among them). That made 11 of the 105 answers `FAIL`, short by up to 315,629 roots. The index now keys by `md5_number(path)` (128 bits), and a second, independent hash must agree within each key, so a collision raises instead of merging. Never key paths by DuckDB's `hash()`.
- **Owner slices are rare in the `path` sort.** 778,376,239 rows hold 778,372,702 distinct paths.

#### Cache layers (n2-highmem-8, one 375 GB local SSD)

| layer | `mem` index load through it (40 GB) | `duckdb` vocabulary load, cold / warm | `duckdb` query p50 / max, cold / warm (7-query subset × 3 views) | NVMe held per generation for the `duckdb` subset (allocated) |
|---|---|---|---|---|
| copy first (parallel ranged GETs, as in phase 0) | 120 s + 18 s | 14 s copy + 10 s | 2.8 s / 14.4 s (from NVMe) | 9.9 GB (whole files) |
| gcsfuse file cache (`--file-cache-max-size-mb -1 --file-cache-cache-file-for-range-read --file-cache-enable-parallel-downloads`) | **89 s** | 13.8 s / 9.4 s | 2.9 s / 14.5 s, then 2.7 s / 14.3 s | 9.9 GB (whole files) |
| `rclone mount --vfs-cache-mode full` (64M chunks, 16 streams) | 163 s | 25.6 s / 12.0 s | **3.6 s / 134 s**, then 2.8 s / 15.7 s | **2.1 GB** (sparse: only the ranges read) |

- **gcsfuse is the fastest way to a hot generation.** Its parallel whole-file download (89 s for 40 GB) beat our one-file-at-a-time copy (120 s) and rclone's chunked reads (163 s). After the first touch, DuckDB over the gcsfuse cache runs at NVMe speed. Mounting takes under 1 s for either.
- **rclone is the block-granular option, and it costs cold latency.** It held 2.1 GB of the DuckDB generation against gcsfuse's 9.9 GB, but its first reads are 10–20× slower, since each query fetches its row groups chunk by chunk (`ckpt -eval`: 134 s cold).
- **For `mem`, the access pattern is "read every byte once"**, so block granularity buys nothing: a generation is 40 GB on NVMe whichever layer holds it. A 375 GB local SSD holds about 8 generations, not a month's 30.
- DuckDB's `cache_httpfs` was not measured.

#### Recommendation

- **Engine: the custom in-memory index (`mem`).** It is exact on all 105 cases. On 8 vCPU it answers in 0.44 s at the median, 2.1 s at p90 and 5.2 s at worst (the 20M-root `step-` / `.npy` at the store root); selective terms take 0.03–0.5 s. DuckDB names-first is exact too, but its broad terms cost 14–91 s: it is the fallback for a generation that isn't loaded, not the primary.
- **Cache layer: gcsfuse's file cache on the local SSD**, LRU by size (`--file-cache-max-size-mb` about 300 GB). It holds the latest ~7 generations' index files (40 GB each) and the parquet the fallback reads, and it gave the fastest cold load (89 s to RAM). rclone's sparse cache only pays off for DuckDB-style range reads, and it makes them 10–20× slower cold.
- **VM size: n2-highmem-16 (128 GB), not -8, if diffs go through the box.** (Superseded by §6.3: the slimmed index holds two scans in n2-highmem-8.) One generation holds 40.6 GB resident and peaks at 49 GB. A diff needs two generations, which is over 64 GB even on a diet:
  - sharing one vocabulary across consecutive generations saves 14 GB on the second;
  - dropping the children CSR, which filters don't use, saves 6 GB;
  - an exceptions map for the original-case names saves about 3 GB (57M of the 127M names have upper case, 3.3 GB).

  The vocabulary sharing needs name ids that are stable across generations. With all three, two generations come to about 51 GB of arrays, plus the ~9 GB of working memory measured above resident: about 60 GB, too tight for a 64 GB VM (the container got 62 GB). n2-highmem-16 is roughly 2× §5's n2-highmem-8 price (about $760 a month on demand, $480 with a 1-year commitment; e2-highmem-16 about $520 on demand). If phase 4 forwards only `/api/subtree?q=` (one generation), n2-highmem-8 holds it, and the second generation's diff can fall back to DuckDB over the NVMe cache.
- **Build the index in the daily Batch job, not on the box.** The build takes 21 min and 175 GB of DuckDB memory on n2-highmem-32. The job publishes the index under `index/<gen>/mem/`, and the box's `/reload?gen=` pulls it through gcsfuse: about 90 s to hot.
- **Expected latency behind the Worker**, as measured on n2-highmem-8 (n2-highmem-16 should be no slower): 0.05–0.5 s for anchored and selective terms, 1–2 s for dense substrings, and about 5 s for the 20M-root sets at the store root, plus the Worker→tunnel→box hop (unmeasured). Against today's Worker (§6.1: median 6.9 s, 71 of 105 flagged), every answer would be exact and unflagged. The box's response draws a tree, so it never lists millions of roots: the bench's 62 s to list `step-`'s 18.7M root paths doesn't apply.
- **Sizes that need the box.** gcs (778M rows) does. cw-s3 is 12× smaller (about 63M rows per scan): its Worker-only cold probe takes 4.7 s at the root, 5.2 s at a bucket, 6.8 s for a filter hit and 1.5 s for a miss. Scaled by rows, cw's `mem` index would be about 3.3 GB, small enough for a small VM, or for gcs's box. Whether cw needs a box at all depends on how often its filters come back flagged; the per-scan cold-probe records landing under `gs://oa-gcs-usage-dvx/probes/cw/` will show that.

### 6.3 Phase 3 results: the slim index and `dt-cloud serve-query` on gcs 2026-10-01

The index was slimmed first, since its size decides where the box can run. Then `dt-cloud serve-query` serves it over HTTP. Both ran on Batch against the phase-1 truth, on the box's size (n2-highmem-8, one local SSD).

**Code** (on `cloud`; gcs's Batch wrappers `job/serving-box-{submit,bench}.sh` gained `STEPS=…,mmap,serve`):

- `dt_cloud.bench.mem`, index format 2 (`dt-cloud bench-index` writes it; format 1 no longer loads).
- `dt_cloud.box.view` ports `view.ts`'s filter branch, `buildView` and `buildDiff`; `dt_cloud.box.server` serves it.
- `dt-cloud serve-query`, and `deploy/serve-query/Dockerfile`.

#### The index, format 2

Where the 40.4 GB went (§6.2's table) and what each structure became. 778,372,702 nodes, 127,349,049 names:

| structure | format 1 | format 2 | how |
|---|---:|---:|---|
| node → parent | 3.1 | 3.1 | int32; nodes renumbered breadth-first (depth, parent, bytes descending), so `parent` is non-decreasing |
| node → children (`child`, `child_off`) | 6.2 | 0 | a node's children are one id range, found by binary search on `parent`, already largest first |
| node → depth | 0.8 | 0 | a depth is an id range (`dstart`) |
| node → name (`nid`) | 3.1 | 3.1 | int32 |
| bytes `b` | 6.2 | 3.1 | uint32, plus an overflow table for the 289,184 nodes past 4 GiB |
| objects `o` | 3.1 | 0.8 | uint8, plus an overflow table for the 370,541 nodes past 254 |
| name → nodes | 3.6 | 3.6 | unchanged |
| vocabulary | 13.2 | 7.3 | lowercase blob (5.6) + offsets (1.0); the original case as one bit per byte (0.7), plus the 75,006 names whose case isn't plain ASCII stored whole (7 MB) |
| vocabulary sort orders | 1.0 | 1.0 | unchanged |
| **resident** | **40.4** | **22.1** | 28 bytes per node, from 52 |
| cold detail (`detail.parquet`) | — | 3.8 | not loaded. Per node: owner, kind, mean write time, last read, class bytes. Read by row group for the nodes a response shows, plus every root (the root and ancestors' totals). |

Owner slices are in the detail: 3,254 paths have more than one, kept slice by slice in `slices.parquet`. The response's per-node fields (`d`, `a`, `cb`, `us`) and the `o=` / `cl=` scopes come from them.

Not done, still levers:

- `parent` by rank over a bitmap (−2.7 GB);
- 32-bit vocabulary offsets (−0.5 GB);
- one vocabulary shared by consecutive scans (−7 GB on the second).

None is needed for the targets below.

#### Measured (n2-highmem-8: 8 vCPU, 64 GB)

| | format 1 (§6.2) | format 2 |
|---|---|---|
| exact, in-process (`bench-engine`) | 105 / 105 | **105 / 105** (read, and mapped with `-M`) |
| exact through HTTP (`probe -Q` against `serve-query`, full responses) | — | **105 / 105** |
| latency p50 / p90 / max (roots + totals, in-process) | 0.44 / 2.1 / 5.2 s | **0.44 / 1.9 / 4.9 s** |
| `serve-query` time to first byte p50 / p90 / max (search, aggregates, tree, every root path sorted) | — | 1.29 / 3.3 / 82 s; 1.23 / 3.3 / 5.1 s without the three answers of over 1M roots |
| resident after load | 40.6 GB | **22.4 GB (20.9 GiB)** |
| peak, `bench-engine` (it lists every root path) | 49.4 GB | 31.2 GB |
| peak, `serve-query` over all 105 cases | — | 30.4 GB (28.3 GiB): 21.1 GiB loaded, 7.2 GiB more for the 20M-root answers |
| cold load: GCS → local SSD → RAM | 138 s (120 s copy + 18 s) | **102–109 s** (94–101 s parallel copy of 25.9 GB, index and detail, + 8 s read) |
| warm load from local SSD, page cache dropped | 55 s | **30 s** |
| mapped (`-M`) | — | 0.14 s, then pages fault in as queries touch them: the first pass's p50 / max was 0.67 / 10.6 s (before the regex fix below) |
| `serve-query` `/warm` from local SSD, page cache warm | — | 7 s |
| build | 21 min | 36 min on n2-highmem-32 (vocabulary 7.3 min with its sorts and case bits, nodes 15.3, arrays and renumbering 7.3, detail 3.1) |

Three bugs surfaced on the real data, all fixed and retested:

- **Drilled views took 25–127 s.** `np.searchsorted` with an int64 needle on the int32 `parent` copies the whole array to int64 first: 3 GB per child-range lookup. The needles are now cast to the haystack's dtype.
- **Regex verification was 5–7× slower.** Rebuilding original-case paths cost it (`/step-[0-9]+$`: 0.18 → 1.19 s). A case-insensitive regex is now tested on the lowercase paths; only paths holding a case exception are re-tested on their original case.
- **`serve-query` peaked at 33 GB.** The 20M-root answers held every root's full aggregate at once. Roots are now aggregated 2M at a time.

Reading it:

- **One scan fits a Cloud Run instance.** It holds 20.9 GiB resident; the worst request (the 20M-root `step-` / `.npy` at the store root) takes the process to 28.3 GiB of the 32 GiB.
  - On Cloud Run the files sit in the in-memory filesystem, so `-s /tmp/… -M` maps them and their RAM counts once.
  - `-D` leaves the 3.8 GB detail on GCS, read by range.
- **Two scans fit 64 GB, so n2-highmem-8 serves diffs.** 2 × 20.9 GiB resident, plus one request's working set (7.2 GiB at worst, measured), comes to about 49 GiB of the 62 GiB a container gets. §6.2's n2-highmem-16 isn't needed.
- **Latency is unchanged in-process.** Through HTTP, the server time grows with the number of match roots, about 5–10 µs per root. The time goes to listing every root's path, sorted, for `matches` and `matched`: it equals the bench's path-listing time (`config.json`: 528K roots, 3.7 s; `/step-[0-9]+$`: 317K, 1.7 s).
  - Reading the detail isn't the cost. Two variants barely moved the server p50, so neither was kept (the published index's detail is the 16K-row variant):
    - 16K-row detail groups: 1.27 → 1.23 s;
    - the detail as fixed-width mapped columns (13.2 GB): → 1.17 s.
  - Faster listing would build paths from shared ancestors, or the lists could be capped (§9).
- **Where the 20M-root answers go.** They take ~82 s and a response of about 1.5 GB, because `matches` and `matched` list every root, streamed.
  - The Worker's contract has no cap on those lists; it never got there, since its budgets cut the search.
  - Either the contract caps them, or the Worker forwards such a query asking for the tree only (§9).

**`serve-query`** answers `/api/subtree?q=` and `/api/diff?q=` with the Worker's bodies and scoping params (`path`, `w`/`h`, `minArea`, `atten`, `depth`, `o=`, `cl=`, `qs=`; `top`, `summary` on a diff).

- **Headers:** every response carries `x-query-engine: box;dur=…` and `server-timing`.
- **Errors, so the Worker can fall back:**
  - a scan that isn't loaded: 409;
  - a lens: 409;
  - a regex no name filter plans: 501;
  - still loading: 503 with `retry-after`.
- **`/healthz`:** `loading | ready | error` and the scans loaded; no auth.
- **`/warm`:** blocks until loaded. On Cloud Run with request-based billing, an instance has CPU only while a request is in flight.
- **Auth:** reads need `Authorization: Bearer $QUERY_BOX_TOKEN`.
- **Scans:** `ROOT/<date>/` index dirs, local or `gs://`. The latest loads by default; `-2` adds the one before, for diffs.

Deliberate differences from the Worker (the `dt_cloud.box.view` docstring):

- **Every match is found:** no `partial` / `approximate`, `truncated: false`.
- **Past 50K roots** the ones under the forest threshold fold into their parent's `(other)` (`folded: n`). Below 50K every root is drawn, as the Worker does.
- **A NOT-only query at the store root subtracts.** The Worker's `rootFor('')` doesn't (§6.1).
- **The diff's lookups are exact and uncapped**, and a store-root match covers the paths under it.
- **Ties** between equal-sized siblings are ordered by path.

**Parity.** `site/functions/_lib/boxParity.test.ts` writes the Worker's bodies for 39 subtree and 3 diff cases over the `v2-search` fixture (`box-parity.json`). `cloud/tests/test_box.py` answers the same cases with the box and asserts equality field by field, except `tier` / `index` and the coverage flags. All are equal but one: the store-root NOT-only case, where the box's answer is asserted against the Worker's error.

The tests also cover:

- the owner and class scopes, on a generation with multi-slice paths;
- the overflow columns and case exceptions;
- chunked aggregation;
- a diff with changes;
- the server's statuses.

**Batch spend this phase:** one build (n2-highmem-32, about 50 min), four n2-highmem-8 bench runs and one detail rewrite, about $3. With phase 2's ~$5, the serving box's Batch total is about $8.

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
- **Caps on `matches` / `matched`:** the box lists every root, so a 20M-root answer (`step-`, `.npy` at the store root) is a 1.5 GB response and ~80 s of listing (§6.3); the Worker never got there, since its budgets cut the search. The contract could cap the lists, with a count and a flag, or the Worker could ask for the tree only.
- **Anchors in the simple syntax:** `^tomat` / `tomat$` as sugar. `/tomat` and `tomat/` already work as segment-start and segment-end matches.

## 10. Deployment modes and costs

`serve-query` runs unchanged in each mode. The Worker's only switch is `QUERY_BOX_URL` (§6, phase 4). The numbers below are for gcs, with one scan at about 21 GiB resident and a cold load of about 100 s from GCS (§6.3).

- **Cloud Run, request-based billing:** 8 vCPU / 32 GiB per instance (the maximum). One scan fits; diffs don't (32 GiB is one scan). Diffs fall back to the Worker.
  - **Price:** $0.000024 per vCPU-second plus $0.0000025 per GiB-second, while the instance handles a request. That's $0.000272 per second for 8 vCPU / 32 GiB, about $0.98 per busy hour. An idle instance that isn't a min instance is free.
  - **Cold start:** an instance starting from zero loads the scan, about 100 s, which is about $0.03 per cold start. Requests in the meantime get 503s, and the Worker answers them.
    - The caller (the Worker on a 503, or the daily job after publish) holds `/warm` open, which keeps the instance's CPU.
    - The files go to the in-memory filesystem with `-s /tmp/idx -M`, so they cost their RAM once. `-D` keeps the detail on GCS.
  - **Usage example:** 1,000 filtered views a day at ~1.5 s each is about 25 busy minutes a day. With a cold start or two a day, that's about $14 a month.
  - It's the cheapest mode at low use, and the worst first query: up to two minutes of Worker answers while the instance loads.
- **GCE VM:** n2-highmem-8 (64 GB), one or two scans, so diffs too. The prices are §5's:
  - **24/7:** about $380 a month on demand, about $240 with a 1-year commitment. n2-highmem-4 (32 GB, half the price) holds one scan, with less CPU for the vocabulary scan.
  - **Stop/start on a schedule:** billed only while running. Business hours, about 50 h a week, cost about $115 a month. Each start reloads from GCS in about 100 s; a local SSD doesn't keep its data across a stop.
  - **Spot:** discounted and preemptible. The MIG's autohealing restarts it, and each restart reloads the scan (~100 s) while the Worker answers.
  - **1-year commitment:** only worth it 24/7, and once a month of use has been measured (§4.4).
- **A small or local box, for small fleets:** the index costs about 28 bytes a node, plus the detail on disk.
  - cw-s3 (about 63M rows per scan) needs about 1.8 GB resident. Any small VM holds it, or a laptop or home box behind the tunnel, at roughly $0–50 a month.
  - The local tier is the app build (§6, phase 4).
- **The Worker-only tier:** no box, no cost beyond the Worker. Filters come back `partial` / `approximate` past the search budgets (§6.1: 71 of 105 flagged on gcs, median 6.9 s). This is the default when `QUERY_BOX_URL` is unset.

**Recommendation for gcs:** Cloud Run first, at about $10–20 a month. It answers single-scan filters, with diffs left to the Worker. Move to a stop/started or committed n2-highmem-8 only if the cold starts or the missing diffs show up in the probes. Provisioning either is phase 5, and needs a go.

[phase 0]: filter-query-service-p0.md
