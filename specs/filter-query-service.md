# Filter query service: exact path filters at any scale

Status: proposed (2026-10-02), reviewed by the root session (names-first evaluation, early hand-off, match-volume grouping, phase 0). Owner: the gcs session. Lands on `cloud` (shared); deployments opt in.

## 1. Why

The path filter (`q=`) is served by the Pages Function that serves everything else: a Worker with ~128 MB of memory, limited CPU, and 6 concurrent subrequests. [`path-store-search.md`] made it exact on small stores (r2: 1.24M rows, every query exact, ≤ ~2 s). At gcs scale it can't be, and it says so (`partial`) instead of missing matches silently. Measured on gcs's 2026-10-01 generation with search sidecars (v1 layout, 778M rows, 127M distinct names, 5.2B trigram postings):

| query | trigram candidates | result |
|---|---:|---|
| `tomat` | 52,586 | 4 matches, `partial` (128 name groups + 1.5 s) |
| `grug` | 8,308 | 13 matches, `partial` (same) |
| `safetensors` | 1,327 | 0 matches, `partial` (96 path groups) |
| `ckpt -eval` | 166 | 0 matches, `partial` (1.5 s) |

Two limits, neither fixed by a layout change:

- **Candidate volume.** With 127M names, even a specific term shares its trigrams with thousands of names; verifying them is many reads. Layout v2 (name-major rows) cuts reads per candidate but not the number of candidates.
- **Match volume.** A file-name term (`safetensors`, `.json`) matches a file in nearly every run directory: millions of rows. The answer is a rollup of millions of rows. That is a scan-and-aggregate job, not a request-sized read.

## 2. What

A small query service, **`dt-query`**: a container running DuckDB over the same path-store files in GCS. It answers the filter view's two questions exactly:

1. **Match roots:** the outermost paths satisfying the query AST (OR of AND-groups, plus whole-query NOT; [`path-store-search.md`] §1.1), with each root's net aggregates (`b`, `o`, class mix, owners), after NOT subtraction.
2. **The drawn tree:** for a view root `P`, canvas `w×h` and `minArea`, every node under the match roots at or above the size threshold (the same threshold and attenuation as `view.ts`), and `(other)` remainders. This is the same `TreeNode` shape `/api/subtree` returns, so the client doesn't change.

Plus the diff (`/api/diff?q=`): the same two reads on both scans, joined, with `buildDiff`'s row semantics.

The Worker stays the front door. The service is a backend it calls only when its own read can't answer exactly within budget.

## 3. Request flow

1. The client calls `/api/subtree?q=…` as today.
2. The Worker parses `q` (the shared parser, so syntax errors stay 400s). Before spending its own budget it estimates the query's cost from the trigram directory: over a threshold (e.g. >2K candidate names), NOT-only, file-extension-shaped terms (`.json`, `safetensors`), or the `regex` syntax on a large store go straight to step 4. Otherwise it tries its own path: the index with budgets.
3. If the Worker's answer is exact (no `partial`, no `approximate`), it returns it. Selective queries on indexed scans stay sub-second and never touch the service.
4. Otherwise the Worker forwards the parsed AST (not the raw string) plus the view parameters to `dt-query`, and returns its answer, cached like any other view response (immutable per generation and parameters).
5. If `dt-query` is down or over its own deadline, the Worker returns its own flagged `partial` answer. Never silently short.

The client may show the Worker's partial first paint while the exact answer loads (the existing `firstPaint` mechanism), flagged as such.

## 4. The service

- **Runtime:** Cloud Run (gen2), Python + DuckDB, one container image built from `cloud/` (a `dt-cloud serve-query` command), so the AST evaluator is shared with the job's code and tested alongside it.
- **Auth:** private (no unauthenticated invoke). The Worker calls it with a Google ID token for a dedicated service account (key in a Pages secret, like `GCP_SA_KEY` for sweep dispatch). The Worker has already authorized the viewer.
- **Data:** reads the generation the Worker names (`listing/<date>/index/<gen>/`), via DuckDB `httpfs`/GCS. The pointer and scan list stay in D1. The Worker passes the generation, and the service never reads D1.
- **Evaluation:** names first, `path` as the fallback.
  - **Hot generations (with sidecars):** the names vocabulary (127M names, 1.45 GiB compressed on gcs 10-01) is pinned decoded in instance memory per hot generation (a few GB). The AST's segment terms are evaluated against it with vectorised `contains`/`ILIKE`, or `regexp_matches` for the regex syntax (~1–2 s warm). Matching name ids then map to rows by range reads: v2 `rows` is name-major, and v1 lists each name's `path` row groups.
  - **Unindexed generations:** a scan of the `path` column with the AST compiled to SQL, using predicate pushdown on `depth`/`path` ranges for a drilled view root. That's a cold scan of several GB (estimate 10–30 s), so it is the fallback, flagged slow in the UI while it runs.
  - **Match volume:** matched rows are grouped by parent directory first (the `(depth, path)` sort clusters siblings). Where a whole directory matches, its existing dir aggregate is used instead of expanding its files. Match roots are an anti-join of matched paths against matched ancestors. The drawn tree is a `GROUP BY` over ancestors, cut at the view's threshold as `view.ts` does. The match-roots list in the response is capped with a total count (e.g. `roots: 1,071+`), like the series chart's cap.
  - So sidecars aren't needed for correctness, but they make the service fast. That feeds the retention decision: hot scans get sidecars, older ones take the `path` fallback.
