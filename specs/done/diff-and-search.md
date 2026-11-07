# Snapshot diff + recursive filter/search, at 1e9 rows

Two features with a lot of runway, designed together because they are the same primitive underneath: **select a subset of layer-2 rows, roll their sizes up to ancestors, emit a nested slice.** Diff selects "rows that changed between scans A and B"; filter selects "rows whose path matches a query"; the age lens, storage-class views, and future attribution overlays are the same shape. Build the subset→rollup→slice machinery once.

Target scale: marin's GCP estate (~588M objects → ~1e9 layer-2 rows including dirs). Everything here lands generically in DT and cherry-picks to mgu as the first large-scale consumer.

## Two properties that make this cheap

Both fall out of the existing layer-2 format (`path,size,…,n_desc,n_children,depth`, sorted depth-major then by path):

1. **A matched directory needs no descendant scan.** Its `size`/`n_desc` are already aggregates. "Recursively include everything under a match" costs one row, not a subtree — total cost is dominated by *finding* matches, never by re-aggregating them.
2. **Sorted-by-path makes nested-match dedup a linear pass.** A match inside a match (dir `foo` containing file `foo.txt`, both matching) is skipped by tracking the last included prefix while walking matches in path order.

## Measured baselines (2026-08-19, largest local scan: 4.02M rows, 153MB parquet)

| target | strings | regex match | throughput |
|---|---:|---:|---:|
| full paths, all rows | 4,019,754 | 167 ms | 24M str/s |
| distinct basenames (vocab) | 811,530 (20.2% of rows) | 22 ms | 37M str/s |
| distinct **dir** names | 49,957 (1.2% of rows) | 1.4 ms | 36M str/s |

Depth cumulative: depth ≤ 4 = 2.7% of rows, ≤ 6 = 11.9%, ≤ 12 = 87%.

Takeaways: RE2-style regex is not the bottleneck — **string count is** (throughput is flat across tiers). Dir-name vocabulary is 80× smaller than the path column and is what people actually search ("a project name across buckets" is a directory query). File basenames are high-cardinality (`part-00042-….parquet`), so the vocab trick is weak for files — they need the block index (below) instead.

Extrapolating to 1e9 rows: brute-force full-path regex ≈ 40s single-threaded / ~5s at 8 threads; dir-name vocab (even at 10× the distinct-ratio) ≈ tens of ms. The tiers below exist to close that gap interactively.

## Item 1 — path-range pushdown (`path_prefix` on `StorageBackend.load`) — **DONE**

The enabling fix for everything else, and for plain browsing: `load()` takes only `min_depth`/`max_depth`, so viewing/diffing one subdir loads *every row in the tree* at those depths. Layer-2 is sorted `(depth, path)`, so within each depth a prefix is a contiguous run and parquet row-group min/max stats on `path` prune it.

