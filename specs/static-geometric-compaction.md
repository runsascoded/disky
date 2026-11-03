# Size-ratio ("geometric") compaction for the static name index and the interval store

Status: design (2026-10-26). Nothing built. Written against `gcs` at `3fc8f99b` (which merges `cloud` through `8734f7b2`); the root session lands it on `cloud`. Line numbers below are at that commit.

Goal: replace "fold the base and every run into a new generation every 2^L scans" with a policy whose costs follow **sizes**, not scan counts:

- amortized O(log n) rewrites per run byte, and O(1) amortized base rewrites per input byte (no base rewrite cadence that is fixed in scans while the base keeps growing);
- a hard bound on the runs a read fans over;
- the worst single job bounded by a fraction of the base, not by "whatever 2^L scans plus the base add up to";
- storage O(base + changes since the base fold), with predecessor generations deleted after a TTL-style grace, not kept or soft-deleted.

## 1. What exists today

### 1.1 Two mechanisms: the binary counter (runs) and the full fold (base)

- **Runs.** Each scan publishes one level-0 run (`publish_scan`, `append_runner.py:880`). The merge stage replays a binary counter over the newest manifest's runs (`plan_carries`, `append_runner.py:252-275`): push each run, and while the two newest share a level (and agree on drill presence) carry them into one run a level up. Chained carries become one N-way merge. A carry never reaches `max_level` (`append_runner.py:264`). For the static store the merged run is built tier by tier from the input runs only (`static_merge.build_merged_run`, `static_merge.py:144`: shards, catalog, drill, names and anchors), never touching the base.
- **Base.** `Policy` (`append_runner.py:281-330`) decides when the base and **every** run fold into a new generation (`compaction_why`, `append_runner.py:376-404`). Rules: `level` L (gcs's static and interval profiles: `compact_level: 5`, `job/static-names.json`, `job/interval-store.json:45`), `every` N days/scans, `geometric` r, `none`. Under `level` L the counter's carries stop below L (`Policy.carry_level`, `append_runner.py:317-319`), and the fold is due once two runs sit at level L − 1 (`append_runner.py:384-389`): every 2^L scans past the base. The staged runner checks this after its carries (`Runner.carries`, `append_runner.py:1945-1999`) and submits `compact_stage` (`append_runner.py:1916`, called at `:1997`).
- **The fold** is `compact_store` (`append_runner.py:2242`) over the store's `Compactor`. For the static store that is `static_compact._fold_gen` (`static_compact.py:1159-1323`). It folds every part, so its cost is O(base + runs):
  - `fold_ranges` (`:1275`): every `cintervals/` range rewritten;
  - `plan_shards` + `merge_shards` (`:1280-1282`): every suffix shard re-cut and rewritten;
  - `merge_catalogs` + census (`:1270`, `fold_catalog`);
  - `fold_drill` (`:1298`): every long and short root read, combined with the runs' and rewritten. The incremental measure path (`static_measure.blocker`, `static_measure.py:140-150`, chosen at `static_compact.py:397`) skips `dir_stats` and the rollups when every run is `dcount: fresh`, but each range is still loaded, sorted and rewritten (specs/static-incremental-compaction.md step 4);
  - `build_base_anchors` (`:1311`): `names/` and `anchors/` **rebuilt from scratch** over the new generation's shards. This is a recompute, not a merge, though runs' anchors are merged by `merge_tiers` (`static_merge.DUCKDB_TIERS`).

  Then the publish, `successor.json`, `CURRENT.json`, and a copy of the **whole** new generation to R2 (`r2_publish_gen`).

### 1.2 Is the counter already geometric?

By **scan count**, yes. With carries unbounded, the counter keeps at most ⌊log2 n⌋ + 1 runs, and each scan's rows are rewritten once per level they climb: O(log n) rewrites per row. If every scan's run were the same size, this would be exactly a size-tiered policy with ratio 1.

It is not geometric by **size**, and under the deployed policy it is capped:

1. **The cap makes the base fold the only relief.** Under `level: 5`, carries stop at level 4, so each scan's rows are rewritten at most 4 times by carries. After that, the base and everything else are rewritten every 32 scans regardless of size. Per scan, that amortizes to B/32 of base rewrite, where B is the base's size. B grows with every scan (the index keeps full history: closed versions are content, not garbage; §2.4). So over N scans the base-rewrite work is Σ B(32k) ≈ N·B̄/32, which grows faster than the input. The asymptotics break here, in the cadence, not in the counter.
2. **Count is not size.** A run's size varies a lot from scan to scan. A substring that becomes indexed carries its whole history into the run that indexes it (specs/static-compaction.md "Why it's needed now": in one scan's run, about a fifth of the rows were newly indexed substrings' histories). Catch-ups and sub-daily scans add more variation. The counter merges a large run with a small one because they share a level, and a large run never merges early.
3. **`geometric: r` is also count-based, and leaves carries unbounded** (`append_runner.py:401-404`). It fires when the scans past the base reach r × the base's own scan count. The base's scan count says nothing about its size: a base built with backfilled history has many scans, so the first fold comes very late. Meanwhile the top carry grows toward the base's size, so it costs about as much as a fold (gcs's level-4 carry took 183 min on a spot n2-highmem-16; `LONG_MERGE_LEVEL`, `append_runner.py:127-130`). Everything that scales with "scans since the base" grows without bound too (§1.4).

