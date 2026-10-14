# Static name search: per-scan append

**Status:** done. Live on gcs prod: the daily job runs `dt-cloud static-names runs add -c` (gcs `2fe4f89a`), with deferred carries (`5343fa32`), one append runner for both stores (`9922694a`) and the long drill streamed in bounded batches (`eebd2987`); the reader serves base ⊕ runs (`site/functions/_lib/staticDrill.ts`). Follow-ups tracked elsewhere: compaction (`static-compaction.md` and `static_compact.py` on the unmerged `compaction` branch), GC of superseded merged runs, a sharded top merge for L ≥ 5, and the per-scan chain's length against its 30 min target. The dated checkpoints below are history.

**Oct 12 checkpoint (superseded by the Oct 13 update below): scheduled and served in GCS production**, search-covered through manual scan `2026-10-12T1042`, generation `2026-10-08c`. Instrumented recovery confirmed host OOM at the historical-root gather. Streaming insertion, avoiding bulk Python key caches and lowering drill DuckDB allocation from 90GB to 60GB passed 104 full drill tests and 133 targeted regressions. The real-data daily long drill succeeded in 27m30s, peak RSS approximately 97.3 GiB, no OOM events; the catch-up controller succeeded and all coverage cells are present for both scans. That headroom fix was promoted to `:latest`. Periodic stage/RSS/CPU/Arrow/cgroup logs, DuckDB allocation/spill samples and surviving-shell exit evidence are now built in. A separate bounded boundary-row-budget repair passed 142 reader tests and shipped to production Pages `f4ad1682`; the formerly refused `nemotron` query works on both scans and in Chrome. `job/run.sh` calls `runs add -c` with `job/static-names.json`; static/interval/D1 staging runs concurrently. Carries remain deferred N-way jobs publishing immutable revisions. **The projected 10–12-minute drill is not validated**; this larger-workload recovery is not an A/B speedup benchmark. See [GCS operations](gcs:specs/gcs-operations.md). Dated checkpoints below remain historical evidence, not outstanding rollout instructions.

