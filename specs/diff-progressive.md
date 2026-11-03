# Progressive diffs: one directory level per request, bounded by construction

**Status:** step 1 (`churn_cum` / `churn_end` on the folded path versions: build, append, carries, compaction, `backfill-churn`) built 2026-11-01, branch `churn-cum`; step 2 (`/api/diff/level`, `_lib/diffLevel.ts`) built 2026-11-02, branch `diff-level`; step 2b (bounded level reads, §3b) decided 2026-11-02: exact only; page-level reads first (2b.1, with a parallel cut), the dyadic churn index only for wide directories (2b.2), per-directory errors; measure offline, then build, on dev only. Step 3 (the client) waits on 2b. Shared (`[cloud]`). Replaces the lazy descent's planner and budgets (`specs/diff-from-changes.md` revision 3: `DESCENT_MAX_EXPANDABLE`, `DESCENT_BUDGET_MS`, `DESCENT_BUDGET_ROWS`, `HandOff`), which were tuned per deployment rather than derived.

## Goal

A diff map that is **always** fast, by a cost bound we can state, not on average:
- first paint after one round of reads, whatever changed and wherever;
- every request's work bounded by a constant of the design (cells per level), never by subtree size, depth or fleet churn;
- the map fills in progressively, level by level, as answers arrive;
- what changed is never hidden, renames included (§4).

## Open question 1 (for Ryan): what does a cell's area mean?

Today a Δ-mode cell's area is its **net** change |b − a|. A folder whose children were renamed (`a/x` → `a/y`, 500 TB) nets ≈ 0, draws as a sliver, and never expands: 1 PB of churn is invisible until someone drills into `a` by hand. Proposal: **area = gross churn** (bytes removed + bytes added under the cell, between the two scans), **color = net** (grew / shrank / neutral). A pure rename then draws as one large neutral cell that opens into red `x` and green `y`. Cost: a folder rewritten in place every day (scratch, checkpoints overwritten) draws large too, which is arguably what a "what changed" map should show. `?dm=max` (area = max(a, b)) is unchanged.

Everything below works for either answer. §3 adds one column, and with it the work bound holds by construction: **churn is the default area**, net is a toggle (`dm=net`), and max stays as is. Ryan, 2026-11-01: churn "is nice because it's a monoid, both up the tree and across versions"; worth offering as a toggle or making default.

## Cost model (why the current readers miss "always")

- **As-of walk** (`buildDiff`): reads everything above a byte floor under P on both scans, all depths in one batch. Wall time ≈ 1–4 s regardless of what changed, plus CPU the Workers clock doesn't count (cw: median `cpuTime` 1.18 s on cold misses, ~23 ms on edge hits).
- **Lazy descent** (`buildDiffDescent`): cost follows the changes, but each level waits on the one above, inside one request. A deep change chain costs depth × round trip, and a finer Δ floor draws 500–1,000 cells against the walk's 80–290. The planner and budgets cap the damage, but they're thresholds, and a hand-off pays the descent's spent time on top of the walk.

The fix is structural: **don't chain levels inside a request.** Let the client do the descent. Each request answers one directory's children, and the client draws each level as it lands.

## Design

### 1. The level request

`GET /api/diff/level?from=&to=&path=P&k=K` returns P's **changed** children between the two scans (D1 < D2), the K with the largest change and the rest folded into `(other)`:

```
{ path, a: {b, o}, b: {b, o}, churn?, rows: [{p, k, a, b, oa, ob, churn?, s}], other: {a, b, oa, ob, churn?, n}, unchanged: {b, o} }
```