So the real gap is precisely the **count-triggered full fold** (and secondarily count-based carries). The proposal below keeps the counter's structure, prices it by size, and replaces the fixed cadence with a base fold triggered by the runs' size relative to the base.

### 1.3 Read fan-out

- Light index: `Tiers.each` (`site/functions/_lib/staticRuns.ts:197-206`) runs every query over the base and **every** run in parallel (`state.tiers.map`). Fan-out = 1 + #runs.
- Drill: `perTier` (`staticDrill.ts:448`) asks every drill tier. The root-dispatch bound is `R + 2·Σ rg` over all tiers (`DRILL_RULES.bound`, `staticDrill.ts:408`), so each tier loosens the bound by two row groups and sends more views to root reads. Rollups stack newest-first down to a full header (`stack`, `staticDrill.ts:421`).
- Anchors: every tier's `names` reader (`staticAnchors.ts:389`, `:405`, `:485`).
- Interval store: the base plus every run. A merged run's closed versions are **one** segment, not dyadic (specs/interval-store.md §2.7 "Why a new generation"), so a historical read inside a big run decodes its closed rows for every scan.
- Under `level: 5`: at most 5 runs (levels 4…0) plus a second level-4 run while a fold is due, so 6–7 tiers. If folds keep failing or being voided, level-4 runs pile up without bound (the `Policy` docstring, `append_runner.py:286-287`).

### 1.4 What else scales with "since the base"

- Each static append merges the base's and every live run's catalog (`catalog_delta` → `merge_catalogs(tiers, …)`, `static_append.py:628`). This is O(base catalog + runs) per scan, independent of the policy.
- The open-version refold reads every earlier scan's `cdelta/` since the base (`rebuild_open`, `static_append.py:823`; the catalog's `xcount` carries counts when trusted, `static_append.py:607-612`, else it refolds). Folded runs keep `cdelta/` and `scans.json` for this (`static_merge.FOLDED_RETAIN`, `static_merge.py:64`), so retained bytes grow with scans since the base.
- The drill's per-run `dcount` / headers are conservative (they may over-roll-up; answers stay exact), and classes only split. A base fold resets both.

### 1.5 Cost profile, today (gcs static)

| | `level: 5` today |
|---|---|
| Carries per run row | ≤ 4 (levels 1–4) |
| Base rewrite | all of B + Σruns, every 32 scans |
| Amortized base rewrite per scan | B/32, growing with B |
| Worst single job | the fold: O(B). gcs's last one took ~2h40m on one n2-highmem-48 plus 16 helper tasks (fold fan-out, measure, anchors), mostly drill measure/rollup and anchors. Its predecessor (2026-10-08c → c001) took ~10 h before the fan-outs (specs/static-measure-fanout.md) |
| Second-worst | a level-4 carry, 16 scans of runs (183 min on a spot n2-highmem-16) |
| Read fan-out | 1 + ≤ 5–6 runs (unbounded if folds fail) |
| R2 egress | the whole generation per fold |
| Storage | B + Σruns + retained `cdelta/`; the old generation until `store-gc -k` (static: `Compactor.prune = False`, `static_compact.py:1525`) or 1 h (interval: `prune_predecessors`, `append_runner.py:2124`, `GRACE_S`, `:1412`) |
| Fold voided by a late append | yes (`landed` / `superseded`, `append_runner.py:2308-2318`); carrying late runs is specs/static-carry-appends.md, not built |

