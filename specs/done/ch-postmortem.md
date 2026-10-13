# ClickHouse post-mortem, and where DuckDB fits

Status: written 2026-10-10, after the ClickHouse VM and code were retired the same day ([retirement]). Audience: the 2026-10-20 demo, and anyone asking "why not ClickHouse / DuckDB / MotherDuck?". Every number below cites a spec, and the section in it where it has one. Anything not taken from a source is marked *inferred*.

## 1. TL;DR

- **ClickHouse won its benchmark and lost on architecture.** On the 105-query filter bench it was exact on all 105, with warm p50/p90/max of 0.20/1.46/4.33 s. That beat our 41 GB in-RAM custom index (0.44/2.1/5.2 s) without holding the data in RAM ([serving-options §6.1][so]). But it needed an always-on VM, about $728/month at list price by the end ([retirement, Cost][retire]).
- **We tried it for one week** (2026-10-03 → 10-10). In that week we built an append-only SCD-2 history store, frozen numeric read models, aggregate-first "coarse" treemaps, "hot" L1/L2 name catalogs, a consolidated name index over 68 scans, four native C++ kernels and a query box behind a Cloudflare tunnel. The retirement deleted 254 files and 98,447 lines (`git show --stat c41a5778`). The code survives at tag `ch-store-final`.
- **Broad exact search never got interactive.** A global `zarr.json` treemap over 68.4M matches took 153 s optimized, against 576 s for the canonical plan ([clickhouse.md §2][arch-ch]). Retrieval was fast. Aggregation was the bottleneck: ancestor rollups and folded remainders over tens of millions of matches.
- **What replaced it is static files on R2, read by the Worker:** suffix-ordered name postings, a catalog plus "drill" files for heavy terms, and an SCD-2 interval store for trees and diffs. A ~100K-row rare term costs ~0.3–0.7 s end to end, against 3.8–5 s cold on ClickHouse ([static-name-search, Reading it from a Worker][sns]). There is no server on the query path. The daily build is ≈ $0.53/day ([retirement, Cost][retire]).
- **ClickHouse was genuinely good at history.** Seven daily scans in SCD-2 took 1.009 scan-equivalents of storage. As-of views, diffs and series came back in ~0.1 s warm and ~0.3 s cold ([serving-options §6.2][so]). Its ingest was also the independent oracle we used to verify the static index's 1.15B intervals ([static-name-search, Full build][sns]).
- **DuckDB is our batch workhorse, not a serving engine.** It runs the out-of-core aggregation, every external sort that cuts a path-store tier, the interval-store and static-name builds and merges (some through pyrmts), and most of our verification oracles. It always runs as a library inside a Batch task, with a `memory_limit` and a spill directory sized to the machine.
- **DuckDB's pain points were memory and spill at the 100M–600M-row scale.** We measured RSS of 169 GB against a 100 GB `memory_limit`, sorts that over-commit instead of spilling, and a single sort that spilled more than 100 GiB ([mgu-scale-a3-gate][a3], [depth-partitioned-finalize][dpf]). The biggest aggregation moved to a hand-written streaming engine, because our listings arrive already sorted.
- **The gap is still open:** an append-only, versioned, row-group-pruned parquet store on object storage, planned from a SQL-held footer index and readable from an edge isolate under 128 MB. Iceberg and R2 SQL (now "Basin") cover the metadata half, but not the edge read ([index-landscape §6.3][landscape], [r2-sql-eval][r2sql]).

## 2. The problem we were solving

- **Scale:**
  - The fleet was 3 PiB and 613M objects across six GCS buckets on 2026-09-05 ([mgu-scale-unification][msu]).
  - One scan is ~778M path rows (objects plus directories) with 127M distinct names. The `path` column is 3.6 GiB compressed but 95 GiB decoded ([serving-options, Baseline][so]).
  - Over 70 scans (2026-07-30 → 10-08) that came to 1,153,480,980 path versions ([interval-store §1][iv]).
