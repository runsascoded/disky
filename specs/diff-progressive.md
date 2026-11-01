# Progressive diffs: one directory level per request, bounded by construction

**Status:** proposal, 2026-11-01, for review before anything is built. Shared (`[cloud]`). Replaces the lazy descent's planner and budgets (`specs/diff-from-changes.md` revision 3: `DESCENT_MAX_EXPANDABLE`, `DESCENT_BUDGET_MS`, `DESCENT_BUDGET_ROWS`, `HandOff`), which were tuned per deployment rather than derived.

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

**Wide directories (a sweep of 10⁶ changed children).** This is the churn analogue of a plain treemap folder with 10³+ similar-size children, which `bysize` solves because size is a stored per-row value. Range churn is a difference, not a stored value, but `churn_cum`(D2) is an **upper bound** on it (cumulative churn is non-negative and monotone). So a **`bychurn` sort** (P's children in `churn_cum` descending order, like `bysize`) supports an exact top-K with a stopping rule. Traverse the after side in that order; a row live since before D1 (`vf ≤ D1`) has range churn 0 with no lookup; otherwise look up its D1 version for `churn_cum`(D1) (Ryan's traversal). Stop once the next row's `churn_cum`(D2) is below the K-th range churn found or the floor: nothing unread can qualify. No zipped traversal of the before side is needed, because the after side's value already bounds the range churn. The bound is loose for children with a busy history and a quiet range (their cumulative churn is large, their range churn 0): they're read, but cost no lookup. Measure on gcs's sweeps and on long ranges before deciding whether it needs tightening (e.g. a per-version `churn_step`, which makes one-scan ranges exact).

**Renames.** `a/x` → `a/y` (500 TB): `a`'s churn = 1 PB, net ≈ 0. It draws as one large neutral cell (color = net) and opens into `x` red (−500 TB) and `y` green (+500 TB).
- **Later phase, move labels.** Pair a removed and an added sibling with equal size, object count and content (crc32c where listed, as `dups` does) into one "moved x → y" cell. Not needed for correctness.

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
