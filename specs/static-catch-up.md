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

## C. The drill's long kind, memory-bounded (so cw's drill goes back to an n2-highmem-16)

cw's 11-26T0003 drill was OOM-killed (exit 137, cgroup `oom_kill 1`) at 131 GB on its n2-highmem-16, in `drill long`'s newly-heavy step: `gov_reports` made 2,096 directories newly heavy, whose rollups read 125M prior roots. Stopgap: cw's drill runs on an n2-highmem-32 with 1500 GB of local SSD (`machines.drill` / `ssd_gbs.drill`, cw `546518031`, `f39042686`: an n2-highmem-32 takes 4, 8, 16 or 24 SSDs, and the default 750 GB failed the job at scheduling; profiles now refuse such a pair, cloud `98c210def`). That grows the machine with `gov_reports` instead of bounding the drill.

What the step does already (`static_drill.newly_heavy_rollups`): chunks of consecutive `(q, dir)` of ≤ `NEWLY_HEAVY_CHUNK_ROWS` estimated rows; per chunk it reads the prior tiers' roots of the chunk's top ranges (`insert_prior_roots`, prefetched through `TierReads.hold`), combines them with today's rows, rolls up (`full_rollups`), appends to `rup`. So it isn't one global join like `pvc` or `measure_kind` were.

On the highmem-16 (11-26T0003, first attempt), RSS rose ~1 GiB a chunk on top of DuckDB:

| chunk | prior roots so far | DuckDB bytes | DuckDB spill | process RSS |
|---|---|---|---|---|
| 5/29 | 23M | 44.2 GB | 41.7 GB | 100 GiB |
| 15/29 | 72M | 50.0 GB | 39.9 GB | 111 GiB |
| 22/29 | 108M | 54.7 GB | 38.2 GB | 118 GiB |
| 27/29 (combine) | 125M | — | — | 123 GiB, then killed |