- **Queries:**
  - Exact, case-insensitive name search (`zarr.json`, `5418`, `^ckpt`, `.npy$`) at any scan.
  - The result is not a list. It is a **filtered treemap**: every matching byte is accounted for, bucketed under the view's children, with an exact `(other)` remainder. Diffs (dTM) and series work the same way.
  - "The problem is aggregation, not just retrieval" ([alternatives][alt]).
- **Front end:**
  - Cloudflare Pages Functions (Workers): ~128 MB per isolate, 6 concurrent subrequests, no state between requests ([serving-options, Baseline][so]; [filter-query-service §1][fqs]).
  - Before this work, the Worker answered 71 of the 105 bench queries truncated, with a 6.9 s median ([serving-options, Baseline][so]).
- **Latency target:** interactive. The working gates were "within 2× of the in-RAM index, max ≤ 10 s" for the bench ([serving-options, Proposed benchmark][so]) and a 1–3 s cold budget for views ([r2-sql-eval §3][r2sql]).

## 3. What we tried with ClickHouse, in order

### 3.0 Why ClickHouse

- **It is a columnar server with the indexes this workload wants:**
  - MergeTree sorting keys, so a subtree is one primary-key range;
  - projections, which give a second physical order maintained on every insert;
  - minmax skip indexes;
  - an inverted text index with `ngrams` tokenizers that accelerates `LIKE` (GA in 26.2; we ran 26.9.8);
  - materialized views that fan an insert out to several tables ([serving-options §4.3, §6][so]).
- **It is disk-backed.** It doesn't need the 41 GB of RAM per scan that the custom index held ([serving-options §4.3][so]).
- **SCD-2 history fits it naturally.** The estimate was ~1.6B rows for 1,000 daily scans at 0.1% churn, a 2–3× single-scan problem rather than a 1,000× one ([serving-options §4.6][so]).

### 3.1 Bench B: one scan plus 7 days of history (2026-10-03, ~$4 for two experiments)

- **Setup:** an n2-standard-8 (8 vCPU, 32 GB) with a 500 GB pd-ssd ([serving-options §6][so]).
- **Layout:**
  - `names` with a trigram text index;
  - `nodes` ordered by DFS `pre`, so a subtree is a `pre` range;
  - `nodes_by_name`.
  - One scan came to 23.1 GB on disk ([§6.1][so]).
- **Latency:** 105/105 exact. Warm 0.20/1.46/4.33 s; cold (caches dropped) 1.03/4.67/6.25 s. Regexes cannot use the text index, so cold `match` reads the whole 3.6 GB `names` table, e.g. 0.67 → 6.25 s ([§6.1][so]).
- **Memory:** 1.6–1.9 GB resident between queries, a 2.8 GB peak and an ~11 GB working set ([§6.1][so]).
- **History over 7 scans:**
  - 569M object versions (4.2 GB) and 218M dir versions (2.4 GB), against 19 GB of raw rows;
  - as-of views, view-level diffs and series take 0.10–0.11 s warm and 0.25–0.32 s cold ([§6.2][so]).
- **Caveat** that later mattered: these timings were for roots and totals, not complete rich treemaps ([alternatives][alt]).

### 3.2 The append-only store and the query box (2026-10-04 →)

- **Schema** ([ch-store §2][journal]):
  - `nodes` holds immutable versions of `(depth, path, usr)`, `ORDER BY (depth, path, usr, vf)`, `PARTITION BY vf`;
  - `closures` ends versions without rewriting them;
  - `changes` holds signed deltas, fanned out from a `Null` table by materialized views;
  - `names` carries the `ngrams(3)` text index;
  - two projections: `by_name` and `by_parent (depth, parent, size)`.
- **One index decision mattered more than any other:** `by_parent` plus a `size` minmax index took the root view from 209 s to 18 s, then to 1.3 s ([ch-store §2.2][journal]).
- **The query box:**
  - `dt-cloud serve-query -e ch` answered in the Worker's exact response shapes.
  - The Worker forwarded `/api/subtree|diff|series` to it after an edge-cache miss and fell back to its own reads on 409/501/503 or a timeout ([ch-store §4–5][journal]).
  - It reached the box through a quick tunnel at first, then a named Cloudflare tunnel ([retirement, Inventory][retire]).
