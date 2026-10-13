# R2 Data Catalog / R2 SQL (now "Basin"): can it replace or simplify our index tiers?

Status: timeboxed evaluation (~2 h), 2026-10-10, `[cloud]`. Desk research only: Cloudflare docs, changelog and blog, read 2026-10-10, against `specs/index-landscape.md` §6.3, `interval-store.md`, `static-append.md`, `anchored-search.md` and `latency-observability.md`. Nothing was enabled, written or queried on any bucket, D1 or KV. Latency claims about R2 SQL below are **inferred from its architecture**, not measured; the experiment in §6 would measure them.

## Verdict

| Tier | Verdict | One-line why |
|---|---|---|
| Interval store (as-of subtree, top-N, diffs) | **no** for serving; **partial** as a metadata format | Iceberg snapshots don't fit the model: copy-on-write makes each scan a full copy, merge-on-read makes old snapshots slower forever, and Basin SQL documents no time travel. Our SCD-2 rows (`vf`/`vt` predicates) *can* be registered as Iceberg tables and queried, but then Iceberg only adds a catalog, not a capability, and the Worker loses its colo caches and its planning |
| Static name index (substring over ~1B names, anchors, heavy terms) | **no** | `LIKE '%q%'` is a full scan; min/max stats can't prune infix. A suffix table in Iceberg would be our `sx/` under another catalog; heavy terms (catalog, drill, rollups) have no SQL-engine equivalent |
| Per-scan path store | **no** | It is being retired for store-held deployments; one file per scan is already pruned by D1 footers |
| Binary-counter merges and compaction | **no** | Basin compaction is bin-pack only (no sort or z-order today), with 64–512 MB target files. It doesn't keep our sort order, our dyadic segments or byte-identical outputs, and its snapshot expiration (default 30 d, keep 5) would delete history under a snapshot model |
| Ad-hoc / offline analytics over the stores (ops questions, digests, verify) | **partial: worth an experiment** | SQL over our existing parquet without a VM, at $2.50/TB scanned. Not on the request path |

**Recommendation.** Keep the bespoke serving tiers. The "append-only, versioned, row-group-pruned parquet store readable from an edge isolate" gap that `index-landscape.md` §6.3 names is still a gap: Basin is a serverless analytics engine reached over HTTPS, not an edge reader. Two cheap follow-ups:
1. When the `runs/` store library is extracted (§6.3 there), consider writing **Iceberg table metadata beside our files** (pyiceberg `add_files`, one file per segment block) so any engine (DuckDB, Spark, Basin SQL) can read a generation, while the Worker keeps reading our footers. That is interop, not serving.
2. Run the §6 experiment only if the answer would change something: e.g. to replace the laptop/VM DuckDB used for ad-hoc analysis, or to settle whether a one-SQL treemap is fast enough to matter. I'd not run it for serving alone; the structural reasons below are enough.

## 1. What Basin Catalog and Basin SQL are today

**Naming and status.** On **2026-10-01** Cloudflare renamed and GA'd the "Cloudflare Data Platform" as **Basin**: Basin Pipelines (was Pipelines), **Basin Catalog** (was R2 Data Catalog), **Basin SQL** (was R2 SQL). "Existing … resources and configurations will continue to work and will be deprecated over time" ([changelog 2026-10-01][cl-ga]). Both are **GA** as of that date; the docs pages are "Last updated Oct 1, 2026". R2 SQL was announced in open beta on 2025-09-25 ([blog][deep-dive]).

Timeline (from the Basin SQL changelog, [cl-sql]):

| Date | Change |
|---|---|
| 2025-09-25 | R2 SQL open beta: filter queries only |
| 2025-12-12 | aggregations, schema discovery |
| 2026-02-09 | approximate aggregates |
| 2026-03-23 | ~190 functions, complex types |
| 2026-04-20 | JSON functions, `EXPLAIN FORMAT JSON`, unpartitioned tables |
| 2026-05-15 | joins, subqueries, multi-table queries |
| 2026-06-08 / 06-22 | set operations, `DISTINCT`, window functions |
| 2026-05-28 / 08-03 | pricing announced / billing enabled (SQL and Catalog) |
| 2026-07-13 | Catalog accepts read-only API tokens ([cl-r2]) |
| 2026-10-01 | GA as Basin |