- Semantics (exact, not superset): `path == prefix OR path.startswith(prefix + '/')`.
- The identity that makes the range exact: `{p : p.startswith(pfx + '/')} == {p : pfx+'/' <= p < pfx+'0'}` since `'0' == chr(ord('/')+1)` — no post-filter needed in SQL backends; parquet uses DNF filters `[[…, path==pfx], […, path>=pfx+'/', path<pfx+'0']]`.
- All 4 backends; hybrid previously did **no pushdown at all** (full `pd.read_parquet` then pandas masks — depth included), so it gains depth *and* prefix pushdown here. With `follow_refs=True` hybrid expands chunks first, then applies the prefix as an exact mask (no pushdown; chunk-placeholder ancestors wouldn't survive a range filter).
- Wired into: `/api/scan` (subdir views), `/api/compare` `get_children` + `get_subtree_stats`, `cli/diff._children_at`, `ParquetBackend.get_path_stats` (fresher-child patching), `/api/histogram` (no depth pushdown possible there, but the prefix prunes sibling subtrees).
- Measured on the 4.02M-row scan above (hybrid backend, which previously full-read **2.55s for every request**): root browse (depth≤2) 50ms, subdir browse (prefix + depth≤3) 40ms, single-path stats 53ms, subdir histogram (prefix, all depths) 0.56s — 4.5–64×, and the factor grows with tree size since post-fix cost tracks the *slice*, not the scan.

## Item 2 — fix the accidental O(C²) in diff — **DONE**

`/api/compare` (`server.py`) and `cli/diff.py::_delta_rows` did `children[children['rel_path'] == rp].iloc[0]` *inside the loop over children* — a full boolean mask per child; a 100k-wide prefix is 10¹⁰ comparisons. Replaced with `set_index('rel_path', drop=False)` hash lookups. Output unchanged. Measured: a 50k-child dir diff went from ~145s (extrapolated from the old code on 5k) to 0.95s.

## Item 3 — diff

### 3a. The 90/10: best-first pruned recursive diff — **DONE**

One-level-at-a-time (the current `/api/compare`) answers "what changed in *this* dir"; at PB scale the question is "what changed *anywhere*, ranked". The 90/10 is a recursive descent with two cheap ideas:

- **Prune on stats equality**: if a dir's `(size, n_desc, n_children, mtime)` match on both sides, don't descend. Overnight snapshots leave >99% of the tree untouched; the walk touches only changed spines. Heuristic (compensating changes can hide), so surface it as the fast default with an `--exact` escape hatch; the digest column (3d) later makes pruning exact.
- **Best-first, not plain BFS**: priority queue on `|Δsize|` — pop the biggest-delta dir, merge-join its children (per-dir loads are cheap now via `path_prefix`), push differing subdirs. A row/time budget bounds the work; emit the frontier when it's spent. This subsumes "root children, then grandchildren if feasible" and spends the budget where the signal is.

Surfaced as `dt diff -r/--recursive [-b/--budget N]` and `recursive=1` (+`budget`, `max_depth`) on `/api/compare`, returning the delta *frontier* across depths: rows carry `depth`, `expanded` (descended into) and `pruned` (differing stats below, unexplored — budget/depth cut it), summary carries `expansions`/`truncated`. Implementation: `src/disk_tree/diff.py` — `ScanSource` (per-dir children loader: uri rebase into ancestor scans + hybrid chunk resolution + item-1 pushdown per load; correctness never relies on pushdown fidelity, an exactness mask enforces "one level under one prefix") + `recursive_diff` (heap on `|Δsize|`). `resolve_blob`/`resolve_chunk_for_path` moved here from `server.py` so the CLI shares them without importing Flask. Totals sum depth-1 rows only — a frontier row's Δ is already inside its ancestors'. `mtime` is in the descend-trigger (not the reported status), so a same-size rename surfaces as `added`+`removed` rows under a dir whose own Δ is zero.

### 3b. Materialized delta scans ("a diff is a scan") — **demoted: on-the-fly is the primary path**

Owner question (2026-08-19): do we need these at all, given on-the-fly recursive diff? Mostly no — 3a computes any pair, any depth, interactively, and precomputing every pair is O(N²) snapshots. Materialization survives for exactly two cases, both deferred until a consumer asks:

1. **Static-hosting consumers** — mgu's site is CF-hosted static JSON with *no server to compute on the fly*; a nightly artifact (adjacent pair only: yesterday→today, N−1 diffs, never all pairs) is how a diff view reaches it.
2. **Audit trail / repeated heavy pairs** — a much-revisited pair of huge scans where even the pruned walk is minutes; cache the walk's output as a blob (which is just 3a's result persisted, not a separate merge-join engine).

The original design (streaming merge-join of two layer-2 parquets → delta table registered as a scan blob) stays here as the implementation sketch for whenever (1) lands.

### 3c. Diff treemap: area = max(a, b) — **DONE**

Current `<CompareTreemap>` sizes cells by `|Δ|` (churn-only: unchanged/Δ=0 rows are dropped). Add the context-preserving mode as the default:

- **Area = `max(size_a, size_b)`** — deleted subtrees still occupy their old area; added ones their new area; stable structure stays visible as context.
- **Sub-rect encoding** (owner feedback, 2026-08-19): each cell splits into a grey rect of `min(a,b)` bytes plus a full-strength colored band of `|Δ|` bytes filling from the bottom — magnitude by *area*, not saturation (a `linear-gradient` background; `CellStyle.bg` already accepts gradients).
- **Polarity = git convention**: green = added/grew, red = removed/shrank — the view is a *diff*, and the summary chips and row tints already used it (the Δ colors previously used the opposite "growth = red" cost lens; that can return as a toggle if a consumer wants it). Applied across treemap, Δ text, bars, and Total Delta.
- Caveat to label: Σmax over cells exceeds either side's true total (an area metric, not a byte total). Keep `|Δ|`-area as the alternate churn view.
- Landed: `max`/`Δ` toggle in the treemap legend, `max` default; unchanged rows render as neutral context in max mode only; Δ mode tints the full cell by `Δ/max|Δ|`.
- **Nested (depth-2+), landed 2026-08-19**: the treemap consumes the recursive frontier (`recursive=1`, budget 200) alongside the flat depth-1 rows (which contribute the labeled grey context). Weights are bottom-up — leaf `max(a,b)`, parent `max(own, Σ children)`, so delete-X-add-Y churn honestly grows the parent instead of overflowing it; where children under-fill a parent, a grey `(unchanged)` filler cell absorbs the gap (no need to ship every unchanged row). Parent title strips tint by net `Δ/weight` (trend cue; magnitude stays in leaf bands). Children order by signed Δ at every level. Cells with in-tree children drill in-widget; frontier (`pruned`) and unchanged dirs navigate to `/compare` there. Expanded-but-unchanged intermediates (net-zero renames) get synthesized so their children still nest.