- **What's read.** P's direct children (depth dP+1, path range `[P/, P0)`) from the `path` sort, **pruned by version bounds before reading**. Every group carries `vf_min/vf_max/vt_min/vt_max` (`GROUPS_SCHEMA`). An open-segment group with `vf_max ≤ D1` holds only versions live since before D1 and still open, i.e. unchanged children, so it's skipped unread. A closed-segment group is read only if some version in it closes in `(D1, D2]`. So **unchanged children are never read**; they're `unchanged` = P's totals − Σ changed children, by subtraction from `readRootAgg` (two point reads).
- **Bounded fanout.** A directory with very many changed children (a sweep) is read from the `bysize` sort instead, largest first, with the same version pruning. It stops when the size bucket's upper bound falls below the K-th largest |Δ| found so far (|Δ| ≤ max(a, b), so nothing unread can enter the top K). That's an exact top-K with a stopping rule, not a budget.
- **Bound.** Rows decoded ≤ (groups holding P's changed children) × group size, and with `bysize`, the groups above the K-th |Δ|. No dependence on P's size, depth or anything outside P. Measured target: p99 server wall ≤ 1 s cold, on all four deployments' diff-check cases.
- **Cache.** The answer depends on (store generation, D1, D2, P, K) only: **not on canvas size or floor**. One edge-cache entry per (pair, directory) serves every canvas, zoom and later drill.

### 2. The client: progressive descent

- On a diff view, request `level(P)` and draw it at once (one round of reads). Above it, the summary (`summary=1`, totals only) already lands first.
- Then request `level(child)` **in parallel** for each drawn child whose cell is big enough to show its children (cell area ≥ a minimum legible area), largest first, at most M requests in flight. Draw each into its cell as it arrives. Recurse until no cell is big enough.
- The design has one knob per level: K (children drawn per directory) and the minimum cell area. Both are display choices, set from the canvas, not from data.
- Time to first paint = one level request. Time to complete = depth × one level request, but every intermediate state is a correct, summing map (each unexpanded cell is drawn as a leaf at its own size).
- The loading state is explicit per cell: a cell whose level request is in flight shows a small spinner in its corner; the map header shows "N of M folders loaded". (Ryan asked for a clearer loading state; this makes it structural.)

### 3. Churn: definition, storage, and why it bounds the work

**Definition.** For a scan s and its predecessor s⁻, a file's churn is |size_s − size_s⁻| (its full size where it appeared or went away), and a directory's churn is the sum over the files under it. Over a range `(D1, D2]`, churn is the sum over the range's adjacent scan pairs. So a file created and deleted inside the range counts twice, while net change misses it entirely.

**It's a commutative monoid in both directions,** additive up the tree (a directory's churn = Σ its children's) and across time (a range's churn = Σ its adjacent pairs'). It's actually a group (sums have inverses), so time needs no dyadic index: **prefix sums are enough.**
- **Storage: one column, `churn_cum`, on every path version.** A path's cumulative churn up to the version's `vf`. It changes exactly when something under the path changes, which already opens a new version of the path (its size, count or mtime moves). The exception is a pure same-size swap, where the column itself forces the boundary. So it adds no rows, one int64 per version, written by the same append and compaction (an O(changes × depth) rollup of |Δfile| to its ancestors per scan).
- **Range churn = `churn_cum`(D2) − `churn_cum`(D1).** These are the two versions the level request already reads for a changed child; an unchanged child has churn 0 and is never read.

**Why it gives the bound we lacked:**
1. **Children's areas sum to the parent's.** Churn is additive up the tree, so a churn map is a true treemap: each level partitions its parent's rectangle. The cells at or above the legible minimum area per level are therefore ≤ canvas area / minimum cell area, and the cells expanded over the whole map are ≤ depth × that. **Work is bounded by the canvas, not by the data.** Net |Δ| has no such property: children's |Δ| can sum to far more than the parent's |Δ| (a rename sums to 2× while the parent nets 0), which is why the descent drew 500–1,000 cells and needed budgets.
2. **Churn ≥ |net| at every node** (triangle inequality per file, summed). So churn is an admissible pruning bound for the net map too: any node whose net |Δ| clears the floor has churn that clears it, and expanding by churn never misses a net cell. A net-area map can be computed exactly from the same reads. Its cost is bounded by the churn cells over the net floor, which exceeds the net map's own cells only by the churn/net ratio, i.e. exactly where moves dominate (and the churn map is the view that shows them).

**Wide directories (a sweep of 10⁶ changed children).** This is the churn analogue of a plain treemap folder with 10³+ similar-size children, which `bysize` solves because size is a stored per-row value. Range churn is a difference, not a stored value, but `churn_cum`(D2) is an **upper bound** on it (cumulative churn is non-negative and monotone). So a **`bychurn` sort** (P's children in `churn_cum` descending order, like `bysize`) supports an exact top-K with a stopping rule. Traverse the after side in that order; a row live since before D1 (`vf ≤ D1`) has range churn 0 with no lookup; otherwise look up its D1 version for `churn_cum`(D1) (Ryan's traversal). Stop once the next row's `churn_cum`(D2) is below the K-th range churn found or the floor: nothing unread can qualify. No zipped traversal of the before side is needed, because the after side's value already bounds the range churn. The bound is loose for children with a busy history and a quiet range (their cumulative churn is large, their range churn 0): they're read, but cost no lookup. Measure on gcs's sweeps and on long ranges before deciding whether it needs tightening (e.g. a per-version `churn_step`, which makes one-scan ranges exact). Superseded by §3b.2: the dyadic churn index bounds a wide directory's read by the canvas, whatever its children's earlier history.

**As built (step 1, `interval_churn.py`).**
- **Columns.** On the folded path versions (`pvl`) only, the pieces that already split on `last_read` without splitting the path version. `pv`, `pvf` and the owner slices are untouched.
  - `churn_cum`: the path's cumulative churn at the piece's `vf`.
  - `churn_end`: on a closed piece, the cumulative churn at its `vt`, that scan's churn included; NULL while open.
  - A path not live at D has cumulative churn = its last piece's `churn_end`, and a reappearing path continues from it. ("Last `churn_cum` + its size" assumed a directory's size is the sum of its files and that its going away was counted; neither holds in the v1 era.)
  - Served in `path` and `bysize`; the Worker projects named columns, so readers that don't use them are unaffected.