**Basin Catalog** ([catalog]): a managed Iceberg REST catalog built into an R2 bucket. Any Iceberg engine can attach (DuckDB ≥ 1.4, Spark, Snowflake, PyIceberg, Trino, StarRocks). Maintenance ([maint]):
- **Compaction:** automatic, per catalog or table; target file size 64–512 MB; Parquet only. Pricing says it is **bin-packing**; "sort or z-order may be priced differently in the future", i.e. not offered now ([catalog pricing][cat-pricing]). A daily cap on manually queued requests exists (error `42901`, value not stated).
- **Snapshot expiration:** `--older-than-days` (default 30) and `--retain-last` (default 5); both must hold. Free.
- **Not supported:** buckets in a non-default jurisdiction. Manual deletes on a catalog-enabled bucket can corrupt tables; the dashboard and Wrangler now warn ([cl-r2], 2026-07-07). This rules out enabling it on our live index buckets, where prune/GC delete objects directly.

**Basin SQL** ([sql], [limits], [troubleshooting]): a read-only distributed engine. The coordinator plans from Iceberg metadata (table metadata → manifest list → manifests → Parquet footers, pruning by partition stats, file column min/max and row-group stats) and streams row groups to query workers running Apache DataFusion ([deep-dive]).

| Capability | Today |
|---|---|
| Filters | `WHERE`, `LIKE`, `ILIKE`, `SIMILAR TO`, `BETWEEN`, `IN`, regex functions |
| Aggregates | basic, approximate (`approx_top_k`, `approx_distinct`, …), statistical; `GROUP BY`, `HAVING`, `GROUPING SETS`/`ROLLUP`/`CUBE` |
| Joins / subqueries | INNER/LEFT/RIGHT/FULL/CROSS, self-joins, CTEs, `EXISTS`, scalar subqueries (since 2026-05-15) |
| Windows | inline `OVER`, `QUALIFY` (budget-gated: a 400 if the estimated scan is too large) |
| Sorting / limits | `ORDER BY`; `LIMIT` 1–**10,000**, default 500; **no `OFFSET`** (cursor pagination only). The 2025 blog's early-stop top-N works only when ordering on partition-key columns; nothing since says that was lifted |
| Time travel | **not documented.** No `AS OF`, no snapshot selection, no `$snapshots` table in the SQL reference. Every query reads the current snapshot |
| Writes / DDL | none (read-only) |
| Timeouts / concurrency | not published. "Resource-heavy queries may time out" (multi-way joins, high-cardinality `COUNT(DISTINCT)`, large sorts, big windows); errors 502 or a pre-execution 400 |
| Latency | no published figures; the blog says "results in seconds" over petabytes. Each query plans from the catalog afresh; no result cache is documented |

**Access from a Worker / Pages Function.** There is **no Workers binding** for Basin SQL (only Pipelines has bindings). The interface is an HTTPS POST to `https://api.sql.cloudflarestorage.com/api/v1/accounts/{ACCOUNT_ID}/basin-sql/query/{BUCKET}` with `Authorization: Bearer <API token>` ([query-data]), or `wrangler basin sql query`. A Function could `fetch()` it with a secret, which puts an account API token in the Pages env. The token scopes conflict between pages: the query page lists "Basin SQL read, Basin Catalog read, R2 storage **admin read/write**", while the catalog page and the 2026-07-13 changelog say read-only tokens suffice for readers. That needs checking before any wiring.

