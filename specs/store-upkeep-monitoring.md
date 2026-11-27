# Store upkeep on /health: carries, folds and generations at a glance

Status: design (2026-11-26; Ryan's idea, relayed by the root session), answered 2026-11-27 (§ Answers). [cloud]. Nothing built yet.

Builds on [`/health`][health-spec] (the stores' stacks, coverage, merges in progress), [`maint_runs`][maint_runs] (compactions and merges as `scan_runs` rows) and [scan cost by stage][cost-spec] (upkeep's cost apart).

## Why

The state of a store's upkeep (its carries, its fold, its generation) is visible today only by reading manifests in buckets and Batch job lists. Three misses in two days:

- **gcs, 11-25 → 11-26:** the static index's level-5 fold was due for two days and never submitted: the scan's merge stage submitted the carry detached, `StillRunning` returned before `compact_stage` (fixed: cloud `1b0d304c3`). `/health`'s gauge did show `compaction due`; nobody was looking, nothing said for how long, and nothing alerted.
- **cw, 11-22 → 11-26:** the static fold ran DuckDB out of memory every attempt (12 h apart, ~2.5 h each; [static-catch-up][catch-up]), then a mid-run image promote split one across code versions (fixed: cloud `4d56eb43b`). Each attempt is a `compact` row on /scans, interleaved with the scans; nothing counts them, says they're consecutive, or shows the memory they died at.
- **cw:** `cw-sn-merge` failed 8 times with no surfaced history: merge jobs publish no Batch events (only compaction jobs do), a scan logs `merge: failed, not fatal`, and `merging.json` keeps only the newest failure.

Goal: per deployment, per store, one glance says **ok / carry due / folding / fold due / failing**, with the counts and times behind it, and `#<deploy>-admin` hears about the bad ones without anyone looking.

## Terms

| term | meaning | code |
|---|---|---|
| store | an append-only run store: the interval store or the static name index | `append_runner.Runner` subclasses |
| generation | a base plus the runs its manifests list, `<prefix>/<gen>/` | `scans.json`, `manifests/` |
| carry | runs merged one level up (the binary counter), published as a manifest revision | `plan_carries`, `merge_pending`, row kind `merge` |
| fold | a compaction: base + every run → the next generation | `compaction_why`, `compact_generation`, row kind `compact` |
| upkeep | carries and folds (the `upkeep: carry \| fold` Batch label) | `append_runner.UPKEEP_LABEL` |
| attempt | one upkeep job's run (one `scan_runs` row) | `maint_runs.MaintRun` |

## 1. What exists today, and where

Per store, per deployment:

| state | where | written by | does /health show it |
|---|---|---|---|
| current generation | `<prefix>/CURRENT.json` (GCS and R2; the static index's lineage pointer `static-names/primary-CURRENT.json` where `-u`), `successor.json` in each folded generation | `compact_store`, `r2_publish_gen` | the pinned or resolved gen (`INTERVAL_STORE_GEN` / `STATIC_GEN`) |
| base, its scans | `<gen>/scans.json` | build / fold | yes |
| runs by level | newest manifest `manifests/<scan>[.mNNN].json` (`runs[]`: key, first, last, level, scans, rows, bytes); R2 `uploaded` of every manifest | `publish_scan`, `publish_revision` | yes (stack timeline) |
| carries due | derived: `plan_carries` over the newest manifest | — | yes (`planCarries` port) |
| fold due and why | derived: `compaction_why(runs, policy, drilled, base)` | — | **level policy only**, default level 5 (`healthModel.compaction`); the manifest never carries the policy (`compact_level` is read if present, never written); `every` / `geometric` deployments would read wrong |
| fold due **since when** | nowhere | — | no |
| merge in progress | `<gen>/merging.json` on R2 (the merge lease mirrored, `StatusMirror`): holder, planned merges, heartbeat, `failed` + error | merge job | yes (chip: merging / stuck / failed) |
| merge history | `scan_runs` kind `merge` (status, phases `carry L<n>` …, error, machine, cost estimate) | merge job (`maint_runs.from_env`), when the profile has `record_secrets` | no (`isScanProducer` drops it); /scans lists it among scans |
| fold queued (submitted, VM not up) | `compacting.lease.json` in the **scratch bucket** (holder, `gen`; up to `COMPACT_START_S` = 1 h before the job adopts it) | `compact_stage` | no (the site has no scratch binding) |
| fold running | the same lease (heartbeat); `scan_runs` kind `compact`, `running`, its phase notes (`fold` progress every 30 s, with work-disk and RSS peaks) | compaction job | no |
| fold attempts, durations | `scan_runs` kind `compact` (and `race`): `started_ts`, `finished_ts`, status `succeeded` / `failed` / `nop` (not due, or superseded), `error`, `failed_phase`, `gen` = `<prefix>/<gen>` then `<prefix>/<gen> → <new>`; phases `read`, `due`, `fold`, `upload`, `pointer`, `r2`, `prune`; the staged static fold's helper tasks as child rows (`task_recorder`, `DT_PARENT_RUN`) | compaction job | no; /scans per row |
| a killed attempt (OOM, preemption) | the row stays `running` until `scan_runs.reconcile` marks it `failed: lost: Batch job FAILED; last event: … exit code 137` (on a Batch event for jobs on `events_topic`, else at the next reconcile) | batch-events / reconcile | no |
| last fold | `<new gen>/compaction.json` (`from`, `manifest`, `runs`, `base_scans`, `scans`, `level` / `rule` / `why`, md5s) and its R2 `uploaded`; the succeeded `compact` row | compaction job | no |
| peak memory | text only: the `fold` phase note ("RSS peak N GiB"), `static_compact`'s per-part log lines, `resource_monitor` `DRILL_RESOURCE` lines in Cloud Logging; scan tasks' `oom_note` | jobs | no |
| cost | `scan_runs.cost_usd` (estimate); `upkeep_usd` apart in `scan_cost.report` | `maint_runs`, `scan_cost` | /scans run detail |
| coordinator lease, deferrals | scratch bucket (`coordinator.lease.json`; `defer-alert.json`, static `lag-alert.json`) | runners, scan steps | no |
| alerts | static compaction jobs: any failure / cancel (`batch-events serve`, `ALERT`); interval compaction jobs: the same when `events_topic`; scan step: static `lag` start, `UNMERGED` (> 8 runs) warn, interval deferral start | — | — |

**Not recorded anywhere:**

1. When a fold became due (and so how overdue it is), and that no attempt followed (the gcs case).
2. The count of consecutive failed attempts, for folds or carries (the cw cases): the rows exist, nothing aggregates them per store and generation.
3. Peak memory as a number (only phase-note text; nothing at all for an attempt killed outright, whose last note may be 30 s to minutes stale).
4. The policy in force, on anything the site reads.
5. Any upkeep done **in-process** inside a scan task (`interval-store add`, `static-names add`: cw's interval store, r2's and hf's both stores, the r2 demo): no row of its own, no `merging.json`, no lease; only the scan's `interval-store` / `static-names` phase and the task log.
6. A fold between submit and VM start (only the scratch-bucket lease).

## 2. Data model

Principle: the site derives everything it can from what the readers already read (R2: `scans.json`, manifests, `merging.json`, `compaction.json`) and from `scan_runs` rows the jobs already write. New writes only where a fact exists nowhere.

### 2.1 Derived (no new writes)

- **Due since.** Due-ness is monotone within a generation (runs only gain scans; a carry keeps its scans; under `level` L the two L−1 runs never merge), so the scan that made it due is a function of the newest manifest, the base and the policy: `level` L, the newer run of the first adjacent L−1 pair after `plan_carries` (its `last`); `every Ns`, the N-th scan past the base; `every Nd`, the first scan at least N days after the base's newest; `geometric r`, the ⌈r × |base|⌉-th scan past the base. `since_ts` = that scan's manifest's R2 `uploaded` (`health.ts` `newest()` already lists every manifest's upload time; keep the map instead of only the newest). Python twin `append_runner.compaction_since(runs, policy, drilled, base) -> str | None` beside `compaction_why`, and the TS port beside `planCarries`.
- **Attempts.** `scan_runs` rows of the D1 store (`store` column) with `kind IN ('compact', 'race', 'merge')` and `gen` = `<prefix>/<gen>` or starting `<prefix>/<gen> ` (a finished compaction's `→`), where `<prefix>` is `interval-store` or `static-names`. A fold's attempts since due: `compact` rows with `started_ts ≥ since_ts`; consecutive failures: `failed` rows newest-first until a `succeeded` (a `nop` superseded by a landing scan counts as neither). Carries: `merge` rows of the current generation, same rule. Phases come along (`loadScanRuns` already joins them); a running row's liveness is `/scans`' `liveness` / `isLost` (`QUIET_S`, `LOST_S` = 30 min).
- **Last fold.** The current generation's `compaction.json` (one R2 get) and its upload time; the succeeded `compact` row whose `gen` ends `→ <current gen>` gives duration, machine, cost and phases. Older generations from the succeeded rows' `a → b` (a pruned generation's `compaction.json` is gone).
- **Usual duration.** Median of the store's last 3 succeeded folds (and carries, per level): what "running 2h10m (usual 2h30m)" and the slow-run rule (§2.4) compare against.
- **Machine memory.** From the machine type (`n2-highmem-N` = 8 GiB × N, `-standard` 4, `-highcpu` 1; N2D the same): a small table in `scanRunsModel.ts`; unknown types show none.

Query cost: one indexed D1 read (`idx_scan_runs_started` on `(store, started_ts)`, bounded to the last 90 days) beside what `/api/health` reads now; R2 adds one `compaction.json` get per store. The 60 s colo cache stays.

### 2.2 New on R2 (no D1 change)

- **The policy in the manifest.** Every manifest and revision either path publishes (`publish_scan`, `publish_revision`, `static_local add`, `interval_local add`) carries `"policy": str(Policy)` (`level:5`, `every:7d`, `geometric:1`, `none`). The site's `compaction()` becomes a port of `compaction_why` over it (default `level:5` for manifests written before). A pinned `-C none` append (r2's pinned generation) then reads "folds off" instead of a false "due".
- **`compacting.json`** (Phase 2): the compaction lease mirrored to R2 beside `merging.json`, by the same `StatusMirror` (generalized over the record's name): `queued` from `compact_stage`'s lease take (holder, `submitted_ts`, `why`), `compacting` once the job adopts it (heartbeat every `HEARTBEAT_S`), removed on a clean end, kept `failed` on a raise. It covers the submit → start hour and deployments whose compaction job can't record to D1. Best-effort, as the merge record.

### 2.3 D1

- **Reuse `scan_runs`** for every Batch attempt: no new table.
- **In-process upkeep gets rows** (Phase 4, no migration: `kind` is free text): `interval_local add` and `static_local add` take a `Recorder`; their carry and fold become `merge` / `compact` rows with `parent` = the scan run (`$SCAN_RUN_ID`), `run_id` = `<scan run>:<prefix>:<kind>`, the task's job facts, `cost_usd` NULL (it is the scan task's VM time; [scan-cost-by-stage][cost-spec]'s open item). The upkeep view queries by `kind` and `gen`, so it sees them; /scans nests a parented maintenance row under its scan run, as a task (answer 3).
- **Peak memory as columns** (Phase 5, **a gated D1 change**): recipe `site/migrations/run-mem-peak.sql` adds `scan_runs.mem_peak_bytes INTEGER` and `scan_runs.mem_limit_bytes INTEGER`, `schema.sql` regenerated (`dt-cloud d1-schema dump`), applied to each branch's databases. `MaintRun`'s 60 s heartbeat writes `resource_monitor.memory_snapshot()`'s cgroup peak (else VmHWM) and limit each beat, so an OOM-killed attempt keeps its peak within a minute of the kill; `finish` writes the final one; `reconcile` leaves them. Scan heads (`scan-run supervise`, which has `oom_note`) can fill them too. Per-phase peaks stay in the notes: VmHWM and `memory.peak` are monotone per process, so a per-phase column would say little more than the run's.

### 2.4 The document

`/api/health`'s `stores[]` each gain `upkeep` (`src/healthModel.ts`, DOM-free, shared with the page and read by the alert check):

```ts
interface Attempt {
  run_id: string; kind: 'compact' | 'race' | 'merge'; status: string   // scan_runs.status
  started_ts: number | null; finished_ts: number | null; secs: number | null
  machine: string | null; spot: boolean | null; cost_usd: number | null
  error: string | null; failed_phase: string | null
  note: string | null                       // a running attempt's live phase note
  mem_peak_bytes?: number; mem_limit_bytes?: number   // Phase 5
}
interface Upkeep {
  policy: string                            // from the manifest, else 'level:5'
  status: 'ok' | 'off' | 'carry-due' | 'carrying' | 'fold-due' | 'fold-queued' | 'folding' | 'stuck' | 'failing'
  overdue_s: number | null                  // fold-due only: seconds past since_ts
  fold: {
    due: { why: string; since_scan: string; since_ts: number | null } | null
    attempts: Attempt[]                     // since due, newest first
    failing: number                         // consecutive failed attempts
    running: Attempt | null
    usual_s: number | null
    last: { from: string; gen: string; ts: number | null; scans: number; rule: string; run: Attempt | null } | null
  }
  carry: { due: Carry[]; merging: MergeState | null; failing: number; last: Attempt | null }
  history: Attempt[]                        // the store's last 20 upkeep attempts, newest first
}
```

**Status** (first that holds):

1. `failing`: the newest fold attempt since due failed (`fold.failing ≥ 1`), or the newest carry attempt of the generation failed / `merging.json` is `failed` (`carry.failing ≥ 1`).
2. `stuck`: a running attempt `isLost` (no write for 30 min), a merge record `stuck`, or a running fold past 2 × `usual_s` (my proposal; none when there's no usual yet).
3. `folding` (a running `compact` row or `compacting.json` `compacting`) / `fold-queued` (`compacting.json` `queued`) / `carrying` (merging, or a running `merge` row).
4. `fold-due`: due, no attempt since; `overdue_s` from `since_ts`.
5. `carry-due`: carries due, none running.
6. `off`: policy `none` and nothing due. 7. `ok`.

## 3. The page

On **/health** (it is already "the state of the stores"); /scans keeps the per-job table and gains a pointer.

- **Summary strip** at the top of /health, above Freshness: one chip per store, worst first, each a link to its section (`/health#upkeep-static`). The same strip, compact, in /scans' header (one fetch of `['health']`, which /health already caches): `Upkeep: static failing ×4 · interval ok`.
- **Per store**, a new `Upkeep` block in the existing store section, above the stack timeline (which already draws runs, due carries and merges in progress): status chip; the fold line (due since / running / last attempt); the carry line; the last fold; `history` collapsed.
- **/scans**: a `kind` filter `upkeep` (`compact`, `race`, `merge`); a maintenance run's detail links its store's `/health#upkeep-…`.
- Chips reuse `sr-status` colors: `failing` / `stuck` red / amber, `fold-due` amber (red once overdue past the alert's threshold), `folding` / `carrying` / `fold-queued` blue, `carry-due` grey, `ok` / `off` green / grey.

Desktop (illustrative numbers, cw-shaped):

```
Upkeep   [static: failing ×4]  [interval: ok]

Static name index  2026-10-15cw.c003                         [failing ×4]
  policy     level:5 · 47 scans past the base (6 runs: L4 L4 L3 L2 L1 L0)
  fold       due since 11-22 06:10 (4d ago): level 5: two runs at level 4
             4 attempts, all failed · last 11-26 00:03, failed in fold after
             2h31m: OOM-killed (peak 371 of 384 GiB, n2-highmem-48) · run · logs
  last fold  c002 → c003, 11-14 06:04 · 32 scans · 4h12m · $11.20
  carries    none due · last 11-26 12:40 L2, 6m, ok
  ▸ history (20)
  ──────────────────────────────────────────────── stack timeline (as today)

▾ history
  when         kind     status     took    machine          peak       cost   note
  11-26 00:03  fold     failed     2h31m   n2-highmem-48    371/384G   $4.10  fold: OOM-killed
  11-25 12:02  fold     failed     2h28m   n2-highmem-48    369/384G   $4.02  fold: OOM-killed
  11-25 12:40  carry    succeeded  6m      n2-highmem-16    31/128G    $0.08  L2
  …
```

Phone (≤ 600 px): the strip's chips wrap; each fact is a `dt` over its `dd` (the `sr-facts` grid already stacks); history rows become two-line items (`11-26 00:03 · fold · failed` / `2h31m · 371/384G · $4.10`), not a scrolling table; error text wraps, clipped at 3 lines with a "more" toggle.

Tooltips (`@floating-ui` `Tooltip`, as the page's): the rule (`compaction_why`'s text), `since_scan`, each attempt's phases as a `PhaseBar`.

## 4. Alerts (`#<deploy>-admin`)

One evaluator, **`dt-cloud upkeep check`**: reads `/api/health` with the job's own grant (`site.get_json`, as `fleet-status` does), so the status rules live once, in `healthModel.ts`; applies the thresholds; posts episodes through `disk-tree notify` (each deployment's bot). An episode posts once at its start, replies in its thread as it changes (each further failed attempt: error, peak), and on its end is struck through with when it resolved, as the quota alert is (`digest.resolved_alert`, `faaebb894`). Episode state: a marker per store and kind beside the existing ones (`<prefix>/upkeep-alert.json` in the scratch bucket; the r2 demo, with no scratch bucket, its R2 store).

| episode | fires when (thresholds are my proposals) | level |
|---|---|---|
| fold overdue | `fold-due` (no attempt recorded, nothing queued) 30 min past `since_ts`: it needs Phase 2's `queued` state, without which a fold waiting up to `COMPACT_START_S` (1 h) for its VM reads as never submitted | red |
| fold failing | `fold.failing` ≥ 2 | red; each later failure a reply |
| fold stuck | a running fold `isLost`, or past 2 × `usual_s` | amber |
| carries failing | `carry.failing` ≥ 3, or `merging.json` `failed` across two checks | red |
| carries not running | carries due across ≥ 2 scans with no `merge` attempt (cw 10-24 → 10-29's `merge: false`, which `UNMERGED_RUNS_ALERT` caught only at > 8 runs) | amber |

Where it runs:

- **After the stores' steps in every scan** (a `soft` scan step `upkeep`, or inside `probe -c`): the gcs case would have alerted at the end of the scan whose merge stage skipped the fold (the fold due since that scan's publish, nothing queued).
- **On Batch events** for upkeep jobs: `batch-events serve` already alerts on a failed / cancelled compaction job; merge jobs get `events_topic` too (`Runner.merge_job` → `with_events(…, ALERT)`), and a terminal upkeep event runs the check instead of a bare per-job alert, so the post carries the count; each further failed job is a reply in the episode's thread, not a post of its own (answer 5).
- On a clock: an hourly Cloud Scheduler `upkeep check` per deployment (infra), at Phase 3 (answer 4).

The per-scan run thread (`nominal`) gets one line when a store isn't `ok`: `upkeep: static failing ×4 (fold) · interval ok`.

## 5. Parity

Every deployment gets the page block, the strip, `/scans`' filter and the check. What differs is how each store's upkeep runs:

| | interval store | static name index |
|---|---|---|
| gcs | Batch (`Runner`; recorded, `events_topic`, `merging.json`) | Batch (recorded, events) |
| cw | in-process in the scan task (`interval-store add`, 40 GB) | Batch (`runs add`, fold via `compact_cmd` → `cw-static-submit.sh`; recorded, events) |
| r2, hf | in-process (`interval-store add`) | in-process (`static-names add`) |
| r2 demo (r2.rbw.sh) | in-process in GHA (`daily-ingest.yml`) | in-process in GHA |

What can't be shown, and why:

- **In-process upkeep** (cw's interval store; r2, hf and the demo throughout): no queue (`fold-queued`), no lease or `merging.json` / `compacting.json` (one process; `running` comes from its row once Phase 4 lands); no cost of its own (the scan task's VM time); machine and peak are the task's. Until Phase 4, its `history` is empty and `fold` shows due / last fold from R2 only.
- **The r2 demo** runs in GitHub Actions: no `$BATCH_JOB_UID`, so `maint_runs` records nothing. Phase 4's recorder takes `$GITHUB_RUN_ID` as the run id there if the demo's D1 records runs at all (answer 6); otherwise the demo shows the R2-derived state only and alerts nothing.
- **gcs's static `compact-r2`** (the R2 account's job, when the compaction's can't hold R2 credentials) isn't recorded: a fold whose GCS half published and whose R2 copy failed reads as succeeded with the site still on the old generation. The block flags it: the last succeeded fold's `→ <gen>` isn't the generation R2 serves.
- **Meta stores** (`STORES_JSON` `meta`, their own interval store under `meta/interval-store/`): `/api/health` reads only the deployment's `INTERVAL_STORE_GEN` / `STATIC_GEN`, so they're not covered today either; Phase 6.
- A pinned generation appended with `-C none` shows `off`, not due.

## 6. Phases

Each phase ships on `cloud` and merges into every deployment; tests run in CI (not on the laptop). Assertions are exact (`toEqual` over whole documents and rows, exact message text), per the repo's test rules.

1. **The block, from what exists** (site only). `upkeep` in `/api/health` from R2 + the `scan_runs` maintenance rows; level policy as today; `since_ts` from the manifest uploads; the strip on /health and /scans; /scans' `upkeep` filter. gcs (both stores) and cw (static) light up at once.
   - `src/healthModel.test.ts`: `upkeep()` over three fixtures rebuilt from the incidents: gcs 11-25 (two L4 runs, no `compact` row → `fold-due`, exact `since_scan` / `overdue_s`); cw 11-22 → 11-26 (four failed `compact` rows, one `lost: … exit code 137` → `failing`, `failing: 4`); cw's merges (8 failed `merge` rows → carry `failing: 8`); plus `ok`, `folding` (running row with a fresh `updated_ts`), `stuck` (quiet 31 min), a superseded `nop` counting as neither, a succeeded fold resetting the count, the last fold matched by `→ <gen>`.
   - `functions/_lib/health.test.ts`: `/api/health` over the fake `INDEX_R2` and `sqliteD1('cw')` with those rows: the whole `upkeep` object per store; a row of another D1 store and of the other store prefix excluded.
   - CIC: the local stack of [health-page][health-spec] (`site/./dev --local-db`, `tmp/health-local/gen.py` extended with maintenance rows), dark and light, desktop and phone width.
2. **Policy and the queued fold.** `"policy"` in every manifest (both stores, both paths); `compaction_since` in Python and its TS port, fed one shared case file (`cloud/tests/fixtures/compaction-cases.json`: runs, base, policy → why, since) by pytest and vitest; `compacting.json`, the compaction lease mirrored (queued → compacting → cleared / failed), and the site's `fold-queued`. What the alerts' overdue rule needs.
   - `test_append_runner.py`: each publish path writes `policy`; `compaction_since` over the shared cases (each rule, at and one short of its threshold); `compacting.json` written at `compact_stage`'s take, rewritten on adopt, heartbeat advancing `updated_ts` only, cleared on success, `failed` kept on a raise, write failures never failing the fold. `healthModel.test.ts`: the same cases through the port. `health.test.ts`: queued, compacting, stuck by heartbeat, malformed → none.
3. **Alerts.** `dt-cloud upkeep check`; the scan step; merge jobs on `events_topic` (a terminal upkeep event runs the check; each failed job a reply in the episode's thread, answer 5); the hourly Cloud Scheduler check per deployment (infra, answer 4); the `nominal` line. The thresholds stored in D1 (a recipe: a per-deployment row of `overdue_s`, `fold_failing`, `carry_failing`, `stuck_factor`, the §4 values its defaults), edited on `/admin` (admin only, each edit logged), and read by both the check and `/api/health`'s status rules (answer 2).
   - `test_upkeep_check.py`: episodes over fake `/api/health` documents: start (exact post text), a reply per new failure, resolve (struck through), no repost within an episode, thresholds at and one past each edge; a site it can't read exits 3 and posts nothing.
   - Mutation check: each threshold `>` ↔ `≥`, the since rule off by one scan, the episode marker ignored, `queued` not counted as an attempt: each fails a test.
4. **In-process upkeep recorded.** `interval_local add` / `static_local add` record `merge` / `compact` rows under the scan run. cw's interval store, r2 and hf light up.
   - `test_interval_local.py` / `test_static_local.py` over `SqliteSink`: an `add` that carries and folds writes exactly those two rows (ids, `parent`, `gen` before and after, phases), none for an `add` with nothing due; recording off (`$DT_RECORD=off`) writes none and changes no store byte.
5. **Peak memory as columns** (deferred, answer 7: the notes' `RSS peak` is parsed until a scan head wants the column; gated D1 recipe `run-mem-peak.sql`, Ryan's go and each database's apply). Heartbeat and finish write the peak, so an attempt killed outright keeps it.
   - `dt-cloud d1-schema check -p`; `test_maint_runs.py`: the heartbeat's write carries the snapshot's peak and limit, a cgroup v1 host's too, a host with neither writes NULL; `reconcile` keeps them; `healthModel.test.ts` formats `371/384G` and none.
6. **Meta stores and the fleet.** `/api/health?store=meta` (each `STORES_JSON` store with its own generations); `dt-cloud fleet-status` gains an upkeep column (worst store per deployment).

## Answers (Ryan, 2026-11-27: "Your rec sg" on every question)

1. **/health vs /scans:** a block on `/health` plus a strip on `/scans` (§3), not a page of its own.
2. **Thresholds:** as proposed (§4): overdue at 30 min, failing at 2 folds / 3 carries, stuck at 2 × usual. Ryan also wants them **stored in D1 and editable on `/admin`** (per deployment, so cw's 12 h retries can have their own): that is Phase 3's, beside the check that applies them. Until then they are constants (`healthModel.ts` `OVERDUE_S`, `STUCK_FACTOR`; the alert counts with the check).
3. **In-process rows on /scans:** a parented `merge` / `compact` row (Phase 4) nests under its scan run, like a task, not a row of its own.
4. **A clock:** later, at Phase 3, and infra: an hourly Cloud Scheduler `upkeep check` per deployment (for a store whose scans stop), beside the scan-end and Batch-event checks.
5. **Per-job alerts:** yes: once the check posts episodes, `batch-events`' per-job compaction failure alert becomes a reply in the upkeep alert's thread (one post per failure streak).
6. **The r2 demo:** its R2-derived state (due, the last fold; no history, no alerts) is enough until Phase 4 settles whether its D1 records runs (the question's implied recommendation; not separately answered).
7. **Peak memory:** parsed from the run's phase notes (the `fold` note's `RSS peak N GiB`) for now; the `run-mem-peak.sql` columns (Phase 5) wait until a scan head also wants them.

[health-spec]: done/health-page.md
[maint_runs]: ../cloud/src/dt_cloud/maint_runs.py
[cost-spec]: scan-cost-by-stage.md
[catch-up]: static-catch-up.md