- **Input:** only the AST as JSON, validated against a strict schema. No raw SQL, and no regex passed through except as an AST matcher compiled by the service. On large stores the `regex` syntax is service-only, since `regexp_matches` over the vocabulary is cheap there but unbounded in a Worker.
- **Caching:** results are keyed by the canonicalised AST (AND/OR operands sorted, case folded, so `a b` and `b a` share an entry) plus generation, view root, canvas and `minArea` bucket. The key is used both in the service's in-instance LRU and in the Worker's edge cache, which sits in front, so a repeated view never reaches the service.
- **Sizing:** start at 4 vCPU / 16 GiB and measure. A diff pins two generations' vocabularies; 16 GiB should hold two gcs generations, but measure. Targets on a warm instance with a hot generation: selective terms under ~1 s, `safetensors` at the gcs root under ~3 s. Phase 0 sets the cold numbers.

## 5. Costs and trade-offs

- **Cold starts:** `min-instances=0` keeps the idle cost near zero, but the first heavy query after idle waits for container start plus loading the vocabulary (estimate 10–20 s). A warm-up ping from the daily job after each scan publishes (beside `warm-cache`) pre-loads the new generation, so the day's first real query isn't the cold one. `min-instances=1` at 4 vCPU / 16 GiB is roughly $150–250/month always-on. Start at 0 with the ping, and measure.
- **Egress:** none. Same region as the data bucket (`us-east1`), so the service should run in `us-east1`, not the job's `us-central1`.
- **Interaction with the search sidecars:** the service is exact without them (the `path` fallback), but they make it fast: its names-first path reads them, and they keep selective queries in the Worker. Retention is then "how many recent scans get the fast path", not a correctness question.
- **What it doesn't fix:** plain (unfiltered) views and diffs still run in the Worker. Their remaining cost is decode CPU (pure-JS zstd), tracked separately.

## 6. Phases

0. **Measure before building.** On a Batch VM in us-east1, run plain DuckDB over 10-01's files: `tomat`, `grug`, `safetensors` and `ckpt -eval`. Compare a cold `path` scan with names-vocabulary-first evaluation (load time, decoded memory, per-query time), and the match-volume grouping on `safetensors`. This settles §4's strategy and the sizing before any infra.
1. **Service + AST → SQL**, with tests: exact parity with `matchRoots` + NOT subtraction on the existing fixtures, run against the same parquet files the vitest fixtures use.
2. **Worker hand-off** behind a deployment var (`QUERY_SERVICE_URL`; absent = today's behaviour), with the flags rules in §3. `dt-cloud probe` gains broad-query scenarios (`safetensors`, a 3-character common term).
3. **Infra:** `infra/gcp` component for the Cloud Run service, its SA and the invoker binding (the gcs stack adds an instance), plus a `deploy/query-service/` build like `sheet-mirror`.
4. **Measure on gcs**, then decide `min-instances`, and the sidecar retention window (the root session's proposal: last ~7 scans plus pinned dates).

## 7. Open questions

- **Diff on two scans:** one DuckDB query over both generations (simpler, and keeps the join exact), at the cost of pinning both vocabularies.
- **User-lens queries with claims:** the service needs the ledger's claims (D1). Simplest: the Worker passes the relevant claim regions in the request, as it already computes them.
- **Where the AST compiler lives:** TypeScript (Worker) and Python (service) both need it. Keep the AST JSON schema in one place (the TS types plus a JSON-schema test fixture), with an evaluator per language and parity tests over one shared case table.

[`path-store-search.md`]: path-store-search.md
