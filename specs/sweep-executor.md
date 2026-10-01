# Sweep executor — plan → review → execute (deleting what's marked `sweep`)

The marking phase is done ([mark-sweep-ui.md], [actions-ledger.md]); ~1,108 Ti is marked `sweep` and nothing can actually delete yet ([repo-hygiene-audit.md] ranks this spec #1). This spec designs the deletion system: **manifest-based, three-phase, dry-run-first**, with an admin review UI. We are deleting other people's data with (currently) **no undo** — soft delete is disabled on all six buckets (`retentionDurationSeconds=0`, no versioning, verified 2026-09-01) — so the design leans hard on: pinned inputs, an explicit reviewable manifest, independent cross-checks against the site's own fate totals, a temporary soft-delete safety net, and IAM that physically can't delete until the execute window.

## Principles

- **Nothing deletes except from an approved, pinned manifest.** The manifest is a parquet of exact object keys derived from one scan's listing + one ledger snapshot (`head = max(actions.id)`), both recorded in the plan. No "delete whatever is under prefix X at execute time".
- **Only objects present in the pinned listing die.** Anything written after the scan survives even inside a swept prefix (and overwrites are caught by generation matching, below).
- **Two independent implementations must agree before execute.** The plan's per-fate byte totals must reconcile with the site's `?marks=1` manifest / `mark_totals` (TS `computeTotals`) for the same scan+head — a full-estate cross-validation of the fate fold.
- **Reversibility window.** Enable soft delete (3–7d; window TBD with Percy) on the six buckets immediately before execute; disable after we're confident. Cost ≈ swept bytes × class rate × days/30.4 (computed exactly by `plan`; EM at 3d ≈ $0.7–1.1K, 7d ≈ $1.5–2.5K one-time vs ~$5–8K/mo saved).
- **IAM as a physical interlock.** The executor SA gets `storage.objects.delete` on `marin-*` only for the execute window (granted at go-time, revoked after). Until then every path through the code is incapable of deleting.

## Phase 1 — `gcs-usage sweep plan`

New CLI subcommand (runs on Batch for the full estate; laptop-OK for `-p` scoped runs).

**Inputs** (pinned + recorded in `plan.json`):
- `listing/<date>/<bucket>/*.parquet` — the per-object listing (`bucket, name, size_bytes, created, storage_class_id`).
- The live ledger via `GET /api/actions` (expanded `keeps` + `owners` rows joined to raw actions; regex patterns don't exist yet, so the expanded prefixes are exact). Record `head`.

**Fate resolution** (Python port, heavily tested):
1. Resolve each ledger prefix's *effective* fate: most-recent-wins across its ancestor-or-equal ledger rows (`ts DESC, id DESC`; NULL keep = unmark) — the exact `/api/resolve` rule.
2. An object's fate = the effective fate of its **deepest marked ancestor** (its covering set of ledger rows is identical to that ancestor's, so the winner is the same) — unmarked if none. Implemented as a longest-prefix match of each key against the ~7K ledger prefixes (sorted-array/trie; ~594M keys is minutes of work).
3. **KLC expansion** at object level, mirroring `klcSplits` exactly (`CKPT_NUM_RE = ^(?:step|checkpoint|ckpt|iter|epoch|global_?step)[-_]?(\d+)`): walking down from a `keep_last_ckpt` prefix, at the first level with step-numbered children the max-step child's subtree is kept and **everything else at that node sweeps** (non-step siblings included — that's what the UI painted red for reviewers); step-free levels recurse. A band where the walk finds no steps at all is **unresolved → keep + flagged** (`klc_ambiguous`; the UI's amber). Implemented + spec-tested in `gcs_usage.sweep_plan` (`klc_split`).

**Outputs** → `gs://oa-gcs-usage-dvx/sweep/<plan-id>/` (plan-id = `<scan>-h<head>`):
- `manifest/<bucket>.parquet` — fate=sweep keys: `name, size_bytes, storage_class_id, created, covering_prefix, action_id, actor, ts, klc` (sorted by name; row-grouped for ranged reads).
- `rollup.parquet` — dir-level rollup of the manifest (`dir, depth, bytes, objects, class bytes, actors`) at every ancestor depth — what the review UI drills.
- `plan.json` — scan date, ledger head, per-bucket/per-class/per-actor totals, monthly-savings estimate, **early-delete-fee bound** (per class: bytes younger than min duration × remaining term; `created`-based, so an upper bound — class-transition times aren't in the listing), **soft-delete window cost** at 3/7/14d, KLC-ambiguous totals, and the reconciliation block (plan totals vs `/api/marks/totals` for the same scan+head, with pass/fail).

`plan` is pure read — safe to run anytime, idempotent per (scan, head).

## Phase 2 — review: `/sweep` admin page + sign-off

- **New D1 table `sweep_plans`**: `plan_id, scan, head, created_by, created_ts, status ('planned'|'approved'|'executing'|'executed'|'abandoned'), approved_by, approved_ts, note`. Writes gated by the `admin` scope (new `/api/sweep` routes); reads `gcs`.
- **`/sweep` page** (admin nav): lists plans; a plan view shows `plan.json` headline (totals, costs, reconciliation status, KLC-ambiguous), a drillable rollup table (ranged reads of `rollup.parquet` via the existing `/v1/files` infra), per-actor grouping ("whose stuff dies"), and the KLC expansions spelled out (kept step vs swept steps per run). Deep-drill to raw manifest rows via the `/files` parquet browser.
- **Exclusions are ledger marks, not plan edits.** To pull something out of a plan: mark it `keep` (in the normal UI, from the `/sweep` drill's link-outs) and **re-plan** — a new head → new plan-id. One source of truth; no shadow exclusion list to reconcile.
- **Approval**: admin clicks approve (typed-confirmation of the plan-id + headline bytes) → D1 row. The runbook keeps the human step: post the plan summary to Slack (#gcs-usage) for Percy/David sign-off before approving. Approval is void if the ledger head moves — execute re-checks.
- **Users**: v1 read-only visibility — their existing lens views already show fate; the `/sweep` plan page is admin-only. Self-serve "delete my claimed data now" is explicitly **v2** (same manifest machinery scoped to `owner=me` + their own approval; not built until the admin path has survived a real sweep).

### Attribution gate — approval deletes the sweeper's own slice (2026-09-02)

Band-level `owner_match` on the console is a *plurality* signal and turned out
to be misleading on shared dirs: `marin-eu-west4/checkpoints/` badged "=
sweeper" with Kaiyue at 41% while the other 59% (Percy's `llama-8b-tootsie`,
`coral`, `isoflop-curation`, big unattributed `vlm-*`) belonged to others —
the broad-sweep pattern (Kaiyue/Pranshu dir-level sweeps) that also caused the
9/1 clobber incident. Quantified over all 150 candidate bands: ~258 TB of
band-child bytes are attributed to the sweeper themself, ~320 TB (gross) to
other users, ~79 TB unattributed.

So the manifest pushes the check to **directory level** (`gcs_usage/attr_index.py`,
default ON, `-X` to disable): an approved-band dir is `eligible` only when the
attribution top user of its nearest indexed ancestor (path-index parquet,
run-dir depth) **is one of its sweepers with ≥50% of the subtree's bytes**;
otherwise the new `deferred_attr` category. Approving a band therefore means
"delete the sweeper's own slice of it" — other users' data inside a broad
sweep is a *nomination* deferred to them (they cast their own vote under the
vote model: their sweep makes it unanimous, their keep makes it a conflict),
never deleted on someone else's vote. `candidates.json` bands carry the
per-child split (`attr_match_bytes` / `attr_other_bytes` / `attr_unattr_bytes`)
and the console shows "≈ deletable" per band and on the real-delete confirm.

## Phase 3 — `gcs-usage sweep execute`

Batch job (same image/infra as the snapshot). **Dry-run is the default**; `--for-real` is required for deletion, and hard preconditions are re-verified in code at startup:

1. Plan status `approved` in D1, and ledger head: any post-approval `keep`/unmark action covering a manifest prefix → **abort** ("re-plan"). (Extra `sweep` marks are fine — they just aren't in this plan.)
2. Soft delete on (≥ the configured window, default 3d) on every target bucket (API check) — refuse `--for-real` otherwise (no override flag; enabling it is cheap and deliberate). Orthogonal to everything else here: it's bucket config the executor merely verifies, and `plan` prices the chosen window.
3. SA can delete (probe via a canary: create + delete one object under `tmp/ttl=1d/sweep-canary` in each bucket).

**Delete mechanics** (per bucket, per covering-prefix shard, resumable):
- **Fresh re-list of each swept prefix at execute time** capturing `name, generation, timeCreated` (listings lack generations — this closes the overwrite race): delete only keys in manifest ∩ fresh listing **with matching `timeCreated`**, via `DELETE ?ifGenerationMatch=<gen>`. Keys missing from the fresh list → already gone (log `skipped_gone` — a marked path that no longer exists is a graceful no-op, at the object or whole-prefix level); `timeCreated` moved → overwritten since scan (log `skipped_overwritten`, keep).
- **Drift detection — marks are as-of a past scan; reality moves.** The re-list also surfaces the reverse cases: (a) *new* keys under a swept prefix (written after the pinned scan — never in the manifest, so structurally undeletable here, but their presence means the prefix is live); (b) a KLC band that gained a **new max-step checkpoint** since planning (the planned "last" is no longer last — proceeding would delete older steps while keeping *both* the planned and the new max, i.e. strictly conservative, but the intent is stale). Both are **flagged and raise by default**: drifted prefixes are skipped, reported in the summary (bytes/objects of new data, per prefix), and the run continues on the clean ones. `--drift=proceed` deletes the manifest keys anyway (new keys always survive regardless — that's a structural guarantee, not a flag) for the common case where the drift is incidental; the cleaner path is re-planning against a newer scan, which folds the drift in properly.
- JSON-API batch requests (100 deletes/request), bounded concurrency w/ adaptive backoff (429/503 → EB, same philosophy as the S3 lister's adaptive mode). Object deletes are free-tier ops; the re-list LISTs are the main op cost (small).
- **Deletion log** → `sweep/<plan-id>/deleted/<bucket>/*.parquet`: `name, generation, size, class, created, deleted_ts, shard`. Checkpoint per shard (`_SHARD_DONE` markers) → kill-safe resume without re-listing completed shards.
- **Memory (2026-09-10)**: the manifest is one Arrow table — `name` as `large_string` (35M ~150-byte names overflow `string`'s 2 GB offsets on `take`), `dir` dictionary-encoded (5 GB of repeats → 140 MB), chunks combined once — plus a sorted *index*; each listing root's slice is materialized 256k rows at a time and decisions stream to the log in 64k-row groups. Measured on marin-us-central2's 35.6M-key manifest (node `mgu`): 13.7 GB peak, so the www dispatch's n2-highmem-8 has 4× headroom; the 2026-09-10 runs on n2-standard-8 died at 28 GB (dicts + two pandas copies) and then on the offset overflow.
- Dry-run mode runs the identical pipeline — re-list, intersect, generation match — writing `would-delete/` instead of deleting, plus the same summary. This is the "extensive dry run": it exercises everything but the DELETE verb.

**Post-run**: verification pass (re-list swept prefixes; manifest keys must be gone), summary to `#gcs-usage-alerts` + a human-readable report (bytes/objects deleted per bucket/actor, skips, duration), `sweep_plans.status = executed`. The next daily scan reflects reality; the ledger tombstone pass ([actions-ledger.md] §tombstones, still unbuilt) is follow-up work, not a blocker.

## Testing (before any `--for-real`)

1. **Resolver parity, property-tested**: Python fate of N sampled paths (marked, unmarked, nested-carve-out, KLC) == `/api/resolve` for each, exact equality. Plus unit fixtures for the tricky orderings (recency-beats-specificity, unmark, re-mark).
2. **KLC object-level vs tree-level**: synthetic layouts (HF `checkpoint-N`, `global_step_N`, `step-N`, mixed/none) — exact expected key sets.
3. **Full-estate reconciliation** (the big one): `plan` totals == site `mark_totals` for the same scan+head. Two codebases, one answer.
4. **End-to-end on a scratch bucket**: synthetic tree + synthetic ledger → plan → dry-run → `--for-real` → verify; includes overwrite-race and written-after-scan cases proving they survive.
5. **Prod dry-runs**: full plan + `execute` (dry) on the real estate; review the would-delete log; sit on it ≥ 1 day; re-plan to catch late marks; then the real thing, **one bucket first** (smallest: marin-us-west4), verify, then the rest.

## First real runs — what the dry runs never exercised (2026-09-11)

Three east5-only real dispatches failed in a row, each one layer deeper, before any deletes of consequence; the dry runs (three of them, all green) share none of these layers. Each is now covered by a check the rehearsal runs:

1. **IAM.** The job SA had `roles/storage.objectViewer` only — listing works, `storage.buckets.get` (the soft-delete guard) and `storage.objects.delete` don't. A real run needs `roles/storage.objectUser` (delete + restore) and `roles/storage.legacyBucketReader` (bucket GET) per bucket; Ryan grants them (the classifier won't let Claude). `sweep execute` now preflights all three permissions per bucket before any listing: a real run refuses naming the whole gap, a dry run warns and records `missing_perms`.
2. **The soft-delete guard** read `retention_duration_millis`; the library's `SoftDeletePolicy` has `retention_duration_seconds`. The test fake had invented the attribute. The fake now returns the library's own class; the guard runs in every mode (`soft_delete_days` per bucket) and `record_run`'s `undo_deadline` uses the narrowest measured window.
3. **The first delete batch got a whole-request 503** ("server(s) are not responding") with no retry; 241 deletes had already landed and the log had 0 rows (it flushed at 64k rows or the end). Now `delete_batch` settles each item from its own sub-response (2xx delete, 404 skipped_gone, 412 skipped_overwritten), retries transient answers with backoff (8 attempts), and reports the rest as `delete_failed` in `failed_dirs` — the run continues, the CLI exits 2 after logging and recording. Workers write the log as dirs finish (8k-row chunks on a real run), flushed in `finally`. `sweep reconstruct-log` rebuilt the third run's log from the bucket's soft-deleted listing (241 objects, all in its manifest).

**Rehearsal (do this before a real dispatch after any executor change):** ~250 scratch objects under `gs://oa-gcs-usage-dvx/sweep-rehearsal/<stamp>/` (same 7 d soft delete), a plan dir with `approved` = that prefix and a manifest built from the live listing, `execute_plan(plan, for_real=True)` (the CLI's reclassify drops dvx dirs — no sweep votes), then `sweep undo --no-record gs://…`; expect every object deleted, logged, and restored. Verified 2026-09-11: 250/250/250.

## Colocation and clean stops (2026-09-11, during the first big-bucket runs)

- **Run the executor in the bucket's region.** The eu-west4 run dispatched from us-central1 listed at 0.6 pages/s and deleted at ~200/s while central2 (same VM shape, colocated) did 1,750/s: every listing page and every delete sub-request is a round trip to the bucket. `/api/sweep/dispatch` now places a one-bucket cut in that bucket's region (`BUCKET_REGION` in `_lib/gcp.ts`); an uncut run stays in us-central1. Batch has no us-central2 location, so the central2 cut maps to us-central1 (the first central2 run ran there and still hit the bucket's write ceiling; the 2026-09-11 17:20Z dispatch to us-central2 was rejected). `/api/sweep/jobs` lists every region. **Open:** the daily listing job (`job/batch-submit.sh`, one task per bucket in one us-central1 job) has the same geometry; per-bucket jobs in each region would cut its 2 h too.
- **`gcs-usage sweep stop PLAN_DIR`** drops `PLAN_DIR/STOP`; the executor polls it every 10 s (and handles SIGTERM the same way): roots not yet started are skipped and counted in `interrupted`, everything done is logged and recorded, the CLI exits 130 so the Batch job ends red. A re-run of the same plan finishes the rest (done keys resolve as `skipped_gone`). A job killed from outside (`gcloud batch jobs delete`) gets no such courtesy — its log parquet has no footer — and needs `sweep reconstruct-log`.

## Durable logs, progress, one runs table (2026-09-11, later the same day)

- **Part-file logs.** `<mode>/<bucket>/part-NNNNN.parquet`, one complete file per flushed chunk (8k rows real, 64k dry). Nothing depends on a writer closing: a job killed from outside loses at most the chunk in memory. `read_log` (undo, reconstruct-log) globs the parts and still reads the single `<mode>/<bucket>.parquet` of earlier runs. `sweep reconstruct-log` is now the last resort, not the plan.
- **`progress/<bucket>.json`** every 30 s and at the end: roots done/total, decisions so far, bytes, started/updated, `done`. The console's live rows read it (rate = deletes ÷ elapsed).
- **Stop from the console.** `POST /api/sweep/stop {job_id}` writes `sweep/runs/<job>/STOP` with the dispatch route's identity; the executor's watcher does the rest. A `stop…` button sits on live rows for admins.
- **One runs table.** A dispatch is a run's pre-record state, so `/sweep` shows one row per run: the Batch job (state, region, buckets, elapsed, logs link) joined to the D1 run (totals, undo window) by the job id in the run's log dir; live rows carry a progress bar; D1 runs without a job (CLI, or older than Batch's list) still list.

## Root scheduling (2026-09-11, from central2's tail)

Listing roots are the unit of parallelism (one thread each, `-w`), and they were run alphabetically. Central2 fell from 1,750 to 400 deletes/s over its last two hours: the roots left at the end were a few huge ones, each a single-threaded listing the delete pool waited on. Now: roots run **largest first** (object counts come from the sorted manifest index, a bisection each), and a root over `max_root_objects` (250k) **splits into its children**, repeatedly, unless a manifest object sits directly in it (splitting would skip it). Ryan's framing: a task queue with uneven tasks should be descending by size.

### Beyond roots (not built; on record 2026-09-11)

A root is one sequential listing (GCS pages by continuation token), so a huge *flat* directory can't be split by children and pins a thread. Not the case in this data — largest dirs: 3,335 objects (central2), 365 (eu-west4); p99 a few hundred — and with the child split a root is ≤ 250k objects, ~4 min at a colocated ~1,000 keys/s. If a future round shows a real tail, in order:

1. **Key-range shards.** The manifest is sorted; cut any prefix into equal-sized name ranges and list them in parallel with `startOffset`/`endOffset`. Uniform work units, no tail. Drift ("a dir gained keys → skip the dir") must then be aggregated per directory across shards before that dir's deletes fire. ~a day with tests + rehearsal.
2. **No re-list.** Capture generations in the daily listing and delete straight from the manifest with `if_generation_match`; drift needs a cheaper probe. Belongs with colocating the daily listing job per bucket.

## Non-goals (v1)

- User self-serve deletion (v2, above). — Regex mark patterns (don't exist). — Ledger tombstoning (follow-up). — CW/S3 sweep (separate estate, no marks yet).

[mark-sweep-ui.md]: mark-sweep-ui.md
[actions-ledger.md]: actions-ledger.md
[repo-hygiene-audit.md]: repo-hygiene-audit.md

## Deletion records — executed sweeps as first-class data (2026-09-02)

Marks are intent; deletions are fact, and they deserve their own table + UI
(Ryan's ask, night of the first execution). D1 migration `0015_deletions`:

- **`deletion_runs`** — one row per executor invocation: plan (scan+head),
  head at execution, actor, mode (dry/real), started/finished, totals,
  expected-vs-actual counters (gone / overwritten / drift / ledger-drift),
  **`undo_deadline`** (finish + the soft-delete window) + `undo_state`, and
  `log_dir` — the plan dir holding the object-level
  `{would-delete,deleted}/<bucket>.parquet` logs + summaries (kept in the GCS
  data bucket rather than R2: the `/files` parquet browser and `/v1/files`
  ranged proxy already render there; a second store buys nothing).
- **`deletion_bands`** — the run aggregated per covering band prefix: the
  queryable per-path unit. A path's deletion history = bands ancestor-or-equal
  to it; "deletions of descendants" = the prefix range under it (both hit
  `idx_deletion_bands_prefix`; paginate descendants by keyset).
- The executor records both by default (`--no-record` to skip); recording
  failure warns and never masks a completed run.
- **Undo** — built 2026-09-10: `gcs-usage sweep undo <run_id | gs://log-dir>
  [-p gs://bucket/dir/ …] [-b bucket …] [-n]` restores every `delete` row of
  the run's `deleted/<bucket>.parquet` by its logged generation
  (`objects.restore` with `ifGenerationMatch=0`, so a name that is live again
  is left alone: re-runnable, never clobbers a rewrite), per object on a
  thread pool (a partial failure must be attributable per key — restores
  aren't batched; ~50 ms/object, so a 30M-object bucket is hours, not
  minutes). Refuses dry runs and anything past `undo_deadline` up front.
  Outcomes per key — `restored` / `already_live` / `unrestorable` (no
  soft-deleted copy left) / `failed` (+ error) — go to
  `restored/<bucket>-<stamp>.parquet` + `undo-<stamp>-summary.json` beside
  the run's logs, and D1 gets `undo_state` (`full` when every deleted object is
  live again, else `partial`) + per-band `undone_objects`. The web UI shows
  the window and per-band undo state; the *trigger* stays a CLI/Batch job — a
  Worker cannot restore millions of objects in-request.
- **UI (to build)**: `/api/deletions?path=` (covering + descendant bands,
  paginated) feeding a "deletions here" panel on the drill/pin view; later a
  distinct "swept (executed)" surface in the fate coloring.