**Operational update (2026-10-13):** GCS search is now served through `2026-10-13T0702`, including the recovered Oct 12 manual scan. Today's daily completed core scan/interval publication but its long drill exhausted DuckDB's 60GB allocation at the global newly-heavy combine; its short drill and anchors succeeded. The directory-by-directory replacement passed exact and constrained-memory regressions and the real long-only recovery in 19m52s, at the same memory budget with zero OOM events. The controller reused completed stages and published after R2 verification; production health, four query modes and OA Chrome are verified. No full scan was repeated. After explicit user approval, verified image `e439a01c…` was promoted to production `:latest` at 11:19 UTC and the live digest checked; future dailies use the fix. The prior 10–12-minute projection remains unvalidated. Current facts and job/image identifiers are in [GCS operations](gcs:specs/gcs-operations.md#oct-13-drill-combine-recovery).

## Original rollout checkpoint (2026-10-09)

As of 2026-10-09: appended, verified (175/175 against brute force), on R2 and served by the dev site. Code on `cloud` (2026-10-09: the pipeline, the reader behind `FILTER_STATIC` / `NAME_SUMMARY_STATIC`, default off); the entry point `dt-cloud static-names runs add` (`static_runner.py`; it replaces `job/static-daily.sh` on `gcs-static`). Not yet scheduled; not on prod. **2026-10-10: carries deferred** ("Deferred carries" below, on `cloud`): a scan's publish adds only its level-0 run; the counter's merges run as their own job, each published as a revision `manifests/<id>.m<NNN>.json`. The drilldown's run for 2026-10-09 (`deltas/2026-10-09/drill/`, "Drilldown runs" below) is verified (653/653 against brute force) and on R2; the site does not read runs' drills yet.


## Historical state (2026-10-09 15:00 UTC)

- **First sub-daily run, gcs `2026-10-09T1236`** (`runs add -t …`, staged `cloud` tree `9d852323…` + pyrmts 541bc8e as `STATIC_NAMES_SRC`, `STATIC_NAMES_PROFILE=gcs`): prepare 12 s, append 6.4 min (16 spot tasks × 16 ranges), shards ∥ catalog 10.4 min, publish 8.4 min, R2 2.4 min. The run: 85,705,437 suffix rows, 2.37 GB. Verify (the 30 terms of `deltas/2026-10-09/terms.txt`) ran after: see `deltas/2026-10-09T1236/verify.json`.
- **Incident:** the counter merged `deltas/2026-10-09` (which carries the drilldown's `drill/`) with T1236 into `deltas/2026-10-09_2026-10-09T1236`, which had no `drill/` and no `catalog/meta.json`. Published at 14:32:33, it made prod's heavy filter 500 for a few minutes and dropped 10-09's drill coverage. Fix, by hand at 14:35–14:39: `deltas/2026-10-09T1236` copied to R2, then `manifests/2026-10-09T1236.json` **rewritten** to list the two level-0 runs (the merged version is kept at `gs://oa-gcs-usage-dvx/static-names/2026-10-08c/manifests-superseded/2026-10-09T1236.merged.json`). That rewrite broke rule 2 below, once, on the coordinator's call. The merged dir `deltas/2026-10-09_2026-10-09T1236/` on GCS is orphaned: nothing lists it, and it was never copied to R2. It is kept, not deleted.
- **Fixed in code** (`9a4e5c29`):
  - the counter never merges a run holding a tier it can't carry (`pinned_runs`, `MERGED_ENTRIES`);
  - a merged run's catalog gets its `meta.json`;
  - `publish` refuses a manifest whose runs lack a reader file (`RUN_FILES`).
- **Superseded** (`drill-runner`): merges carry `drill/` (`merge_drills` → `merge_tiers`), so the pin rule is replaced by drill parity: `push_run(drilled=…)` merges two runs only when both carry `drill/` or neither does (a drilled run with one without would drop the one's drill: the reader stops at the first run without one), and the merged run is drilled iff its inputs are. `MERGED_ENTRIES` would also have pinned 10-09 for good (its `drill-verify/`).
- **Rules:**
  1. Write a manifest only after every file of every run it lists exists on that store, R2 included. GCS: `publish` checks `RUN_FILES` and, for a run with `drill/meta.json`, every file it implies (`static_drill.tier_files`). R2: the runner's one job copies each listed run (its `drill/meta.json` last), then `r2-verify -m <id>` (every served file of every listed run on R2, same size and md5), then the manifest, chained with `&&`.
  2. Manifests are immutable. Never rewrite a published key; fix forward with a new manifest instead (the next scan's).
  3. A manifest's runs never cover fewer scans with a drill than the last manifest's (`drill_scans`): `publish` refuses otherwise.
- **The drill stage** is in `runs add` (profile `drill`, on in gcs's example): after shards ∥ catalog, before publish (see "Entry point").
- **Drill ∥ anchors** (2026-10-10): the drill and anchors stages run at once (neither reads the other's output: the drill reads the run's `sx/`, `cdelta/`, `sidecar.parquet` and catalog and the earlier runs' `drill/`; the anchors the run's `sx/`, `cdelta/` and the earlier tiers' `names/` / `anchors/`); each keeps its skip-if-done and post-check, a failure of either fails the run once both end, and a rerun submits only the missing one. gcs 2026-10-10 ran them in sequence (drill 2046 s, then anchors 1338 s, of ≈ 98 min); at once the pair is the drill's ≈ 34 min, so ≈ 76 min.
- **T1236's drill** (built after the fact, 2026-10-09 15:40–16:18 UTC; the runner's drill job with `-t` to a scratch trial, staged tree `54acae4b…`): one Batch job, 2 spot n2-highmem-16 tasks, **37 min wall** (long 21 min, short 34 min: 120K probes 12 min, 509 dirs settled 9 min); 3.65 GB on GCS, 3.25 GB served. Verified against brute force from each scan file: **683/683 views on T1236 and 683/683 on 10-09** (389 roots, 294 rollups; `deltas/2026-10-09T1236/drill-verify/`). Copied md5-identical to `deltas/2026-10-09T1236/drill/` (`meta.json` last), then to R2 by the runner's R2 job (16 new keys, `drill/meta.json` last; `r2-verify`: 50/50 served files of both listed runs). No manifest touched: the readers probe `<run>/drill/meta.json`. The site's TS readers over R2 (current `cloud`) see drill tiers `[base, deltas/2026-10-09, deltas/2026-10-09T1236]` and answer `son` / `ab` on all three scans. Cost ≈ $1 (drill ≈ $0.5, brute ×2, R2).
- **Per-scan time:** with T1236's other stages (≈ 28 min), the chain is ≈ 65 min, past the ~30 min target; the drill's short kind is the tail (probes and settling). T1236 was a 12 h delta of 85.7M suffix rows (10-09's daily: 44M).
- Next gcs scan: `2026-10-10` (cron 07:00 UTC) → `runs add -c 2026-10-10`. With T1236's drill built, the stage builds 10-10's and the counter merges T1236 + 10-10 into level 1, drill included.

## Backfill: every listed run's drill and anchors (2026-10-10)

**Incident (cw-s3, generation `2026-10-10cw`).** cw caught up 10-09T1801, 10-10T0001 and 10-10T0601 with an image built before cw's `drill=True` (`abea8e4c`) and before deferred carries: it carried 1801 + 0001 inline into `deltas/2026-10-09T1801_2026-10-10T0001` (level 1), with no `drill/`. The rerun planned a drill only for 0601's own run, and that build failed: it reads the earlier runs' drills (`FileNotFoundError: …/deltas/2026-10-09T1801_2026-10-10T0001/drill/meta.json`). Meanwhile the readers cut the drill stack at the drill-less run, and the view fell to the thresholded walk for heavy literals on 1801, 0001 and 0601 ("no matches · approximate"). The reader now refuses those (`scan-not-indexed`, `39ea488d`).

**Fix (`Runner.backfill`, first thing in `runs add` and `runs merge`).** For each tier the profile asks for (`drill`; `anchors` when the base has `anchors/meta.json`), every run the newest manifest lists that lacks it gets it, oldest first (`backfill_plan`):
- a level-0 run `deltas/<s>`: first the runs of the newest manifest before `s` (what its build reads as earlier tiers, `static_drill build` / `static_anchors run`), recursively, then its own build, the stage's usual job;
- a merged run: each of its scans' level-0 runs (as above), then `static_merge tier -r <key> -t drill|anchors` (`backfill_tier`): `merge_tier` of those level-0 runs' tier into the run's dir, uploaded with its liveness markers last (`drill/meta.json`; `names/sidecar.parquet`, `anchors/start/meta.json`, `anchors/meta.json`). The tier merges are associative, so an N-way merge of level-0 drills equals the carry's (`test_static_drill` stack `backfill-2+1+1`: a merged drill backfilled from separately built level-0 drills, then later runs, equal to the rebuild and to brute force). A run that a later carry folded (listed only by an older manifest that an inner scan's build reads) is merged the same way.
- **Skip-if-done:** a run with the tier is skipped (`drill/meta.json`; `anchors/meta.json`, plus `anchors/start/meta.json` when the base carries one). No existing drill needs rebuilding when an earlier one appears: a build refuses earlier tiers without theirs, so every existing drill was built over a whole stack.
- The drill's and the anchors' chains run at once. A failure fails the run (exit 1), as the drill stage does, and nothing after it runs. A rerun resumes at the failed step.
- **Publication:** the backfilled runs reach R2 by the R2 job that follows: the scan's own (it copies every run its manifest lists, `drill/meta.json` last), or, when nothing is appended and SCAN_ID is not the newest, or in `runs merge`, the R2 job for the newest manifest (`r2_newest`). A revision's runs are copied by the merge stage, as before.
- Once the stack is drilled, drill parity lets the counter carry again: cw's `[L1, L0]` after the backfill has nothing due, and its next scan makes `[L1, L0, L0]` → L2 due.

**cw's command** (the job image at or after this change; `STATIC_NAMES_PROFILE=cw`): `dt-cloud static-names runs add 2026-10-10T0601`. That runs four steps in order: drill builds 1801 and 0001, the merge of `deltas/2026-10-09T1801_2026-10-10T0001`'s drill, the 0601 build. Then the R2 job copies both listed runs and prune runs. Or `runs add -c <next id>` when the next scan is published: the backfill comes first, then that scan's chain. `-n` shows the plan without submitting.

## Deferred carries (2026-10-10)

**Problem.** `publish` ran the binary counter's carries inside the scan's chain. gcs's 2026-10-10 publish did one level-1 merge (2 scans, 114M suffix rows, 3.2 GB) in 806 s: 13 of the run's ~76 min. A carry to level j merges about 2^j scans, and the old counter merged a chain of carries one level at a time (an L4 carry wrote an L1, L2, L3 and L4 in turn: 30 scan-units ≈ 15 × 806 s ≈ 3.4 h). That is past the scan job's 120 min cap, near Batch's 4 h `maxRunDuration`, and fails the whole scan's search-index step. gcs's live stack after 10-10 is `[L0 10-09, L1 T1236‥10-10]`, so the next L2 carry is the second scan after 10-10 (likely 2026-10-12).

**Design.**

- **A scan's publish adds only its own run.** `publish_run`: the newest earlier manifest's runs (a scan's or a revision, below) plus the scan's run at level 0, then `manifests/<id>.json`, written once and last (rules 1–3 as before). It merges nothing, so it takes seconds; the runner calls it in-process (`Runner.publish`), not as a Batch job, which also saves a job's provisioning (~2–3 min) per scan.
- **Carries run apart** (`static_merge.py`), as a `merge` Batch job (`python -m dt_cloud.static_merge carry -g GEN -m /gcs/BUCKET`, 1 task on the profile's machine, Batch's own 4 h cap):
  1. **Plan** (`plan_carries`): the binary counter replayed over the newest manifest's runs (oldest first; `push_run`'s drill-parity rule). Carries that chain at one step fold into **one N-way merge** of every run they consume: `[L1, L0, L0]` is one L2 written from three inputs, not an L1 and then an L2. The stack after is exactly `push_run`'s (`test_plan_carries_ends_where_push_run_does`). No carry reaches the profile's **compaction level** L (`compact_level`, below); a backlog of deferred runs plans as the counter would have.
  2. **Merge** one planned run at a time into its own dir (`build_merged_run`): shards, catalog, drill and names + anchors (each when its inputs carry it, as before), **each tier in its own spawned process** (`-j`, default 4), the DuckDB tiers' memory limits summing to 75% of the machine less 4 GiB per streaming tier (`tier_resources`: n2-highmem-16 → drill 44 GB ∥ anchors 44 GB, 8 threads each), then `meta.json` (the run + the shards' doc). Uploaded with its top `meta.json` last. A dir holding keys this merge doesn't write is refused; a dir whose `meta.json` already names the planned scans and that has every reader file (`complete`) is reused, not rebuilt.
  3. **Publish a revision** (`publish_revision`): `manifests/<id>.m<NNN>.json` (`NNN` = 001…999, fixed width), a new key beside the newest manifest `<id>.json`, with the same `scans` and `date`, the inputs replaced by the merged run, and `rev` / `revises` fields. Written with `if_generation_match=0` only after every file of every run it lists exists and its drill covers no fewer scans (rules 1 and 3). If a scan's publish lands meanwhile, the merge is **rebased** onto the newer manifest (`rebase`: the inputs must still be consecutive runs there, else `Superseded`) and the revision is of that one; the loop re-checks that its revision is the newest after writing it.
  4. Then the next carry is planned from the new newest manifest, until none is due.
- **The compaction level is per deployment profile** (`Profile.compact_level`, shared by both run stores: `STATIC_NAMES_COMPACT_LEVEL` / the interval profile's `append.compact_level` or `INTERVAL_STORE_COMPACT_LEVEL`; an integer ≥ 1, or `none` / JSON `null` for an unbounded cascade, below). Carries stop below level L, so a stack holds at most 2^L − 1 scans in carried runs and a compaction into a new base generation is due after about 2^L scans; the merge stage logs "level L is due" when two L−1 runs sit side by side. The examples set it explicitly (`static_profile_examples` GCS and CW, `interval_profiles/gcs.json`: 5, i.e. 32 scans: ~32 days on gcs, ~8 on cw at 6-hourly scans); `COMPACT_LEVEL` (5) is only the fallback for a profile that sets none. The runner passes it to the merge job (`carry -L <L|none>`), and `plan_carries` / `merge_pending` take it as `max_level`.
- **One merger per generation:** a lease, `gs://<scratch>/static-names/<gen>/merge.lease.json` (`if_generation_match=0`), held for the job's life and released at its end, failure included; a lease older than `LEASE_S` (4 h, the task's `maxRunDuration`) is stale and taken over. A merge that finds it held exits 0 without writing.
- **Readers resolve the newest manifest as before.** Every reader takes the greatest key under `manifests/` matching `<name>.json` in code-point order (TS `latestManifest`: `^manifests\/[^/]+\.json$`, R2 `list` paginated, held per isolate for 5 min; Python `_latest_manifest`, `static_anchors._manifest_runs`, the runner's `manifests()`). `<id>.m001.json` sorts after `<id>.json` (`.m` > `.j`) and before every later scan's manifest (a later id is greater at a character before `.`, or extends `<id>` with `T…`, and `T` > `.`), so **no reader change is needed**: the deployed reader serves revisions as is. Python now filters names explicitly (`static_merge.manifest_keys`: `<scan id>[.mNNN].json`). Cost per request is unchanged: one `list` + one `get` per isolate per 5 min; revisions add about one key per carry (≈ 1 per 2 scans) to a listing of ≤ ~64 keys per generation.
- **Answers and caches.** A revision lists the same scans with an equal stack (a merge's rows are the combine of its inputs'), so every answer, and every cache keyed by the manifest's `date` (`SuffixHits` `<key>@<date>`, the tiers' `version`), stays valid across it. A reader holding the pre-merge state for up to 5 min still reads only runs that exist: nothing is deleted.
- **The store is servable at every instant.** Before the revision, the newest manifest is the one before (every run it lists whole); the merged dir is unlisted, so a partial upload is invisible; the revision is one atomic object create. `test_an_interrupted_merge_leaves_the_store_servable_and_resumes` kills a merge while building, after 5 uploaded files, and before its revision: the manifests are unchanged, the newest one's runs whole and its answers exact, the lease released; the rerun completes it (reusing a wholly uploaded dir), byte for byte as an uninterrupted merge.
- **Prune** deletes only the scratch bucket's earlier open-version states (`state/<prev>/`), as before; it never touches runs or manifests (`prune_plan` refuses any object outside `state/`). A merge deletes nothing either: its inputs stay listed by the older manifests (and the level-0 runs' `cdelta` is what `rebuild-state` and the catalog append read). Garbage-collecting superseded merged runs (on GCS and R2) is not built; it would delete only runs no manifest newer than a grace window (≫ the readers' 5 min TTL) lists.

**The runner** (`runs add`): after the scans' chains (once, not per scan of a catch-up), the **merge stage** (`Runner.carries`): if the newest manifest has a carry due, submit the merge job and watch it for `-w/--merge-wait` seconds (default **0**: submit and go); if it ended, and the newest manifest is now a revision, the R2 job for it (the runs it lists, `drill/meta.json` and `anchors/meta.json` last, `r2-verify -m <id>.mNNN`, then that manifest alone). **Non-fatal**: a failure or an unfinished wait is logged and `runs add` still exits 0; the store is as it was, and the next run plans again (a merge resumes). `-M/--no-merge` skips the stage. A merge the runner didn't wait for publishes its revision on GCS when it ends; the next scan's publish builds on it, so that scan's R2 job copies the merged run with its own (a revision that lands after that publish is rebased onto it, and copied by the next run's merge stage or the scan after). `dt-cloud static-names runs merge [-w SECS] [-n]` is the stage alone (default: watch to the end), for a schedule of its own or to catch up; it exits 1 on a failure.

**R2: each job copies one manifest.** The per-scan R2 job now copies `manifests/<id>.json` alone (it copied all of `manifests/`), so a revision never reaches R2 before its runs are checked there.

**Cost per scan.**

| | before | deferred |
|---|---|---|
| publish | a Batch job; plus the carry's merges in it: L1 806 s (gcs 10-10), est. L2 ≈ 2 × 806 ≈ 27 min after L1 (40 min sequential), L4 ≈ 8 × 806 ≈ 1.8 h after L1–L3 (3.4 h sequential) | in-process, seconds, no merge |
| merges | inside the scan's chain, against its 120 min cap | a separate job, its own 4 h cap; the scan's chain waits `-w` s at most (0 by default) |
| merge writes over 31 scans (scan-units rewritten) | 98 (each chained carry rewrites the levels below) | 64 (one N-way merge per carry): ≈ 35% fewer bytes merged and uploaded to R2 (egress) |
| merge wall time | tiers in sequence (`publish`'s laps) | tiers at once: ≈ the slowest tier's |

At gcs's ≈ 76 min chain, removing the carries leaves ≈ 63 min per scan, carry or not. The biggest merge before a compaction is an L4 (16 scans), which the estimates above put at ≈ 1.8 h sequentially by tier, within the job's 4 h; per-tier parallelism lowers it further (to be measured from the first runs' per-tier laps).

**Readers' fan-out.** With merges keeping up, the stack is the binary counter's (≤ ⌊log₂ n⌋ + 1 runs). While a merge lags, the stack grows by one level-0 run per scan, and the drill's summed slack (`R + 2 · Σ rg`) by 2 · 2,048 per run; a lagging merge is caught up as one N-way merge by the next plan.

**Rollout.**

1. Land on `cloud` (this change). No site deploy is needed first: the deployed readers already resolve revisions (`staticRuns.test.ts` "takes a merge's revision…" runs the unchanged reader on a revision fixture).
2. Each deployment branch merges `cloud`; its scan job image picks up the new `runs add` at its next build. From then: publish adds level-0 runs, the merge stage submits carries. gcs on 2026-10-11 has none due (`[L0, L1, L0]`); the 2026-10-12 scan makes one N-way L2 due (`[L1, L0, L0]` → L2, `test_gcs_live_stack_carries_to_level_2_on_the_second_scan`).
3. Optionally, a deployment's job passes `-w SECS` (its cap less its chain's time) to see the R2 copy of a merge the same day, or schedules `runs merge` apart.

**Rollback.** Revert the `cloud` change and redeploy the job image. The old code reads the new store correctly: its `_latest_manifest` (sorted keys, `stem < before`) and runner (`manifests()[-1]`) take revisions as the newest; its `publish` carries on whatever stack it finds (a deferred backlog included, merged pairwise, as before). One caveat: the old R2 job copies all of `manifests/`, so before rolling back make sure the newest GCS manifest's runs are on R2 (`runs merge` once, or `runs add <newest id>`), else its R2 copy could list a merged run R2 lacks (a broken tier: the readers cut the stack there until it's copied — degraded, not wrong).

**Unbounded cascade (`compact_level: none`, 2026-10-10: supported, no deployment uses it).** The counter carries at every level and never asks for a base rebuild; after n scans the live runs are n's set bits (365 → 6 runs, at most ⌊log₂ n⌋ + 1 = 9). Write cost: each scan's rows are rewritten once per level they pass through, O(log n) amortized per scan, against a capped cascade plus periodic full rebuilds, which costs O(history / 2^L) per scan and so grows linearly with history. Audited before any deployment flips (the examples stay at 5):
- **Format: nothing assumes a maximum level.** Levels appear in no key (`deltas/<first>[_<last>]`), no fixed-width or bit-width field, no reader loop (the TS readers type `level` and ignore it); revisions are ~1 per merge per scan id, far from `REV_DIGITS`' 999; manifest listings are paginated.
- **Breaks: the top merges.** An L_j merge rewrites ~2^j scans: static L1 measured 806 s, L4 ≈ 1.8 h, so L5 ≈ 3.6 h, L6 ≈ 7 h, L8 ≈ 28 h, against the merge task's 4 h `maxRunDuration` and `LEASE_S` (raised together, or a second merger takes over a live lease); the interval store's L1 was 182 s (L8 ≈ 6.5 h). A merged dir resumes only when whole, and `merge_pending` always takes the oldest due merge, so one that can't finish blocks every smaller carry and the stack grows by a run per scan. The 750 GB local SSD also bounds an L8's output plus spill. Unbounded needs a sharded (per key range / per shard, on Batch) top merge first.
- **Breaks, slowly: the static catalog stage.** Each scan's `catalog` reads every scan's `cdelta` since the base (`catalog_cmd`: `days` = every listed run's scans) and suffix-expands the opened rows: O(n) per scan (~57M suffix rows per gcs scan → ~20B a day after a year). Level-0 `cdelta`s can never be collected. The drill's new-member history and `rebuild-state` are linear too, but over paths.
- **Readers: linear in live runs, no worse.** Per tier, warm, the light index makes ~2 R2 GETs per query (sx range, catalog cells), ~6 cold (manifest-time `catalog/meta.json`, `shards.json`, footers, `catalog/index.parquet`): ~24 / ~70 at 12 tiers, within the paid subrequest cap but serialized by the 6-connection limit. The drill holds each tier's whole `aliases.parquet` map in isolate memory (~82.6K long members per tier): measure at ~10 tiers against the 128 MB isolate. The interval reader opens each run (footers + `head`) and its caps (4000 spans, 250 groups, 700K rows decoded) are over the sum of tiers, so more runs mean more "too wide" refusals. The summed bounds loosen linearly: drill dispatch `R + 2·Σ rg_i`, the light filter's `MAX_ROWS` over the summed extent (more literals fall back: correct, slower).
- **Base-only pieces stay correct, but drift never resets.** Catalog V is the base's constant and the incremental catalog equals a rebuild; heavy dirs are sticky and S(q, dir) = base measurement + runs' `dcount`, exact, but base versions' close records never collapse into the base, so S over-counts permanently and more dirs turn heavy (compaction was the reset); `anchors/start/` grows with newly heavy prefixes (correct); the hex-run rule is frozen at the base's, and format changes deferred "to the next compaction" (the split `path`, the interval store's dyadic floor) never land. Interval `ranges.json` covers the whole keyspace (correct; skew only unbalances tasks), but runs have only open/closed segments, so a big merged run's closed groups can't be time-pruned and read amplification grows with churn since the base. The interval store's `reads` sort is base-only: post-base scans may lack last-read ages (to verify).

**Verification** (exact equality throughout). `cloud/tests/test_static_merge.py` over real runs (a one-scan base, four runs, all three fixture flavors): the stack after each publish and merge (`[L0]…[L0,L0,L0]` → revision `[L1, L0]` → publish `[L1, L0, L0]` → revision `[L2]`, one 3-input merge), every manifest (scans' and revisions') read by the tiered suffix reader and catalog against brute force and the rebuild; a merge killed while building, mid-upload and before its revision; a foreign key in the merged dir; a revision refused while a listed run lacks a file; a publish landing mid-merge and right after a revision (stale: rebased; built on the revision: nothing to write); the lease (held, stale); tier merges in processes byte-identical to one process; `plan_carries` vs `push_run` over 1–19 scans. `test_static_runner.py`: publish in-process, the merge stage waited / detached / failing (non-fatal) / skipped (`-M`), `runs merge` exiting 1, the R2 job of a revision, `BatchRunner`'s wait. `test_static_append.py`: prune with revisions and merged runs present. `test_compact_level.py`: plans at L = 3, 5 and unbounded (8 scans; 365 unbounded = one run per set bit), `merge_pending` merging up to the level, `-L` (`3`, `none`, the default 5) on both stores' `carry` and merge jobs, the level-due log, env overrides and validation (14 mutations, each killed). `staticRuns.test.ts`: the unchanged reader on a revision fixture (`deltas/2026-10-01_2026-10-02`, `manifests/2026-10-02.m001.json`). Mutations, each killed: pairwise-only folding, carrying into the compaction level, no missing-files check on a revision, no re-check that the revision is newest, the lease ignored or not released on failure, no reuse of a whole merged dir, `publish` carrying inline, a fatal merge stage, the R2 job copying all of `manifests/`, and (TS) a reader ignoring `.mNNN` names.

**One runner for both stores (2026-10-10, `append_runner.py`; specs/index-landscape.md recommendation 1).** The machinery above is no longer the static index's own: `dt_cloud.append_runner` holds it once, and the static index (`static_runner`, `static_merge`) and the interval store (`interval_append`, specs/interval-store.md §2.7) each plug in their stages and their merged-run build.
- **Shared:** `pending_scans` and exit codes (`NOT_NEXT` 3, a failed stage 1: `exit_on`); `Runner` (`run` → the optional `backfill` hook → each scan's `one` → `carries`; `stage` with skip-if-done and timings, `concurrently`, `job`, `r2_newest`); `BatchRunner` (polls every 10 s doubling to a **30 s** cap — the static chain's was 20→30, the interval store's 10→20); `job_spec` (labels `purpose`, `stage`, `gen`; cost label `component`); `task_command`, `duckdb_args`; run keys, manifest names and `manifest_keys` / `latest_key` (the explicit `<scan id>[.mNNN].json` filter, now the interval store's too: it took any `*.json` by plain `sorted()`); `plan_carries`, `rebase`, `publish_scan` (a scan's level-0 manifest), `publish_revision`, `merge_pending` and the lease over a `RunStore` (`LocalRunStore`, `GcsRunStore(prefix=…)`); `prune_state` (threaded deletes: the static prune's batch delete is gone); the R2 helpers (`r2_copy`, `r2_missing`, `r2_publish`: runs, liveness markers last, check, manifest last; `static_names r2-copy` / `r2-verify` and `interval-store r2-copy` copy through them).
- **Per store:** a `Carry` (`build(dirs, run, outp, …)`, `missing(store, runs)`, `drilled`, `refuse`): static `build_merged_run` (shards, catalog, drill, anchors) with rules 1 and 3 (`static_merge.carry`), interval `build_merged_run` (per range `merge_range`, then `cut_run`) with its `RUN_FILES` (`interval_append.carry`); and the `Runner` subclass's `one` (stage list), `spec`, `merge_job`, `r2`, `drilled`, `backfill` (static only: the drill / anchors backfill).
- Unchanged: every static command line, job and message the tests pin (`test_static_runner.py`, `test_static_merge.py`, `test_static_append.py` pass as they were, but for the prune fake's per-object delete). `push_run` stays in `static_append` as the eager counter the tests compare `plan_carries` against.

## Why

The static name index (`specs/architecture/static-name-search.md`) answers `/names` and the map's `?f=<literal>` filter from R2 alone, but only for the scans of its generation. `2026-10-08c` ends at 2026-10-08. Every newer scan gets a 400 from `/api/name-summary` and the filter falls back to the per-scan path-store walk. Rebuilding a whole generation per scan (~$10 Batch, 136 GB re-copied to R2, ~$16 egress) and keeping a full copy per scan is the cost this design avoids.

The goal: fast, exact `?f=` text search on the main map (treemap, table, diff) at any drill path and any date, so each scan has to join the index when it lands.

## Shape

The base generation stays immutable. Each scan `D` adds one small **run** under new keys. A small immutable **manifest** per scan lists the base plus the live runs. Readers query the base and every listed run in parallel and merge the results. Under a binary-counter policy, runs merge into bigger runs, so a lookup reads at most ⌊log₂ n⌋ + 1 runs after n scans. Periodically, the base and its runs compact into a new generation.

Nothing is overwritten or deleted in place. A day's outputs are new keys, and the manifest that publishes them is a new key too.

## Per scan D: the stages

### 1. Coalesced append, per key range (256 ranges, Batch)

The base generation's coalesced versions (`cintervals/r####.parquet`, `CINTERVAL_SCHEMA`) are runs of `(depth, path, usr)` keyed on `size` and `n_files` alone (`ANSWER_COLS`). A rebuild from the scans with only those change columns gives the same files byte for byte (`build_range(coalesced=True)`, `test_coalesced_from_scans_equals_two_step`). So the append can skip the full intervals:

- `pyrmts.intervals.append_intervals(prev = the range's open coalesced versions, new = scan D's merged rows in the range projected to (depth, path, usr, size, n_files), key_cols = KEY_COLS, state_cols = ANSWER_COLS, stamp = D)`.
- **Only open versions are state.** Closed versions never change, and `append_intervals` only reads `prev`'s open rows and the newest `vf`. The state carried from day to day is therefore `copen/r####.parquet`: the open coalesced versions, sorted `(depth, path, usr, vf)`. That is about one row per live key of the newest scan, not the whole history.
- Outputs per range:
  - `cdelta/r####.parquet`: `CINTERVAL_SCHEMA` + `op int8` (1 = opened at D, `vt` = OPEN; −1 = closed at D, the version's own `vf`/`size`/`n_files` with `vt` = D), sorted `(depth, path, usr, vf, op)`. This is exactly `coalesce_append`'s `cdelta` and the rows `delta_sql` returns.
  - the next `copen/r####.parquet`;
  - `dhist/r####.parquet`: the delta's suffix rows per three-character prefix, for the shard plan.
- The first append reads `prev` from the base's `cintervals/` (`WHERE vt = OPEN`).
- **Where the state lives:**
  - `copen/` goes to the scratch bucket (`gs://oa-gcs-usage-scratch/static-names/<gen>/state/<id>/copen/`, beside `state/<id>/done/r####.json`, each range's marker, written after its `copen`). The bucket has a 7-day age-based delete rule and no soft delete.
  - `cdelta/` goes to the data bucket under the run (`gs://oa-gcs-usage-dvx/static-names/<gen>/deltas/<id>/cdelta/`), kept permanently: it is small (21.9 MB on 2026-10-09), and tier merges write new run dirs without touching the per-scan ones.
- **Only the newest complete state is kept** (`runs prune -g GEN -d ID`, the chain's last stage). A scan's state is a full copy of the open versions (4.4 GB on 2026-10-09), and the next append reads only the newest. Once `state/D/` is complete (every range's `copen/r####.parquet` and `done/r####.json`, for all `ranges.json` `k`) and D's run is published (`manifests/D.json`), `prune` deletes `state/<prev>/` for every prev < D. It never deletes before D is complete, so the only complete state is never deleted, and a rerun of D's append still finds its `prev`. It deletes nothing outside the scratch bucket's `static-names/<gen>/state/` (an object under there whose dir isn't a scan id is an error), and it is idempotent: a rerun finds nothing before D. Without it, the 7-day rule alone would keep about a week of states (~31 GB).
- **A lost state is rebuilt, not stored** (`runs rebuild-state -g GEN -d ID -m MOUNT`, per task like `append`). The open versions after D are the base's `cintervals` open rows, minus every version a run's `cdelta` closed (`op` −1), plus every version one opened (`op` 1), over D's manifest's runs oldest first. A version's identity is `(depth, path, usr, vf)`, and one opened by a run has that run's `vf`, past every base version's. The rebuilt `copen` matches the appended one byte for byte (`test_rebuild_open_equals_the_appended_state`). This is the recovery when the newest state expires: the 7-day rule removes it if the chain stalls for a week. It reads the base's `cintervals` (4.72 GiB over 256 ranges for `2026-10-08c`) plus the runs' `cdelta`s (~22 MB a scan), about what one scan's `append` reads, and writes the same 4.4 GB `copen` the append would. That is cheap enough as a recovery path, so no state needs to be kept beyond the newest.

### 2. Delta suffix rows → the run's shards (1 Batch task)

- Every `cdelta` row at depth ≥ 1 is expanded to its suffix rows (`suffix_sql`), opens and closes alike.
- A close record is the closing version's suffix rows with their final `vt`. It carries the same `size` and `n_files` as the version, so it is self-contained: a reader holding only the record can answer for it.
- DuckDB sorts the rows `(s, path, usr, vf)`. `write_run_shards` streams them into shards in the base's format (format below), closing a shard at the first three-character-prefix boundary past ~50M rows, so no plan pass is needed. The run gets its own `shards.json` (same shape as the base's), `sx/`, `sidecar/` and `sidecar.parquet`.

### 3. Catalog delta (1 Batch task)

- `static_catalog.append(prev, base, deltas, V)` already equals a rebuild byte for byte (`test_append_equals_rebuild`).
- Its `prev` here is the merged catalog of the base plus every live run (the reader's merge, materialized locally: ~25 MB). `deltas` is every run's `cdelta`, oldest first.
- The run's catalog is the rows of the new full catalog that are not in `prev`:
  - the cells dated D, for existing members and one- or two-character literals;
  - every cell of a literal that became a member at D (its whole history);
  - the header row of every literal whose header changed or is new.
- Nothing else changes, because a cell is a running total at its `vf` and appending D only adds events at D.

### 4. Tier merge (when the counter carries, apart: "Deferred carries") and manifest

- **Binary counter.** Runs carry a level; a day's run is level 0. After adding it, while the two newest runs have the same level `k`, they merge into one run of level `k + 1` spanning both. So after n days the live runs are the binary digits of n: at most ⌊log₂ n⌋ + 1 runs, and each day's rows are rewritten O(log n) times in all.
- **Merge.** `pyrmts.runs.merge_sorted` (pyrmts Phase 2, a streaming k-way merge) runs over the runs' `sx` (each run's shard files in prefix order), with `reduce={'vt': 'min'}` on `(s, path, usr, vf)`. The merged stream is re-cut into shards at three-character-prefix boundaries (`write_run_shards`).
  - The catalogs merge with `merge_parquets(tiers, ['q', 'bucket', 'vf'], reduce='newest')`.
  - The `cdelta`s merge with `identity=(depth, path, usr, vf)`, `reduce={'vt': 'min', 'op': 'max'}` (`merge_cdeltas`). The per-scan `cdelta`s are kept anyway: the catalog append consumes one per scan, as point events.
- **Compaction into a new base generation.** When the counter would reach the profile's compaction level (`compact_level`: 5, 32 scans, in the examples), or on demand, the base plus its runs fold into a new generation.
  - The same merge (base first) is cut to the new generation's shard plan (`merge_shards(..., plan=…)`: shard `i` holds prefixes `[lo, hi)`, empty ones written as the build writes them). It is then the full build byte for byte (`test_compaction_equals_full_build`).
  - The catalog merge of base ⊕ runs is likewise the rebuilt catalog byte for byte.
  - At fleet scale this runs per base shard (a shard's prefix range of every run), in parallel on Batch.
- **Publish.** Every new file goes to R2 (`r2-copy`) before `manifests/<D>.json`; the manifest is uploaded last, so a reader never sees a run that is not all there. A merge publishes the same way, through a revision `manifests/<D>.m<NNN>.json` ("Deferred carries").

## Formats

All files are written through pyarrow from a total sort order, in fixed row-group sizes, so a rerun on the same inputs (and image) is byte-identical. Timestamps are UTC.

### Run layout

`static-names/<gen>/deltas/<first>/` for a level-0 run (`<first>` = its scan id), or `static-names/<gen>/deltas/<first>_<last>/` for a merged run covering scans `first … last`:

| Key | What |
|---|---|
| `meta.json` | `{gen, first, last, level, scans: [ids], rows, bytes, shards, catalog: {cells_rows, row_groups, bytes}}` |
| `scans.json` | the run's scans, same shape as the base's (`{bucket, scans: [{id, src, generation, size, md5, crc32c, ts, version}]}`) |
| `shards.json` | the run's own shard plan, same shape as the base's (`shards: [{i, lo, hi, rows, prefixes}]`; `[lo, hi)` over three-character prefixes, never split) |
| `sx/s####.parquet` | suffix rows (below) |
| `sidecar/s####.parquet`, `sidecar.parquet` | per row group `(file, rg, s_min, s_max, offset, length, rows)`, `SIDECAR_SCHEMA`, exactly as the base |
| `catalog/{cells,index}.parquet`, `catalog/meta.json` | the catalog delta (below) |
| `cdelta/r####.parquet` | version-level delta per key range (GCS only; the refold source and the merge input) |

### Suffix rows (`sx/`), base and runs alike

| Column | Type |
|---|---|
| `s` | string (the lowercase name's suffix from a position, ≥ 3 characters, untruncated) |
| `depth` | uint8 |
| `path` | string |
| `usr` | string, dictionary-encoded (`''` = unattributed) |
| `vf` | timestamp(ms, UTC), required |
| `vt` | timestamp(ms, UTC), required; 2106-01-01 (`OPEN` × 1000) while open |
| `size` | int64 |
| `n_files` | int64 |

- Sort order: `(s, path, usr, vf)` in code-point order.
- Row groups: 8,192 rows (`SX_RG`). Compression: zstd. Statistics: untruncated.
- The file is split by three-character prefix, so a literal's rows are one contiguous byte range of one file per run.

**Version identity and close records.** A row is one suffix of one coalesced version. The version is identified by `(depth, path, usr, vf)`; `depth` is a function of `path`. A row's identity is `(s, path, usr, vf)`, which is unique within any one run. A row in a run is one of three things:

- **opened** in the run and still open at its end: `vf` ∈ the run's scans, `vt` = OPEN;
- **opened and closed** in the run: `vf` and `vt` both its scans;
- a **close record**: `vf` before the run's first scan (the version lives in the base or an older run), and `vt` = the run's scan that closed it. The record repeats the version's `size` and `n_files`.

There is no tombstone and no `op` column at this level: a close record is the version's row with its final `vt`.

**Combine rule (the merge's hook):** rows with equal `(s, path, usr, vf)` are the same version's row; the merged row is the one with the **smallest `vt`**. A version's `vt` only ever moves once, from OPEN to a scan, so "smallest" is "newest information". The other columns are equal by construction.

- Applied over base + runs, the combine gives exactly the rows a rebuild through the last run's scan would hold.
- The rule is commutative and associative, so runs can merge in any grouping (the tiers) and the reader can fold them in any order.
- The reader's liveness test `vf ≤ D < vt` is applied after the combine.

### Version delta (`cdelta/r####.parquet`)

- Columns: `CINTERVAL_SCHEMA` (`depth` uint8, `path`, `usr` dictionary, `vf` int64 epoch s, `vt` int64 epoch s, `size`, `n_files`) + `op` int8.
- Sort order: `(depth, path, usr, vf, op)`. Row groups: 65,536 rows. Compression: zstd.
- `op` = 1 is a version opened at the scan (`vt` = OPEN, or the closing scan after a merge). `op` = −1 is a close record (`vt` = the closing scan).
- A merged run's `cdelta` combines on `(depth, path, usr, vf)` with the smallest `vt`, keeping `op` = 1 when the version opened inside the run.

### Catalog delta (`catalog/`)

- `cells.parquet`: `CELL_SCHEMA` `(q, bucket, vf, b, o)`, sorted `(q, bucket, vf)`, 4,096-row groups. `index.parquet` and `meta.json` are as the base's.
- Each literal touched by the run is led by its header `(q, '', 0, rows, n)`:
  - `rows` is its suffix-range rows as of the run's last scan (−1 for one or two characters);
  - `n` is its cell count across the base and every run through this one, so a reader can check a merged lookup.
- Then come its new cells: for an existing member, the cells dated in the run (a bucket's running total at each scan where it changed); for a new member, its whole history.
- **Combine:** union the cells (never duplicated across tiers: a cell is keyed by `(q, bucket, vf)` and each `vf` belongs to one tier), and take the header from the newest tier holding `q`.
- A literal is a member iff any tier holds its header. Membership is monotone: once a member, always one.

### Manifest

`static-names/<gen>/manifests/<id>.json`, one per scan (by scan id), never rewritten; plus `manifests/<id>.m<NNN>.json`, a merge's revision of the newest one (same `scans`, `date`; `rev`, `revises`), never rewritten either:

```json
{"gen": "2026-10-08c", "date": "2026-10-09", "scans": ["2026-07-30", "…", "2026-10-09"],
 "runs": [{"key": "deltas/2026-10-09", "first": "2026-10-09", "last": "2026-10-09", "level": 0, "rows": 0, "bytes": 0}]}
```

- `runs` is oldest first.
- The reader lists `manifests/` (R2 `list`, held per isolate for 5 minutes) and takes the greatest key (a revision sorts after its scan's manifest, before the next scan's). The manifest's `date` versions its caches.
- With no manifest, the reader is today's base-only reader.

## Readers

**Suffix reader** (`staticNames.ts` `StaticNames`, the Python `static_names.Reader`):

- One `StaticNames` per tier (base, then each run), each over its own key prefix. Each run has its own `shards.json` and group indexes, cached under its own prefix.
- `extent(key)` is the sum over tiers. It is an upper bound on rows read, and since rows read ≥ range rows (closes only add rows), a sum ≤ V still proves `key` is not a member.
- `read(key)` fetches every tier's range in parallel and folds each into `FirstHits`. Then the hits are combined by `(path, usr, vf)` with the smallest `vt`.
- Combining at the hit level equals combining the rows. A hit is decided per row by `s` and `path` alone (name match, depth, parent test), so a version's rows are hits in every tier or in none.

**Catalog** (`staticCatalog.ts` `StaticCatalog`, the Python `Catalog`):

- `lookup(q)` runs on every tier in parallel. The header comes from the newest tier holding `q`. The cells are the union, sorted `(bucket, vf)`, and their count must equal the header's `n`.
- `catalogAnswer` is unchanged (the newest cell with `vf ≤ D` per bucket).

**Dispatch** (`nameSummaryStatic.ts`, `staticFilter.ts`):

- The scan list is the manifest's `scans` (else `scans.json`).
- An indexed date's answers never change when a run lands: a later close sets a `vt` after that date. So response cache keys (`staticTag`) stay per generation.
- A literal's hit list spans every date, so it is held and cached per manifest date (`SuffixHits`: `<key>@<date>`; the base alone keeps the bare key).
- `MAX_ROWS` (the filter's bound) applies to the summed extent. A literal near V whose runs add close records can then go over it and fall back, which is correct but slower. Compaction resets this.
- Heavy literals (`HitSource` `heavy`, the drilldown of `specs/architecture/static-name-search.md`) read the drill tiers below. The drill's stack stops at the first run without a live `drill/`; on a scan past it a heavy literal's view is refused (`scan-not-indexed`, at every depth) — never the thresholded walk, which can't see a heavy literal's matches and read as "no matches · approximate" (cw dev, 2026-10-10, scans past the drill-less merged run `deltas/2026-10-09T1801_2026-10-10T0001`).

## Drilldown runs (heavy terms)

The base generation's `drill/` (spec `static-name-search.md`, "Drilldown for heavy terms") answers a catalog member's filtered view at any directory P: its match roots under P (`roots`), or for a heavy `(q, dir)` (more than R = 100K roots under it) per-child running sums with the K = 256 children of largest peak bytes kept by name and the rest summed into a remainder (`rollups`). Each run gets its own `drill/`, in the base's layout and schemas, read beside the base like the suffix runs.

### Per scan D: the `drill` stage (1 Batch job of 2 tasks, after `catalog`, before `publish`)

`python -m dt_cloud.static_drill build -g GEN -d D -k task`: task 0 builds the long kind, task 1 the short, each on a whole machine; each uploads its files and its part (`meta.<kind>.json`), and the second to finish writes `meta.json` from both (`join_meta`). A rerun task whose part is there only joins. The stage is done once `deltas/<D>/drill/meta.json` exists.

1. **Delta roots.** A member's roots at D are first-hit rows of D's version delta, so they are a function of the run's own rows:
   - long members: `static_roots.member_roots` over the run's suffix rows (`sx/`: opens and close records alike) for every long member as of the previous tier;
   - short literals: `short_roots` over the run's `cdelta` (a literal first seen at D simply has no earlier rows);
   - each row is `(q, path, usr, vf, vt, size, n_files)`: an open (`vf` = D, `vt` = OPEN) or a close record (`vf` < D, `vt` = D, the version's own values). Combine rule as the suffix runs: rows equal on `(q, path, usr, vf)` are one root, the **smallest `vt`** wins.
   - **A literal that became a member at D** (a long header new in the run's catalog): its whole history, `member_roots` over its tiered suffix range (base ⊕ runs combined; at most V rows in the base by definition, plus the runs'), written into this run. It starts a class of its own (below).
2. **Classes (aliases).** Base aliases group members with identical root sets. Two members of a class can diverge at D (`x.js` adds a root for `.js` but not for `.json`). Classes only ever split: a root identity `(path, usr, vf)` in one member's set and not the other's never goes away. So the classes at D are the classes before D refined by the digest of each member's delta (`static_roots`' order-free `(n, h1, h2)`), the canonical being the class's least member in code-point order, as the base's. The run's `aliases.parquet` is that map for every long member; only canonical members' rows are written.
3. **Rollups.** For every heavy `(c, dir)` with delta roots under it (heavy = it has a rollup in some tier; it stays heavy), with the previous state read from the tiers (below):
   - per child X: `Δ` = Σ opens − Σ close records under X (bytes, objects);
   - a kept child's new running cell at D, the remainder's (Σ `Δ` of the rest), each only where it changes — the rebuild's cells at D exactly;
   - **kept set.** The rebuild keeps the top K by `(peak bytes desc, child)`. A child outside the kept set lost to the K-th at D−1, and the K-th only rises (kept peaks only grow), so X can enter only if its value on D, `(v_D(X), X)`, beats the new K-th. Candidates are the non-kept children with positive byte `Δ` (and any child with no earlier roots), pruned by `v_D(X) ≤ min(remainder_{D−1}, K-th peak_{D−1}) + Δ(X)`; the survivors' `v_D` are exact: new children's `Δ`, a root child's path total from D's open versions (`copen`), a heavy child's own updated rollup, else its roots under `dir/X` read from the tiers (≤ the dispatch bound);
   - if an existing child leaves (or one with earlier roots enters), the whole `(c, dir)` is **restated**: header, every kept child's cells and the remainder's, over its whole history (the entrant's series read as above, the leaver's from its cells), under a **full header** (`kind` 0). Otherwise the run holds only the cells dated in it under a **delta header** (`kind` −1, so it sorts first, as a full header does). A child with no earlier roots that joins (fewer than K kept) or goes to the remainder needs no restatement (it has no history to move);
   - **header.** `b` (root rows under `dir`) grows by the opens; `o` (children with roots, ever) by the children new at D: a root child is new iff its path never had a version (`cintervals` ⊕ every run's `cdelta`); a directory child that is kept was not new; a non-kept one is new iff no tier holds a root under it before D (an index probe: a group boundary key inside the range settles it, else the group's `q`, `path` decoded). Dirs with no remainder need no probe (every existing child is kept, by name). `vf` (kept count) is `min(K, o)`.
4. **Heavy dirs.** A `(c, dir)` becomes heavy when `S(c, dir)`, its stored root rows summed over the tiers (base ⊕ every run, close records counted), exceeds R; its rollup is then built in full from its roots read from the tiers (`rollup_sql`'s rule, one directory) under a full header. `S` comes from the base measurement (`roots-measure/`: exact per `(q, dir)` with ≥ 10K rows; below that, an exact count from the base index and at most two decoded groups when the runs could push it over) plus each run's `state/dcount-<kind>.parquet` (rows per non-heavy `(q, dir)` in that run, GCS only).
5. **Write.** Roots sorted `(q, path, usr, vf)` and rollups sorted `(q, dir, kind, child, vf)` in 8,192-row groups, files cut at `q` boundaries, the two-level indexes and top files exactly as the base's (`write_indexed`, `write_index_levels`); `meta.json` last.

### Run layout (`static-names/<gen>/<run>/drill/`)

| Key | What |
|---|---|
| `meta.json` | as the base's (`R`, `K`, `rg`, `idx_rg`, `dispatch_rows`, per set files / row groups / rows / bytes), plus `first`, `last`, `level`, `scans`, `classes`, `new_members`, `heavy_new`, `restated`; written last: a run's drill is live iff it exists |
| `aliases.parquet` | `(q, canonical)` for every long member as of the run's last scan |
| `long/roots/r####.parquet`, `short/roots/r####.parquet` | roots, `ROOT_SCHEMA`, in **2,048-row groups** (`rg` in the meta; the base's are 8,192), so each run adds little to the reader's summed slack |
| `long/rollups/r####.parquet`, `short/rollups/r####.parquet` | rollups, `ROLLUP_SCHEMA`; `kind` 0 full header, **−1 delta header** (`vf` kept count, `b` root rows under `dir`, `o` children ever), 1 kept child's cells, 2 remainder's cells; the header is a `(q, dir)`'s first row either way |
| `{long,short}-{roots,rollups}-index.parquet`, `….top.parquet` | the two-level indexes, as the base's |
| `state/dcount-{long,short}.parquet` | GCS only: `(q, dir, rows)` per non-heavy `(q, dir)` with rows in the run |

### Reader (base ⊕ runs)

Tiers are the base and the manifest's runs, oldest first, up to the first without `drill/meta.json`; a scan past it is not covered (a heavy literal there is `scan-not-indexed`). `runs add` keeps every listed run drilled ("Backfill" below), so the cut is transient.

1. Long term `t`: per tier, `c_i = aliases_i[t] ?? t` (each tier's own map). Short: `c_i = t`.
2. **Dispatch.** Sum each tier's roots bound for `[(c_i, P/), (c_i, P0))` (the base's two-level `select`). At most `R + 2 · Σ_i rg_i` (each tier's `rg` from its meta: 116,384 for the base alone, 120,480 with one run, 136,864 with five): read the roots of every tier, combine on `(path, usr, vf)` with the smallest `vt`, and answer as the base. Otherwise `(t, P)` is heavy: since each tier's bound is at most its rows + 2·`rg`, the stored rows exceed R, and the builder has given it a rollup.
3. **Rollups.** Each tier's rows for `[(c_i, P), (c_i, P\0))`; walk the tiers newest first, taking each tier's cells (its rows' first is the header: `kind` 0 or −1), and stop after the first tier whose header is `kind` 0 (it holds the whole history). The header is the newest tier's; the cells are the union; per date each `(kind, child)`'s newest cell with `vf ≤ D`, as the base.

The combine is commutative over roots, and rollup tiers only ever add later cells or restate, so runs merge in any grouping.

### Aliases: why each tier keeps its own map

The other choice, dropping an alias forward (writing the diverged member's own rows from D on), would make the run copy the member's whole base root set (up to 227M rows for `.js`) the day it diverges, and divergence is common in the big nested families. Keeping the base aliasing plus per-member deltas instead costs one map per tier and one rule (`c_i = aliases_i[t] ?? t`, applied per tier, the base's lookup unchanged), and the run rows stay deltas; classes that stay together still share one copy.

### Heavy dirs and exactness

- The roots of base ⊕ runs (per member, combined) are exactly a rebuild's.
- Every rollup holds exactly the rebuild's cells, kept set and header for the same `(q, dir)`.
- Heaviness is by `S` (stored rows) rather than the rebuild's distinct root rows: close records count once per tier until a merge collapses them, and a rollup is never dropped once built. So a run can hold a rollup a rebuild would not have (a directory within its closes of R), and the summed bound can send a few directories near R the other way. Such a view is still exact against brute force: roots are the whole answer; a rollup's kept children are exact and its remainder is the total less them. Compaction resets it.

### Tier merges and compaction

- **Merge** (when the binary counter carries, in `publish`; only runs that all carry `drill/`, `push_run(drilled=…)`): the newest input's alias map; each input's rows re-keyed to the merged canonicals (`alias_i(c)` for each merged canonical `c`: its class at input `i` contains `c`'s) and combined (roots: smallest `vt`; rollups: the newest-full-header rule, the merged header `kind` 0 if any included input was full); `dcount` summed.
- **Compaction** into a new base generation reruns the drill build (`roots digest` → `build` / `short-*` → `index`) on the compacted generation (~85 VM-hours ≈ $19 at today's size); a fold of the runs into the base drill is not built.

### The 2026-10-09 run (measured)

`dt-cloud static-names daily drill build -g 2026-10-08c -d 2026-10-09` (now `runs drill build`) on one spot n2-highmem-16 (`MODULE=static_drill`), first to the scratch bucket, then copied to `deltas/2026-10-09/drill/` (server side) and to R2 with `r2-batch` (`meta.json` in a last pass).

| Step | long | short |
|---|---:|---:|
| delta roots | 84,479,040 (14,143,806 of them the whole history of the 141 new members) | 49,330,377 |
| classes | 35,915 → 35,916 (one split), +141 new | — |
| heavy dirs touched / newly heavy | 13,612 / 423 | 9,345 / 144 |
| probes (children new or not) | 84,381 (26,611 had earlier roots) | 338,492 (94,643) |
| candidates to enter a kept set / dirs settled / restated | 26,691 / 629 / 618 | 98,053 / 648 / 600 |
| rollup rows / `dcount` rows | 242,088 / 10,369,274 | 241,139 / 1,998,647 |
| time | 23.8 min | 38.6 min |

- **Footprint.** 2.15 GB served (long roots 1.28 GB in 2 files, short roots 0.85 GB, rollups 6.6 MB, indexes 7.7 MB, aliases), plus 43 MB of `state/` on GCS only: 0.7% of the base drill's 299 GB. Roots compress to 15 B/row for long and 17 B/row for short, against the base's 6.3 B/row: a day's rows are scattered paths. That is ≈ 2× the light run's 1.1 GB/day, and ≈ 65 GB per 30 days ≈ $1/month on R2.
- **Cost.** The build ran 62 min (one spot VM ≈ $0.25). The steps behind it, at that run's code: history 55 s; probes 7.5 + 20 min, `CachedGroupFile` group reads 0.35 + 0.62 GB from the base files through the mount; settling 7.5 + 14.7 min; newly heavy dirs' earlier roots 38.8M + 12.7M rows. Since then, probes bisect within cached groups and settling preloads its inputs; not yet re-measured. R2 egress ≈ 2.15 GB × $0.12 ≈ $0.26/day. All told ≈ **$0.5/day**, about the light run's.
- **The first attempt** (`sn-drill-build-20261009-a`) was stopped after 85 min. New members' history went through the Python reader (27 min for 12.2M suffix rows; now DuckDB, `history_table`), settling looked up `dr` per directory, and newly heavy rows were inserted one by one.

**Verification** (`deltas/2026-10-09/drill-verify/`):
- **Cases.** 654 `(term, P)` pairs over 46 terms (34 long, 12 short): the base's 455 `drill-cases.jsonl` plus 215 from `drill cases` (what the run changed: per term, its run rollups with a full header first, then a delta header, then parents of its run roots; plus 8 new members). 95 of the cases read a rollup the run restated or built, 37 a delta-header rollup, and 48 a new member.
- **10-09 vs brute force** from the 10-09 scan file (`roots drill-brute -S <run scans.json>`, 1 spot task, 9 min): **653 / 653 equal** (1 plain). 343 were read from roots, up to 110,731 rows; 310 from rollups, 91 of them with a remainder. 507 are non-empty, comparing 1,306,746 children, and 44 cases have more than 200K children.
- **10-08, tiered vs the base alone.** All 605 comparable cases are equal: every child both name is equal, and so are the totals (kept + remainder). That leaves out the 48 new-member cases, which the base drill cannot answer. 546 views are identical. In the other 59, a restated kept set names other children on every date, exactly as a rebuild through 10-09 does.
- **Read cost** (laptop over GCS): 0.40 s median and 5.0 s max per case for both dates.

### Drill time (T2022, 2026-10-10, profiled)

`sn-drill-2026-10-10-2022-215101` (us-east1, 2 tasks × spot n2-highmem-16, `build -k task -p 16 -M 90GB`, prior tiers read through the gcsfuse mount `/gcs/oa-gcs-usage-dvx`; logs saved under `tmp/drill-logs.tsv` in the profiling worktree). The job was created at 21:51:02 and succeeded at 22:25:41: **34.7 min, all of it the short task's**. The long task finished at 22:06:52, 18.8 min before it.

| Stage | long (task 0) | short (task 1) |
|---|---:|---:|
| queue → running, image pull | 58 s | 58 s |
| `pnew` (new paths, DuckDB; **both tasks compute it**) | 61 s | 58 s |
| new members' history (DuckDB) / delta roots + classes | 40 s / 18 s | ≈ 30 s (`short_day_roots`, `meas`, the `tdir`…`nk` SQL) |
| **probes** (Python, serial `reads.any`) | **399 s** (36,370 probes) | **1,068 s** (134,535 probes) |
| **settling** (Python, serial `_value_on` / `_series` reads) | **285 s** (429 dirs, 422 restated) | **780 s** (656 dirs, 625 restated) |
| newly heavy dirs' earlier roots | 25 s (9 dirs, 745,817 rows) | 36 s (18 dirs, 1,498,651 rows) |
| write (`write_kind`, `write_state`) + upload | ≈ 21 s | ≈ 21 s |
| task total | 15.0 min | 33.6 min |

**Where it goes: serial, latency-bound reads.** (Superseded: this diagnosis was wrong. The 10-11 profile below, with the prefetch in place, shows the loops CPU-bound, with ≈ 2 % of the main thread waiting on fetches.) Probes plus settling are 1,848 of the short task's 2,015 s (92%) and 684 of the long's 900 s (76%). Both are single-threaded Python loops that call `TierReads.any` / `.rows` per item, and each cache miss is a `Tier.fetch`: `open` + `seek` + `read` of a 2,048-row group (≈ 53 KB average) through gcsfuse. The short task fetched 25,761 groups (1.37 GB) and 331 index spans (43 MB): 26.1K fetches in ≈ 1,850 s is **≈ 70 ms per fetch**, which is about the whole time. That is GCS first-byte latency, not bandwidth (0.74 MB/s), and not CPU: the decoding (`to_pylist` of each group, 501K decoded `any` checks over 5 tiers) is a few ms per group, ≈ 1–2 min at most. Compute (DuckDB) is ≈ 2 min per task, and writing plus uploading ≈ 20 s. The long task's 133K + 11K fetches, at 11 ms per probe, follow the same pattern. By the 10-09 run's numbers (7.5 + 20 min of probes, 7.5 + 14.7 min of settling), the "probes bisect within cached groups" change helped, but the fetch count still sets the time.

**Levers, by estimated saving on the job's critical path (the short task):**
1. **Concurrent group fetches (≈ −25 to −28 min).** Probes are independent, and settling's reads are known per directory up front. Plan first: walk `select` (the index level, cached; 331 spans) for every probe range, and collect the distinct data groups that the decode path needs. Then fetch them with ≈ 64 in flight (a thread pool; GCS ranged reads release the GIL) into the group cache, and only then run the existing loops over warm caches. 26K fetches at 64-way ≈ 30 s, plus ≈ 1–2 min of decoding, so the short task drops from ≈ 34 to ≈ 5–6 min, and the long from 15 to ≈ 4–5 min. `GROUP_CACHE` (2,048 groups) must grow to hold the prefetched set: 1.37 GB of compressed bytes is well within the 123 GB box. Cache the raw bytes and decode lazily. Settling needs a second plan pass, because `_value_on` / `_series` ranges come from `cand`, not `probes`, but `cand` is fully known before `_settle` loops.
2. **Ranged GCS reads instead of gcsfuse `open` per fetch (part of 1, or ≈ −30% alone).** `GcsTier.fetch` already exists, but `build` uses `Tier` over the mount. A pooled `google-cloud-storage` (or `gcsfs`) client avoids gcsfuse's per-open stat round trip, so roughly 1 round trip per fetch instead of ≈ 2. Alone that is maybe 70 → 40–50 ms per fetch (short ≈ −8 to −10 min). With 1, it mostly matters for throughput under concurrency.
3. **Coalesce adjacent groups (≈ −30–50% of the fetches, alone).** Probes are sorted `(q, dir, child)`, so consecutive misses tend to be neighbouring groups of the same file. One request per contiguous run of needed groups (bounded at a few MB), as `_load` already does for index spans, would cut the round-trip count. This overlaps with 1, which makes it secondary once fetches are concurrent.
4. **Rebalance across more tasks (≈ −18 min alone; small after 1).** Today it is long ‖ short, 15 vs 34 min. Splitting the short kind by `q` range into 2–3 tasks (probes, settling and newly heavy dirs are all per `(q, dir)`, so a `q`-range partition is exact) would give ≈ 12–13 min per short task, and the job would become max(long 15, short/3 + 2.5 setup) ≈ 15 min. It costs per-part outputs (roots/rollups files cut at `q` boundaries already, so parts concatenate), a `dcount` per part, and a `meta` join of N parts. After lever 1 both kinds are ≈ 5 min and setup-bound, so this is only worth it if 1 slips.
5. **Small change: compute `pnew` once (≈ −1 min of VM time, 0 on the critical path).** Both tasks spend ≈ 60 s building it. It could be one SQL output both read, but since both tasks run it concurrently it does not shorten the job.

Not worth chasing: write + upload (≈ 20 s), the DuckDB stages (≈ 2 min), and job start (≈ 1 min, spot provisioning plus image pull).

**Implemented (levers 1–3, `static_drill`):** every serial read loop (probes, settling, base counts below the floor, newly heavy directories' earlier roots) first plans its reads from the index alone and prefetches them, then runs unchanged over the warm caches.
- **Plan:** `TierReads.plan_any` / `plan_rows` list the groups each item decodes. `CachedGroupFile.peek` selects without touching the reads' state, so `meta.json`'s `reads` counts stay what they were without a plan. Settling's plan (`_settle_plan`) is each non-new candidate's roots range, except a heavy child's: exactly what `_value_on` reads (and `_series` reads a subset of it). A probe's plan stops where a tier's index settles it, and holds the later tiers' groups when a tier's decoded rows settle it (a superset there only).
- **Prefetch:** first the index row groups of every item (`prefetch_index`), then the data groups per window, with `-j` (default 64) requests in flight. Requests are coalesced per file (runs ≤ 64 KB apart, ≤ 8 MB each), and footers are fetched alongside. The raw bytes go into a `ByteCache`, an LRU over compressed bytes capped at `-C` GB (default ⅛ of the RAM, ≈ 16 GB on the n2-highmem-16). Decoding is still lazy, through the 2,048-group LRU per file.
- **Windows:** past the cap, the loop runs window by window, each prefetched in turn. A single item past the cap gets a window of its own, and its evicted bytes are fetched in the loop (correct, only slower).
- **Ranged GCS reads:** `build` reads the prior tiers' groups with ranged GCS reads (`Tier(ranged=…)`, one pooled client sized to `-j`) instead of `open`/`seek` through gcsfuse. Everything else still reads the mount. `-F` keeps gcsfuse.
- **Expected effect:** short task ≈ 34 → 5–6 min, long ≈ 15 → 4–5 min, so the job takes ≈ 6–7 min instead of 35. **Measured: no speedup** (10-11, below): the fetches had never been the time.
- **Checks:** output byte-identical on the fixtures (with and without prefetch, under a cache too small for one window, and against the pre-change code). The tests assert that nothing is fetched inside the loops after the prefetch. With 70 ms injected per fetch, a fixture run's build went from 19–49 s serial to ≈ 2.5 s.

### Drill time, corrected (2026-10-11, py-spy)

With the prefetch on (image `0006ba90`), gcs 10-11's drill `sn-drill-2026-10-11-082145` still took 33.5 min. Its short task spent 885 s on 258,986 probes and 829 s settling 651 directories. Every group was prefetched (1,605 coalesced requests, 922 MB, 0 evicted, 0 misses), so the time was not I/O.

**Profile:** `sn-drillprof-2026-10-11-092040` re-ran the short kind as a dry run (`build -g 2026-10-08c -d 2026-10-11 -k short -n`, same image, one spot n2-highmem-16 in us-east1) under `py-spy record --threads --idle -r 50`. It reproduced the run: probes 963 s, settling 850 s, short drill 1,934 s. The raw stacks, flame graphs (`drill-short-main.svg` and `drill-short-all.svg`) and summary are in `gs://oa-gcs-usage-scratch/drill-prof/sn-drillprof-2026-10-11-092040/` and the profiling worktree's `tmp/drill-prof/`. The main thread's 2,103 s break down as follows:

| Where (main thread, self time) | s | % |
|---|---:|---:|
| `CachedGroupFile._read_rgs`: `read_row_group(g).to_pylist()`, every decoded 8K-row group (2K in runs) turned into Python dicts | 790 | 37.6 |
| `static_roots.GroupFile._meet`: per `select`/`peek`, two tuple lists rebuilt over every index entry of the spanned top row groups (≥ 1,024 each), for ≈ 3 tiers × (probe + plan) per item | 471 | 22.4 |
| DuckDB `executemany` row inserts: `rup` (settling's rollup rows) 166, `pr` (probe results) 85, `sr_` 55, `sk` 23, `hk` 22 | ≈ 350 | ≈ 17 |
| the rest: SQL stages, `_settle_ctx` fetches, writing, imports | ≈ 450 | ≈ 21 |
| waiting on the prefetch pool | 41 | 2 |

The 10-10 per-fetch arithmetic (≈ 70 ms × 26K fetches ≈ the loop time) matched by coincidence. The real per-unit costs were ≈ 27 ms to decode a group into dicts (29,676 decodes) and ≈ 0.3 ms per index `_meet` (an estimated ≈ 1.5M calls).

**Fix (`static_drill`, output byte-identical):**
- **Decoded groups stay Arrow:** `group_rows` caches the row group as a `pa.Table`, plus each `q`'s row span (the group is sorted `(q, key)`: dictionary-encoded `q`, `searchsorted`). A `q`'s key list is built on its first bisect. `rows_in` converts only the matched slice to dicts. `any` uses `has_in`, which decodes the same groups as before (so `meta.json`'s `reads` counts are unchanged) and converts nothing.
- **Index spans memoized:** `CachedGroupFile._span(a, b)` builds the entries and their first/last key lists once per span of top row groups, and `select` and `peek` bisect them. `select` still `_load`s, so the counts are unchanged.
- **Arrow inserts:** `insert_rows(con, table, rows)` registers one Arrow table typed by the DuckDB table's schema and runs `INSERT … SELECT`, replacing every `executemany` in the module. 267K rollup rows take 0.13 s instead of 25.8 s.

**Measured locally** (`tmp/bench_drill_reads.py` in the profiling worktree: a 3M-row base at rg 8,192 / `IDX_RG` 1,024 plus two 150K-row runs at rg 2,048, 39,462 probes and 10,000 `rows` reads through the build's plan, prefetch and windows): probes take 10.3 s before and 2.6 s after, and `rows` 0.85 s before and 0.25 s after. The local index has fewer entries per top row group than production, so `_meet`'s share (and the gain) is larger there.

**Expected (estimated from the profile, not yet measured on Batch; it needs an image with this code):** the short drill drops from ≈ 1,934 s to ≈ 7–9 min. Group decoding goes from ≈ 820 to ≈ 100–150 s, `_meet` from ≈ 470 to ≈ 50 s, and the inserts from ≈ 350 to a few seconds. The long task drops similarly, from 985 s to ≈ 5–6 min, so the job should take ≈ 10–12 min instead of 33.5. The next lever is still lever 4 (splitting the short kind by `q` range across tasks), now worth ≈ 3–4 min.

**Checks:** the drill, roots, merge, append and runner tests pass (331). Every run drill the fixture world builds (separate runs, counter and backfill tiers, both configs: 316 files) is byte-identical to the pre-change code, and so is `meta.json` apart from build times (`reads` counts included). Mutations are caught: `has_in` true on empty ranges, the `q`-span bisect off by one, and `insert_rows` dropping a row.

### R2 layout for the reader (`oa-gcs-usage-index`)

- `static-names/2026-10-08c/drill/…`: the base (unchanged).
- `static-names/2026-10-08c/deltas/2026-10-09/drill/`:
  - `meta.json` (`rg` 2048, `R`, `K`, `first`/`last`/`level`/`scans`);
  - `aliases.parquet` (all 82,757 long members → their canonical at 10-09);
  - `long-roots-index.parquet` + `.top.parquet` (41 top entries);
  - `long-rollups-index…`, `short-roots-index…`, `short-rollups-index…`;
  - `long/roots/r0000.parquet`, `r0001.parquet`;
  - `long/rollups/r0000.parquet`, `short/roots/r0000.parquet`, `short/rollups/r0000.parquet`.
- Manifest `manifests/2026-10-09.json` is unchanged (it predates the drill). A reader takes a run's drill as live iff `<run>/drill/meta.json` exists. Index `file` paths are relative to the tier's `drill/`, as the base's.

## Implementation

- `cloud/src/dt_cloud/static_append.py`: the stages, `dt-cloud static-names runs {prepare,append,shards,catalog,publish,prune,rebuild-state,verify}` (Batch tasks run `python -m dt_cloud.static_append <stage> …`).
- `cloud/src/dt_cloud/append_runner.py`: the chain's machinery, shared with the interval store ("One runner for both stores" above); tests `cloud/tests/test_append_runner.py` and both stores'.
- `cloud/src/dt_cloud/static_runner.py`: the chain, `dt-cloud static-names runs add SCAN_ID [-c] [-M] [-n] [-t TERMS] [-w SECS]` and `runs merge` (below).
- `cloud/src/dt_cloud/static_merge.py`: the static store's merged-run build and its `Carry` over `append_runner`'s deferred carries (`plan_carries`, `merge_pending`, `publish_revision`, the `RunStore` seam over GCS or local dirs), the merge job's `carry` (`runs carry`), the tier backfill (`runs tier`); tests `cloud/tests/test_static_merge.py` (real runs) and the runner's.
- `cloud/src/dt_cloud/static_profile.py`: the deployment profile (`Profile`); `static_profile_examples.py`: `gcs` and `cw`, worked examples. `cloud/src/dt_cloud/scan_ids.py`: scan ids (`SCAN_ID`, `scan_epoch`, `check_order`).
- `cloud/src/dt_cloud/static_drill.py`: `dt-cloud static-names runs drill {build,query,cases}` (Batch: `python -m dt_cloud.static_drill build …`), tests `cloud/tests/test_static_drill.py`; `merge_tiers` for the binary counter's merges (not yet called from `publish`, nor from `runs add`).
- Tier merges use `pyrmts.runs` (pinned 541bc8e).
- Reader: `site/functions/_lib/staticRuns.ts` (`Tiers`, `TieredNames`, `TieredCatalog`), wired into `nameSummaryStatic.ts` and `staticFilter.ts`; the generation is the deployment's `STATIC_GEN` var (required).

### The unit is a scan

A deployment may scan once a day, every 6 h, or once more on demand; nothing here assumes a cadence. Each scan is one run, keyed by its scan id (`YYYY-MM-DDTHHMM`, or a bare `YYYY-MM-DD` from before a deployment went sub-daily; specs/done/scan-ids-not-dates.md): `deltas/<id>/`, `manifests/<id>.json`, `state/<id>/`. Ids sort in time order (a bare date as its midnight), so "the newest manifest" is the greatest key, and `prune` keeps the newest complete state by id. Two scans of one day are two runs and two manifests.

### Deployment profile

`static_profile.Profile`: layouts (key templates of the scans' `path` sorts, `{id}` and `{gen}`), data and scratch buckets, base generation, R2 bucket and the R2 copy's Secret Manager secret names, GCP project, region, job image, accounts, machine, local SSD, spot, append tasks, an optional staged source tree. `STATIC_NAMES_PROFILE` selects an example (`gcs`, `cw`) or a JSON file; every field's env var (`static_profile.ENV`: `STATIC_NAMES_{LAYOUTS,BUCKET,SCRATCH,GEN,R2_SECRETS,REGION,IMAGE,SA,R2_SA,MACHINE,SSD,SPOT,APPEND_TASKS,SRC}`, `R2_BUCKET`, `R2_ENDPOINT`, `GCP_PROJECT`) goes on top. Nothing defaults to a deployment: a missing field is an error naming its variable. A base's `scans.json` records its layouts; a base from before that (gcs's `2026-10-08c`) uses the profile's.

### Entry point: `dt-cloud static-names runs add SCAN_ID`

```
prepare → append (key ranges, `append_tasks` tasks) → shards ∥ catalog → [drill: long ∥ short] ∥ [anchors] → publish (local: manifests/<id>.json) → R2 (each run in the manifest, its drill/meta.json last; r2-verify; then manifests/<id>.json) [→ verify, -t] → prune
then, once: merge (Batch: the due carries + manifests/<id>.m<NNN>.json; watched -w s, default 0) → R2 of the revision — non-fatal
```

- **Strictly in scan-id order.** The published scans (under the base's layouts) after the generation's newest (base + the newest manifest's runs), through SCAN_ID, are pending. SCAN_ID must be the oldest of them, else exit 3 naming the others; `-c` appends every pending scan in order. A scan already appended reruns only the backfill (below), the R2 copy and prune.
- **Idempotent, resumable.** Each stage is skipped when its output is in the data bucket: `deltas/<id>/scans.json` (prepare), all `ranges.json` `k` of `deltas/<id>/dhist/` (append; `append` also skips done ranges within a job), `deltas/<id>/sidecar.parquet` (shards), `deltas/<id>/catalog/meta.json` (catalog), `deltas/<id>/drill/meta.json` (drill), `manifests/<id>.json` (publish), `deltas/<id>/verify.json` (verify). `r2-copy` skips objects already on R2 (size + md5), so the R2 step always runs. The merge stage plans from the newest manifest each time (nothing due: no job).
- **Writes only new keys:** the scan's run dir, merged run dirs (`deltas/<first>_<last>/`, by the merge job), `manifests/<id>.json` and `manifests/<id>.m<NNN>.json` (`if_generation_match=0`), and the scratch bucket's `state/<id>/` and merge lease. The deletes are `prune`'s (earlier scans' `state/<prev>/` in the scratch bucket, once `state/<id>/` is complete) and the lease's release.
- **Exit status:** 0 done; 3 the scan is not published yet, or an earlier published scan is pending (without `-c`), or `prepare` refuses; 1 a stage failed.
- **GCS and Batch over their APIs** (ADC; no `gcloud`), so it runs inside a scan job's image. Batch jobs are named `sn-<stage>-<id>-<hhmmss>` and labelled `purpose=static-names`, `stage`, `gen`.
- **R2:** one Batch job per scan (every run the manifest lists, each with its `drill/meta.json` last; `r2-verify`; then `manifests/`), as the profile's R2 account, with the R2 key from Secret Manager (`secretVariables`) and the endpoint from its secret or `R2_ENDPOINT`.
- **`-n`:** dry run. It reports each stage as done or that it would run; it submits and writes nothing.
- **Scheduling:** after a scan's `path` sort is written, run `dt-cloud static-names runs add -c <id>` (gcs: from `job/run.sh` after `index-sync`; cw: from `job/cw-run.sh`). On exit 3, try again later; it is safe to run on a timer.

Per scan, by hand (what `runs add` submits):

```bash
dt-cloud static-names runs prepare -g GEN -d ID
python -m dt_cloud.static_append append -g GEN -d ID -n N -m /gcs/BUCKET      # Batch, ⌈k/N⌉ tasks
python -m dt_cloud.static_append shards -g GEN -d ID -m /gcs/BUCKET           # Batch ∥ catalog
python -m dt_cloud.static_append catalog -g GEN -d ID -m /gcs/BUCKET
dt-cloud static-names runs publish -g GEN -d ID                               # manifests/ID.json (no merges)
python -m dt_cloud.static_names r2-copy -g GEN/deltas/ID && python -m dt_cloud.static_names r2-copy -g GEN -o manifests/ID.json
python -m dt_cloud.static_merge carry -g GEN -m /gcs/BUCKET                    # Batch: the due carries, each a revision manifests/<id>.mNNN.json
dt-cloud static-names runs merge [-w SECS] [-n]                                # the merge job, then the R2 job for its revision
dt-cloud static-names runs prune -g GEN -d ID [-n]
MODULE=static_drill SPOT=1 job/static-names.sh run build 1 -g 2026-10-08c -d D [-t gs://<scratch>/…/drill]          # the run's drill/ (meta.json last)
dt-cloud static-names runs drill cases -g 2026-10-08c -r deltas/D -t job/static-names/drill-terms.txt > cases.jsonl     # + drill-cases.jsonl
dt-cloud static-names runs drill query -g 2026-10-08c -r deltas/D -c cases.jsonl -d D > answers.jsonl
MODULE=static_roots SPOT=1 job/static-names.sh run drill-brute 1 -g 2026-10-08c -d D -c gs://…/cases.jsonl -k gs://…/answers.jsonl -S gs://…/deltas/D/scans.json -O …/deltas/D/drill-verify/brute.jsonl
dt-cloud static-names roots drill-verify brute.jsonl answers.jsonl
job/static-names.sh r2-batch 2026-10-08c/deltas/D -o drill/{l,s,a}; then -o drill/meta.json                            # wt/gcs-static's r2-batch
```

Dry runs on 2026-10-09 (UTC, gcs, `runs add -n`): `2026-10-10` and `2026-10-09T1236` exit 3 (not published yet), and `2026-10-09` (appended) reruns only the R2 copy and prune.

## Cost and footprint

**Measured on 2026-10-09** (the first run; spot n2-highmem-16, us-east1):

| Stage | Tasks | Run time | Output |
|---|---:|---:|---|
| `append` (256 ranges) | 16 (+ a 1-task smoke run) | 3.5 min each (≤ 19 s per range) | 1,291,160 versions opened, 387,872 closed, 609,231,660 open; `cdelta` 21.9 MB; `copen` 4.4 GB (scratch; only the newest day's is kept) |
| `shards` | 1 | 1.2 min | 44,125,015 suffix rows, one 1.05 GB shard (25 B/row: sparse paths compress worse than the base's 10 B/row) |
| `catalog` | 1 | 5.6 min | 94,549 cell rows (874 KB): 17,811 headers (17,670 changed), 141 new members |
| `publish`, `r2-copy` | laptop, VM | seconds; 17 s copy | 8 objects, 1.12 GB to R2; the manifest last |
| `verify` (30 terms) | 1 | 30.3 min | 175 / 175 checks equal |

- **Cost.** About 1.4 VM-hours a day without `verify` (~2.2 with it), ≈ **$0.40/day** spot (≈ $0.60 with `verify`), plus ~$0.13/day GCS → R2 egress. End to end, ~15 min after the scan publishes.
- **Footprint.** **~1.1 GB/day** on R2, about 0.8% of a full 135.7 GB copy; 30 days ≈ 34 GB ≈ $0.50/month. A day's suffix rows are 0.33% of the base's 13.2B.
- **Tiers.** Tier merges collapse close records against their opens, so merged runs hold at most the sum of their days. Compaction at level 5 (32 days) folds them into a new base.

### Egress: merges re-upload (corrected 2026-10-09)

The `$0.13/day` above counts only a run's own upload. Every tier merge writes a new run that also goes to R2. GCP bills internet egress on every byte that leaves it: a merged run written on a Batch VM is billed in full when uploaded, wherever its inputs were read from. Reading the merge's inputs from R2 instead of GCS saves nothing, because GCS reads in-region are already free. So under the binary counter a row is uploaded once per level it passes through. Over the 32 scans before compaction that is 5 uploads per row (32 runs at level 0, 16 merges of 2 runs at level 1, …, 2 of 16 at level 4: 5 × 32 run-sizes in all). Compaction then uploads the whole new base again.

| | run (per scan) | uploads per scan, amortized | + compaction | ≈ egress |
|---|---:|---:|---:|---:|
| gcs (1 scan/day) | 1.1 GB sx + 2.15 GB drill | ~5 × 3.25 GB ≈ 16 GB/day | base 136 GB sx + 299 GB drill per 32 days ≈ 14 GB/day | **≈ $3.5/day** |
| cw (4 scans/day, estimated from `2026-10-09cw`'s versions: 10–80M suffix rows a scan at 37.8 B/row) | 0.4–3 GB sx (+ drill, unmeasured) | ~5 × that × 4 ≈ 8–60 GB/day | base 119 GB sx per 32 scans = 8 days ≈ 15 GB/day | **≈ $3–9/day** + drill |

Levers (none built):

- **(a) Merge and compact where egress is free.** R2 charges no egress, so a job outside GCP can read runs from R2 and write the merged run back to R2 for free. GCP then pays egress only once per row, on the run's first upload. For cw, a CoreWeave node does this (cw's own compute; CoreWeave doesn't bill internet egress). The merge (`merge_sorted` + `write_run_shards`, `merge_parquets`, `merge_tiers`) is pure Python/DuckDB over parquet, so it ports as is. It needs an R2 read/write key on that node and the GCS-side bookkeeping (the run's `meta.json`, the manifest) written after the R2 copy. `cdelta` merges stay on GCS (they're small and never served).
- **(b) Merge less often.** A base-`b` counter merges `b` runs of a level into one: each row is uploaded about `log_b n` times instead of `log_2 n` (b = 4: 2.5 instead of 5 over 32 scans), and readers read up to `(b − 1) · log_b n` runs (4 → ~7 at n = 32).
- **(c) No R2 merges between compactions.** Readers fan out over every run since the last compaction: each row is uploaded once plus once per compaction. This is fine for gcs's daily cadence with weekly compaction (≤ 7 runs). It's too many runs for cw at 6-hourly unless compaction is ~2-daily, which re-uploads the base each time.
- **(d) Fewer suffix rows per hash name** (approved 2026-10-09; specs/done/static-hex-runs.md). 79% of cw's suffix rows (1% of gcs's) are suffixes of names holding a run of ≥ 16 hex digits. Indexing such a run only from its start keeps 27% of cw's suffix rows (3.7× fewer), and cuts every upload and merge of them by the same factor.

**Recommended:** (d) for both deployments (cw: a rebuilt generation; gcs: at its next compaction), then (a) for cw on a CoreWeave node, and for gcs (b) with b = 4, plus compaction only when the run count or the summed run bytes cross a bound, not at a fixed level.

### Shard size: the split `path` (planned, with the next compaction)

Measured on `2026-10-09cw` (specs/cw-static-names.md, "Why 3.8× gcs's bytes per row"): the `path` column repeats on every suffix row the name the row's `s` already holds. Storing it as `parent` (dictionary-encoded) + `head` (the name before the suffix) + `tail` (null unless the name's tail differs from `s` in case) gives path = `parent/head + (tail ?? s)` with no second lookup. That is −26 to −33% on cw's shards at zstd 9 against today's writer (39.1 → 26.6, 47.3 → 31.7 and 34.8 → 24.3 B/row on 64K-row samples of three shards); zstd 9 alone gives −7.5%. Nothing else in the format (DELTA_BYTE_ARRAY, 16K/32K groups, dictionaries) moves it by more than 1%.

It is a format change for every `sx` reader and writer (`write_sorted`'s callers, `Reader`, `staticNames.ts` `decodeGroup`/`FirstHits`, the merges' sort key, census/answers, the drill builders). The sort order stays `(s, path, usr, vf)`, with `path` rebuilt for comparison. Land it at a **compaction**, which rewrites the base and would re-upload it anyway, so the re-encode costs no extra egress. Readers dispatch on the file's columns, so old-format tiers (gcs's `2026-10-08c` and its runs) keep working until compacted.

## Verification

**Python** (`cloud/tests/test_static_append.py`, exact equality; the base is the fixture's first three scans, plus two runs, each run separately and merged):

- the open versions after each append equal the rebuild's open `cintervals`; each `cdelta`'s opens and closes are exactly the rebuild's versions opened and closed at D;
- base ⊕ runs under the combine rule holds exactly the rebuild's suffix rows;
- every literal's first hits `(path, usr, vf, vt, size, n_files)` equal the rebuild reader's, so every drill path and date agrees, and per-bucket answers equal brute force on every date;
- base ⊕ runs' catalogs are the rebuilt `cells.parquet` and `index.parquet` byte for byte, including a literal that crossed V on an append;
- a compaction (base ⊕ runs cut to the full build's plan) is the full build's shards and sidecars byte for byte;
- merged `cdelta`s equal the combine;
- `verify_terms` (the real-run check) passes on both runs;
- the counter's levels and merges are a binary counter.

**TypeScript** (`site/functions/_lib/staticRuns.test.ts`, fixture `fixtures/static-runs/gen.py`: a base through 09-01, runs for 10-01 and 10-02 with close records and a literal crossing V). Every literal on every date equals a brute-force oracle through each of:

- the suffix reader, at each manifest;
- the catalog (cells exactly);
- `/api/name-summary`'s dispatch;
- the map filter's per-root live totals.

A new manifest is picked up after the TTL. A mutation that keeps the largest `vt` fails 4 of the 9 tests.

**Real run** (`runs verify`, Batch): brute force from D's scan file versus the base + runs read through the reader logic. Per literal:

- a catalog member: its per-bucket totals;
- otherwise: its live first hits as a list, both whole and under drill roots (`''`, its top buckets, its top depth-2 dirs);
- in both cases: the day before, tiered, equals the base alone.

**Real run, 2026-10-09** (`deltas/2026-10-09/verify.json`): **175 / 175 checks equal**.

- 30 literals: the 22 named build terms, `a`, `_`, `.`, `zz`, `east5`, `us-east1`, `marin-us-c` and `-us-`.
  - 21 were answered statically: **562,054 live first hits** compared list for list, plus **94 drill-root checks** (e.g. `gof` under `marin-us-central1/podcast_audio_top1000_60s_clips`: 9,185 hits).
  - 9 came from the catalog (per-bucket totals).
- The day before (10-08), every literal's tiered answer equals the base's.
- The scan's 609,231,660 keys equal the append's open versions.
- **Dev site** (`dev.gcs.oa.dev`): the registry lists 71 dates through 10-09. `/api/name-summary` on 10-09 reads 2 tiers, and 8 literals' totals equal the Python tiered reader's. The map's `?f=gof` on 10-09 is served by the static filter (`tier: static+fine`), with match counts equal to `verify`'s at the root (9,287), `marin-us-central1` (9,222), its `podcast_audio_top1000_60s_clips` (9,185) and `marin-us-east5` (47).