- **Serving on 8 vCPU / 16 GB:**
  - Plain views took 1.0–1.5 s warm, against 1.7–3.2 s for the uncached Worker.
  - The full-response filter bench (every root listed) took 3.2/8.6/39 s warm.
  - Two store-root queries with 18.7M and 20.4M roots ran out of memory at both 16 and 32 GB ([ch-store §6.2][journal]).
- **Ingest:**
  - A daily v2 ingest took ~14 min on 32 vCPU and ~64 min on the 8-vCPU box ([ch-store §6.1][journal]).
  - The 10-05 ingest took 81.6 min, with explicit spill and memory bounds ([ch-store §7][journal]).
  - 30 scans took ~61 GiB on disk ([ch-store §6.1][journal]).
- **Wake-up:** about 20 s from suspend to a first answer and ~36 s from a stop ([ch-store §6.3][journal]). That fails a 10 s gate ([index-landscape §6.3][landscape]).

### 3.3 Making broad queries fast (2026-10-05 → 10-07)

- **Frozen numeric read models:**
  - Dense preorder IDs over the 10-04/10-05 union: 785,812,929 paths and 133.8M names, passing nine full-domain audits ([clickhouse.md §2][arch-ch]).
  - These are frozen union IDs, not an incremental allocator.
  - Numeric plans beat the canonical plan on 5 of 6 global TM/dTM requests (e.g. `tensorstore` 2.13 vs 6.36 s). They regressed on `config.json` (10.80 vs 5.96 s) ([clickhouse.md §2][arch-ch]).
- **The broad-query wall:**
  - Global `zarr.json` with 68.4M roots first failed at the 8 GiB query cap (error 241).
  - GraceHash then spilled 64 external-join parts (3.27 GB compressed, 17.52 GB uncompressed).
  - The first verified body took 153 s against 576 s canonical. Its scalar ancestor pass alone was 98 s ([ch-store §6.4 journal][journal]).
- **Finer granules are not fewer physical reads:**
  - `g64` cut logical reads from 7.07 GB to 1.10 GB, but OS reads went up, from 2.99 to 3.17 GB, and wall time rose ([ch-store §6.4][journal]).
  - At pd-ssd's advertised 480 MiB/s, ~3 GB of physical reads is a ~6 s floor (an estimate from documented limits, [ch-store §6.4][journal]).
- **Aggregate-first coarse serving:**
  - Sparse prefix summaries over leaf postings answered exact byte/count partitions for literal basenames.
  - Six cold `zarr.json` views over ~68.4M matches took 0.35–0.41 s ([clickhouse.md §3][arch-ch]).
  - The cost is a narrower contract: leaf literals and byte/count totals only.
- **Hot name catalogs:**
  - **L1:** 71,182 registered literals (T100k/L16), with exact root totals per bucket.
  - **L2:** a sparse bucket-child catalog of 701,754 cells in a 211 MB artifact, built in 3,368 s. All 427,092 diff bodies match, at a same-node median of 0.726 ms ([clickhouse.md, Status][arch-ch]; [architecture README][arch-readme]).
  - Precomputed answers were fast. The trouble was that the census and catalog builds kept moving into native C++. Oct 6's census took 20 min in ClickHouse SQL and 3m39s in the native engine ([architecture README][arch-readme]).
- **Daily refresh** of the root-summary catalog for one new scan: 75m42s, of which 42m34s was an interval audit in Python ([clickhouse.md §5][arch-ch]).

### 3.4 One store for every scan: the "mega-index" (2026-10-08)

