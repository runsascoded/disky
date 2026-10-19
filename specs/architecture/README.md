# Storage query architecture

As of 2026-10-14. Start here for how the deployments serve treemaps, diffs and name search today. For a per-index audit of what depends on what, see [index-landscape].

| Document | What it explains |
| --- | --- |
| [Serverless architecture][serverless] | Scanning and publication, D1 footer metadata, R2/GCS range reads, Worker aggregation, the interval store, static name search, auth and caching. Component and request-sequence diagrams. |
| [Static name search][static-name] | Exact name search without a server: suffix-ordered postings on R2 (one range read per rare query), a catalog for short and heavy literals, drill files for the filtered map at any path and date, anchors, per-scan append runs. |
| [Architectural alternatives][alternatives] | What was measured against the static/Worker design: in-RAM custom index, serverful DuckDB, ClickHouse, sparse predicate summaries, managed services. Why aggregation, not retrieval, is the hard part. |

## The present architecture

There is no server on the query path. Every deployment is the same shape: Batch jobs build immutable parquet artifacts into object storage, a sync step loads their footers into D1, and Pages Functions (Workers) plan from D1, range-read the artifacts and aggregate.

- **Trees and diffs** come from the **interval store** ([interval-store]) where a deployment has one: one SCD-2 table of path versions (objects and directories) in dyadic time segments, plus per-scan append runs. gcs prod reads it for every scan its generation holds (`PATH_STORE = "intervals"`); scans outside it, and deployments without one (cw-s3, the r2 demo, m3), read the **per-scan path store** ([path-store]: the `path` and `bysize` sorts).
- **Name search** (`?f=` on the map, `/names`) comes from the **static name index** on R2 ([static-name]): suffix shards for rare literals, a catalog for short and heavy ones, drill files for the filtered view at any path, anchored `^q` / `q$` catalogs. Each scan appends a run (`dt-cloud static-names runs add`). gcs sets `FILTER_INDEXED_ONLY`, so the map's filter answers only what the index answers exactly and refuses the rest with a reason; deployments without static names fall back to the approximate path-store walk, marked as such.
- **Planning metadata** lives in D1: each generation's pointer and its row-group footers (`index-sync`), with cold `.groups.parquet` sidecars once D1 retires a generation's rows. D1 never holds path rows.
- **Caches**: the colo Cache API and a global KV cache hold responses keyed by generation, query and view; the interval-store reader also keeps its state, footers and decoded groups in the colo cache across isolates.
- **Builds** run on GCP Batch (gcs, cw-s3), GitHub Actions (the r2 demo's daily ingest) or AWS Batch over laptop captures (m3); DuckDB is the out-of-core engine inside them.

What is still open for exact `?f=` at any drill and date (compaction of the static index's runs, compound and scoped queries, heavy anchored terms below the root) is tracked in [index-landscape] and [search-extensions].

## Retired: the ClickHouse query box (2026-10-03 → 10-10)

For one week a ClickHouse VM behind a tunnel served name search and history experiments. It was exact and fast on retrieval, but broad aggregation stayed slow and it needed an always-on server; the static R2 index replaced it, and the VM, its code and its DNS were removed on 2026-10-10 (code at tag `ch-store-final`). See the [post-mortem][postmortem], the [retirement runbook][retirement], the [designs that were tried][clickhouse], the [consolidated name index][mega-index], the [short-query catalog][short-query] and the [experiment journal][journal].

[serverless]: serverless.md
[static-name]: static-name-search.md
[alternatives]: alternatives.md
[index-landscape]: ../done/index-landscape.md
[interval-store]: ../interval-store.md
[path-store]: ../done/path-store.md
[search-extensions]: ../search-extensions.md
[postmortem]: ../done/ch-postmortem.md
[retirement]: ../ch-store-retirement.md
[clickhouse]: ../done/architecture/clickhouse.md
[mega-index]: ../done/architecture/mega-index.md
[short-query]: ../done/architecture/short-query-index.md
[journal]: ../done/ch-store.md