**Pricing** ([sql pricing][sql-pricing], [cat-pricing]):
- Basin SQL: **$2.50/TB scanned** ($0.0025/GB), 10 GB/month included, **minimum 10 MB billed per query**, no per-query fee; failed queries free.
- Catalog: $9.00 per million catalog operations past 1M/month (metadata requests: load table, list, …; unclear whether Basin SQL's own planning reads count, but "standard R2 and catalog request charges still apply" even to `EXPLAIN`).
- Compaction: $0.005/GB processed and $2.00 per million objects, past 10 GB / 1M a month.
- Storage and ops: standard R2.

## 2. Fit, tier by tier

### 2.1 Interval store: "subtree of P as of scan S, top-N by size", and diffs

Two ways to put it in Iceberg:

**(A) A state table, one Iceberg snapshot per scan.** The table holds one row per `(depth, path)`; each scan is a `MERGE` (2–4.5M upserts, 0.6–1.9M deletes; the sweeps closed 58M and 126M, `interval-store.md` §6). "As of scan S" = "read snapshot S", the cleanest semantic match. It fails on mechanics:
- **Copy-on-write** rewrites every data file a change touches. A scan's changes scatter over the whole `(depth, path)` keyspace, so nearly every file is rewritten: each snapshot becomes a ~17–19 GB copy. That is the 3.3 TB per-scan growth the interval store exists to remove (`interval-store.md` §0).
- **Merge-on-read** keeps growth proportional to change, but a read at S applies every delete file and added file through S. Compaction rewrites only the *current* snapshot; old snapshots keep their fragmented files, so historical reads never get cheaper. Our dyadic segments bound a read at any scan to ⌈log2 n⌉ + 1 blocks (§2.4 there); Iceberg has no equivalent.
- Snapshot expiration has to stay off (defaults would drop history after 30 days), so metadata grows without bound: ~365 snapshots/year at gcs's cadence, ~1,460 at cw's 6-hourly one.
- **Basin SQL can't read an old snapshot anyway** (no time travel documented). DuckDB (`AT (VERSION => id)`, `snapshot_from_id` [duckdb-iceberg]), Spark and Trino can, but not from a Worker.
- Two sorts (`path`, `bysize`) mean two tables with two merges per scan. Iceberg has one sort order per table.

**(B) Our SCD-2 rows as an append-only Iceberg table** (`vf`/`vt` columns; the runs' open rows and close records as appended files). As-of becomes `WHERE vf <= :D AND :D < vt` plus the runs' `min(vt)` combine (a `GROUP BY (depth, path, vf)` inside a CTE). This works, and is exactly our model; snapshots carry no meaning. It can even reuse our files: `add_files` registers existing parquet, and one file per segment block gives file-level `vf`/`vt` bounds that prune as our group bounds do. But at that point Iceberg is a catalog of our layout, and Basin SQL is a remote executor for it. Against the Worker reader we'd lose:
- the colo and isolate data tiers (decoded footer groups and row groups, `latency-observability.md` L3/L4), which make warm views ~0.4–1 s (`interval-store.md` §6.4). Response caches L1/L2 stay, since they sit above the reader;
- `sizeNeighbours`, the depth-band planner (`IV_BAND_ROWS`), the version-bounded diff lookups (`Ask.vtLe`/`vfGe`), `asOfScans` run skipping, the 30 s broken-run cut and `ivRetry`. Each is a pruning or robustness rule beyond min/max stats. Iceberg manifests also truncate string bounds to 16 characters by default (`write.metadata.metrics.default = truncate(16)`), which weakens `path` pruning for deep prefixes unless set to `full`;
- offline tests: the TS reader runs against fixtures. A Basin SQL reader is testable only against the live service, or against DuckDB/DataFusion as a stand-in, with dialect drift.

What SQL would give in return: exactness is reachable. A thresholded subtree with per-depth attenuation and `(other)` folds, or a diff as a full outer join of two as-of reads, can each be **one** SQL statement (CTEs and windows), where today's reader makes several dependent reads. That is the one real attraction, and the only thing the experiment (§6) would test.

### 2.2 Static name index

Confirmed: not Basin SQL's strength.
- `LIKE '%q%'` over ~1.15B name versions can't use min/max stats; every query scans the name column. At an estimated 5–15 GB compressed (not measured), that's ~$0.01–0.04 and multiple seconds per query, with no colo cache, and the result is rows, not match roots or per-bucket totals.
- Storing `sx/` (suffix rows) as an Iceberg table makes `contains q` a range `s >= q AND s < q￿`, which stats *can* prune. But that is our index under another catalog: 13.2B rows / 135.7 GB on gcs. The heavy-term machinery (catalog, drill roots, K = 256 rollups, anchors) has no engine-side counterpart, and `LIMIT ≤ 10,000` caps a light term's row fetch.
- `index-landscape.md` §6.3's conclusion stands: suffix-ordered postings on R2 are the right structure.

### 2.3 Per-scan path store

No. A per-scan Iceberg table (or a table partitioned by scan) would prune to one scan's file trivially. But planning from D1 footers already does that, and per-scan stores are being retired wherever the interval store holds the dates (`index-landscape.md` §6.2). For r2/m3, small corpora where the per-scan store is adequate, Basin adds a second service for nothing.

### 2.4 Compaction and maintenance vs the binary counter

No.
- Basin's compaction is size-driven bin-packing into 64–512 MB files. It doesn't sort, so it would mix our sorted runs into files whose `path` bounds span the keyspace. It doesn't produce dyadic segments, and it can't give the byte-identical "merge equals rebuild" guarantee the runner tests (`interval-store.md` §2.7).
- Our merges are also *semantic*: they combine close records (`vt` min, `op` max) and keep per-tier liveness markers. Iceberg's MOR compaction applies delete files, which is a different operation.
- What Iceberg gets right, and what we already have: immutable files, an atomically swapped pointer (our manifests and revisions), and expiration (our prune plus, still open, an interval-gen GC).

## 3. Latency at a 1–3 s cold budget

Unmeasured; this is the structural estimate.
- **Per query:** a Function → `api.sql.cloudflarestorage.com` POST (TLS, auth), then catalog lookups (table metadata JSON, manifest list, manifests: ≥ 3 sequential R2 reads), Parquet footers (our served sorts have 141K row groups per file; their thrift footer is several MB, which is why we moved footers into `.groups.parquet`), dispatch to workers, row-group reads, Arrow back. Nothing is documented as cached between queries. A floor of several hundred ms per query, and more on a cold catalog, seems likely.
- **Per view:** today's reader makes ~3–6 dependent reads per subtree and dozens of level-by-level lookups per diff. Ported one-for-one, that's several SQL round trips per view and a non-starter for diffs. Collapsed into one statement, a cold view *might* fit 1–3 s. Today's interval-store dev timings are 0.37–3.0 s cold and 0.16–1.1 s warm for views, and 5.0 s cold / 3.6 s warm for the worst root diff (`interval-store.md` §6). There would be no warm tier below the response cache.
- **What we'd lose:** colo/isolate data caches (the warm path), early termination on `bysize` order (Basin's top-N early stop is partition-key-only per the blog), `Server-Timing` phase granularity (one opaque duration), and control over failure modes: Basin returns 502 or timeouts, where our reader degrades by cutting a broken run.
- **What stays possible:** exact top-N with thresholds and `(other)` folds, as SQL. Exactness isn't the problem; round trips and cache tiers are.

## 4. Cost and lock-in

| | Today | Basin for serving (est.) |
|---|---|---|
| Storage | R2 ~107 GB of iv sorts on `oa-gcs-usage-index` (≈ $1.6/mo), plus static gens; D1 footers | same files (or copies) + Iceberg metadata; snapshot history if model (A) |
| Query | Workers CPU + R2 class B reads; colo-cached | $2.50/TB scanned, **10 MB minimum per query**. At the ~3.8 MB median view (`interval-store.md` §6) every query bills 10 MB: $0.000025/query, ~$25 per million uncached queries. A filter scan of a 19 GB sort is ~$0.05 |
| Ops | our runner, GCP Batch | catalog ops ($9/M past 1M), compaction ($0.005/GB) if used |

- **Cost isn't the blocker.** For pruned queries it's small; for unpruned search it's bounded but per query.
- **Lock-in:** the format is open. Iceberg plus Parquet stay readable by DuckDB, Spark and Trino; leaving costs a reader, not a data migration. The engine is Cloudflare-proprietary, though its SQL is DataFusion-flavoured and ports mostly to DuckDB.
- **Churn risk is real.** One rename in the last two weeks ("will be deprecated over time"), the API path changed (`/basin-sql/`), the token docs disagree with each other, and SQL features landed monthly through 2026.

## 5. Other options, one line each

| Option | Fit |
|---|---|
| DuckDB-WASM in a Worker over Iceberg/parquet | Marginal. A stock build is ~31 MB; trimmed community builds (`ducklings`, parquet/httpfs/iceberg) sit near the paid Worker size limit (reportedly raised to 64 MiB uncompressed in 2026-08, unverified), with a 128 MB isolate memory cap and no runtime wasm compilation, so it must be vendored (`cfn-edge-wasm-and-dom-free`). It would replace our hand-written parquet reader with a generic one, not our planner or caches. Worth a spike only if reader maintenance becomes the bottleneck [ducklings] |
| MotherDuck | Surveyed in `serving-options.md` §4.4: ≈ DuckDB-on-a-box latency, $250/mo floor plus compute. Good for ad-hoc analytics over Iceberg on R2; adds a remote hop and an always-billed service to serving |
| Hyperdrive + Postgres | Hyperdrive pools connections; it doesn't make 1.15B-row SCD-2 range scans cheap. Aurora + `pg_trgm` was already rejected on latency per dollar (`index-landscape.md` §6.3); storage of ~200+ GB with indexes means a several-hundred-$/mo instance |
| Turso / libSQL | Per-row-read billing against views that decode 100K–600K rows, and a ~1B-row SQLite whose size sits past comfortable per-DB limits; no time partitioning. No |
| ClickHouse Cloud | Self-hosted CH was retired: ≈ $728/mo always-on, and broad search never got interactive (`ch-store-retirement.md`). Cloud's scale-to-zero has an undocumented wake time, then a cold cache (`serving-options.md`), which fails the same 10 s gate the suspend/resume test failed |
| Cloudflare Containers + native DuckDB | The "query box" again, scale-to-zero. The cold start plus a cold page cache over R2 likely misses 1–3 s; it's the retired box's shape |

## 6. The experiment that would settle latency (proposed, **not run**)

**Question:** is a one-statement as-of treemap view or diff in Basin SQL, over the real gcs interval store, within 1–3 s cold?

**Bucket:** a new scratch bucket in the **OA** Cloudflare account (the data is OA's and auth-gated), default jurisdiction, e.g. `oa-gcs-usage-iceberg-scratch`. Do **not** enable the catalog on `oa-gcs-usage-index`, whose prune/GC deletes objects directly. Creating the bucket and enabling Basin Catalog on it is Ryan's call.

**Steps:**
1. **Copy** `interval-store/2026-10-09b/served/{path,bysize}.parquet` (~36 GB) R2 → R2 within the account (no egress; a few thousand class A ops). Also copy one manifest's runs if diffs past the base matter.
2. **Variant 1, as-is:** register both files with pyiceberg `add_files` into two unpartitioned tables, with `write.metadata.metrics.default = full`, so file stats aren't truncated. One file per table, so pruning is row-group-level only. This tests "our layout, their engine".
3. **Variant 2, re-cut:** rewrite each sort with DuckDB into one file per dyadic segment block, ≤ 512 MB each, sort kept, 8K-row groups, and register those. File-level `vf`/`vt` bounds then prune blocks. Needs one VM-hour on the `mgu` node.
4. **Queries**, each run 5× cold-ish (a new table name per round, so metadata isn't warm) and 5× repeated, from a script on `mgu` via the HTTP API. Take `EXPLAIN FORMAT JSON` for files and row groups pruned, plus bytes billed:
   - Q1: one-statement treemap at P = `marin-us-central2`, D = 10-09 (newest) and 09-15 (v1 era): thresholded rows to depth dP+3 with attenuation, `(other)` per parent;
   - Q2: the root and `marin-us-central2` diff 10-01 → 10-09 as a full outer join of two as-of reads;
   - Q3: `depth = 1` band under a bucket;
   - Q4: `path LIKE '%checkpoints%'` at the root (a full scan, the bound for search).
5. **Compare** with `interval-store.md` §6 / §6.4 timings for the same views.

**Cost:** storage ~36–72 GB × $0.015 ≈ $0.5–1 for the month (delete afterwards); queries ~100 × mostly ≤ 100 MB, plus ~10 full scans × 19 GB ≈ $0.5; catalog ops within the free 1M. **Under $5 total**, about half a day of work.

**Kill criterion:** Q1 cold median > 1.5 s, or Q2 > 3 s, means stop: no serving role, and analytics only if wanted.

## 7. Open questions

- **Token scopes:** does a Basin SQL query need R2 *write* (query-data page) or only read (catalog page, 2026-07-13 changelog)? It matters if a Function ever holds the token.
- **Do Basin SQL's planner reads bill as catalog operations?** The pricing pages don't say; at serving volume this could exceed the SQL charge.
- **Can `add_files` register files in another bucket** (e.g. the live index bucket) from a catalog on the scratch bucket, avoiding the copy? Undocumented.
- **Has the partition-key-only early-stop top-N been lifted** since the 2025 blog? The current docs are silent.
- **Concurrency limits and timeouts** are unpublished. Ask in `#basin-sql` before anything user-facing.
- **Is Iceberg metadata worth emitting** from the shared `runs/` library (verdict row 1, recommendation 1)? Cheap if done when that library is extracted. Its value depends on whether anyone (Marin, OA analytics) wants to query generations with stock engines.
- The name-column size estimate (5–15 GB) in §2.2 is a guess, not a measurement.

## Sources

Read 2026-10-10. Raw copies in `tmp/r2sql-eval/` (untracked).

- [Basin SQL overview][sql], [limitations and best practices][limits], [query data / HTTP API][query-data], [troubleshooting][troubleshooting], [pricing][sql-pricing]
- [Basin Catalog overview][catalog], [table maintenance][maint], [pricing][cat-pricing]
- [Basin SQL / R2 SQL changelog][cl-sql] (GA entry [2026-10-01][cl-ga]); [R2 changelog][cl-r2] (catalog billing, read-only tokens, delete warnings)
- [R2 SQL deep dive][deep-dive] (2025-09-25: planner, pruning, top-N early stop, DataFusion)
- [DuckDB Iceberg time travel][duckdb-iceberg]; [ducklings (DuckDB-WASM for Workers)][ducklings]; [Workers limits][wk-limits]

[sql]: https://developers.cloudflare.com/basin-sql/
[limits]: https://developers.cloudflare.com/basin-sql/reference/limitations-best-practices/
[query-data]: https://developers.cloudflare.com/basin-sql/query-data/
[troubleshooting]: https://developers.cloudflare.com/basin-sql/troubleshooting/
[sql-pricing]: https://developers.cloudflare.com/basin-sql/platform/pricing/
[catalog]: https://developers.cloudflare.com/basin-catalog/
[maint]: https://developers.cloudflare.com/basin-catalog/table-maintenance/
[cat-pricing]: https://developers.cloudflare.com/basin-catalog/platform/pricing/
[cl-sql]: https://developers.cloudflare.com/changelog/product/r2-sql/
[cl-ga]: https://developers.cloudflare.com/changelog/product/r2-sql/
[cl-r2]: https://developers.cloudflare.com/changelog/product/r2/
[deep-dive]: https://blog.cloudflare.com/r2-sql-deep-dive/
[duckdb-iceberg]: https://duckdb.org/docs/current/core_extensions/iceberg/overview.html
[ducklings]: https://github.com/tobilg/ducklings
[wk-limits]: https://developers.cloudflare.com/workers/platform/limits/