- **The VM was resized** to n2-highmem-32 (32 vCPU, 251 GB, 2 TB pd-ssd) ([mega-index][mega]).
- **Consolidated name postings over 68 scans:** 1,149,549,991 node versions in 12.36 GB ([mega-index, Postings size][mega]).
- **The store's own `by_name` projection was too coarse.** With 343 per-scan parts in 8,192-row granules, `5418` read 228M rows (32 GB) in 9–12 s. Name-sorted postings in 256-row granules, unpartitioned, fixed it ([mega-index][mega]).
- **Served latency for literals below the catalog threshold:** the consolidated index cut median cold latency 2.5–3.7×. The tails were still 2.7–3.8 s ([mega-index, Served latency][mega]).
- **The consolidated catalog:**
  - Backfilling all 68 scans took 71 min, against ~14.7 h estimated for per-scan builds.
  - The served state was 24 MB for all 68 scans ([mega-index, Backfill][mega]).

### 3.5 The cost at the end

| Item | Monthly |
|---|---:|
| n2-highmem-8, on demand | $382.56 |
| 2,000 GB pd-ssd | $340.00 |
| IP, egress, staging | ~$6 |
| **Total** | **≈ $728 list (≈ $650 with sustained use)** |

Source: [retirement, Cost][retire]. The replacement pipeline costs ≈ $16/month, so the net saving is ≈ $710/month at list. When it was deleted, ClickHouse held 475 GB on the 505 GB used, most of it experiment databases ([retirement, Inventory][retire]). In its last 3 h of logs the box had served no requests besides a health check ([retirement, Inventory][retire]).

## 4. Why it was retired

### 4.1 What the static R2 index does instead

**Name search:** the static name index ([static-name-search][sns]; [static-append][sa]).
- **Layout:** one row per (suffix of a name, version of that name), sorted by suffix. Literal `q` is then one contiguous range.
- **Bounds:**
  - Literals over V = 100K rows are answered from a catalog of ~25 MB.
  - Their map drilldown is answered from precomputed roots and rollups, ≤ ~116K rows per drill.
  - Every other literal reads fewer than V rows, about 3 MB, in one ranged GET.
- **Size and cost:** built at 150.6 GB (9.1 B/row), ~$2.3/month on R2 ([sns, Full-scale size][sns]).
- **Exactness, against brute force:**
  - 259/259 (term, date) answers ([sns, Full build][sns]);
  - drilldown 2,270/2,270 ([sns, Drilldown][sns]);
  - anchored `^q` / `q$` 1,110/1,110 ([anchored-search][anch]).
- **Daily appends** are LSM-style runs merged on a binary counter, with immutable manifests ([static-append, Deferred carries][sa]).

**Trees, diffs and series:** the interval store ([interval-store][iv]).
- **Model:** SCD-2 versions in parquet, with closed versions in dyadic time segments: 1,156,324,516 folded versions, with `path` and `bysize` sorts of 16.8 and 14.8 GB ([iv §6][iv]).
- **Verification:** 240/240 views and 64/64 diffs equal per-scan reads ([iv §6][iv]).
- **Cost:** a per-scan append took 1,162 s and ≈ $0.77 ([iv §6.3][iv]).
- **Latency:** over a warm colo cache, views take 94–597 ms and diffs 329–358 ms ([iv §6.4][iv]).

**Why this fits Workers better:**
- **No always-on server.** Every expensive step (sorting, the census, catalog and drill builds, merges) runs as GCP Batch spot jobs. The query path is a Worker, R2 and D1.
- **Edge-cacheable immutable files.**
  - Runs and manifests are never rewritten: rule 2 in [static-append, State][sa].
  - So footers, footer groups and decoded row groups can sit in the colo cache across isolates ([iv §6.4][iv]).
- **Reads are planned before any byte is fetched.**
  - The per-group index lives in D1, or in `.groups.parquet` on R2. A query is one lookup and then a few ranged GETs.
  - A Worker never parses a footer. The 9 MB footer of a 4K-row-group file takes ~0.9 s to parse in a Worker ([sns, Reading it from a Worker][sns]).
- **Cost:** ≈ $16/month of builds plus R2 storage, against ≈ $728/month ([retirement][retire]).
- **Latency is bounded by construction, not by planner luck.**
  - The weight census guarantees a bound on every literal's read, at build time.
  - On ClickHouse, logical hit caps didn't bound physical reads. Two literals with nearly the same hit counts read 26M rows and 139M rows ([clickhouse.md, Status][arch-ch]).