- **Base.** Churn is counted after the last scan that first lists objects after directory-only scans (v1 → v2: gcs's 2026-09-30), else after the first scan. It follows from the scans' `version`s (`churn_from`, `counts_at`), so `scans.json` carries only `"churn": true`. An append at such a scan counts nothing. In the v1 era every `churn_cum` is 0, since there are no file rows. A D1 before the base has no churn, and the diff falls back to the as-of walk; that's a data limit, not a threshold.
- **Build and backfill.** A rollup across the key ranges, because a directory's files sit in other ranges (`churn_gen`). First, every range's file events with their ancestors' sums (depth ≥ 1; the root's churn is the depth-1 sum, as its totals are). Then each range's pieces get the columns, splitting a piece where its path's churn moves inside it (a same-size swap). The local build runs it after `fold`; the staged build has `interval-store churn`, one task between `fold` and `cut`. `interval-store backfill-churn -O NEW` folds an existing generation's base and runs into a new one with the columns, byte for byte a fresh build's (`test_a_backfilled_generation_is_a_fresh_build`).
- **Append.**
  - `ranges`: `churn_cum` rides as a carried column (continuing versions keep theirs). Each range also writes its files' churn at D with the ancestors' sums (`state/<D>/churn/`), and its reappearing paths' last `churn_end` from the published roots (`state/<D>/prior/`).
  - `publish`: first sets the run's `pvl` deltas from every range's churn (`apply_run`): opened versions get their `churn_cum`, closed ones their `churn_end`, and a path churned without an opened version splits. It uploads them before the cut, and writes the opened versions to `state/<D>/pvl-fix/`, which the next scan's `ranges` takes in place of its open state.
  - Carries reduce `churn_end` by `max` (only the close record has one), and compaction keeps the columns as the inputs have them.
  - Base + runs, merged or not, and compacted, equal a fresh build version for version (the interval suites, on fixtures whose directory sizes aren't sums, so same-size-swap splits are frequent).
- **Measured on gcs** (base `2026-10-09b.g3`: 1.23 B versions, 560 M directory versions, 35 scan pairs after 09-30):
  - file events per pair: median ~2.8 M, range 0.4 M–126 M (126 M on a bulk rewrite);
  - (directory, scan) churn rows: 2.9 K–3.6 M per pair, 26.0 M in total, so the rollup costs per directory changed, not per file × depth;
  - forced splits: 0 (every churned directory already opens a version there), apart from the root, which has no row.

**Renames.** `a/x` → `a/y` (500 TB): `a`'s churn = 1 PB, net ≈ 0. It draws as one large neutral cell (color = net) and opens into `x` red (−500 TB) and `y` green (+500 TB).
- **Later phase, move labels.** Pair a removed and an added sibling with equal size, object count and content (crc32c where listed, as `dups` does) into one "moved x → y" cell. Not needed for correctness.

**As built (step 2, `_lib/diffLevel.ts`, `api/diff/level.ts`).**
- **Request.** `GET /api/diff/level?from=&to=&p=<dir>[&p=…]&k=&m=churn|net`: up to 64 directories per request, usually one depth's (the client sends a batch per level, asking only for the directories it doesn't already hold). Answers `{ from, to, k, m, levels }`, each level `{ path, a, b, oa, ob, churn, rows: [{p, k, s, a, b, oa, ob, churn}], other: {n, …} | null, unchanged: {a, b, oa, ob}, next? }`. `rows` are the `k` largest changed children by the measure (`churn` by default, `net` = |b − a|); `p` is the name under the directory.
- **Cache.** Each directory's answer is its own edge-cache entry, keyed by (store generation, D1, D2, directory, K, measure, `LEVEL_V`), never the canvas. A batch matches every directory first and reads only the misses, together. K is snapped to a power of two in [4, 512] (`levelK`) so nearby canvases share entries.
- **Reads.** One `path`-sort read per batch (`planChanged` / `readChanged` in `index.ts`): each directory's children rectangle and its own point, every tier, keeping only the groups whose version bounds allow a version opening or closing in `(D1, D2]`. Decoded groups go through the isolate's and the colo's group caches, so a sibling batch doesn't decode them again. The churn columns are projected only by this reader (`withChurn`), and its groups are cached apart from a plain read's (`ivGroupKey` / `groupColoKey` `c`), so other views don't fetch their bytes.
- **Churn per child, from the changed versions alone.** `c2 − c1`. `c1` is the D1 version's `churn_cum`, or, for a child absent at D1, its first version in the range's `churn_cum` less its size (it appeared there, all of it new). `c2` is the D2 version's `churn_cum`, or its last version's `churn_end`. So no lookup of an older version is needed, and D1 must be at or after the churn base (`IvStateDoc.churn`, from `scans.json`'s `churn` flag and scan versions; before it the request is a 409 and the client uses `/api/diff`). A run whose scans switch v1 → v2 isn't seen by the reader (the manifest doesn't carry versions).
- **Totals.** A changed directory's own row comes from the same read. The root (no row) and a directory one version spans read their totals as of each scan (`readRootAgg`). `unchanged` is the directory less its changed children, by subtraction.
- **Zero-churn directories are never asked** (the client knows from the parent's row).
- **Chains.** A directory whose only changed child is a directory is followed into it in the same request (`next`, its answer included and cached on its own), a round of reads per link.
- **Wide directories.** A directory whose changed children span more groups than the decoder takes (250 groups / 700K rows) is a 413. A batch that doesn't fit is split first, but one such directory still fails its whole batch (measured, §3b). §3b replaces this.
- **Refused (409):** a union view, a store or generation without churn, D1 before the churn base. A path-restricted link: a hidden directory is a 404 (nothing under a visible directory is hidden).
- **Tests** (`diffLevel.test.ts`, fixture `fixtures/iv-churn/`: a base over four scans, three appended runs merged by the carries, 4 rows per group): every (from, to, directory) answer from the churn base on equals one computed from the scans' files alone (60 answers, renames and same-size swaps among them); a batch equals its directories alone; a chain; K folding and both measures; the churn-base refusal; the range plan is a subset of the unpruned plan, strictly smaller on some.

### 3b. Bounded level reads (step 2b)

Decided 2026-11-02 (Ryan): cell values are exact, always — no approximate cells, no `[lo, lo + τ)` bands. Measure, then build, on dev only. Order: the page-level read first (the common case), then the dyadic churn index only where a directory is genuinely wide, with per-directory errors in the batch throughout.

**Before.** gcs's dev stack, 2026-11-02, generation `2026-10-09b.g3.c` (scans to 11-01), 18 diff-check cases × cold/warm, K = 32, expanding every directory whose churn-sized cell holds ≥ 4 cells of 48² px on 1664×845:

| | cold | warm |
|---|---|---|
| first level (one request) | 0.2–3.1 s | 0.1–0.5 s |
| fully expanded (5–8 levels, 1–210 dirs) | 0.6–57 s | 0.1–6.4 s |
| slowest single request | 21 s (8 dirs, 120 + 7 chained rounds of 60–110 groups) | 4.5 s |
| failures | 4 of 36: 413 for the whole 10–13-dir batch, from one flat directory (`…/8582115warcs/deduped_c075_decon`, 10-24 → 10-31: changed children over 90 groups, 710K rows) | |

**Cost model of step 2 (measured; corrects an earlier "children × versions" reading).** The served `path` sort stores each closed version in the smallest aligned block of scan indices holding its lifetime (`seg_sql`): with `.c`'s 121 scans, 238 closed segments plus the open one. That layout is built for as-of reads (one block per level, ~8 segments). A range diff instead overlaps every block intersecting `(D1, D2]` (~40 for a week), and in each it decodes the whole 8,192-row group(s) holding P's children, even when P has a handful of rows there. Measured offline from the groups sidecar, `marin-us-east5` (depth 2):

| range | rows needed (P's children's pieces in the range) | decoded | amplification |
|---|---|---|---|
| 10-24 → 10-31 | 1,123 (41 changed of 715 children) | 43 groups, 338,601 rows | ~300× |
| 10-30 → 10-31 | 42 | 9 groups, 73,728 rows | ~1,750× |

So the cost is whole-group decode × the number of time segments the range overlaps, not P's history. P's own rows are few and contiguous within each segment.

**Step 2b.1: page-level reads (first).** Write the served sorts with a parquet page index (column/offset index) and pages small enough that a directory's rows in one segment cost about one page, and have the reader decode only the pages whose `(depth, path)` bounds intersect the rect (ranged GETs per page, coalesced when adjacent). Exact by construction: the same rows, fewer bytes decoded.
- **Measure offline first**, on the east5 cases above and all 18 diff-check cases: per level request, rows decoded vs needed at the current 8,192-row groups and at candidate page sizes, plus the GETs each implies at ~6 concurrent connections. The page size comes from that table: the smallest one whose page-index and footer overhead stays below the decode it saves, not a cutoff tuned per case.
- **Measured (2026-11-02, gcs `path` sort, 804 level requests: the 18 diff-check cases' every level + one wide directory over a week).** A page here is a run of rows inside a row group with its own `(depth, path)` and `vf`/`vt` bounds; a page is kept by the same test the group is today (`planChanged`), plus overlap with the rect. Rows decoded per request, p50 / p99 (needed: 5 / 331):

  | unit | rows decoded p50 / p99 | total (804) | GET runs p50 / p99 |
  |---|---|---|---|
  | 8,192-row group (today) | 132,717 / 681,672 | 191.8M | 18 / 89 groups |
  | 1,024-row page | 9,216 / 47,930 | 15.7M | 9 / 44 |
  | 256-row page | 2,304 / 13,568 | 4.3M | 9 / 44 |
  | 128-row page | 1,152 / 7,936 | 2.4M | 9 / 45 |

  Decoded rows fall about linearly with page size; GET runs don't move (a request's kept pages sit in the ~44 interval segments a week overlaps, never adjacent across them), so the GET count is a layout lever (a closed version's segments laid out by range), separate from 2b.1. The wide directory needs 274,767 rows and decodes 734,354 today, ~280K at any page size: churn-bound, 2b.2's case. Chosen: 256-row pages (44× fewer rows decoded than today at 1/32 the group; 128 halves the rows again for 2× the page bounds). Harness: `page_sim.py` over a tier's sidecar + the served parquet (GCP Batch, n2-standard-16, ~25 s end to end).
- **How (2b.1).** The page bounds are parquet's own page index (ColumnIndex: per-page min/max; OffsetIndex: per-page byte range and first row), written by the cut (`write_page_index=True`), not copied into the `.groups` sidecar: at 32 pages a group that would make the sidecar (and D1's hot footers) 32× larger. The reader keeps planning groups from the sidecar as today, then reads only the kept groups' page-index entries (ranged GETs, adjacent ones coalesced), keeps the pages whose `(depth, path)` range meets the rect and whose `vf`/`vt` bounds pass `planChanged`, and decodes those (hyparquet's page-location reads). Pages are exactly 256 rows and aligned across columns: pyarrow has no row cap per page, so the writer flushes a page after each 256-value batch (`write_batch_size=256`, a 1-byte `data_page_size`). `path` is written without a dictionary: it is almost all distinct, so its dictionary page is about the whole column chunk, and a page read would fetch it anyway.
- **Re-cut, in parallel.** Adding a page index means re-cutting the sorts (no re-sort). The cut uses every core of the machine it rents: parallel per-range writers concatenated by row group into the one file (today each sort ends in one serial pyarrow writer: 1h30m on ~1.3 of 32 cores in the churn backfill). The Batch machine is sized from the measured RSS and core use, not taken by default. This lands with 2b.1, not as a follow-up.
- Then: re-run `level_check.py` (cold/warm, 18 cases) on dev and add an "after" column to the table above.

**2b.1 measured live (cw, paged generation `c003h`, 2026-11-03): slower than the walk.** Cold level requests (median of 3, Server-Timing total) vs the walk: day 02a 853 vs 429 ms; day rno2a 648 vs 765; day 02a/marin/grug 2939 vs 497; week 02a 2013 vs 1117; week rno2a 2116 vs 926; week grug 2430 vs 575. Rows decoded were small (3K–23K, ~25× fewer than whole groups) but each request read 12–77 groups by page. The cost is round trips, not decode: a paged read is three dependent waves (group metadata → page index → page data) where a whole-group read is two, each wave `⌈runs / RUN_READS⌉` sequential GET rounds (`planRuns` merges a group's spans within `RUN_GAP`, so runs ≈ groups ≈ the time segments the range touches, ~50 a week). Server-Timing now carries each wave (`pjson` / `pindex` / `pdata`: ms, `-gets`, `-bytes`).

**The bar.** A client's full expansion is depth × level requests (~3–5 waves of them in flight), so one cold level request must beat the walk by that factor, not match it: **p50 cold level ≤ walk / 3 on cw's six cases**, and the cost model predicts each case's GET rounds before it is measured. Cost(request) ≈ RTT · Σ_waves ⌈runs_w / RUN_READS⌉ + rows · c_decode, runs_w ≈ the separate byte regions the directory's rows occupy in that wave.

**Next, in order (no re-cut and no gcs prod change until each is measured):**
1. *Fewer waves.* The page index joins the group metadata's wave (fetched with it, or cached per group as the metadata is), so a paged read is two waves, as a whole-group read is.
2. *No barrier between waves.* Each group's page reads start when its index arrives, not after every group's.
3. *Layout, simulated offline first.* Served sorts key-major within B coarse time bands, `(band, depth, path, vf)`, groups allowed to cross segments (pages carry the `vf`/`vt` bounds that prune time): a directory's versions in a band are contiguous, so runs ≈ the bands the range touches instead of ~S segments, at the cost of decoding the bands' unchanged versions too. B ∈ {1, 2, 4, 8} through `page_sim.py` over the 804 level requests (rows decoded and runs per request), then the cost model's predicted cold time per cw case, before any re-cut.

If 1–3 can't reach the bar, that is said plainly: the client design (step 3) depends on it.

**Step 2b.2: wide directories (dyadic churn index, exact).** A directory with ~10⁵–10⁶ changed children in the range is still unbounded under 2b.1 (its rows themselves are that many). For those only:
- **Index.** Number the scan transitions `1..S`; a block at level `L` is `[b·2^L + 1, (b+1)·2^L]`. For every block at every level, and every directory `P`, one row `(L, b, P, child, churn, net)` per child of `P` with churn > 0 in the block (`churn` = Δ`churn_cum` across the block; `net` = Δsize; both additive over disjoint blocks), sorted `(L, b, depth(P), P, churn desc, child)`. A full segment tree (not the binary counter): an append writes level 0 plus each level whose block it completes; a compaction rewrites it (a fresh build's bytes); the backfill builds it from `churn_cum` at each scan boundary. ≤ E · (⌈log₂ S⌉ + 1) rows, E = changed paths summed over transitions. **Measure E and the real total on `.c` before building it.**
- **Read for P over `(D1, D2]`.** Decompose into m ≤ 2⌈log₂ S⌉ canonical blocks. P's churn C from two point reads. τ = C · minArea / area (the canvas's cell, sent with the batch). A child with range churn ≥ τ has ≥ τ/m in some block (pigeonhole), so each block's P run read in churn-desc order down to τ/m yields candidates X with |X| ≤ m · area/minArea, independent of P's width and history.
- **Exact values.** Each candidate's range churn and net from point reads (`churn_cum` and size at D1 and D2), now cheap page GETs under 2b.1. Cells are the candidates with exact churn ≥ τ; `other` = C − Σ cells, exact. Net mode uses the same candidates (churn ≥ |net|).
- **Which directories.** Not a tuned width: per directory, the reader computes both reads' costs from metadata it already has (2b.1's pages covering the rect vs the index's m runs + |X| point reads) and takes the cheaper. Both are exact, so the choice changes cost only.

**Per-directory errors (with 2b.1, or before it as a fix to step 2).** One directory's failure never fails its batch: the response carries `errors: {dir: message}` beside `levels`, and the client redraws that cell from `/api/diff`.

**Tests.** (i) 2b.1: over `fixtures/iv-churn` cut with small pages, every level response is byte-identical to step 2's, and the decoder's page counters show only rect pages decoded; (ii) the parallel cut's file is byte-identical to the serial cut's at the same page size; (iii) 2b.2: the dyadic decomposition and per-block sums equal per-transition churn sums for every child and range; every child with oracle churn ≥ τ is a cell, with exact values, and Σ cells + other = C; (iv) a 1M-child synthetic directory, all children changed across many scans, reads ≤ m runs and ≤ |X| + m index rows plus |X| point reads, whatever the range length; (v) a batch with one failing directory answers the others.

**Before any comparison with prod:** `.c` stops at its backfill's scans; later dailies append to g3 only. Append them to `.c` (`interval-store add -g 2026-10-09b.g3.c -C none`, so `CURRENT.json` doesn't move), or let the next compaction carry churn.

### 4. What goes away

The lazy descent's planner, ms and row budgets, `HandOff`, `DESCENT_V`, and `DIFF_DESCENT`. The as-of walk stays for `dm=max`, lens, owner pool, class scope and filtered (`q=`) diffs: those need unchanged children's sizes or matches, and their costs and readers are unchanged.

## Tests

- **Parity:** over the interval fixtures, the progressive map fully expanded equals the as-of walk's Δ rows (net sizing) at a matching floor, cell for cell; every directory's rows + `(other)` + `unchanged` sum to its totals.
- **Bound:** a no-change directory reads 0 data groups (footers only); a 1-changed-child directory in a 1M-child directory reads that child's groups only; a sweep of 1M changed equal-size files stops at the top-K stopping rule. Assert the decoded group counts.
- **Rename:** under gross sizing, `a/x` → `a/y` draws `a` with churn 2·size and expands into `x` removed and `y` added.

## Measurement before building

1. On gcs, cw, hf and r2: for each diff-check case, the level requests the progressive client would issue (count, depth), and each one's decoded groups and rows, computed offline from footers (`tiers plan`-style). That gives a projected first paint and full-map time per case, against the walk's measured wall and `cpuTime`.
2. Churn rows' size per scan on gcs (the busiest), to confirm O(changes × depth) in practice.

## Open questions

1. Area = net or gross (above).
2. K and the minimum cell area: from the canvas (e.g. K = cells that fit legibly in the parent's rectangle), or one constant.
3. Whether `level` replaces `/api/diff` for Δ mode or sits beside it until parity is proven.
