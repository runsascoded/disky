# pyrmts adoption: one engine for age / over-time / diff, plus redundant-scan compression

Status: **spec, no code** (2026-09-22). Direction set by the user ("we should probably adopt here, especially for our public demo where scans will be largely redundant"). Design inputs from three pyrmts-session heads-ups are folded in. **Reviewed by pyrmts @ `857dfad` (2026-09-22): "faithful — §3/§4/§5 are exactly the intended shape."** Both open questions are answered and two corrections applied below — **A:** two pyramids (path-index + age), not one; **B:** under per-location scoping `bucket` is the *dataset* scope, not a dim. **Correction round 2 (pyrmts, 2026-09-22), applied below:** the diff-treemap engine is our rendering-bounded walk (`buildDiff` in the base, `recursive_diff` in Flask), **not** `diffOverSpan` — the diff-index is a flat changeset (audit / churn / build input), not a frontier (§3, §5); MS archives *are* fine for per-dir random access into any scan (§3); the path-index writes **two files per scan** (hot `b DESC` subset + full `(depth, path)` shard, §3.2); and the diff-index layout is now **final** (pyrmts main `a783633`: L0 events log + optional aligned levels under a cap, default cap 0 — §4), so its ingest stage can be wired whenever phase 2 starts. Next step (awaiting the user's go): draft the two `Pyramid` configs (§3.3) and ping pyrmts to check them against the engine; the walk-perf plan (§5.1) is likewise recorded, not started.

Supersedes the deferred age-index item in [`union-of-roots.md`](./union-of-roots.md) (phase 7) and resolves it: cw's "age-index redesign" *became* pyrmts.

## 1. Why

disk-tree's `cloud` base carries **three ad-hoc structures** that pyrmts replaces with one cross-consumer engine:

| today (ad-hoc, base) | pyrmts equivalent |
|---|---|
| `write_age_pyramid` (age tiers, `age-pyramid-<bin>.parquet`) | the tier pyramid — age = created-date `binCol` |
| the over-time index (`_lib/overTime.ts`, `overtime.py`, `d249b55`, US'd from cw) | multi-scan store → `seriesFor` / `seriesAcrossGroups` |
| the persisted diff-index backend (`/api/compare`, `<a>-<b>.parquet`) — **kept**; its *build* for non-adjacent pairs composes the pyrmts changesets instead of loading two full scans | the always-on diff-index → `diffOverSpan` as build input + audit/churn views (**not** the diff-treemap engine, §5) |

And it adds the thing we can't do today: **compress the kept-forever daily scans.** The r2 public demo stores one full index per daily scan, each ~a duplicate of the last — the exact O(N) blowup pyrmts's multi-scan consolidation targets. cw-s3 measured on real path-indices: **~0.020 % churn per 12 h scan → 5.9× @ N=6, ~78× @ N=81, ~335× @ daily·1 yr, ~626× @ N=730** — compression *grows* with scan count on near-static data.

Because the diff-treemap, over-time chart and age chart are widgets of the **shared base**, cw/gcs inherit the serve wiring by rebasing. The serve side is wired **once, here**.

## 2. What pyrmts is (the adoption surface)

`~/c/pyrmts` — multi-scale timeseries pyramids: pre-compute `(shard × bin)`-tier aggregates once; serve any range × bin-budget query in O(log) bins from the edge. Polyglot: Python build (`pyrmts`, `pyrmts-engine`, `pyrmts-ops`) + TS serve (`pyrmts`, `pyrmts-cfw`, `pyrmts-geo`, `pyrmts-react`). Shipping; used by ctbk, awair, crashes; co-developed with cw-s3. Dist current @ pyrmts main `e03c490`.

A consumer provides exactly four things — pyrmts owns everything else (`pyrmts:specs/done/pyrmts-ops-adoption.md`):

1. a **pyramid config YAML** (dims, metrics/monoids, tier ladder, storage, `multiScan:`),
2. one **raw → base-tier ingester** function,
3. **storage / D1 bindings + env**,
4. thin **CLI / handler shims**.

## 3. The two orthogonal indexes (both on by default) — and the correction

These are **separate structures with separate jobs**; don't conflate them (pyrmts's own correction, 2026-09-22):

- **Multi-scan (MS) store** — `pyrmts:specs/multi-scan-consolidation.md`. Folds the re-observation (scan) axis into shards with interval-encoded SCD-2. **Bounds storage** and **serves the over-time line** (`seriesFor`, `seriesAcrossGroups`). It does **not** give cheap diffs between arbitrary scans: a pair straddling an archive junction has no stored delta → O(fleet).
- **Diff-index** — always-on, landed pyrmts main `e03c490`. A dyadic **changeset hierarchy**: per scan, one adjacency changeset (`k → k+1`, the single O(fleet) step, paid once at ingest) and one **immutable composed node per power-of-2 level**. `diff(a, b)` composes only the `popcount(|j − i|)` = O(log) disjoint nodes → **O(log N + changes-in-span)**, no snapshot read at query time. Verified against the 2-snapshot oracle for every pair, incl. remove-then-re-add netting out (changeset composition is non-invertible, hence disjoint blocks). **Does NOT serve the diff-treemap** (correction round 2): `diffOverSpan` returns a *flat* changeset — every key whose state differs over `(a, b]`, no tree, no frontier — which for a long span of a big fleet is millions of entries, far more than the rendered cells. A treemap needs O(rendered) work (per expansion, merge-join the two children listings, descend only where |Δ| clears screen resolution), which is exactly what our walks already do (§5). The diff-index is the right primitive for **audit / changelog views**, **gross churn** (Σ|Δ_leaf|, which net rollups can't give), and as the **build input** for `diff_index.py` on *non-adjacent* pairs (compose the L0 adjacency changesets over `(i, j]` instead of loading two full scans; unchanged-sibling context then comes from per-dir reads of scan j). pyrmts is reworking its layout (sliding-window nodes were O(N) storage overhead → aligned blocks with a level cap, L0-only default); **don't wire it into ingest until they ping that the layout is final.** MS archives, conversely, *are* fine for the walk: they are key-sorted with interval columns, so "children of P at scan s" = RG-prune-by-path + filter `__scan_lo <= s < __scan_hi` — the archive is a snapshot store with per-dir random access into any scan.

Storage: MS = O(log N) archives under `scheme: exponential` (§3.1); diff-index = O(N log N) tiny nodes (fine on low churn). Both are independent of *whether* individual scans are consolidated.

### 3.1 MS grouping: use `scheme: exponential` from the start

The user's demo was the motivating case for this option. Fixed-K grouping ("every K scans → one archive") leaves the archive count O(N/K) — unboundedly many tiny archives for a low-churn demo kept forever. **Exponential** (Bentley–Saxe / logarithmic method) coalesces old scans into power-of-2-sized archives so the **archive count stays O(log N)**: for daily scans kept forever, ~9 archives/year instead of ~365.

- Config: `multiScan: { scheme: exponential, base: 2, dataset, tier, shard, drop: true }`.
- Run: `multiscan seal <config>` — **idempotent**; a post-scan stage fires it each cycle and it compacts as needed.
- **D1 gotcha:** exponential *compacts* — it deletes superseded manifest rows as blocks merge — so the routing-manifest → D1 sync must **mirror the row set, not append-only** (fixed-K is append-only; exponential isn't). `MultiScanD1Index` (`pyrmts-cfw`) backs the manifest.
- **Read-path gotcha (exponential-only):** an archive's **key moves as it compacts** (the key encodes `first-scan-label + block-size`; a scan starts in a size-1 archive, then merges into size-2, size-4, …). The manifest + `resolveScan` / `seriesAcrossGroups` always route to the *current* archive (self-correcting), **but any embed must resolve `(dataset, scan/path)` through the manifest per read — never cache or hard-code an archive URL.** Cache the query, not the resolved key. `seriesAcrossGroups` re-lists each read (`list_multiscans → filter tile → per-group seriesFor`), so it is automatically current.
- Reassuring corollary: **dropping individuals after `seal` loses nothing** — `extract_scan` reconstructs any single day's tree from its current archive, so "show the tree as of day X" survives compaction.

### 3.2 The disk-tree mapping: union path-index → `Pyramid` **(resolved with pyrmts)**

Our served artifact is a **snapshot** path-index keyed `(path)` with no event-time axis (no created-date `binCol` at the path grain). pyrmts's contract for this is explicit: a **constant `binCol`** (a `dt=0`-style column, as its unit tests construct shards) — consolidation keys on `(binCol, *dims)`, so a constant binCol reduces the logical key to `(*dims)` = `(path)` cleanly. **Precondition satisfied:** the binCol is scan-invariant per key (trivially, it's constant), so a key's `(tier, period)` placement is stable across scans and shards align 1:1.

- **dims:** `path` only. Per-location scoping (below) makes `bucket` the **dataset** scope, **not a dim** (pyrmts **Correction B** — this spec originally had it inverted; bucket-as-dim is what a single *union* dataset would need to keep colliding paths distinct across buckets). Per-depth structure stays what the path-index already carries (`depth`, `usr`).
- **metrics / monoids:** `b` (bytes, sum), `o` (objects, sum), `c2/c3/c4` (class bytes, sum), `wts`/`wb` (created-weighted, sum) — all additive, so the existing coarse-tier floors (`COARSE_EXPS`) map to pyramid tiers.
- **tier ladder — resolved: the floors are NOT pyramid tiers; they are a read filter.** A pyrmts tier is a monoid rollup along the bin axis (tier k+1's rows are *combines* of tier k's — cascade / consolidate / canonicalize all rely on that invariant). A byte-floor (E = 16/20/24) is a value-threshold *subset* of the same rows with no combine, so "a tier per floor" would be a category error that makes cascade do the wrong thing. And with a constant `binCol` the path-index pyramid has **exactly one time tier** — there is nothing to ladder. So: **one tier, floors as a read filter**, which pyrmts already makes cheap the idiomatic way: `ColumnFilter` supports `{ col: 'b', range: { min: 2**E, max: Infinity } }` and `fetchShardData` prunes row groups on min/max stats. **Layout (correction round 2): two files per scan.** The `b DESC` order a floor read wants and the `(depth, path)` order the diff/drill walk needs are different sorts, so: a small **hot shard** — rows with `b ≥ 2^16`, written sorted `b` DESC (the writer takes a `sort` override, as a filtered + sorted write) — serves the top-level render, a floor being a row-group prefix of it; the **full `(depth, path)` shard is the tier** and serves drill + diff. The hot subset is a few % of rows, so the duplication is cheap. This still replaces today's three `path-index-coarse<E>.parquet` tiers with one hot file. **Caveat:** MS archives are *path*-sorted (interval contiguity), so floor-pruning does **not** apply to them — but they serve history, not the hot treemap. Hence: **keep the newest scan's individual shard hot — never `drop` the latest**; the read path prefers it for the current treemap (floor-pruned), while MS + diff-index serve history; "tree as of day X" = `extract_scan` (full) then filter, fine for an occasional historical read.

**Dataset scoping — one union dataset vs. one per location. Open question #2, and the federated-scans lever.** Today the ingestion builds **one** union path-index (three buckets grouped as the Map's top cells). pyrmts's guidance is "per-location dataset = its own dyadic ladder." Two shapes:

- **(a) one dataset for the union** — matches today's single index; simplest; one MS ladder + one diff-index. But couples the three buckets' scan cadences and makes "add a bucket / a new cloud location" a re-consolidation.
- **(b) one dataset per bucket/location** (`ctbk`, `crashes`, `jc-taxes`) — each with its own MS ladder + diff-index; the union Map reads across datasets via the manifest. This is exactly the [federated-scans](./federated-scans.md) north-star (each location consolidates independently; the union reader routes via the manifest) and lets a new location join without touching the others. Cost: the union view composes N dataset reads.

**Resolved: (b) per-location** (pyrmts agrees). Three refinements from the review:

- The fan-out is **smaller than feared.** A path lives in exactly one bucket, so a per-path over-time read hits **one** dataset — no fan-out at all. Only two *union* reads fan out, and both compose trivially because bucket key-spaces are disjoint: the union root line = **sum** of N bucket roots (additive metrics, N small reads), and the union diff = **concat** of N `diffOverSpan` results (no merge). Since the Map's top cells *are* the buckets, per-dataset results map onto the widget directly.
- If bucket scan cadences ever differ, "day X" is a **different position per dataset** — resolve labels per dataset via each `index.json` at read time; **never assume aligned positions across datasets.**
- `bucket` is the dataset scope, not a dim (Correction B above).

### 3.3 Correction A — two pyramids, not one

The path-index and the age index have **different `binCol`s**, so they are **two `Pyramid` configs / two datasets** (per location), not one config serving both:

- **The path-index pyramid** — constant `binCol` (the degenerate-time case): one tier (the full `(depth, path)` shard) plus the hot `b DESC` shard, floors-as-filter (§3.2); the **MS store + diff-index run over it**. This is what serves the treemap, the over-time line, and — via our own walk over the full shard / MS archive, not `diffOverSpan` — the diff-treemap.
- **The age pyramid** — `binCol` = created-date, a *real* time-bin ladder (like cw's 1h → 8d dense ladder): here tiers genuinely *are* bin-coarsening rollups, so it is standard pyrmts. This is what serves the age chart.

`§5`'s age bullet and `§6` item 3 refer to the age pyramid specifically.

## 4. Ingest wiring (ours — `daily-ingest.yml`)

After the existing `dt-cloud path-index` build + upload, two **idempotent** post-scan stages (both no-op when nothing is new):

1. **MS seal:** `multiscan seal <config>` (`scheme: exponential`) → consolidates the new scan into the dyadic ladder; then sync the routing manifest → D1 **mirroring the row set**.
2. **Diff-index update** (layout final at pyrmts main `a783633`): `pyrmts-engine diffindex update -D <dataset> -k <tile-key> -R <scans-root> -o <index-root> [-L <levels>] <config>` → appends every not-yet-indexed scan in order. Layout: an **L0 events log** — one adjacency node per scan pair, `L0/{k}.parquet` (standard changeset rows: key cols + `{c}__a` / `{c}__b`) — plus optional **aligned** levels under a cap; `index.json` = `{dataset, scans, levels}`. `-L/--levels N` sizes a *new* index; an existing index keeps its cap. Nodes are append-only / immutable → plain R2 puts. **For the daily demo start with the default (L0 only):** storage is strictly below the interval archive and a span read is `j − i` tiny files; add levels later only if audit reads over year-long spans matter.

Both run **per dataset** — and per §3.3 there are two datasets per location (path-index + age), so per bucket that is two configs × two stages. **Tile-key stability:** `diffindex update -k <tile-key>` with a constant bin means **one nominal period label — pick it once and keep it stable across scans**; it is part of the tile key that *both* the MS store and the diff-index key on, so changing it later orphans history. The existing generation dir (`listing/<date>/index/<gen>/`) keeps serving the *current* subtree/map read until the pyrmts read path replaces it; individuals are only `drop`ped after digest-verify (Phase 2c verify-then-drop), and **the latest scan is always exempt from `drop`** (it stays the hot, floor-pruned shard — §3.2).

## 5. Serve wiring (ours — `site/functions`, the base; inherited by cw/gcs)

- **Over-time line** (`/api/series`): `pyrmts-cfw` + `MultiScanD1Index` for the D1-backed manifest; `resolveScan` / `readMultiScan` for routing; `seriesAcrossGroups` for a per-path line across archives. Per-read manifest resolution (never a cached archive URL — §3.1). Footer-pruned per-path reads stay O(pruned).
- **Diff-treemap** (`/api/diff`): **stays on our walk** (correction round 2). In the base that is `buildDiff` (`site/functions/_lib/view.ts`): both scans' listings read at one shared byte floor under the pixel budget, O(rendered). Under pyrmts the only change is *where* "children of P at scan s" comes from: the hot / full shard for the latest scan, the MS archive (`__scan_lo <= s < __scan_hi` after RG-prune-by-path) for a dropped one — resolved per read via the manifest (§3.1). `diffOverSpan(scans, schema, a, b, loadNode, levels)` (`{ scans, levels } = parseDiffIndexManifest(bytes, dataset)`; `loadNode(level, i)` = R2 fetch of `L{level}/{i}.parquet` → `readChangesetNode`; `alignedBlocks(i, j, levels)` picks the nodes) is kept for **audit / changelog / gross-churn** views and as the **build input** for the Flask line's `diff_index.py` on non-adjacent pairs. It is span-pruned, not per-key prunable — never footer-prune it per path the way the over-time read does.
- **"Tree as of day X"** (subtree/map for a historical scan): `extract_scan` from the current archive once individuals are dropped.
- The age chart (currently hidden on r2) un-hides once the **age pyramid** (its own config, §3.3) serves the created-date tiers.
- **Footer cache:** `fetchShardData` takes a `metadataCache` (Map-like, keyed `key@etag`); a module-level `new Map()` in the Worker is the per-isolate version, so a warm isolate skips the footer fetch + decode per shard. Adopt the same in the base's own parquet read path (`_lib/index.ts`) when wiring pyrmts — it is the serve-side twin of §5.1 item 3.

### 5.1 Making the Flask walk rendering-bounded (pyrmts's measured plan; recorded, not started)

Applies to `src/disk_tree/diff.py` `recursive_diff` (the Flask `/api/compare` engine). The base's `buildDiff` already stops at the pixel-budget frontier (item 2 below is its design), so this is the `flask` line's catch-up; items 3–6 also apply to any per-listing parquet read. From our own numbers: 45 s → 5 s at budget 200 with 8-way batching is ~100 ms wall per listing, and 3,257 rows were still `pruned` (the budget stops before the render frontier). pyrmts probed a 200-child listing on a 64K-row RG at ~1.3–2 ms decode (pyarrow / duckdb), so the ~100 ms is **not** decode. Candidates: remote read latency (`blobfs`), chunk resolution, `to_pandas` (object strings), the Python-lambda depth mask over every loaded row, `str.rsplit` / `set_index`, `_aligned`'s `reindex` + `add_suffix` + sorted outer join, and the **footer**: `pq.read_table` re-fetches and re-parses it on every listing (~100 RGs × cols of Thrift per call at 64K-row RGs on a home scan). Page index is a non-lever (pyarrow / duckdb / hyparquet all ignore it for filter pruning, measured); small RGs is the lever, already pulled.

In order, all independent and composable:

1. **Harness first:** a repeatable walk benchmark with per-stage timers per expansion (locate RGs, fetch bytes, decode, post-process) + totals (expansions, listings, bytes, wall), on a synthetic tree + one real pair. Three of the levers below are guesses until this exists.
2. **`min_frac` as the stop criterion:** don't expand a dir whose size *and* |Δ| are both < `min_frac × root` (the `MIN_CELL_PX / canvas_px` the index-served slice already computes); keep `budget` as a backstop. The walk then terminates at exactly the rendered frontier instead of a fixed expansion count.
3. **Decoded-footer cache in `blobfs`** keyed `(path, mtime / etag)`: `pq.ParquetFile(path, metadata=cached)` skips the fetch + parse. Cheap, unconditional. (pyrmts is adding the same to `fetchShardData` on their side.)
4. **Batch / cache expansions by RG within a request:** sibling dirs' children are adjacent sub-runs in `(depth, path)` order and usually share an RG; decode it once.
5. **Arrow-native listing** (or polars; benchmark both vs pandas): filter with `pyarrow.compute`, take only `path` + compare cols to lists, align in a dict — removes the per-op pandas fixed costs and object-string churn.
6. **Measure 16K-row RGs vs 64K** (`migrate-row-groups` exists): footer grows, bytes/listing shrink 4×.

### 5.2 Bake-off on the r2 demo: `walkDiff` (pyrmts `b6a0fe6`) vs `buildDiff` (2026-09-23)

pyrmts shipped `walkDiff` + `SnapshotReader` (TS, over any `Storage`; dist `b1f862f`) as the deployable twin of their Python harness. Measured here on the demo's real pair (2026-09-21 → 2026-09-22, root view, 1280×800), `walkDiff` locally over `serve-range.mjs`, `buildDiff` as prod `/api/diff` (`Server-Timing`):

| engine | rows | expansions | GETs (cold) | dependent rounds | server/CPU ms (cold → warm) |
|---|---|---|---|---|---|
| `buildDiff` (prod, D1 stats + R2) | 204 | 49 | (D1: spans + rgjson, then group fetches) | — | 700 → 266 (`rootagg` 407 → 127, `walk` 123 → 0) |
| `walkDiff` (local, 0 ms RTT) | 2,774 | 216 | 2 footer + 2 data | 1 | 44 → 29 |
| `walkDiff` (local, 30 ms RTT) | 2,774 | 216 | 2 footer + 2 data | 1 | wall 147 → 101 (warm = `metadataCache` hit, no footer GETs) |

What the numbers say — and don't:

- **At the demo's scale the engine is moot.** Each daily path-index is **7,118 rows in ONE row group (~220 KiB)**, so any walk is "read both files, diff in memory": one dependent round, ~30 ms CPU. `buildDiff`'s 266–700 ms is not the walk (`walk` = 123 ms cold, 0 warm) but its *indirection* — the D1 row-group-stats queries and `rootagg` — which is pure overhead when the whole tier is one group. The cheap win here is orthogonal to engines: for a single-group tier, skip D1 and read the file.
- **Not the same output.** `walkDiff` emits every changed row above the 4 px render floor (13.7 MB here); `buildDiff` returns the treemap's *row list* (`top=500`, `minArea`, `(other)` folds). So 2,774 vs 204 rows is shape, not correctness. A byte-equivalence check needs the fold applied to `walkDiff`'s rows first.
- **The real bake-off target is cw's index, not ours.** pyrmts already measured it locally (10 M rows / side: walk rows == materialized view, 98 expansions / 48 GETs / 13 dependent rounds at the fleet root). Over a network that is where level-synchronous rounds matter; here there is one round.
- **Adoption constraint at cw scale:** `SnapshotReader` locates row groups by bisection over the **in-memory parquet footer**. Our base deliberately does *not* parse that footer on the edge — cw's floor-free tier has ~27k groups and its thrift footer exceeds the Worker's memory (`_lib/index.ts`) — which is exactly why the row-group stats live in D1 / the `.groups.json` manifest blob. So "adopt `walkDiff`" in the base means **an adapter that feeds `SnapshotReader` our D1 / manifest row-group stats** (the `IndexHandle` seam), not a wholesale swap; the pyrmts hot/full shard split at 64K-row groups would also shrink cw's footer to ~160 groups, at which point in-memory bisection is fine again.

**Follow-ups landed on the pyrmts side (2026-09-23, after this table):** the adoption hook exists — `SnapshotReader({ rowGroups })` takes a `RowGroupIndex` (per-group row range + depth/path min/max, plus `rowGroup(i)` returning that group's column-chunk metadata on demand — from D1 or a manifest); no `head`, no footer read, decode through a synthetic footer of just the run's groups (pyrmts main `b4df01d`; producer side `rowGroupSummaries` / `rowGroupIndexFromMetadata`). That is exactly our `IndexHandle` seam (`index_row_groups.rg_json` is already hyparquet's per-group metadata), so the adapter is small when wanted. pyrmts also pinned the hyparquet fork (`runsascoded/hyparquet#0fc2270`, 1.31.1 + cross-row-group byte-range coalescing) in `71cbfd5`; consumers of pyrmts' dist get it transitively. **The like-for-like bench at scale** (cw's 10 M-row pair) needs: the two cw scans on a network origin (a bucket decision), `walkDiff` from a Worker (a `pyrmts-cfw` bench route), and — ours — importing that pair into the base's layout (`.groups.json` manifests + D1 stats rows + coarse tiers) so `buildDiff` answers the same view. Not started.

Verdict: no engine change for the demo now; keep `buildDiff`. Revisit when (a) the path-index moves onto pyrmts shards (§3.2) and (b) cw wants a shared engine — then plug `walkDiff` in through the row-group-stats seam and re-run this table on cw's pair over the network.

## 6. What this retires (in order, each behind a working replacement)

1. the over-time index (`_lib/overTime.ts`, `overtime.py`, `api/series.ts` additions) → MS store reads,
2. ~~the persisted diff-index backend~~ — **not retired** (correction round 2): the walk stays for arbitrary pairs and the materialized index for the adjacent pair; only its *build* for non-adjacent pairs moves onto composed pyrmts changesets,
3. `write_age_pyramid` / `age-pyramid-<bin>.parquet` → the **age pyramid**'s created-date tiers (a separate config from the path-index pyramid, §3.3),
4. eventually the per-generation full path-index tiers → `extract_scan` + the MS store (storage collapses by the §1 factors).

## 7. Phases

1. **Pin the mapping with pyrmts** (this spec's §3.2 + open questions #1/#2). No code.
2. **Pyramid configs + ingester** for the r2 datasets (per bucket, two configs each — §3.3; path-index writes hot + full shards — §3.2); wire `multiscan seal` into `daily-ingest.yml` (idempotent), R2 puts + mirrored D1 manifest. `diffindex update` with the default L0-only cap (§4). Individuals kept (no `drop`) until the read path is proven.
3. **Serve path in `site/functions`**: over-time via `pyrmts-cfw` manifest routing; `buildDiff` + `/api/subtree` reading the hot / full shard for the latest scan and the MS archive for history. Behind a flag / parallel endpoint until byte-equivalent to today's answers. (§5.1's Flask-walk plan runs as its own track, harness first.)
4. **Cut over + retire** per §6; enable `drop: true` after verify-then-drop **with the latest scan exempt** (it stays the hot, floor-pruned shard — §3.2); un-hide the age chart once the age pyramid serves.

## 8. Coordination

- **pyrmts** reviews this spec (Pyramid mapping, per-location dataset scoping, diff-index wiring). Ping the pyrmts session when it's up (done on commit).
- **cw-s3** owns the DuckDB producer + CFW reader being upstreamed into pyrmts, and is wiring MS for its 12 h GCS fleet; our serve wiring in the base is what cw/gcs inherit on rebase — coordinate via the mgu handoff (`/Users/ryan/c/oa/marin-gcs-usage/specs/disk-tree-as-base.md`).
- Related specs: [`union-of-roots.md`](./union-of-roots.md) (the base plan), [`federated-scans.md`](./federated-scans.md) (per-location scans + union reader — §3.2 (b) is its realization), `pyrmts:specs/multi-scan-consolidation.md` (§"grouping policy" / Phase 2d for exponential), `pyrmts:specs/pyrmts-column-cube.md` (the *other* redundancy — across tiers within a scan; separate axis, not this spec).