### 4.2 What ClickHouse was genuinely better at

- **History, as a database feature.**
  - SCD-2 with as-of reads in ~0.1–0.3 s and 1.009 scan-equivalents for 7 scans ([serving-options §6.2][so]).
  - Our interval store reproduces this, but in ~1K lines of runner code plus a bespoke reader ([index-landscape §6.3][landscape]).
- **Warm broad filters, exact, out of RAM.** The 0.20/1.46/4.33 s bench beat our in-RAM index with an ~11 GB working set ([serving-options §6.1][so]).
- **Iteration speed.** In five days it hosted several read models side by side (canonical, numeric, coarse, coverage, hot L1/L2, mega postings and catalog) on one box, each a few `CREATE TABLE … AS SELECT`s away from the next ([clickhouse.md][arch-ch]).
- **An independent oracle.** Its ingest verified the static pipeline: all 70 scans' 1,153,480,980 opened and 545,152,608 closed versions were equal in counts and digests ([sns, Full build][sns]).
- **Arbitrary predicates on demand.**
  - Any literal at any ingested scan, with no catalog to build first, within a 5 s / 200K-name budget ([mega-index, Serving][mega]).
  - Regex worked too, as a full scan of the `names` table ([serving-options §6.1][so]).

### 4.3 What we gave up

- **Ad-hoc questions now need code.** "Any SQL over any scan, now" became "add a tier and a build stage, then rebuild".
- **Regex and arbitrary Boolean name predicates** have no static index. The static path serves literals and anchors; anchored globs and terms containing `/` fall back to the path store, and nothing indexes regexes ([anchored-search, Semantics][anch]).
- **Simplicity of the read model.** The static index carries five liveness stacks (light, catalog, drill, anchors, starts-with), part of 89 reader decision points ([index-landscape, Executive summary][landscape]).
- **Some measurement data.** The VM was deleted before its ~5 GB of non-derived artifacts were copied off. The journal's conclusions survive, but not the raw `bench-*` files ([retirement, step 4][retire]). Lesson: copy non-derived artifacts before deleting a box.

### 4.4 What we'd revisit

- **If the product needs arbitrary interactive predicates over history:** regex, owner × text compounds, or one-off ops questions at UI latency. A disk-backed columnar server is the honest answer there. Owner compounds were deferred, not solved ([architecture README, Near-term][arch-readme]).
- **If scale-to-zero gets a documented fast wake.** ClickHouse Cloud's wake time is undocumented, and connections can time out while it is paused ([serving-options §4.3][so]). It would have to beat the 10 s gate the suspend test failed ([index-landscape §6.3][landscape]).
- **As a batch engine or a verification oracle**, *inferred*. ClickHouse reads parquet and runs a census well, but our census ran 5.5× faster in native C++ ([architecture README][arch-readme]), so this needs a measurement first.

## 5. DuckDB in this project

DuckDB is a dependency of both packages (`pyproject.toml`; `cloud/pyproject.toml`, with `pyrmts[duckdb]` and `pyrmts-engine[duckdb]`). It always runs as a library inside a CLI or a Batch task, with a memory cap and a spill directory, and never as a service.

