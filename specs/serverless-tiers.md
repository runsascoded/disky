# Serverless tiers: one cost rule for every view, beside VM mode

Status: spec only, 2026-11-12. No code, no deployment change. Written beside VM mode's benchmark (`specs/full-scan-serving-bench.md` on `w/full-scan-bench`), whose numbers are quoted below as the brief gave them; that spec's result tables are still empty. Builds on [fleet-index-research.md] (the output contract and cost-ledger vocabulary) rather than restating it. Numbers marked *estimate* are mine and unmeasured; thresholds marked *proposal* are mine, not Ryan's.

## 0. Summary

- Every view is a pure function of (S,E,D,Q,U,C,L,M) plus pinned revisions. Today the serverless path decides fast / slow / refused feature by feature: which axes are set, whether a term is in the catalog, whether a rollup carries owners. The result is 12 refusal codes ([indexedOnly.ts:19-31]) that track the index's shape, not the query's cost: a person lens with a rare term in a ten-file folder is refused, while a one-character term at the fleet root is answered.
- Proposal: **one rule.** Before reading data, the Worker computes a cost certificate from metadata it already holds or can read in one round: subtree row spans from the group index, match counts from the name index, overlay region counts from D1, the scans named, and M. The certificate prices each plan and picks a lane: **now**, **slow** (the Worker, longer budget), **VM** (hand to VM mode, waking it if needed) or **refuse**. The lane depends on the cost, never on which axes are set.
- The plan that removes the pockmarks is a **Worker scan**: VM mode's algorithm (one pass over D's rows, filters per row, rollup per child) run over the interval store's `path` sort through R2 range reads. Its cost depends only on how many rows D holds in the segments the scans touch, not on Q, U, C or L. So every query under a small enough folder is answered exactly, whatever its filters. Indexes then exist to move broad regions from VM or slow to now, and each is justified by the region it moves.
- Answers are exact under their pins, or not served. Nothing is cut by a timer: a plan that overruns its prediction moves to the next lane instead of returning a partial answer. Four kinds of inexact or timing-dependent output in today's responses go (§2.2).
- VM mode answers everything in the language within ~1.2 s at the gcs root once loaded. The Worker never depends on it being up: a query that needs it while it's asleep gets an explicit deferred answer with an ETA, never a wrong one.

## 1. The query, the pins, the overlay

The axes are those of the bench spec and [fleet-index-research.md] ("Query and output contract"): scans S (one, or S–E for a diff or series), folder D, name search Q (case-insensitive substring within a segment; AND, OR, NOT; anchors), owners U, storage classes C, staged/liveness L, result budget M (cells drawn, up to 10⁴–10⁵). This spec adopts that contract: own objects as the atomic measure, inherited directory coverage for name matches, exact `(other)` per parent, and the four temporal operations (stock, endpoint diff, size churn, membership churn).