## 2. Proposal: rule `tiered`

A new `Policy` rule, `tiered`, alongside the existing ones. `level` stays available, and so do `geometric` and `every`, which the tests use. It has three parameters and one optional pair:

- `ratio` r (default 2): run-merge ratio.
- `base` f (default 0.5): fold the runs into the base once they total f × the base.
- `runs` K (default 6): hard cap on runs.
- optional `horizon` H with `garbage` g: §2.4.

Forms: profile `{"tiered": {"ratio": 2, "base": 0.5, "runs": 6}}`, text `tiered:2,0.5,6` (`parse_policy`); `carry_level` is None (carries are planned by size, not capped by level).

### 2.1 What "size" is

The cost of every step is dominated by the rows it reads and rewrites (specs/static-compaction-perf.md: the drill roots, the suffix shards, the anchors over the suffix shards). Define a tier's size as **rows**:

- static: `suffix rows + long roots + short roots`. Runs already carry `rows` (suffix rows) and `bytes` (`sx/` bytes) in the manifest (`static_append.py:1040`, `RUN_FIELDS` `append_runner.py:242`); a merged run takes them from its `meta.json` (`append_runner.py:1170-1184`). Drill root counts are not recorded anywhere today (`static_drill.base_meta`, `static_drill.py:2060`, has `R`, `K`, `rg` only), so they are added to `drill/meta.json` and to the manifest entry (`size: {suffix, long, short}`). For the base: `shards.json` `total_rows` plus the drill roots' counts, summed from the roots' group index files (`long-roots-index.parquet`, which `static_roots.index_estimates` already reads) for existing generations. That is a footer-sized read, with no rewrite and no backfill job.
- interval: the run's `rows` (its `path` sort; `interval_append.py:546`, `:564`) and the base's `stats.json` rows.
- Bytes are a fallback when rows are absent (old runs), logged as such.

Whether one rows sum predicts cost well enough, or whether the anchors (keyed by suffix rows) and the drill (keyed by roots) need separate weights, is measured, not assumed (§4).

### 2.2 The plan (`plan_tiered`, replacing `plan_carries` + `compaction_why` under this rule)

Inputs: the newest manifest's runs (oldest first, each with `size`), the base's size B, the policy, and the drilled set.

1. **Base fold.** If Σ size(runs) ≥ f · B, or the runs after step 2 would still exceed K, a base fold is due. It folds all runs, which is today's `compact_store`, unchanged. Later it can fold the oldest prefix whose sum reaches f · B and carry the newer runs onto the successor, but only once specs/static-carry-appends.md is built.
2. **Carries, by size.** Replay a stack over the runs. Push each run; while the two newest `a` (older) and `b` (newer) satisfy `size(a) ≤ r · size(b)` and agree on drill presence (the parity rule `plan_carries` already enforces, `append_runner.py:264`), merge them. The merged size is estimated as `size(a) + size(b)`, an upper bound, since combining closes into opens only shrinks. The actual size is recorded when the merged run is written. Chained merges fold into one N-way merge as today.
3. **Run cap.** While more than K runs remain, merge the newest adjacent pair (the cheapest). With r ≥ 2 the cap rarely binds; it guards against pathological size sequences and against the drill bound's per-tier growth.
4. **No carry may produce a run larger than f · B.** If it would, fold the base instead. This keeps the worst run merge below the base-fold trigger, so the base fold is always the biggest job and its size is known in advance.

`level` stays a recorded field (`run_key`, `merge_is_long`). `merge_is_long` switches to its `long_rows` form (`append_runner.py:473-477`), since levels no longer predict cost.

### 2.3 Bounds

Let d be a scan's run size and S = Σ runs < f · B.