| Use | Code | What it does | Measured |
|---|---|---|---|
| Out-of-core aggregation | `src/disk_tree/find/aggregate_duckdb.py` (`disk-tree import -e duckdb`) | Bottom-up rollup of a listing into layer-2 under `memory_limit`, spilling to `temp_directory`; partitioned by depth-K prefix; range-partitioned final sort | Six-bucket fleet gate: 7.5M–290M files per bucket, 1:40 to 1:30:06 wall, central2 OOM at `-M 100GB` ([a3, Round 2][a3]) |
| Tier cuts (external sort) | `src/disk_tree/find/tiers.py` (`disk-tree tiers`, `import -i`) | One sorted `COPY` per tier (`path`, `bysize`), 8K-row groups, default `memory_limit` 8GB, spill beside the output | cw, 57.2M rows: both sorts in 88 s on n2-standard-8 ([path-store][ps]); unbounded, it took a 28 GB peak for 57M rows (`tiers.py` `DEFAULT_MEM`) |
| Path index, over-time, churn | `cloud/src/dt_cloud/index.py`, `overtime.py` (pyrmts `consolidate_parquet_duckdb`), `churn.py` | Cutting the served sorts; the multi-scan over-time consolidation; per-depth churn joins | — |
| Interval store build and append | `interval_store.py`, `interval_append.py`, `append_runner.duckdb_args` | Gaps-and-islands over every scan (pyrmts); per-scan append and carries; DuckDB given ¾ of the task's memory, every vCPU | Build: 32 spot n2-highmem-16 tasks, ~41 task-hours; append 1,162 s, ≈ $0.77 ([iv §6, §6.3][iv]) |
| Static name builds and merges | `static_append.py`, `static_anchors.py`, `static_drill.py`, `static_merge.py`, `static_catalog.py` | Sort suffix rows by `(s, path, usr, vf)`; catalog cells; drill history; N-way tier merges in parallel processes, memory split by `tier_resources` | n2-highmem-16 → drill 44 GB ∥ anchors 44 GB ([sa, Deferred carries][sa]); catalog cells 5–6× faster after replacing pyarrow-over-gcsfuse with DuckDB on local SSD ([sns, Build and cost][sns]); drill history moved from 27 min of Python to DuckDB ([sa][sa]) |
| Oracles and verification | `bench/truth.py`, `bysize_check.py`, `interval_verify.py`, `cascade_a2a.py` | Independent SQL ground truth for the 105-query bench, view parity and engine parity | Every parity number above is checked against one of these |
| Serving experiment (rejected) | `bench-engine -e duckdb` (phase 0 / phase 2) | Names-first search over local parquet on a box | 105/105 exact, 2.0/12.1/91 s; 11.8 GB resident, 39.7 GB peak ([fqs §6.2][fqs]) |

### 5.1 Why DuckDB fits there

- **Parquet in, parquet out.** Every artifact we serve is parquet, and DuckDB writes it with our codec, row-group size and key-value metadata in one `COPY` (`tiers.py`, `index.py`).
- **A library, not a server.** A Batch task starts, runs SQL over local NVMe, writes files and exits. There is nothing to keep alive between jobs and no second auth boundary.
- **Per-job memory limits and spill.** Each task sizes `memory_limit`, `threads` and `temp_directory` to its machine (`append_runner.duckdb_args`, `static_merge.tier_resources`). So the same code runs on an n2-standard-8 or an n2-highmem-32.
- **SQL keeps the oracles readable.** An independent SQL reference over a scan file is the cheapest way to prove a bespoke reader exact.

### 5.2 Pain points, measured

- **RSS overshoots `memory_limit`.**
  - Measured: 169 GB RSS at `-M 100GB` on `marin-eu-west4`. mgu's own DuckDB job showed the same shape, 141.7 GB at 100 GB ([a3, ask 11][a3]).
  - Earlier: 63 GB RSS under a 50 GB cap on a 92.7M-object listing ([streaming-aggregation][stream]).
- **Sorts can over-commit rather than spill.**
  - A 10M-row synthetic finished under a 2.5 GB cap and died in the final `COPY` under a 4 GB cap ([a3, round 3][a3]).
  - The fix was ours: a range-partitioned final sort.
- **Spill size is a cliff.** `marin-eu-west4` (369M layer-2 rows) failed its final `ORDER BY` twice after spilling more than 100.6 GiB, hitting `max_temp_directory_size` ([depth-partitioned-finalize][dpf]).
- **Single-node limits pushed us off DuckDB for the biggest aggregation.**
  - Listings arrive sorted, so `import -e stream` rolls them up in O(depth) memory, like `du`. It is byte-identical to the DuckDB engine and runs at ~525K rows/s on a 2M-row smoke test ([streaming-aggregation][stream]).
  - Its sort-free finalize took ~1 s, against 7.4 min for the DuckDB sort of 185M rows ([dpf][dpf]).
  - For reference: mgu's single-DuckDB job did all six buckets in ~25 min at 99.1 GB peak RSS ([a3][a3]).