### 3d. Later, earned by measurement: `digest` column

Hash of child digests, computed in the bottom-up pass that already computes `size`/`n_desc` — near-free at index time. Buys (1) exact merge-join pruning (row groups whose path range falls inside an equal-digest prefix are skipped — coarse at 262k-row groups but effective when big subtrees are untouched, since depth-major layout scatters a subtree across every depth), and (2) **rename/move detection**: `removed X + added Y` with equal digests is one move — the difference between "40 TB deleted + 40 TB added, investigate" and "one directory was renamed".

## Item 4 — recursive filter/search

### v1: progressive brute force — no new indexes — **DONE** (UI consumes the plain endpoint; EventSource-progressive rendering is the remaining nicety)

Depth-major layout means shallow-first **is file order**: no extra index is needed for iterative deepening — stream row groups in order, match paths (regex or substring; `parseQuery` semantics from `filter.ts`), roll matches up to their depth ≤ N ancestors (N≈4, the treemap display depth), and push partial rollups over SSE (machinery exists for scan progress). Depth ≤ 4 arrives for ~3% of the scan cost; the deep tail fills in behind a progress indicator.

Honest display semantics: under a filter, the shallow treemap is *not final* until deep rows are scanned — a depth-12 match rolls up into its depth-1..4 ancestors, so cells grow as matches stream in. That is exactly iterative-deepening semantics and the UI should show scan progress rather than pretending completeness. This replaces the v0 "filtered (display only)" label with true re-aggregation.

Server side: `GET /api/filter?uri=&q=&depth=N` (+ `/stream` SSE variant); `dt filter <uri> <query>` CLI. Restriction to a subtree = `path_prefix` pushdown from item 1 — prefix and query compose for free.

Also landed: `/api/filter/stream` (SSE — `iter_filter_scan` yields one cumulative snapshot per depth; the final `done` event equals the plain response), and the UI toggle: the treemap footer's "filtered (display only)" label is now clickable and flips to **"filtered (re-aggregated): X GB in N matches"**, swapping the treemap to the matched slice (`buildFilterTree`). Matched dirs stay lazily drillable (their contents are wholly matched, so the normal scan endpoint shows correct sizes); frontier ancestors with matches below the slice depth are deliberately not drillable (a normal drill would mix unfiltered children into filtered sizes). The UI reads the plain endpoint with a 300ms debounce; wiring EventSource for progressive cell growth is the open nicety for scans where the load isn't sub-second.

Landed (`src/disk_tree/filter.py` + `/api/filter` + `dt filter`): outermost-only matching (a match inside a matched dir is already in its aggregate — property 1 made real), depth-major per-level walk with an `on_depth` cumulative-snapshot hook (the SSE seam), and covered-subtree exclusion via **binary search**: level paths are sorted, so each covered prefix is a contiguous `[pfx+'/', pfx+'0')` range — two `searchsorted`s per prefix instead of a vector pass (35s → 5.7s at 150 covered dirs / 4M rows). Real-scan smoke: `dt filter /Users/ryan/c '/node_modules$/'` → 33 GiB across 152 `node_modules`, 5.7s end-to-end. Depth is *always* derived from `path`, never trusted from the column — the smoke exposed that hybrid's chunk expansion (`_unbase_paths`) re-rooted paths while leaving chunk-relative depths (fixed + vectorized; the stale column made 'hccs' match 999k descendants instead of 1 outermost dir). `freshest_scan_covering` moved to `registry.py` so the CLI shares it Flask-free.

### The (prefix × query) product space: don't precompute results, precompute two 1-D indexes

Precomputing treemaps for a dense query trie (even just 3–4 literal chars ≈ 26³–26⁴ nodes × a treemap each) is dominated by storing **postings** instead: the same refinement power, less storage, exact at query time. The two tries considered (query-trie with our current index at the empty-query root; inverted path-prefix/segment trie) **collapse into one structure each**:

- **Occurrence postings sorted by path** — `name → [locations]`. Subtree restriction is a binary-searched path range over a posting list; that *is* the segment-prefix trie, flattened.
- **Trigram postings over the name vocabulary** — `tri → [name ids]`. Query restriction is trigram-set intersection/union; that *is* the query trie, with the monotonicity handled properly (next point).

