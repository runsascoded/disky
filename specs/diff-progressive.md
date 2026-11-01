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

Everything below works for either answer; §3 is what gross sizing needs.

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

### 3. Gross churn (if area = gross; open question 1)

To size and choose cells by churn without reading their unchanged children, add per-directory churn at scan time:
- **Churn rows.** For each scan s and each directory d whose subtree changed since the previous scan: `bytes_in`, `bytes_out` (and objects) under d. These are kept only where non-zero, so the count is O(changes × depth) per scan, consistent with "indexes grow O(changes)". They're written beside the path versions, by the same append/compaction, as a served sort keyed `(depth, path, scan)`.
- **Range churn.** For `(D1, D2]`, a directory's churn ≤ Σ its per-scan churn over the range (a file added then deleted inside the range counts twice: an upper bound, exact for one-scan ranges). The level request reads its children's churn rows in the range (contiguous by path) and sums them.
- **Renames.** `a/x` → `a/y` gives `a` churn = 2 × 500 TB (in and out), so `a` draws large and neutral; its level shows `x` red and `y` green.
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
