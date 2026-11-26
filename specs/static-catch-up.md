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

## C. The drill's newly-heavy step, memory-bounded (so cw's drill goes back to an n2-highmem-16)

cw's 11-26T0003 drill was OOM-killed (exit 137, cgroup `oom_kill 1`) at 131 GB on its n2-highmem-16, in `drill long`'s newly-heavy step: `gov_reports` made 2,096 directories newly heavy, whose rollups read 125M prior roots. Stopgap: cw's drill runs on an n2-highmem-32 (`machines.drill`, cw `546518031`), which grows the machine with `gov_reports` rather than bounding the step.

What the step does already (`static_drill.newly_heavy_rollups`): chunks of consecutive `(q, dir)` of ≤ `NEWLY_HEAVY_CHUNK_ROWS` estimated rows; per chunk it reads the prior tiers' roots of the chunk's top ranges (`insert_prior_roots`, prefetched through `TierReads.hold`), combines them with today's rows, rolls up (`full_rollups`), appends to `rup`. So it isn't one global join like `pvc` or `measure_kind` were. **The growth is outside DuckDB:**

| chunk | prior roots so far | DuckDB bytes | DuckDB spill | process RSS |
|---|---|---|---|---|
| 5/29 | 23M | 44.2 GB | 41.7 GB | 100 GiB |
| 15/29 | 72M | 50.0 GB | 39.9 GB | 111 GiB |
| 22/29 | 108M | 54.7 GB | 38.2 GB | 118 GiB |
| 27/29 (combine) | 125M | — | — | 123 GiB, then killed |

DuckDB stayed under its 60 GB limit (`-M 60GB`) and spilled ~40 GB; RSS rose ~1 GiB a chunk on top of it, to ~68 GiB that DuckDB doesn't account for. Candidates, in order of likelihood:

1. **pyarrow's pool holding freed buffers.** Each chunk's prior roots pass through Arrow (`_insert_root_batch`: `pa.concat_tables`, registered, inserted); the default pool (jemalloc/mimalloc) keeps what it freed. Check `pa.total_allocated_bytes()` vs RSS per chunk; `pa.default_memory_pool().release_unused()` after each chunk, or the system allocator for this process.
2. **`Parts.pending`** (`checkpoint.Parts.add`): a chunk's rollup rows are kept until `every_s` elapses, so a fast run holds many chunks' rows. Flush by bytes as well as time.
3. **`rup` itself** grows in DuckDB across chunks (counted in DuckDB's bytes, so not the gap, but it competes with each chunk's join for the 60 GB): write each chunk's rows out as a parquet part (`Parts` already can) and read them back at the end, instead of holding `rup` across the loop.
4. **DuckDB's limit vs the machine.** `drill_duckdb_args` gives DuckDB 60 GB of 128; whatever Python holds is the rest. Once 1–3 are bounded, pin the split (e.g. DuckDB ½ of RAM, Python a fixed budget) and fail loudly past it rather than be OOM-killed.

Shape of the fix: instrument first (per chunk: RSS, DuckDB bytes and spill, `pa.total_allocated_bytes()`, `Parts` pending bytes), reproduce on a synthetic newly-heavy set big enough to show the climb (a test that asserts RSS flat across chunks, within a margin, at a small `NEWLY_HEAVY_CHUNK_ROWS`), then bound each holder that climbs. Same output at any chunking (the step's contract already; the existing tests check it). Done when cw's drill runs `gov_reports`-sized scans on an n2-highmem-16 again and `machines.drill` goes back out of cw's profile.

## Order

1. Ship the two fixes (done), watch cw's next compaction and the next scans' `anchors` timings.
2. C: bound the drill's newly-heavy step, then drop cw's `machines.drill` stopgap.
3. A, if the next fold still struggles, or as the backlog policy regardless (it's the escape valve).
4. B, measured on cw against today's `anchors end` numbers (above).