- **Smaller gotchas:**
  - Keying 778M paths by `hash(path)` silently merged ~690K distinct paths, so we key by `md5_number` and check with a second hash ([fqs §6.2][fqs]). *Inferred:* a uniform 64-bit hash over ~7.8e8 keys should give ~0.02 expected collisions by birthday arithmetic, so 690K points at something other than plain birthday collisions. The spec doesn't root-cause it.
  - `ILIKE` ran 3–8× slower than `contains(lower(…))` ([fqs-p0][p0]).
  - `HUGEINT::DOUBLE` is not correctly rounded past 2^64 ([dpf][dpf]).
  - The parquet writer cuts row groups in 2,048-row steps, so `ROW_GROUP_SIZE 3000` wrote a 5,000-row group (`tiers.py`).
  - The default memory limit is 80% of RAM (`tiers.py` `DEFAULT_MEM`).

### 5.3 Where DuckDB isn't used, and why

- **Not in the serving path.** A Worker has a 128 MB isolate, and wasm can't be compiled at runtime, so DuckDB would have to be vendored ([r2-sql-eval §5][r2sql]).
  - A stock DuckDB-WASM build is ~31 MB, and trimmed community builds sit near the Worker size limit ([r2-sql-eval §5][r2sql]).
  - Even then, it would replace our hand-written parquet reader (hyparquet plus D1-planned row groups), not our planner or caches.
- **Not on a serving box either.** Names-first DuckDB was exact but took 14–91 s on broad terms, because it decodes the `path` row groups holding every matched name, and past 30% of the file falls back to a full scan ([fqs §6.2][fqs]). A full `path` scan is 34 s warm and 219–288 s cold ([fqs-p0][p0]).
- **MotherDuck** was surveyed, not run: "no gain over self-hosted DuckDB except ops", with a $250/month floor plus compute ([serving-options §4.4][so]).

## 6. ClickHouse vs DuckDB vs what we built

| | ClickHouse (self-hosted) | DuckDB | Static files on R2 + Worker (ours) |
|---|---|---|---|
| Shape | Server: MergeTree parts on local disk, background merges | Embedded library in a process | Immutable parquet on object storage, a footer index in D1 or R2, a reader in a Worker |
| Where it ran here | One GCE VM (8 → 32 → 8 vCPU; 500 GB → 2 TB pd-ssd), behind a tunnel | Batch tasks (n2-standard-8 to n2-highmem-32), laptop, VM | Builds on Batch spot; reads in Cloudflare Workers |
| Strengths | Several physical orders (projections); text index for `LIKE`; SCD-2 history; warm broad filters (0.20/1.46/4.33 s); anything ad hoc | Parquet in and out; out-of-core sort and aggregation with caps; zero ops; readable SQL oracles | No server; edge-cacheable; per-query reads bounded at build time; ~0.3–0.7 s rare terms; cheap |
| Weaknesses | Broad rich aggregations (153 s `zarr.json`); 8 GiB query caps and spills; per-scan parts scatter name reads; cold reads bound by disk throughput; 64–82 min ingest on 8 vCPU | RSS above `memory_limit`; sort over-commit; >100 GiB spills; single node; no shared warm state between requests | Every capability is a build stage; literals and anchors only, no regex index; five liveness stacks; bespoke reader to maintain |
| Cost model | Always-on VM plus SSD: ≈ $728/month list | Per Batch task-hour, spot | Batch per scan (≈ $0.53/day static, ≈ $0.77/append interval) plus R2 storage (~$2.3/month for 150.6 GB) |
| Fit: batch builds | Possible, but census/catalog work moved to native C++ (20 min → 3m39s) | **Yes:** our default build engine | n/a: it is the output of the builds |
| Fit: serving | Fast warm, but needs the box; failed the 10 s wake gate | No: not in a 128 MB isolate; names-first 2.0/12.1/91 s on a box | **Yes:** today's production path for name search |

