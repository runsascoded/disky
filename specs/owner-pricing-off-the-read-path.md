# Owner pricing off the read path

**Status:** proposal, nothing built. Filling or deleting any of these rows on gcs is a prod D1 write and needs Ryan's go.

## Problem

A scoped series (`o=` / `lens=`) needs each scan's owner assignments priced: `seriesRead` → `ownerAssignments` → `ownerTotals` → `compute`. `compute` tries `histAggs` (one row per prefix, `owner_prefix_hist`) and otherwise prices per scan with `readAsks`, storing the result in `owner_prefix_aggs`. Both are done inside the request.

Measured on dev.gcs, `g3.c`: a window of 16 unpriced scans used ~32 s of CPU and died as a 1102. The same windows, priced, used 1.0–2.7 s. `owner_prefix_aggs.updated_ts` for that generation lands on exactly the slow minutes (04:48–04:57, 07:36). Each scan is stored as it finishes, so retries make progress, which is why these 1102s clear on their own after enough attempts.

Windowing (`OWNER_HISTORY_BATCH`) bounds decode, not pricing, so it doesn't fix this.

## Why `owner_prefix_hist` is empty for current generations

Nothing in the pipeline writes it. Its fill is `fillOwnerHist`, reached only from the admin route `/api/owner-prices`, which was run by hand on 2026-10-14 for `2026-10-09b` (1,230 rows, last written that day). Rows are keyed by `indexTag`, which names the generation (`pq:iv:<gen>:s`), so `g3` and `g3.c` start empty. With more than `HIST_PRICE_MAX` (64) unpriced prefixes `histAggs` returns null and `compute` prices per scan.

## Cutover gate (confirmed from the code)

Both tables key on `indexTag`, so a new generation has no pricing. Moving `CURRENT` sends every scoped series back to first-time pricing for all of its scans, for real users. **No `CURRENT` move on a deployment with owner assignments until pricing is carried or prebuilt.** Every deployment's schema has these tables; whether a gate bites depends on whether it has ledger rows, which each deployment's session has to check.

**gcs hold (2026-11-05):** `job/interval-store.json` sets `"compact": "none"` (live on `:latest` since that day), so a due compaction can't move `CURRENT` until pricing is carried off the read path, or Ryan decides otherwise. Carries keep running, and runs accumulate meanwhile. This holds the gate, it doesn't make compaction safe.

## Options

The rule either way: **no read path prices.** A read answers from pricing with one bounded read, or returns an explicit not-ready answer. `HIST_PRICE_MAX` and the per-scan branch of `compute` go away.

| | A: `owner_prefix_hist` in D1, filled by the pipeline | B: pricing as a served file in the interval store |
|---|---|---|
| Written by | `fillOwnerHist` as a job step after each scan and ledger action | the store's append/carry/compaction, as `attribution` is |
| Read | one D1 query per generation, held by the isolate | one bounded range read over a `(path, vf)` sort |
| Cost of a scan | a row per prefix whose `hz` advances: O(prefixes) writes, not O(changes), unless `hz` moves generation-wide | O(changes): only prefixes whose totals changed get a version |
| New prefix | price over history in one read | the same read, in a job |
| Generation move | must re-fill first, or carry rows | built with the generation, so a move can't outrun it |
| Dev sharing prod's D1 | dev tests still write prod rows | no D1 writes |
| Work | small: wire the existing fill into the job | larger: a new sort, carried through every store stage |

**Recommendation: B**, with A as the stopgap if prod's errors need fixing first. B meets O(changes) and removes the generation-move hazard by construction, and it stops growth in D1: `owner_prefix_aggs` grows per (scan, generation), and each row is whole-ledger pricing for one scan.

## Open questions

- A new assignment is a ledger write: under B its prefix isn't priced until a job runs. The not-ready answer has to cover that gap, or the write has to queue the job.
- The `g3.c` rows my dev tests added to `owner_prefix_aggs`: keep or delete is Ryan's call.
- Prod's errors are not yet tied to this by route: a read-only error tail is collecting that.