Measured per holder (`chunk_memory`, cloud `dc00facb9`) on the n2-highmem-32 catch-up of the same scan (`sn-drill-2026-11-26-0003-084428`, DuckDB `-M 120GB`; GiB; gap = anonymous RSS − DuckDB's buffer bytes):

| chunk | anon RSS | DuckDB | spill | gap | arrow alloc / peak | `Parts` pending | freed: arrow pool | freed: `malloc_trim` |
|---|---|---|---|---|---|---|---|---|
| 1/29 | 150.2 | 94.0 | 16.0 | 56.2 | 0.0 / 4.0 | 0.00 | 2.83 | 0.01 |
| 10/29 | 163.4 | 97.0 | 14.9 | 66.4 | 0.0 / 4.0 | 0.07 | 1.88 | 0.10 |
| 20/29 | 163.4 | 100.4 | 13.6 | 63.1 | 0.0 / 4.0 | 0.00 | 0.05 | 0.50 |
| 29/29 | 173.3 | 104.3 | 12.1 | 69.0 | 0.0 / 4.0 | 0.02 | 0.62 | 0.00 |

The step finished: 2,096 directories, 137.7M prior roots in 1283 s, the drill's RSS peak 184 GiB of 256. What it says:

1. **Little grows within the loop.** Over 29 chunks DuckDB gains 10 GiB (its spill falls 4: it moves data into memory near its limit), the gap 13 (~0.45 GiB a chunk).
2. **The suspects this section first named are ruled out for the loop**: pyarrow's live allocations are ~0 after each chunk's `release_unused` (which gives back 1–3 GiB a chunk: kept), the checkpoint's pending rows ≤ 0.1 GiB, glibc's `malloc_trim` gives back ~0.
3. **The gap is there before the loop**: 52 GiB at `long: newly-heavy start` (anon 154.3 vs DuckDB 102.3). It forms in `long: delta roots` (anon 15 → 113 GiB) and `long: rollups` before the loop (56 → 140), neither of which sampled DuckDB or the heap.
4. **It follows the data, not the machine**: 56–70 GiB here, ~68 on the highmem-16. DuckDB gets half the machine (`drill_duckdb_args`); on an n2-highmem-16 that's 60 GB, and 60 GB + ~68 GiB doesn't fit 128 GB. That was the OOM, more than the per-chunk climb.
5. **It lives across the long kind**: anon fell 173 → 133 GiB once the step's tables dropped (`newh_today`, `nhr`), and to 103 during `write`.

Each long-drill stage boundary then logged `memory_breakdown` (cloud `c88614f09`: DuckDB's bytes, spill, limit and allocator; pyarrow's; glibc's in use and free from `mallinfo2`; CPython's block count; the tier reads' caches). cw's 11-26T1203 drill (`sn-drill-2026-11-26-1203-135026`, highmem-32, DuckDB `-M` 111.7 GiB; GiB):

| stage | anon | DuckDB | glibc used / free | Python blocks | index groups |
|---|---|---|---|---|---|
| `long: delta roots` | 12.5 | 1.9 | 7.0 / 0.2 | 0.6M | – |
| `long: rollups` | 47.3 | 39.9 | 7.5 / 0.3 | 0.9M | 0 |
| `long: touched` | 72.8 | 53.0 | 7.5 / 0.2 | 0.9M | 0 |
| `long: settle` (after `_probe`) | 120.7 | 66.2 | 44.2 / 0.8 | 47.3M | 1,083 |
| `long: settled` | 117.0 | 66.2 | 44.0 / 1.0 | 31.6M | 1,083 |
| `long: newly-heavy start` | 135.8 | 83.9 | 47.7 / 0.6 | 41.3M | 1,430 |
| newly-heavy chunk 19/19 | 166.1 | 95.0 | 61.3 / 1.1 | 77.2M | – |
| `long: write` | 135.6 | 57.3 | 9.7 / 52.0 | 0.8M | – |

**The cause: data files' footers, held for the whole kind.** glibc's in-use bytes went 9 → 46 GiB during `_probe` (13,406 probes in 110 s, 14:06:44–14:08:34) and stayed; the newly-heavy loop added 13.6 more as it opened more files. The tier reads kept every data file's footer they touched, as tail bytes (`GroupFile.foot`) and parsed `FileMetaData` (`CachedGroupFile._md`, C++ heap), never evicted within a kind. cw c003's long `roots`: the base's 126 files have ~6 MB footers (r0050: 6,121 row groups × 7 columns, 50.1M rows; parsed ~39 MB, 6.3× the serialized bytes, measured on macOS); its runs' 281 files have 20–25 MB footers, ~160 MB each held. The four big runs' 257 files alone are ~41 GB. The index entries and `_spans` (the 30M+ Python blocks) are pymalloc's small objects, a few GiB. And DuckDB's 95 GiB isn't in glibc's counters (61 at most): its Linux build links jemalloc statically while `duckdb_extensions()` reads it not loaded, so `unattributed_bytes` counted DuckDB twice; without it, ~3–8 GiB is unattributed.

**The fix** (cloud, this commit):
- `FooterCache`: one per drill's reads (`TierReads`), shared by its tiers' group files: footers' tail bytes and parsed metadata, least recently used evicted past min(`FOOTER_CACHE_BYTES` 8 GiB, RAM/16), a parsed one charged `META_PARSE_FACTOR` (7) × its serialized size. An evicted file's footer is fetched and parsed again when read, counted (`footer_refetches`, `meta_reparses`); `cache_stats` lists the files and bytes held, those counts and the first parse's measured glibc growth per serialized byte (`meta_parse_factor`, Linux's), on every stage line.
- Index row groups kept per group file up to `INDEX_CACHE` (1,024), least recently used evicted, reads again counted (`index_reloads`).
- DuckDB's bytes are outside glibc's when its jemalloc is loaded or statically linked (`install_mode`), or when glibc's in-use bytes couldn't hold them (`duckdb_allocator` `outside glibc`).
- Tests: a run's drill is the same bytes with footers capped at 300 bytes and one index row group per file (forced evictions, refetches counted) as unbounded, with the cap holding after every probe pass; `FooterCache`'s eviction order and charges exactly.

Next, by what the next drills show:
1. **The highmem-16 trial:** cw's drill on an n2-highmem-16 with DuckDB at 60 GB (60 + footers 8 + a few GiB of Python ≈ 75 of 128). Watch `footer_refetches` per stage: a refetch is a GET and a parse (a run file's: ~22 MB, ~140 MB parsed); thousands of them mean raising the budget or ordering the probes by file.
2. **No parsed metadata at all:** the two-level index already carries each group's offset, length and rows; read a group from those with one schema per tier, and `FileMetaData` goes. Bigger; if the refetches cost.
3. **Smaller footers:** run files' footers are ~4× the base's: `RUN_RG` = 2,048-row groups and string min/max statistics on `q` / `path` per row group, which the index already holds. Statistics off for those columns, or bigger run row groups, shrink every reader's footer. It changes stored bytes: list every reader of the drill files (the site's static search; anything pruning by row-group statistics) and bench it first.

Done when cw's drill runs `gov_reports`-sized scans on an n2-highmem-16 with DuckDB at 60 GB, and `machines.drill` / `ssd_gbs.drill` leave cw's profile.

## Order

1. Ship the two fixes (done), watch cw's next compaction and the next scans' `anchors` timings.
2. C: the footers are bounded (above); a highmem-16 drill at DuckDB 60 GB, then drop cw's `machines.drill` stopgap.
3. A, if the next fold still struggles, or as the backlog policy regardless (it's the escape valve).
4. B, measured on cw against today's `anchors end` numbers (above).
