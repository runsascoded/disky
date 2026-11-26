# Static names: catching up a backlog (partial compaction) and per-pair counts in the generation

Status: design (2026-11-26, the cw session; Ryan's go on both, as follow-ups to the two memory fixes below). Builds on specs/static-geometric-compaction.md (size-priced folds) and specs/anchors-near-bound.md (the near bound).

## Why

cw's object count went 91M → 237M between 11-01 and 11-25, nearly all one new prefix (`marin-us-east-08a/gov_reports/`: ~96M small `.json` / `_figure.jpg` objects since ~11-12, still ~6M/day). Two things followed:

- **Compaction stopped.** Generation `2026-10-15cw.c003` was last folded at 11-14T0604. Its level-5 fold came due at 11-22 and every attempt since (12 h apart, ~2.5 h each on an n2-highmem-48) ran DuckDB out of 251 GiB in `static_measure.measure_kind`, so 47 scans (6 runs, 3.3B rows, 51 GB) piled up on c003. Nobody saw: the scan submits the fold without waiting, and nothing alerted or recorded it.
- **Every scan slowed.** `anchors end` re-reads every prior version under each near `(k, dir)`: 15–100M rows a scan through 11-16, 230–750M since 11-17 (keys `.json`, `s.json`, `a.json`, `_figure.jpg`, `.txt`). Its per-key window (`prior_dir_counts`' `pvc`) spilled up to 221 GB on the 128 GB anchors machine; the static-names step went from 20–40 min to 100–155 min a scan, the scan from ~1.7 h to 3–4.5 h.

Done already (cw `4f64e954d`, `6db874103`, `dac5d035c`; cw config `cce20256d`, `4706631d1`):

1. `prior_dir_counts`: one sorted pass instead of the window (same `pd`; 25M versions at a 2 GB limit: 35 s vs 60 s).
2. `measure_kind`: joined and written per `q` range of ~50M input rows (same bytes), so the fold's measure is bounded.
3. Compactions record their runs on /scans (`record_secrets`) and alert on failure (`events_topic` → `batch-events serve`).

This spec is the two follow-ups.

## A. Partial compaction: catch up in increments

A fold today is all-or-nothing: base + **every** run → the next generation. After a backlog, that one job is the biggest the store has ever needed, and if it can't run, nothing improves; each retry costs the whole attempt.

**Partial fold:** base + the oldest k runs → generation `G'`, whose scans end at run k's last scan; runs k+1… carry over to `G'` unchanged as its runs. Repeat until caught up. Like merging upstream into a long-lived branch a few commits at a time: each step is small, and one that fails doesn't block the next.

Why it's sound: a run's files depend on the tiers before it only through what they *say* (the prior stacks' headers and counts, `stack_rows`; the near bound's prior rows), not where they sit. Base ⊕ runs 1..k as one tier says the same as base, run 1, …, run k as separate tiers: the fold is content-preserving (`test_compacted_generation_is_the_rebuild`, md5 for md5). So runs k+1… read over `G'` answer exactly as over `G` + runs 1..k.

Shape:

- `compact --runs N` (`-K` is taken: compact now): fold the base and the newest manifest's first `N` runs (oldest first; each whole: a run is the unit). `G'`'s `scans.json` ends at the last folded scan; its manifest lists the carried runs, each still keyed `deltas/<first>_<last>`.
- Carried runs: copy their `deltas/<key>/` into `G'` (cw's 6 runs: 51 GB within one bucket, a server-side rewrite), or reference them in `G'`'s manifest by `G`'s path. Copying keeps a generation self-contained (`generation-gc` can delete `G` whole, readers resolve one prefix); referencing is free but makes `G` live until every run it holds is folded. Start with copying.
- Policy: `compaction_why` sizes the fold (specs/static-geometric-compaction.md §2): fold the longest prefix of runs whose rows fit the job's budget; a backlog becomes several folds, oldest first, each publishing `successor.json` / `CURRENT.json` as now.
- A run that alone exceeds the budget: fold it on its own (k = 1). If even that fails, the bound is wrong, not the policy: that's an alert, not a retry loop.

Checks: partial fold of k then the rest equals one full fold, md5 for md5 (the existing fixture flavors); readers over `G'` + carried runs return what `G` + all runs returned (`static-reader-rich-parity`).

## B. Per-pair counts in the generation: anchors cost O(changes), not O(history)

`run_rollups` decides which `(k, dir)` a run makes heavy (rows > R = 100,000) by counting each near pair's prior rows *exactly*, re-reading every prior version under it from every tier, every scan. A pair near the threshold that never crosses it (most of them) is recounted from history each time: cw reads ~650M prior rows a scan to settle ~1,500 keys' pairs.

Instead the generation keeps the count: `anchors/pd-<mode>.parquet` (`k, lvl, dir, n`: distinct prior versions under each pair the anchors have counted). A run computes its pairs' counts as stored count + the run's own new versions (exactly what `prior_dir_counts` would have summed), writes the updated rows into its own `anchors/pd-<mode>` delta, and probes history only for pairs with no stored count yet (new pairs). Carries and folds merge the deltas like any other per-pair table (last writer per pair, by tier).

Open questions (to settle before building):

- Distinctness across tiers: a version listed in two tiers counts once (`count(DISTINCT (usr, vf))` today). A stored count is per pair; a run adding a version the base already has for that path must not double-count. The run knows its versions' `(path, usr, vf)`; is "new to the stack" (the drill's `fresh` notion) available per pair at anchors time?
- Size: how many pairs would be stored (all near pairs ever, or only those within some factor of R)? Bound it by keeping only pairs whose count is ≥ R / 4, re-probing the rest, and measure on cw.
- Pairs a run removes rows from (closed versions don't reduce the "versions ever" count): confirm the anchors' count is monotone, as the rollups' `b` is.

## Order

1. Ship the two fixes (done), watch cw's next compaction and the next scans' `anchors` timings.
2. A, if the next fold still struggles, or as the backlog policy regardless (it's the escape valve).
3. B, measured on cw against today's `anchors end` numbers (above).