The 2-D product space is served by intersecting two orthogonal 1-D structures — nothing quadratic gets precomputed, and both are cheap sidecars a scan cron can emit.

### Monotonicity: substring yes, regex no — but regex doesn't need it

- **Substring queries are monotone under extension**: any name containing `abcde` contains `abc`, so R(`abcde`) ⊆ R(`abc`) — a longer query's results are always a subset of its prefix's, and trigram postings give a guaranteed superset to verify.
- **Raw regex is not** (`abc|def`, anchors, classes) — the user's suspicion is right. But Cox's regex→trigram compilation ([Google Code Search][cox]) turns *any* regex into a monotone boolean formula over trigrams that over-approximates it: `abc|def` → `tri(abc) OR tri(def)`; survivors are verified with the real regex engine. So there is **no need to relax to segment-substring for tractability** — substring is just the trivially-compilable case, and full regex keeps its power.
- Degenerate regexes (< 3 extractable literal chars, e.g. `a.`) fall back to a brute vocab scan — which the baseline table shows is ms-scale even without trigrams, because the vocab is tiny. The fallback is fine.

### The many-match union (and why it's not scary)

A query matching many names (worst case: single common letter) yields a union of occurrence lists. Cost is O(total occurrences of matched names), not O(rows):

1. matched **dir** occurrences carry aggregates — no descent (property 1);
2. concatenate posting lists, sort by path (or k-way merge — each list is pre-sorted), dedup nested matches in one linear pass (property 2);
3. roll up to depth ≤ N ancestors in the same pass — that's the treemap shape.

Guardrail: if candidate occurrences exceed a threshold, degrade to the progressive v1 scan with the same SSE surface — a query matching most of the tree isn't a useful filter anyway.

### Index tiers (v2, in order of leverage)

1. **Vocabulary sidecar** — distinct segment names, dirs first-class (1.2% of rows here), plus file basenames (20%). Emitted during the finalize pass that already streams every row. — **DONE** (see below)
2. **Name → row-group block index** — the expensive step is names→rows, and this is what fixes it: 1e9 rows at 262k/group ≈ 4k row groups; most names hit a few, so a selective query reads ~10 row groups instead of ~4k. Block-level, not row-level, keeps it small. Covers file-name queries (extensions etc.) where vocab is weak. — **DONE** (folded into the sidecar as a `row_groups` list column)
3. **Trigram postings over the vocabulary** — last mile, turns vocab-scan ms into µs and enables the regex compilation above. 100–1000× smaller than trigrams over paths (a path is a sequence of vocab entries), losing nothing. — not yet needed: unindexed vocab matching is ~1s at 1e9 (probe), so build this when a consumer feels that second.

### Tiers 1–2 implementation (landed 2026-08-20)

`src/disk_tree/sidecar.py` + `disk-tree vocab URI` + auto-use in `dt filter` / `/api/filter` / `/api/filter/stream` (`-B/--brute` and the `indexed` response field for observability).

