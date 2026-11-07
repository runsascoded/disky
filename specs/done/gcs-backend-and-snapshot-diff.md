# disk-tree as a scalable storage-situational-awareness engine (+ reusable viz widgets)

> **Scope note (2026-08-03 rewrite).** This supersedes the earlier draft of this file *and* folds in `external-listings-and-gcs.md` (now a pointer). Those framed disk-tree (DT) as gaining a GCS backend + a snapshot-diff table for one consumer (`marin-gcs-usage`). The corrected framing, per the source project's owner: **DT is the shared engine for "situation awareness of storage across cloud + local stores, from ~TB local disks to multi-PB cloud estates," used by many OA and personal projects.** Each consumer (marin, others) is a *thin domain wrapper* over DT primitives + its own biz logic. That reframes the sequencing: the scalable **listing + out-of-core aggregation engine is the core deliverable**, not a deferred add-on, and the diff/time-series/treemap ride on top of it.

Source/first consumer: `~/c/oa/marin-gcs-usage` (private). Its companion plan lives at `marin-gcs-usage/specs/disk-tree-engine-and-multistore.md` — read both; this one owns the DT-generic engine + widgets + canonical format, that one owns marin's domain layer (attribution overlays, pricing, the CF-gated site, Batch specs, multi-store R2/S3).

## The linchpin: canonical format is a 3-layer pipeline, not a choice between nested-vs-flat

The open question from the source side was "nested JSON (what marin's Batch job emits) vs DT's flat per-path parquet — pick one, or support both?" The answer is **neither**: a layered pipeline with exactly **one canonical middle layer**, so every consumer and every viz is identical across scales and only the *producer* changes.

1. **Raw per-object listing** — flat parquet, one row per object, per store. marin's is `bucket,name,size,timeCreated,storageClass` (~588M rows/day, retained at `gs://…/listing/<date>/`). GCS Storage Insights / S3 Inventory reports are the same shape. This is the scan *source*; it scales by producer only.
2. **Canonical per-path aggregated parquet** — **the source of truth for a "scan."** This is DT's *existing* `scan` frame (`path,size,mtime,kind,parent,uri,n_desc,n_children,depth`; `storage/base.py:40`, `find/index.py:92-172`). Queryable in DuckDB, depth/row-group prunable (`storage/parquet.py`), chunkable for huge subtrees (`hybrid` backend), and every richer view derives from it.
3. **Derived nested JSON slices** — per-widget, per-depth presentation. marin's `tree.json` (`{n,b,o,c[]}`) is *this layer*, and note it currently also carries **domain overlays** (`tm` team-bytes, `us` top-users, `sh` shared, `cb` class-bytes) — i.e. it conflates generic aggregation with biz logic. In the target architecture DT emits the generic slice (layer-2 → nested `{n,size,n_desc,children}` at a depth); the consumer applies overlays on the way out.

Why layer 2 is canonical and not layer 3: the flat per-path table *derives* the nested tree trivially (group-by depth); the reverse loses queryability and forces re-aggregation to change depth. And it's the invariant that makes DT worth sharing — **a LocalBackend laptop scan and a sharded 3.5-PB GCS scan produce byte-identical *shapes* at layer 2, so diff/time-series/treemap are written once.** Do **not** let any consumer shim its nested layer-3 JSON in as an opaque canonical blob; that cements the wrong format and breaks the invariant.

## Current DT state (surveyed 2026-08-03) — what exists vs. what's missing

Good news: the diff is *already built*; the gaps are the engine's scale ceiling and a few viz deltas.