- **Run count**: adjacent runs satisfy size(older) > r · size(newer) after planning, so runs ≤ ⌈log_r(S / d_min)⌉ + 1, and ≤ K always. With r = 2 and f = 0.5, the runs hold at most half the base, so the run count is about log2(B / 2d). Measure the B/d ratio on gcs (§4) to see whether K = 6 binds.
- **Rewrites per run row**: each carry that rewrites a row grows its run by a constant factor (≥ 1 + 1/r when the row is in the older run). So O(log_{1+1/r}(S/d)) carries per row before the base fold absorbs it. With r = 1 and equal-sized inputs this is today's binary counter. With r < 2 the count bound needs equal-ish inputs, so r = 2 is the default. A property test pins both bounds over random size sequences (§3.5).
- **Base rewrites**: each base fold rewrites B + S ≈ (1 + f) · B and is triggered by f · B of new run rows, so (1 + f)/f rewrites of base rows per input row: **constant**, not growing with B. If B grows by a fraction α of run rows (opens, not closes), the fold count to grow from B₀ to B_N is ln(B_N/B₀) / ln(1 + α f): logarithmic. The current 2^L cadence gives linear-in-N folds, each of growing size.
- **Read fan-out**: 1 + K at worst, about 1 + log_r(f·B/d) in steady state. Today it is 1 + ~5.
- **Worst single job**: the base fold, O((1 + f) · B), known when it is planned. f trades fold frequency against fan-out and the largest carry (≤ f · B).

Choosing f: f = 0.5 means a fold whenever the runs reach half the base. Today's effective f is whatever 32 scans of runs come to relative to the base at fold time. §4's replay computes it from the manifests, and f should start near that value so the first deployment doesn't change the fold frequency much. Raise f only if the measured fan-out cost (cold-read latency per extra tier) is low.

### 2.4 Garbage, deletes and the base

The static index and the interval store are **full-history** indexes: a path that disappears gets a close record (`vt` set), and the closed version still answers every scan in which it was live. So the base holds no garbage of its own. What looks like churn garbage is:

- **Shadowed rows across tiers**: a run's close record restates a base version with its final `vt` (the readers' smallest-`vt` combine, `staticRuns.ts` header; `DRILL_RULES.combine`). A base fold collapses each pair into one row. These rows are part of the run sizes, so the f trigger already prices them: the more churn, the sooner the fold.
- **Drill overcounting and stale headers** (§1.4): conservative, reset by a fold, and proportional to the run rows that caused them.
- **Retained `cdelta/` of folded runs** (`FOLDED_RETAIN`): proportional to the scans since the base, and dropped by a fold (the predecessor's runs go with it).

A real garbage trigger exists only if the product adopts a **history horizon** H: versions closed more than H before the newest scan are dropped, and those scans become `scan-not-indexed` for search. That is the only route to a constant-size steady state for a churning bucket. Without it, size grows by exactly the change volume, which is O(#changes) storage already. If H is adopted, the fold drops expired versions (a `vt ≤ horizon` filter in `fold_range`, `merge_shards`, the drill combine and the anchors), and a second trigger fires when the expired rows reach g × B. The expired rows are counted from the `chist/` histograms at append time, without reading the base. Decided 2026-10-28: no horizon. Search covers all history, and storage grows O(#changes). H stays addable later as a fold-time filter, with no rebuild.

### 2.5 Cost of each step under this policy

- **Carry** (`merge_tiers` / `build_merged_run`): O(size of inputs), already incremental, including anchors. Unchanged, except that it is planned by size and capped at f · B.
- **Base fold**: O((1 + f) · B), as today. The incremental measure path removes `dir_stats` and the rollups, but not the roots' read, sort and write, the suffix shards' re-cut, or the anchors' rebuild. The two levers that would make a fold cost closer to O(S) than O(B):
  1. **Anchors by merge, not rebuild.** `build_base_anchors` (`static_compact.py:1311`) recomputes the `end`, `exact` and `start` rollups over every suffix row. Runs' anchors already merge with `merge_tiers` (`DUCKDB_TIERS`). A fold that merges base anchors ⊕ runs' anchors needs the byte-identity proof the drill got (specs/static-incremental-compaction.md step 2: additivity, then md5 equality against `build -r`).
  2. **Copy untouched files.** A part file whose key range no run touches is byte-identical in the successor: a `cintervals/r####` range with no `cdelta` rows, or a suffix shard whose rows no run adds to. It can be server-side copied (GCS rewrite, R2 copy-object) instead of rewritten. For the interval store, keyed by path, churn concentrates in a few prefixes, and this could cut most of a fold. For the static index it likely won't help: each changed path is indexed under many substrings, so every `q` range sees run rows. Measure the per-range distribution of run rows before building it (§4).
- **R2**: a fold copies the whole generation to R2. Under `tiered` that happens (1 + f)/f times per base-sized volume of input instead of every 32 scans. Copy-untouched (lever 2) applies here too: an unchanged file's md5 is already in `compaction.json`, so `r2_publish_gen` can copy it within R2 from the predecessor instead of over the network.

### 2.6 Generations, the pointer and TTL cleanup

- Unchanged: `next_gen` (`.c001`, …), `compaction.json`, `successor.json`, `CURRENT.json` moved last after R2 (`compact_store`, `append_runner.py:2242-2348`).
- **Static predecessors get the interval store's TTL deletion.** The gcs site follows `static-names/CURRENT.json` (`site/wrangler.toml:60`), so the reason for `Compactor.prune = False` (`static_compact.py:1516-1525`, "readers pin `STATIC_GEN`") no longer holds for the main lineage. Set `prune = True`. `prune_predecessors` deletes a predecessor's files (all but `successor.json`) once its successor's `compaction.json` is `GRACE_S` (1 h) old, never while the pointer names it (`append_runner.py:2124-2150`). The grace exceeds the readers' re-list intervals (`Tiers` `ttlMs` 5 min, `staticRuns.ts:81`; the interval store's `IV_STATE_TTL` 1 min) and the edge cache's lifetime. Do the same on R2 after the R2 copy: today R2 predecessors are collected only by `store-gc`.
- **Pinned lineages** must not compact under a pruning compactor. The "meta" store pins `STATIC_GEN` (`site/wrangler.toml:125`); `prune_predecessors` would delete its pinned generation an hour after a fold. Either it follows its own pointer (the one-pointer-per-prefix caveat, specs/static-compaction.md "Open"), or its profile stays `compact: none`. `prune_predecessors` refuses a generation named by any `STATIC_GEN` in the deployment's config, read from the profile's `pins` list. This is a safety rail, not the mechanism.
- **TTL means hard delete.** If a bucket holding a store has object soft-delete retention or versioning, deleted predecessors keep costing for that window. The store buckets' soft-delete settings should be checked and turned off for the index prefixes where possible (open question 6), so reclaim really is TTL-style.

### 2.7 The interval store

The same `plan_tiered` and `Policy` apply (`interval_append` already plugs into `append_runner`'s planner and `compact_store`). Two differences:

- **Merged runs lose dyadic segments**, so a big run makes historical reads decode more (§1.3). Either keep its f and K smaller than the static store's (its fold is far cheaper: per-range folds plus `cut_gen`), or have `cut_run` give a merged run's closed rows dyadic segments over its own scans, as the base's `seg_sql` does (open question 3).
- Its fold is the natural first user of copy-untouched (§2.5 lever 2), since its ranges are path-keyed.

## 3. Implementation plan (on `cloud`, then the deployments)

1. **`Policy` rule `tiered`** + `parse_policy` forms + `to_json`; `compaction_why` returns the base-fold reason (`tiered: runs Σ x ≥ f × base y` / `run cap K`). Pure, with unit tests mirroring `test_compact_level.py`.
2. **`plan_tiered(runs, base_size, policy, drilled)`** returning the same `(after, merges)` shape as `plan_carries` plus the fold decision. `Runner.carries` and `merge_pending` (`append_runner.py:1117-1143`) call it when the rule is `tiered`. `merge_pending` re-plans per published revision as now.
3. **Sizes.** Add `size` to static runs' manifest entries and to `drill/meta.json` (root counts, from the writer's index tables); add `base_size(gen_dir)` per store (static: `shards.json` + roots index footers; interval: `stats.json`). Old runs without `size` fall back to `rows`, then to `bytes`, and the log says so.
4. **`prune = True`** for the static `Compactor`, with the pin rail; R2 predecessor deletion after the grace.
5. **Tests.**
   - A property test over random size sequences (hypothesis-style, seeded): after every push, runs ≤ min(K, ⌈log_r(S/d_min)⌉ + 1); every carry's output ≤ f · B; the rewrite count per unit row is ≤ the bound; the fold fires exactly when Σ ≥ f · B. Equal sizes with r = 1 reproduce `plan_carries` exactly (the regression link to the counter).
   - The existing byte-identity tests (`test_compacted_generation_is_the_rebuild`, the interval compact tests) under `tiered`, with the fold triggered by size.
   - `prune_predecessors` refusing a pinned generation.
6. **A replay CLI**: `dt-cloud static-names policy-replay` / `interval-store policy-replay`, given a store and a candidate policy. It replays the store's manifests (manifests are never deleted, `plan_prune`) under the candidate policy and under the deployed one, and prints per scan: runs, planned carries and their input rows, folds and their (1 + f)·B, and fan-out. A subcommand, not a script: it gets rerun for every parameter choice.

## 4. Measurement plan (real data, side branch first)

Per the perf-rewrite rule (measure on real data on a side branch before landing on `cloud`):

1. **Replay** (cheap, laptop-safe: manifests and footers only). Over gcs's static and interval histories, compute: B/d per scan; today's effective f at each fold; the size spread of level-0 runs (how far count is from size); and the schedule, fan-out and total rewritten rows under `level:5` versus `tiered` at r ∈ {2, 3, 4}, f ∈ {0.25, 0.5, 1}, K ∈ {4, 6, 8}.
2. **Per-range distribution of run rows** (static: per `q` range and per suffix shard; interval: per key range), from the runs' index files. This decides whether copy-untouched (§2.5 lever 2) is worth building per store.
3. **Size → cost fit.** From the recorded carry and fold summaries (per-tier seconds, rows; `maint_runs` records), fit seconds against suffix rows and root rows separately, to check §2.1's single rows sum.
4. **Race** on a side branch, read-only (`static_merge compact-probe`, which writes nothing to the store): fold the current generation at the point `tiered` would have, and run one size-planned carry beside the counter's carry for the same manifest. Compare wall time, peaks and output md5s (a size-planned carry must equal the counter's merge byte for byte when the input sets are the same). Record the numbers in this spec.
5. **Fan-out cost**: cold and warm latency of a filtered view and a drill view per extra tier, measured on dev (`dev.gcs.oa.dev`) with a manifest of K runs. This informs f and K.

Only then switch gcs's profiles (`job/static-names.json`, `job/interval-store.json`) to `{"tiered": …}`.

## 5. Migration

- Profiles switch with no data change. The current generation's runs keep their keys and levels and become sized runs: `plan_tiered` reads their `size` (or `rows`) and the base's size.
- The first evaluation may find Σ runs ≥ f · B right away, e.g. right after a switch late in a 32-scan cycle. That fires an ordinary fold, the same job as today's. Pick f from the replay so the switch doesn't immediately trigger an unplanned fold, or schedule the switch right after a fold.
- Level-capped runs (two level-4 runs waiting on a fold) are simply runs. `tiered` either merges them (if the merged size stays ≤ f · B) or folds.
- `compaction.json` records `{"level": null, "rule": "tiered:…", "why": …}` (`compaction_doc`, `append_runner.py:2154-2160`), so a lineage's history shows which rule produced each generation.
- Rollback: set the profile back to `level: 5`. Runs carried by size are valid runs for the counter, which only looks at levels. A size-planned merge's `level` is set to max(input levels) + 1, which keeps the counter's invariants (it never merges unequal levels) without rewriting anything.

## 6. Open questions

1. **Defaults for r, f, K**: from the replay (§4.1) and the fan-out latency (§4.5). The defaults above are placeholders.
2. **Partial base folds** (fold the oldest prefix, carry the rest): needs specs/static-carry-appends.md built. Worth it mainly because a fold that takes hours races the daily append (`superseded`). Is that spec the next dependency, or does a shorter fold (after the anchors merge) make it moot?
3. **Interval store**: dyadic segments inside merged runs (`cut_run` with stamps), or a small K and f?
4. **Anchors by merge** in the base fold (§2.5 lever 1): which cells are additive, and what proves byte identity with `build -r`?
5. **History horizon H** (§2.4): decided 2026-10-28, no horizon. Search covers all history, with O(#changes) growth.
6. **Soft-delete on the store buckets**: check the GCS and R2 index buckets' soft-delete or versioning settings for the `static-names/` and `interval-store/` prefixes, so the TTL deletes actually reclaim (a bucket-wide setting; test on a scratch bucket first).
7. **Per-append "since the base" costs** (§1.4): `merge_catalogs` over the base catalog per append, and the `cdelta` refold fallback. Under `tiered` the base is folded less regularly in scans, so should a scans-since-base bound join the fold triggers, or should these costs become independent of the base's age (e.g. carry the merged catalog view per run)?
8. **Drill bound**: `R + 2·Σ rg` grows with tiers. Is K enough, or should the reader bound use only the tiers holding the class?