- **One artifact, both tiers**: `<blob-stem>.vocab.parquet` — `name` (sorted), `n_dirs`, `n_files`, `row_groups` (list<int32> of layer-2 row-group ordinals). Sorted names + 64k-row groups make it range-prunable for static consumers (DuckDB-wasm / HTTP range reads — mgu's lakehouse arc); the server/CLI scan the vocab, union matched names' `row_groups`, read only those groups, exact-rematch basenames, and feed the matched rows through the existing dedup/rollup with an empty query (`filter_scan(matched, '')`) — no second rollup engine.
- **Matching semantics keyed on the query, not the index** (PoLS): a query containing `/` matches full paths (brute only); slash-free queries match path *segments* (basenames) — identical to path-matching for substrings after outermost-dedup (a slash-free substring can't span a separator), and the intuitive reading for regexes (`/foo.bar/` stays within one name). `query_mode()` in `filter.py`; the brute path applies the same rule, so results never depend on sidecar presence.
- **Safety**: builds refuse blobs with `child_scan_id` refs (the index can't see inside chunks; silently missing matches is worse than no index) — per-chunk sidecars are the follow-up if hybrid-local wants the speedup. Freshness is mtime-based: `/api/delete` rewrites blobs in place, so a sidecar older than its blob is ignored.
- **Blob row groups now bounded** (`BLOB_ROW_GROUP_SIZE = 262_144`, all save sites): pandas' default wrote whole-file row groups, which quietly nullified the depth/prefix pushdown's row-group pruning *and* the block index. Old blobs still work (correctness never depends on pruning), just prune coarsely.
- Measured (4.02M-row scan, 16 RGs): build 7s → 13MB sidecar (811k names); selective queries (`juq`, `ctbk`) hit 5–6/16 RGs → **4–5× vs brute** with exact node parity; the everywhere-name (`node_modules`, 15/16 RGs) converges to brute (1.3×) — the many-match guardrail case, no regression. Wins scale with RG count: at mgu's ~4k groups a selective name reads ~10 groups instead of the file.
- Follow-ups: out-of-core builder for 1e9-row blobs (DuckDB group-by; the pandas accumulator is fine to ~50M), per-chunk sidecars for hybrid, emit-during-finalize (build currently reads the blob back), trigram tier when a consumer needs sub-second on the vocab scan itself.

The shapes themselves are content-agnostic — nothing gets fitted to one estate. What per-store stats decide is *which sidecars are worth building per scan* and default thresholds (e.g. when to degrade to the progressive scan): collect distinct-name ratios and occurrence skew at index time and choose adaptively per scan (skip the vocab tier where the distinct ratio ≈ 1, etc.).

### mgu probe results (2026-08-19, 594M object rows — `tmp/vocab-probe-mgu-1e9.txt`)

mgu ran the probe over their raw `listing-2026-08-15` shards (6 marin-* buckets, DuckDB on a 61GB box). The vocab tier is decisively validated; two laptop-derived assumptions needed correction:

| metric | laptop (4.02M rows) | mgu (594M rows) |
|---|---:|---:|
| distinct basenames / rows | 20.2% | **17.0%** (100.8M) — ratio holds |
| distinct dir-segment names | 50k (1.2%) | **15.5M (2.6%)** — 38× smaller than rows |
| distinct dir paths (parents) | — | 165M (layer-2 ≈ 759M rows total) |
| vocab substring vs full-path | — | 0.4s vs 5.5s (**14×**, unindexed) |
| vocab regex vs full-path | — | 1.0s vs 9.4s (**9×**, unindexed) |
| rows at depth ≤ 4 | 2.7% | **30.3%** — wide, shallow tree |

Takeaways:

- **Tier 1 (vocab sidecar) pays for itself before any tier-2/3 index exists**: a plain vocab scan is already ~1s at 1e9 vs ~10s brute force, and it's the artifact a *static* consumer can ship (mgu's sites are CF-Pages static — the server-side `/api/filter` doesn't reach them; the sidecar does).
- **Tier 2 (block index) is required for file-name queries**: 100.8M basenames is too many to brute-scan interactively; names→row-groups is the fix, as designed.
- **Trigrams (tier 3) remain the last mile** — 15.5M dir names is small enough that even unindexed matching is ~1s; trigrams take it to ms but aren't the gating step.
- **Correction — the depth histogram does not transfer**: mgu's tree is wide and shallow (depth ≤ 4 = 30% of rows, vs 2.7% locally; median object depth ≈ 5–6). "Iterative deepening gets depth ≤ 4 for ~3% of the cost" is a laptop fact, not a law — at mgu, prefix pushdown dominates depth pushdown, and the progressive scan's early snapshots cost ~⅓ of a full pass, not ~3%.
- **The many-match guardrail is load-bearing, not theoretical**: `checkpoints` as a substring matches 103M full paths (17% of rows). Occurrence-postings unions for common names must degrade to the progressive scan.
- Consumer note: the first consumer wants the sidecar consumable **statically** (pre-sharded, range-requestable — e.g. vocab sorted + trigram postings as separate blobs a browser can binary-search via HTTP range requests, DuckDB-wasm optional). Design the artifact layout for that from the start; the server just gets a faster backend for free.

## Sequencing

1. ~~`path_prefix` pushdown~~ (done, landed alongside this spec)
2. ~~O(C²) diff join fix~~ (done, landed alongside this spec)
3. ~~3a best-first recursive diff (endpoint + `dt diff -r`) and 3c max-area diff treemap~~ (done)
4. v1 progressive filter (`/api/filter` + SSE + UI wiring; retires the "display only" label)
5. 3b materialized delta scans (merge-join engine work)
6. ~~Measure at mgu scale~~ (done — probe results above) → 3d `digest`, vocab + block index + trigrams as the numbers justify. Vocab sidecar is next: validated, and the first consumer (mgu's static sites) is waiting on it.

1–5 need no on-disk format changes and apply to scans that already exist — they CP to mgu without re-indexing. 6 changes the index format and wanted mgu's real cardinalities first (now in hand).

[cox]: https://swtch.com/~rsc/regexp/regexp4.html