**Exists:**
- Scan model = flat per-path DataFrame → parquet, + SQLite `scan` table (`sqla/model.py:81-98`: `id,path,time,blob,error_count,error_paths,size,n_children,n_desc,mtime`) + `scan_progress`. Pluggable storage backends `parquet|duckdb|sqlite|hybrid` (`storage/__init__.py:24-38`, default `hybrid`; interface `storage/base.py:16-110`). `hybrid` already splits large subtrees into child-scan chunks (`cli/scans.py:100-107`).
- Scan-**source** backend interface `Backend.list(url,…) -> Iterator[{path,size,mtime,kind,parent,uri}]` (`backends/base.py:23-63`); `s3|ssh|local` registered (`backends/__init__.py:6-17`, with an explicit `# TODO: r2, gcs, az`). `gfind.py` is a shared `find`-stdout parser, not a Backend.
- **Diff already end-to-end:** `GET /api/compare` (`server.py:1108`) → per-child `added|removed|changed|unchanged` + `size_delta`/`n_desc_delta`, ancestor rebase, response-cached; UI `CompareView.tsx` (route `/compare/*`) with summary + delta table + drill-down.
- **Time-series raw material already served:** `GET /api/scans/history?uri=` (`server.py:1032`) → every dated scan of a path with `size,n_children,n_desc,time`.
- Treemap: Plotly (`ScanDetails.tsx:532-680`, `type:'treemap'`, `branchvalues:'total'`), per-node `colors` array already wired (so Δ-recolor is trivial). Stack: React 19 + Vite 7 + MUI 7 + react-query + Plotly, Flask serves assets + API.