Sources: §3–5 above; [serving-options §6][so], [retirement][retire], [index-landscape §6.3][landscape], [fqs §6.2][fqs].

## 7. Talking points for the demo

1. **"ClickHouse won the benchmark and lost the architecture."** It answered our 105-query search bench exactly, at 0.20 s median and 4.3 s max warm, faster than a 41 GB in-RAM index. But serving one feature from an always-on box cost ~$730/month, and the same search now comes from static files on R2 in ~0.3–0.7 s for a 100K-row term, with no server ([so §6.1][so]; [retire][retire]; [sns][sns]).
2. **"Search is easy; exact aggregation is the hard part."** Trigram indexes find 68M `zarr.json` matches quickly. Rolling them into an exact treemap took 153 s even on ClickHouse. What worked was moving the aggregation to build time: a weight census bounds every query's read at ≤ 100K rows, and heavy terms get precomputed answers ([arch-ch §2][arch-ch]; [sns, Bounding every query][sns]).
3. **"History is a ~1× problem, not a 1,000× one."** With SCD-2 intervals, 7 daily scans cost 1.009 scan-equivalents, and 70 scans of ~780M rows each come to 1.15B versions. ClickHouse proved the model. We kept the model and dropped the server: the interval store is the same idea as immutable parquet plus manifests ([so §6.2][so]; [iv §1][iv]).
4. **"DuckDB is our batch workhorse, and we've hit its walls."** Every sort, merge, build and oracle is DuckDB inside a Batch task with a sized `memory_limit` and spill directory. At 100M–600M rows we saw RSS at 169 GB against a 100 GB cap, sorts that over-commit instead of spilling, and a >100 GiB spill. Because listings arrive sorted, the biggest rollup is now a streaming `du`. Good question for them: has the `memory_limit` overshoot improved since ([a3][a3]; [dpf][dpf]; [stream][stream])?
5. **"Where MotherDuck could fit: next to the store, not in front of it."** Ad-hoc analysis over our parquet is a real need. Today it is DuckDB on a laptop or VM, and Iceberg metadata written beside our files (pyiceberg `add_files`) would let any DuckDB-family engine attach to a generation ([r2-sql-eval, Recommendation][r2sql]). For serving, a 128 MB isolate rules out DuckDB-WASM in practice, and a remote hop with a $250/month floor buys nothing over self-hosted DuckDB ([so §4.4][so]).
6. **"The interesting open problem is an edge-readable, append-only, versioned parquet store."** The data is immutable sorted parquet on object storage, merged on a binary counter. A SQL-held footer index plans row-group range reads, and the reader is an edge isolate under 128 MB. Iceberg standardizes the metadata, but copy-on-write snapshots are full copies and merge-on-read never makes old snapshots cheaper. Basin SQL documents no time travel, and nobody ships a Worker-side reader ([landscape §6.3][landscape]; [r2sql §2.1][r2sql]). *Not evaluated, a question to ask:* DuckLake's "catalog in a SQL database, data in parquet" looks close to our footers-in-D1 design. Is an edge reader on anyone's roadmap?

[retire]: ../ch-store-retirement.md
[journal]: ch-store.md
[so]: ../serving-options.md
[fqs]: filter-query-service.md
[p0]: filter-query-service-p0.md
[arch-ch]: ../architecture/clickhouse.md
[arch-readme]: ../architecture/README.md
[alt]: ../architecture/alternatives.md
[mega]: ../architecture/mega-index.md
[sns]: ../architecture/static-name-search.md
[sa]: ../static-append.md
[anch]: ../anchored-search.md
[iv]: ../interval-store.md
[ps]: ../path-store.md
[landscape]: ../index-landscape.md
[r2sql]: ../r2-sql-eval.md
[msu]: mgu-scale-unification.md
[a3]: mgu-scale-a3-gate.md
[stream]: streaming-aggregation.md
[dpf]: depth-partitioned-finalize.md