**Pins.** A request resolves, before any data read, to:
- the interval store generation and newest manifest;
- the static name index generation and its run stack, where a plan uses it;
- the overlay head (below);
- the language version (search forms, the hex-run rule [static-hex-runs]) and the refinement-policy version (how M turns into kept cells: today's pixel area and attenuation, `filterThreshold`, `tileBudget`).

The cache key, the VM hand-off and the oracle all use the same pins.

**Overlay (decided).** Owner claims and staging stay live in D1 (`owner_prefixes` with `actions`, [ledger.ts:28-31]; `plan_items`, [schema.sql:462]; gcs's `claims`) and apply at query time as subtree ranges, as now. Because the overlay is append-only and timestamped, its head (`MAX(actions.id)`, `ledger.ts`'s `head`; the plans' newest change) is a sufficient pin: two tiers given the same head apply the same regions. Its size inside D, |overlay ∩ D|, is a certificate input: each region is a range of D's pre-order and costs a point read (its directory row) on any plan that sums rollups.

## 2. Today's map

### 2.1 Regions

"Fast" means one bounded read, typically under 1 s warm. Codes are [indexedOnly.ts:19-31]. Every deployment is `FILTER_INDEXED_ONLY=1` (the baseline in `AGENTS.md`), so the table describes that mode; §2.2 covers what the other mode leaves behind.

| Region (axes set; scope; frequency; time) | Today | Code reason |
|---|---|---|
| Q∅ U∅ C∅, one scan, any D | Fast; exact totals | Interval store `bysize` thresholded read, `path` for depth bands ([interval-store.md] §2.6; [view.ts:500-547]) |
| Q∅, a wide flat D (many children over the floor) | Fast but coarser: the floor is raised until the plan fits, so it draws fewer cells than M allows; `(other)` exact. 413 when even that doesn't fit | `coarsen` / `setFloor` ([view.ts:1466-1487]); `TooWide` ([index.ts:2296], `subtreeTooWide.test.ts`) |
| Q∅, diff S→E | Fast; exact | Two as-of reads at one floor, bounded lookups ([interval-store.md] §2.6) |
| Q∅, series | Fast for unscoped (over-time index); U/C/lens series chunked 16 scans a request | [series.ts:262-263], `SERIES_CHUNK` ([seriesLimits.ts:12]) |
| Q∅ + owner pool (`o=`) or person lens | Fast; exact bytes; ledger folded live | Slice sorts (`slices-bytotal`, `slices-bysize-user`) + `poolAgg` / `lensAgg` ([view.ts:808-809]; [interval-store.md] §2.6) |
| Q∅ + C | Fast; **bytes exact, object counts estimated** (scaled by byte share), not flagged in the response | `classRow` ([scope.ts:39-64]: "objects aren't tracked per class") |
| L (staged only) as a map filter | No route: staging shows as rings on the map and as `/staged` rows (≤ 1,000 prefixes per `/api/prefixes` call) | `site/src/Treemap.tsx:184,286`; [prefixes.ts:25] |
| Q light literal (suffix range ≤ V = 100K rows), any D, one scan | Fast; exact | `SuffixHits` ([staticFilter.ts:115,131-210]); ~0.3-0.7 s end to end ([ch-postmortem.md] §1) |
| Q heavy literal (catalog member, or 1-2 chars), D with ≤ R = 100K roots under it | Fast; exact | Drill roots ([staticDrill.ts:1-30]) |
| Q heavy literal, heavy D | Fast. Totals exact, but only the **K = 256** children with the largest peak bytes are named (the rest one "N others" cell, whatever M is), at most 48 nested reads 3 levels down, and `matchCount.n` is root rows over every scan, **an upper bound** | [static-name-search.md:333,354]; [view.ts:194-203]; `FILTER_NEST_READS`, `FILTER_SUBDIV_LEVELS` ([view.ts:2276,2300]) |
| Q heavy literal, deployment without `FILTER_STATIC_HEAVY` | Fleet root as bucket tiles; any folder below: `term-too-common` | `declined` ([staticFilter.ts:466-475]) |
| Q heavy literal + owner pool, heavy D | `owner-too-common` (rollups carry no owners) | [view.ts:1031-1041] |
| Q light literal + owner pool | Fast; exact (hits carry an owner slice) | `aggregateHits` ([view.ts:942-957]) |
| Any Q + person lens, or + C | `unsupported-scope`, whatever D's size | `rejectScope` ([indexedOnly.ts:124-126]); [subtree.ts:112], [diff.ts:104], [series.ts:237] |
| Compound Q (AND / OR / NOT) | Fast and exact within 24 reads / 400K hits; else `too-broad`. Also `too-broad` whenever every term of an AND group is heavy at D, a heavy term sits under a candidate, or a negative's rollup has a non-zero remainder, however small D is | [staticBool.ts:33-37,100] |
| Anchors | `^q` ≥ 2 characters, `q$` ≥ 3, else `anchor-too-short`; a `^q` past 400K rows is `term-too-common` | `ANCHOR_MIN` ([indexedOnly.ts:85]); `START_MAX_ROWS` ([staticAnchors.ts:65]) |
| Path terms (`a/b`) | Off by default: `unsupported-slash`. On: tail ≥ 2 characters (`slash-tail-short`) and a too-common tail refused | `pathTerms` ([indexedOnly.ts:65-70,95-104]); [search-future.md] |
| Regex, glob | Not in the language: `*`, `?` are literal text | `plain` syntax ([indexedOnly.ts:5-7]) |
| Q on a scan the static index doesn't cover yet (append lag; a heavy literal past the drill base) | `scan-not-indexed`, though the interval store holds the scan | [view.ts:1015,1030]; [map-before-search.md] |
| Q, diff | `diff-too-broad` when the two sides' unfolded hits pass 32 MB | `HELD_BYTES` ([shared.ts:540]); `tooBroadToDiff` ([view.ts:1024,1056,2594]) |
| Q, diff across a dirs-only and a full scan | `scan-dirs-only` | [indexedOnly.ts:49] |
| Q, any match root big enough to subdivide | Phase 2 has 1.5 s, 64 groups, 512K rows; past them roots are drawn whole (`interiors.skipped`), the answer is kept briefly and recomputed in the background (`budgetCut`) | `FILTER_PHASE2_*` ([view.ts:2285-2291]); [subtree.ts:217-220] |
| Q, match-root details | Past 300 ms or 16 groups, roots are drawn without kind, ages or classes (**a file reads as a folder**), `detailsLate` | `FILTER_DETAILS_MS` ([view.ts:2292-2296]); `DETAIL_GROUPS` ([view.ts:2633]) |
| Q containing hex-run text (cw) | Matches inside long hex runs aren't counted; the response carries `hexRuns` | `hexNote` ([staticFilter.ts:457-463]); [static-hex-runs] |
| Q on a path-restricted link | Refused (its sums would count hidden matches) | `restrictedSearchRefused` ([series.ts:235]); `AGENTS.md` "A path-restricted link" |

### 2.2 Inexact or timing-dependent outputs

These are in today's answers, mostly where U, C or L were applied on top of machinery built for S, E, D and Q:
1. **Class scope object counts.** `classRow` scales `n_files` by the byte share ([scope.ts:58-64]); bytes are exact, counts are estimates, and the response doesn't say so.
2. **Heavy-literal `matchCount.n`.** Root rows over every scan: an upper bound on this scan's ([view.ts:194-203]).
3. **Timing-dependent shape.** Phase 2's 1.5 s budget, the details' 300 ms and the search's time limit make the same request draw different trees depending on load (`interiors.late`, `detailsLate`, `searchLate`; [view.ts:207-232]). Totals stay exact, but the answer isn't a function of its inputs, and `detailsLate` mislabels files as folders.
4. **Approximate fallbacks**, live in code for any deployment without `FILTER_INDEXED_ONLY` ([view.ts:244-251,1116-1131,1192-1197,1597-1598]). They were measured to miss matches above their own stated threshold (`specs/filter-no-index-misses.md`, untracked in the root clone, on m3), and over the interval store's ended versions a filtered scope view can go empty ([lean-scans.md] "Note"). Every deployment runs indexed-only, so these are dead weight.

The K = 256 cap and the raised floor are exact (their `(other)` cells hold the rest), but they spend less than M for reasons of the index's shape, not the request's.

### 2.3 Why it reads as pockmarks

Each refusal is a statement about one structure ("rollups carry no owners", "hits carry no class", "two heavy rollups can't be intersected"), checked in the order the reader happens to consult them. Cost isn't in the decision, so:
- A cheap query with an unindexed axis is refused: a person lens with any term, in a folder of ten files.
- An expensive query on an indexed axis is answered: a 1-character term at the fleet root, from the catalog.
- Adding a selective filter can move a query from fast to refused: `o=owned` on a heavy term.
- Narrowing D doesn't always help: `unsupported-scope` holds at every depth.

## 3. The principle

### 3.1 Lanes

| Lane | Meaning | Bound (*proposal*) |
|---|---|---|
| **now** | The Worker answers in the request | predicted cold time ≤ T_now = 1.5 s, working set ≤ the isolate's budget |
| **slow** | The Worker answers, with a visible "computing" state; the result is cached under its pins | predicted ≤ T_slow = 15 s and within the Worker's CPU limit (§7 Q4) |
| **VM** | Forwarded to VM mode; if it's asleep, a deferred answer with an ETA, then the result | predicted VM time ≤ T_vm = 5 s once loaded |
| **refuse** | Not answerable: outside the language, forbidden, or over every lane's bound | — |

The refusals left are:
- **Language:** regex while it isn't in the language version; path terms while `SEARCH_PATH_TERMS` is off.
- **Security:** a path-restricted link with a name filter.
- **M** over the policy's maximum.
- **No lane fits:** the VM can't be woken, or the query is over T_vm. The message names the narrowing that fits: the certificate knows which children of D come under T_slow, so "open one of these folders" is computed, not canned.

`scan-not-indexed`, `owner-too-common`, `unsupported-scope`, `term-too-common`, `too-broad` and `diff-too-broad` all go: each becomes the lane its cost picks.

### 3.2 The certificate

Inputs, all metadata (the Worker holds most of it per isolate or in the colo cache already):
- **n(D, T):** rows under D in the segments the request's scans touch (T: the open segment plus ⌈log2 n⌉+1 dyadic blocks per historical scan, [interval-store.md] §2.4), from the `.groups.parquet` group index's depth/path bounds and row spans. Planning already computes it (`planRects`, `held(plan)`, `readFits`; [view.ts:464-547]). It's an upper bound in whole row groups, which is what decoding costs anyway.
- **m_t(D):** per search term, its answer's rows under D. A light literal's suffix range is bounded by the shard group index before it's read (≤ V). A heavy one has the drill header's root rows under D (exact per directory, [static-name-search.md:332]) or the catalog's count at the root. A compound query bounds AND by its smallest term and OR by the sum.
- **|ov ∩ D|:** overlay regions inside D (a D1 range count on `prefix`), for U (claims) and L (plan items).
- **s:** scans named: 1 for a view, 2 for a diff, k for a series.
- **M**, and the refinement policy.

Plans, each with a predicted time `T_p = setup + waves × rtt + groups × c_group + rows × c_row(filters)` (constants fitted from `Server-Timing`, §6 phase 0):

| Plan | When it applies | Cost driver |
|---|---|---|
| **Rollup read** (today's map) | Q∅; U and C via slices; L as overlay regions | ~M rows per level read, plus |ov ∩ D| point reads |
| **Postings** (static name index) | Q of literals, U∅ or a pool, C∅ | m_t(D) rows |
| **Iceberg cells** (drill rollups, faceted, §4) | Q heavy at D, with U/C if the cells carry them | ~M cells per level |
| **Worker scan** | Anything in the language | n(D, T) rows; filters change c_row only |
| **VM** | Anything in the language | VM model: n(D) × per-row cost from the bench (§5.3), plus wake if asleep |

**Lane = the cheapest plan's lane:** now if some plan is under T_now, else slow if some Worker plan is under T_slow, else VM, else refuse. The rule is one comparison over prices. No branch names an axis.

**Monotone by construction.** The Worker scan's price depends only on n(D, T), so narrowing D never makes a query slower, and an added filter never moves it to a worse lane. Pockmarks come from plans whose applicability depends on axes. A plan that covers every axis, priced by size alone, puts a floor under all of them.

### 3.3 Worker scan envelope (*estimate*)

Calibration points:
- A Worker decodes ~60 ms per 32K-row group ([view.ts:2278-2279]): ~0.5M rows/s.
- ≤ 100K suffix rows decode in ≤ 0.5 s ([static-name-search.md:238]).
- cw's unfiltered interval-store views read up to 47 groups / 376K rows ([view.ts:2283]).

So, cold, with six GETs in flight:
- **now:** n ≲ 3 × 10⁵ rows (≈ 40 groups: 6-7 waves, plus ~0.6 s decode).
- **slow:** n ≲ 5 × 10⁶ rows per isolate (~10 s decode), more if a slow query may fan out over service-bound Workers (§7 Q5).

Memory is O(window nodes + one group), since groups stream and are dropped. Q matches per row on the path string (the inherited coverage needs no stack, because the row carries its full path). U applies slices and overlay regions per row; C uses class columns; L tests rows against plan-item ranges. A diff is the same pass with two liveness tests, and a series one pass with a difference array over scans.

What's under these bounds is an empirical question. Phase 0 histograms n(D) by depth per deployment from directory rows.

### 3.4 Exactness and determinism

- An answer is exact under its pins. The only "less than everything" is the policy's own: kept cells, exact `(other)` remainders, and an explicit `coarser: {asked: M, drawn}` when the cheapest plan in the chosen lane draws fewer cells than M allows (today's raised floor, the K cap until §4 lifts it).
- A plan is chosen up front and runs to completion in its lane. If it overruns (a misprediction), the request escalates (now → slow → VM); it never truncates. Mispredictions are logged with predicted and actual costs; they recalibrate the constants, and a plan whose bound is broken isn't offered for that region until fixed.
- `budgetCut`, `interiors.late`, `searchLate` and `detailsLate` go. Detail lookups are part of the plan's price.
- `classRow`'s estimated counts become exact (per-class counts, §4) or are flagged `counts: "estimated"` until then.
- `matchCount.n` is exact (from the roots read or the drill header for this scan) or absent.
- The `APPROX_*` fallbacks are deleted along with the non-indexed-only mode.

### 3.5 Relation to the research spec

[fleet-index-research.md] proposes the same shape: "the recommended hybrid chooses A or B only when its work certificate predicts a win over C". It leaves where C runs and the lanes open. This spec answers both:
- C runs in two places, by size: the Worker scan for small D, VM mode for large.
- Candidates A (joint selected-presence discovery) and B (complete heavy cells) are the precomputation options of §4, each admitted only for the region whose lane it moves.

Its experiments (eligibility census, physical-layout crossover) stay its own. This spec's phase 0 measures the lanes, not index layouts.

## 4. Which precomputation buys which region

Deployment sizes, from what's measured:
- **gcs:** 967M packed rows over 135 scans (VM load). The interval store's `path` sort: 1.34B rows, 25.2 GB. Static index: 150.6 GB at 70 scans ([ch-postmortem.md] §4.1).
- **cw:** ~92M objects; rows with directories and history *estimate* 1.2-2 × that.
- **r2 demo:** 20M suffix rows at 25 scans ([static-name-search.md:119]); *estimate* ~10⁶ path rows.
- **hf:** not measured here; *estimate* ≤ 10⁷ rows.

Every storage and build figure below is an *estimate* unless it cites a source.

| Structure | Exists? | Moves which region, to which lane | gcs storage / build | Smaller deployments |
|---|---|---|---|---|
| **Directory rollups** (interval store dirs, owner slices, class bytes) | Yes | Q∅ with any U, C, L-as-overlay, any D: **now** | In place (`path` 25.2 GB; `bysize` similar; slices) | In place |
| **Per-class object counts** on directory versions (`c2n..c4n`) | No | Q∅ + C: estimated counts become exact | +1-3 GB (3 small int columns on directory versions); a backfill like `backfill-churn`, hours on Batch | Proportional; minutes |
| **Persistence** (vf/vt, dyadic segments) | Yes | Any S, diffs, series, at ≤ ⌈log2 n⌉+1 blocks per scan | +14% on the sorts ([interval-store.md] §2.4) | Same |
| **Series from a path's versions** | Data yes, read not wired ([interval-store.md] §2.6) | Q∅ series under any D: one point read instead of one per scan | None | None |
| **Light postings** (suffix shards) | Yes | Q light at any D, U∅ or pool: **now**; m_t(D) for the certificate | 150.6 GB at 70 scans | r2: built ([static-name-search.md:119]) |
| **Root counts per (term, directory)** (drill headers) | Yes, for heavy (term, dir) | The certificate's m_t(D) for heavy terms | In place | In place |
| **Iceberg cells, faceted:** the drill's rollups (heavy term × heavy dir × child), kept by share (child ≥ 1/M_max of the dir on some scan) instead of top-K = 256, each cell split by owner and class | Partly: rollups exist, K-capped, no owners or classes | Q heavy at broad D with a pool, a lens or C (today `owner-too-common` / `unsupported-scope`): VM → **now**. Lifts the K cap (§2.1) | Today's rollups × (owners per cell, ~1.5-3) × (kept set ≤ min(children, M_max)/256). The drill is already the costliest stage (m3: 3,143 s, 88 GiB spill, [static-name-search.md:119]); facets multiply it, est. ×2-4 | Not needed: the Worker scan or VM covers them |
| **Owner × term cells for compound queries** (Candidate B's complete heavy predicates) | No | Compound Q with ≥ 2 heavy terms at broad D (`too-broad`): VM → now | Combinatorial; admit per measured demand only | Not needed |
| **2D counting over (suffix-array rank × pre-order position)**, wavelet tree | No | m(Q, D) for any literal at any D in O(log n), and outermost-root enumeration by range successor (Candidate A) | Counts only: ~2 × 10¹⁰ points × ~30 bits, est. 70-100 GB. Byte sums per level: TB, infeasible. Pre-order ranks shift per append, so a rebuild per generation or per-run coordinates ([fleet-index-research.md] "Original keys…") | cw est. 8-10 GB; r2 ~100 MB; but the Worker scan already covers these |
| **VM packed store** (RAM) | Bench only | Everything broad: Q∅ at no cost, Q/U/C/L at the root ≤ 1.2 s | RAM est. 30-40 GB (24 B/row × 967M + directory versions + a ~2.5 GB name dictionary). Load: 24 GB copied in 30 s (bench); re-sort and pack from the bench's `phases.jsonl` | cw est. 3-5 GB; r2/hf < 1 GB |

**Why the 2D structure isn't the answer at gcs scale.** A name match covers a subtree, and nested matches (a hit under a hit) must count once. A plain range sum over occurrence points double-counts them, as the research spec notes for its Candidate A ("Minimum is idempotent…summing their weights would still double-count"). Exact sums need outermost-root enumeration, whose cost is the number of roots R_selected, unbounded by M in the fragmented case. That is a cost the certificate prices, not one the structure removes. The one easy case is a term whose hits never nest (`.npy$` leaves), where sums are direct; a per-(term, dir) "no nested hits" bit in the drill header would let the certificate use it. Admit the structure only if phase 0 shows a broad, frequent region that neither the iceberg cells nor VM mode serve.

**Smaller deployments.** At ~10⁶ rows (r2) the whole fleet root is a Worker scan in the slow lane, and most folders are in now. These deployments need no VM and no new index. cw (~92M) is between: Q∅ is now everywhere, a filtered root is VM (~180 s single-isolate scan, *estimate*), and a VM there would be small (3-5 GB).

## 5. VM mode hand-off contract

### 5.1 Routing

The Worker owns every decision. The VM is a plan with a price, never a front door.
1. The Worker resolves the pins (§1) and computes the certificate.
2. If the lane is VM, it checks the VM's state (§5.2).
   - **Hot**, with the pinned generation loaded: forward the request.
   - **Asleep, or on another generation:** answer 202 `{lane: "vm", state, eta}`, start a wake, and let the client re-ask the same key.
3. The VM's answer comes back to the Worker. The Worker checks its echoed pins equal the request's, caches it under the pinned key (immutable, so colo + KV like today's [edgeCache.ts]), and returns it.

The request envelope is the normalized query (AST, axes, M, policy and language versions), the pins, and a deadline. The VM never reads D1. The Worker resolves the overlay at its head into the list of regions inside D and sends them; that keeps the overlay's live semantics in one place.

The channel is authenticated both ways:
- Worker → VM: a signed request (HMAC under a secret only the two hold), over a Cloudflare Tunnel or HTTPS with an allowlist.
- VM → Worker: the VM signs its answer and its pins.

Path-restricted links never reach the VM. Their refusal happens before routing.

### 5.2 Wake-up and states

| VM state | Who moves it | Answer |
|---|---|---|
| **hot**: loaded, the pinned generation resident | — | forward |
| **loading**: a new generation or a cold start | the VM itself after an append; the Worker's wake | 202 + ETA from the load's measured phases |
| **stopped**: VM off, disk image holds the packed store | the Worker's wake call: Compute `instances.start` through a small waker with a scoped service account, idempotent | 202 + ETA (boot + map the store) |
| **gone**: preempted, or no capacity | the waker retries; spot falls back to on-demand per policy (§7 Q3) | 202 while retrying; after the wake deadline, "unavailable" with the narrowing that fits the Worker |

The VM publishes `{state, generation, manifest, loaded_at}` to a small status object, R2 or D1 (read-only for the Worker). It loads each new generation and manifest as the append lands, and serves the old one until the new one is resident. Each request's pins say which it needs. The bench's first cut reloads the whole store; incremental appends are later work in the bench spec.

### 5.3 Timeouts and costs

- **Per query:** the VM gets min(T_vm, the client's patience); past it the Worker returns "unavailable", not partial.
- **VM price** (bench, 16 vCPU, gcs): a plain root view 0.45 s, root searches ~1.2 s, owner/class/staged filters 0.55-0.8 s, a depth-3 drill ~0.15 s. The model scales by n(D) / 967M and by filter class. Phase 0 fits it from the bench's jsonl.
- **Wake deadline** (*proposal*): 10 minutes. Wakes are rate-limited per deployment and logged with who asked (§7 Q3).
- **Preemption mid-query:** one retry after the VM is back, then "unavailable". Nothing VM-computed is cached unless its echoed pins match.

### 5.4 Identical answers, checked by an oracle

The bench already has the oracle: DuckDB SQL over the same store with no shared code (`bench/full-scan/oracle/oracle.py`), exact on nodes, bytes and counts. The contract adds three things.
1. **One policy and language spec, three implementations.** The Worker plans, the VM scanner and the DuckDB oracle all implement the same versioned semantics:
   - the search language, including the hex-run rule where a deployment has it;
   - inherited coverage;
   - owner and class slice rules (the bench's: a file counts when it holds a selected slice);
   - the overlay's region semantics;
   - the refinement policy.

   Today they differ in one known place: the Worker keeps cells by pixel area with attenuation, the bench by `min_ppm` share within a depth window. One must be adopted by both before parity means anything (§7 Q1).
2. **CI parity on fixtures.** For the repo's interval-store fixtures (`gen.py` generations), every query in a generated grid over (S,E,D,Q,U,C,L,M) is answered by each Worker plan that applies, the Worker scan, the VM scanner and the oracle, and all must be equal. A plan that disagrees fails the build.
3. **Shadow parity on real data.** On a deployment with a hot VM, a sample of answers is re-asked on the other tier under the same pins and compared: now-lane answers to the VM, VM answers to the Worker scan where n(D) allows. That covers the static index plans against VM, which the fixtures can't size. A mismatch is an alert with both answers stored, and the plan that disagreed with the oracle stops being offered for that region until fixed.

## 6. Phasing

Each phase is its own go.
0. **Measure, no serving change.**
   - Fill the bench spec's result tables.
   - A certificate simulator over logged requests (warm-cache views, the probe's, `latency-observability` spans): per request, every plan's predicted cost against its measured `Server-Timing`, to fit the constants and T_now / T_slow.
   - Per deployment, the n(D) histogram by depth (what's under each lane's bound).
   - The rate of each refusal code in real traffic.
1. **Exactness, no new structures.**
   - `classRow` counts flagged.
   - `matchCount.n` exact or absent.
   - The `APPROX_*` paths and the non-indexed-only filter mode deleted.
   - `detailsLate` priced into the plan instead of cut.
   - `coarser` made explicit.
2. **The certificate and lanes in the Worker.** One planning function returns `{lane, plan, predicted}`; the refusal codes it replaces go; responses and `Server-Timing` carry the lane and the prediction. Behind a flag, dev stack first; a `diff-bench`-style table before prod ([AGENTS.md] "diff-bench"; the memory's benchmark-before-cutover rule applies to serving changes).
3. **The Worker scan plan.** Exact for any axes under D within the slow bound. This retires `unsupported-scope`, `owner-too-common`, `too-broad`, `diff-too-broad` and `scan-not-indexed` for small D, on every deployment. It is parity-tested against the oracle on fixtures.
4. **VM mode serving.** The box, the waker, the status object, the signed channel, shadow parity. gcs first; cw if phase 0 shows demand.
5. **Precomputation by measured demand.** Per-class counts; faceted, share-kept drill cells; the series read from versions. The 2D structure only if phase 0 finds a region nothing else serves.

## 7. Open questions for Ryan

1. **One refinement policy** for both tiers: the Worker's pixel area with attenuation, or the bench's `min_ppm` share and depth window? Parity needs one.
2. **Lane bounds:** T_now = 1.5 s, T_slow = 15 s and T_vm = 5 s are my proposals. And what does "slow" look like: hold the request with a spinner, or 202 and poll?
3. **Waking the VM:** who may (any viewer, staff, admins), a daily wake or cost budget, always-on hours, and spot with an on-demand fallback or spot only?
4. **Pages Functions' CPU limit** for the slow lane (the Workers Standard usage model's is configurable; Pages may differ). If it's 30 s, T_slow fits; if lower, the slow lane needs a dedicated Worker.
5. **Fan-out:** may a slow query split D's rows over service-bound Workers (each its own isolate and CPU)? That would move cw's filtered root from VM to slow.
6. **Overlay time semantics:** "live, at query time" for a historical scan S means today's claims and plan items applied to S's tree. Is that the intent for history and diffs too, or should a historical view apply the overlay as of S's timestamp (possible, since it's append-only and timestamped)?
7. **L as a map axis:** a staged-only view (the map of what's staged under D, at a plan revision) has no route today. Is it wanted as a first-class view, or are rings plus `/staged` enough?
8. **Language growth:** with lanes, path terms, short anchors and `x/` stop being refusals and become priced queries (a too-common tail goes VM). Un-park them in the language version, or keep [search-future.md]'s order? Regex: VM-only in a later language version, or a non-goal still?
9. **cw:** VM mode there too, or only fan-out and indexes?
10. **The K = 256 cap:** lift it by share, so heavy-term rollups spend M, at the drill's build cost (§4)?

[fleet-index-research.md]: fleet-index-research.md
[interval-store.md]: interval-store.md
[lean-scans.md]: lean-scans.md
[search-future.md]: search-future.md
[map-before-search.md]: map-before-search.md
[ch-postmortem.md]: done/ch-postmortem.md
[static-hex-runs]: done/static-hex-runs.md
[static-name-search.md:119]: architecture/static-name-search.md#L119
[static-name-search.md:238]: architecture/static-name-search.md#L238
[static-name-search.md:332]: architecture/static-name-search.md#L332
[static-name-search.md:333,354]: architecture/static-name-search.md#L333
[AGENTS.md]: ../AGENTS.md
[indexedOnly.ts:5-7]: ../site/functions/_lib/indexedOnly.ts#L5-L7
[indexedOnly.ts:19-31]: ../site/functions/_lib/indexedOnly.ts#L19-L31
[indexedOnly.ts:49]: ../site/functions/_lib/indexedOnly.ts#L49
[indexedOnly.ts:65-70,95-104]: ../site/functions/_lib/indexedOnly.ts#L65-L104
[indexedOnly.ts:85]: ../site/functions/_lib/indexedOnly.ts#L85
[indexedOnly.ts:124-126]: ../site/functions/_lib/indexedOnly.ts#L124-L126
[view.ts:194-203]: ../site/functions/_lib/view.ts#L194-L203
[view.ts:207-232]: ../site/functions/_lib/view.ts#L207-L232
[view.ts:244-251,1116-1131,1192-1197,1597-1598]: ../site/functions/_lib/view.ts#L244-L251
[view.ts:464-547]: ../site/functions/_lib/view.ts#L464-L547
[view.ts:500-547]: ../site/functions/_lib/view.ts#L500-L547
[view.ts:808-809]: ../site/functions/_lib/view.ts#L808-L809
[view.ts:942-957]: ../site/functions/_lib/view.ts#L942-L957
[view.ts:1015,1030]: ../site/functions/_lib/view.ts#L1015-L1030
[view.ts:1024,1056,2594]: ../site/functions/_lib/view.ts#L1024
[view.ts:1031-1041]: ../site/functions/_lib/view.ts#L1031-L1041
[view.ts:1466-1487]: ../site/functions/_lib/view.ts#L1466-L1487
[view.ts:2276,2300]: ../site/functions/_lib/view.ts#L2276-L2300
[view.ts:2278-2279]: ../site/functions/_lib/view.ts#L2278-L2279
[view.ts:2283]: ../site/functions/_lib/view.ts#L2283
[view.ts:2285-2291]: ../site/functions/_lib/view.ts#L2285-L2291
[view.ts:2292-2296]: ../site/functions/_lib/view.ts#L2292-L2296
[view.ts:2633]: ../site/functions/_lib/view.ts#L2633
[index.ts:2296]: ../site/functions/_lib/index.ts#L2296
[staticFilter.ts:115,131-210]: ../site/functions/_lib/staticFilter.ts#L115-L210
[staticFilter.ts:457-463]: ../site/functions/_lib/staticFilter.ts#L457-L463
[staticFilter.ts:466-475]: ../site/functions/_lib/staticFilter.ts#L466-L475
[staticDrill.ts:1-30]: ../site/functions/_lib/staticDrill.ts#L1-L30
[staticBool.ts:33-37,100]: ../site/functions/_lib/staticBool.ts#L33-L100
[staticAnchors.ts:65]: ../site/functions/_lib/staticAnchors.ts#L65
[shared.ts:540]: ../site/functions/_lib/shared.ts#L540
[scope.ts:39-64]: ../site/functions/_lib/scope.ts#L39-L64
[scope.ts:58-64]: ../site/functions/_lib/scope.ts#L58-L64
[ledger.ts:28-31]: ../site/functions/_lib/ledger.ts#L28-L31
[edgeCache.ts]: ../site/functions/_lib/edgeCache.ts
[prefixes.ts:25]: ../site/functions/_lib/prefixes.ts#L25
[seriesLimits.ts:12]: ../site/functions/_lib/seriesLimits.ts#L12
[subtree.ts:112]: ../site/functions/api/subtree.ts#L112
[subtree.ts:217-220]: ../site/functions/api/subtree.ts#L217-L220
[diff.ts:104]: ../site/functions/api/diff.ts#L104
[series.ts:235]: ../site/functions/api/series.ts#L235
[series.ts:237]: ../site/functions/api/series.ts#L237
[series.ts:262-263]: ../site/functions/api/series.ts#L262-L263
[schema.sql:462]: ../site/schema.sql#L462