**Missing (the actual work):**
- **Scale ceiling — the pivotal gap.** The indexer drains `backend.list()` fully into an **in-memory pandas** frame and aggregates there (`model.py:123-136`, `find/index.py:113-172`). That's fine for a TB laptop; it **cannot** do 588M objects / multi-PB. There is **no out-of-core / bulk path**.
- No backend for object *inventories* (pre-made listings) and none for `gcs`/`r2` (s3 exists).
- No sharded/parallel lister (marin's is external).
- No CLI `diff`/`series` (diff lives only in server+UI).
- No treemap-*rendered* diff (CompareView is a table) and no multi-snapshot time-series *chart*.
- Treemap is Plotly-only; no framework-agnostic-ish reusable widget package.

## Work item A — backends: bulk external-listing import + sharded live listers (gcs/s3/r2)

Two backend flavors behind the existing interface:

1. **Import a pre-made listing** (`dt import`, or an `inventory://`-style backend). A row-iterator that *reads files* instead of walking. First-class inputs:
   - **Object-listing parquet**, columns ≈ `bucket,name,size,timeCreated,storageClass` (marin's schema; per-bucket shard globs). `bucket+name → uri`; `timeCreated → mtime`.
   - **GCS Storage Insights** and **S3 Inventory** reports (both parquet; managed daily listings big estates already have on hand). SII schema shim = rename + storage-class-id map + `timeDeleted IS NULL` filter.
   - Degenerate: `find -printf` / `gfind` text, reusing the existing parser.
   Semantics: an import creates a normal dated `Scan`; object listings have no dir rows, so **synthesize parents by aggregation** (see item B). All existing treemap/drill/diff/history features then work unchanged.
2. **Sharded live listers** for `gcs`/`r2`/`s3` (r2 ≈ s3 API; ADC/creds auth). The estate-scale unlock is marin's `bucket_list.py`, generalized into a DT lister primitive:
   - depth-2 prefix **bin-packing** across worker *processes* (`pack_chunks`, greedy heap over a prior listing's per-prefix counts; page parsing is GIL-bound ~1M obj/min/proc);
   - intra-prefix **range-sharding** (`split_hot_prefixes` → `list(start_offset=,end_offset=)`, start-inclusive/end-exclusive so boundary accuracy can't drop/dup) for single hot flat prefixes (e.g. a 142M-object dir) that would otherwise serialize a whole store;
   - shard **boundaries from a bounded reservoir sample** (`USING SAMPLE reservoir(500k ROWS)` before `quantile_disc`), NOT exact quantiles — exact buffers every name under the prefix (30M+) and OOMs; a 500k sample lands within ~0.3% and imperfect boundaries only mis-balance, never drop/dup.
   - Correctness hardening to keep: `dedupe_prefixes` (gcsfs-style listings nest placeholder dirs inside their own listing → double-stream), shallow-row suppression under de-nested parents, `_SUCCESS.json` completion marker `{bucket,prefix,objects}`, exists-policies `error|reuse|clear` (reuse-if-complete → idempotent/gap-filling re-runs).
   - **Measured scaling (CPU-bound, ~linear then a fixed floor):** central2 (280.3M objects) 45 min @ 16 vCPU/12 proc → 23.4 min @ 32/24 → 13.5 min @ 64/48. 16→32 ≈ 1.9× (near-linear, CPU ~83%); **32→64 only 1.73×** — not a core ceiling (steady CPU rises to ~85%) but a **~6 min fixed floor** (container start + shard-compute + the deliberate GCS-QPS ramp / connection warmup), ~45% of a 64-core run (Amdahl). Takeaway: **32 vCPU is the cost/latency sweet spot**; past that, attack the ramp not the core count. GCS prefers ramped QPS (429s on instant fan-out). A task/compute request must derive from the machine (`(vcpus-2)*1000` milliCPU, `vcpus*1500` MiB), never a constant.

The interface likely needs a "bulk index to parquet" path **distinct from interactive `list()` walk** — the 100M+-object regime is batch, not walk (see item B). The small-prefix `gcs://bucket/prefix` live walk (~100 LOC mirroring `s3.py`) is a nice-to-have for ad-hoc use; the import + sharded-bulk path covers the estate case.

## Work item B — out-of-core aggregation (the core that unblocks everything)

Replace the in-memory pandas aggregation with a **DuckDB out-of-core** path for the bulk regime, keeping pandas for small local scans (pick by row-count / a `--bulk` flag / backend hint).

- Input: layer-1 per-object rows (from an import or a sharded lister), possibly hundreds of parquet shards.
- Output: layer-2 canonical per-path parquet — bottom-up group-by that **synthesizes dir rows**, sums `size`/objects into `n_desc`, maxes `mtime`, computes `parent`/`depth`, sorted breadth-first by `depth` for row-group pruning (mirror `find/index.py:113-172`, but in SQL that spills to disk). marin already does exactly this in DuckDB on a highmem node (DUCKDB_MEM≈100GB, spill to local SSD) for 588M objects — port that query shape.
- DT already ships a `duckdb` storage backend, so DuckDB is in the tree; this is about routing *aggregation* (not just storage) through it.
- Keep `hybrid` chunking for the resulting huge subtrees; keep the depth-filtered predicate-pushdown reads in `storage/parquet.py` for interactive slices.

This one item is what makes DT viable for *any* multi-PB store — i.e. it is the core of the cross-project demand, not a marin special-case.

## Work item C — diff / time-series / treemap (mostly assembly on top of what exists)

- **CLI**: `dt diff <scan-a> <scan-b> [-d depth]` and `dt series <scan…|auto-discovered snapshot dir>` — thin wrappers over the existing `/api/compare` + `/api/scans/history` logic (currently server-only). marin's `compare` (exact-output per-path Δ table, `|Δbytes|`-sorted, top-N w/ truncation-aware TOTAL) is a good reference for the table form.
- **Treemap-rendered diff**: recolor the treemap by Δbytes on a diverging scale (grew/shrank/new/deleted) — the per-node `colors` array is already wired; new/deleted subtrees are the high-value signal. Complements (doesn't replace) the CompareView table.
- **Time-series chart**: line/area of total (and per-selected-path) bytes & object-counts vs snapshot date, straight off `/api/scans/history`. Distinct from a *within-scan* created-date histogram (that's when current objects were written; this is how the live footprint changes scan-to-scan). Precompute a small `series.json` rather than loading every tree client-side.

## Work item D — packaging + reusable widgets (de-Plotly)

Split DT along a **capability/dependency axis** so consumers take only what they need:

- **`disk-tree` (core, Python, zero FE)** — backends + sharded listers + DuckDB aggregation + scan registry + diff/history/series *query* logic. The FE-free crown jewel; every consumer imports this.
- **`@disk-tree/react` (widgets, no Plotly)** — reusable treemap + time-series chart + diff-coloring, consuming layer-2/layer-3 data. **Upstream marin's DIY treemap** (its custom `.treemap .map .cell` DOM/SVG component) as the canonical widget and **retire/replace DT's current Plotly treemap** — this matches the source owner's explicit ask ("DT should offer non-plotly FE widgets, esp. the TreeMap we DIY'd here"). Per-project UIs (marin's site) compose these widgets + their own domain chrome; the UI is expected to be *mostly custom per project*, with DT supplying the hard widgets.
- **(optional) `disk-tree-plotly`** — isolate or sunset the legacy Plotly view; don't make core consumers pull Plotly.

## Work item E — deep links (for embedding)

Stable URLs so an embedder (marin's `gcs.oa.dev`) can "open this prefix/scan in DT" and "diff scan A vs B at path P": document/confirm `/scan/<id>?path=<uri>` and a `/compare?...scan1=&scan2=&path=` form. Auth stays the embedder's problem.

## Division of labor — DT-generic vs. consumer-domain

- **DT owns:** everything above — listing/import, out-of-core aggregation, canonical layer-2 parquet + scan registry, diff/series/treemap query + widgets, deep-link URL scheme.
- **Consumer (marin) owns:** identity/attribution overlays (`identities.yaml`, deepest-prefix-wins owner join, the `tm`/`us`/`sh` team/user/shared rollups), storage-class **pricing/cost**, W&B mining, the CF-Access site, and the Batch job specs/scheduler. The overlays attach to DT's generic layer-2 output; they are *not* baked into DT.

## Suggested shape in DT

- `backends/gcs.py`, `backends/r2.py` (≈s3), and an `import`/`inventory` path (object-listing + SII/S3-Inventory parquet) behind the existing `Backend` interface; sharded-bulk lister primitive shared across cloud backends.
- A bulk `index` path that aggregates via DuckDB (out-of-core) → canonical per-path parquet, distinct from the interactive pandas walk; exists-policy + `_SUCCESS` semantics promoted to bulk backends.
- `dt diff <a> <b> [-d]` and `dt series <…>` CLI commands over cached scans (backend-agnostic once scans are canonical).
- UI: Δ-colored treemap mode + a series chart; both in `@disk-tree/react`.

## Coordination + tests to port

- marin keeps its private copy until the DT versions land, then swaps to importing DT (its plan's phase 4 "import shim"). Choosing layer-2 as canonical **now** means marin's near-term integration (feed its layer-1 listing parquet → DT aggregation) is already on the long-term path — no throwaway.
- Port alongside: `test_bucket_list.py` (exists-policies, `dedupe_prefixes`, `pack_chunks`, range-sharding `split_hot_prefixes` boundaries + range packing), `test_listing.py` (SII normalization, source-priority merge), `test_compare.py` (exact-output diff table).

## Motivating context (why this exists)

- The source estate: ~588M objects / 3.5 PB across 6 GCS buckets, plus R2 and S3 (used less). Managed inventories (SII / S3 Inventory) proved unreliable as a *primary* source (generation times scatter 02:26–13:00 UTC, day-1 lag ~30h, one bucket 502s on config for days), so the source project switched to **direct sharded listing** as sole primary — cheap ($3–6/day for ~588M objects at ~$0.005/1k list pages) and fully schedule-deterministic. Keep managed-inventory *import* as a supported backend, but don't model it as required.
- Naive listers truncate huge flat prefixes → ~3× object undercount on some buckets (280M vs 88M). Complete, cheap, self-scheduled sharded listing is the fix — and the reusable primitive.
- **Open data question to carry:** direct-listing counts one bucket ~7.6M objects / ~20 TB *below* its managed report, consistently (two runs agree to 0.05%); versioning is off, so not version history. Unresolved which is ground truth — relevant because the `gcs` backend's correctness bar is "matches an independent inventory," and here they disagree; a per-sub-prefix a2a count would settle it.
