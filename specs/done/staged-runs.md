# `/staged` runs: port the retired sweep console's run features

**Context:** the mark & sweep UI was retired in `9675f7d5` (`SweepPage.tsx`, gcs's mark-era console; `PlanSweepPage.tsx`, cw's plan-first console). `/staged` (`site/src/StagedPage.tsx`) is the plan-first console both deployments now share, but its runs table kept only run / mode / state / deleted / gone / started, and stop / undo / purge rendered only for the plan-first executor (`planFirst`), so gcs had none of them.

**Goal:** every run- and plan-related feature of the old consoles is either on `/staged`, ported here, or obsolete with the marks model (and says why). Everything ported is generic: it keys on the deployment's executor capabilities (`EXEC_CAPS[Store.executor]`), never on a deployment name.

## Audit

Status: **already** = on `/staged` before this spec; **port** = ported here; **obsolete** = gone with the marks model (reason given).

### `SweepPage.tsx` (gcs, mark era) and the routes it called

| # | Feature | Status | Notes |
|---|---|---|---|
| 1 | The latest baked plan (`sweep/latest.json`, `sweep/<plan>/candidates.json`) | obsolete | The plan is the D1 staged plan; nothing bakes candidate bands from marks any more. |
| 2 | Band approvals, slice / full (`/api/db/sweep_approvals`), revoke, per-row ✓ buttons, bulk `a` / `r` keys | obsolete | Staging is the proposal and an admin's dispatch is the sign-off; slice vs full was the ownership gate the plan model dropped ("the plan is the whole intent"). |
| 3 | ≈deletable (attribution cap), `owner_match`, top owner + share columns | obsolete | No ownership gate to estimate; `/staged` shows each row's owners. |
| 4 | Band conflicts expansion (other owners' bytes under a band) | already | The owner(s) column and the user-coloured treemaps (everything staged / the selection). |
| 5 | Hide bands with nothing slice-deletable | obsolete | No slice notion. |
| 6 | Status axis (approved / todo) | obsolete | An item is staged or it isn't. |
| 7 | Filter box (path, user, `owner:`, `sweeper:`, `is:`) | already | `?q=` with `owner:` / `staged-by:`. |
| 8 | Paging + page size, `j`/`k` selection, `⇧x` page, ⌘-click | already | Per-batch paging, `useRowSelection` / `useRowSelectionKeys`. |
| 9 | "How approvals work" intro (remembered closed) | obsolete | Replaced by `/staged`'s one-paragraph explainer. |
| 10 | Plan line (plan, head, approved bytes) | already | Header: prefixes · bytes · objects · plan # · open since. |
| 11 | Dispatch summary (bands, bytes, objects, FULL count, per sweeper) | already | Count / bytes / objects are the header; FULL and per-sweeper are obsolete with #2. |
| 12 | Bucket cut ("limit this run to some buckets") | port | Capability `bucketCut` (gcs: a plan may span buckets; cw runs one bucket per run). The cut's prefixes are what the digest and the real gate name, so a real run needs a dry run of the same cut. |
| 13 | Dry-run / armed real dispatch | already | |
| 14 | "Submitted `<job>`" line | already | `67c2c2a7`. |
| 15 | Dispatch errors with status + body snippet | already | `plans.ts` `call()`. |
| 16 | One row per run: the Batch job joined to its D1 run by the run's log dir, plus jobs with no row yet | port | `/staged` keyed jobs by `run_id`, which never matches a gcs run id (`<scan>-p<plan>/<stamp>`): gcs rows had no live state and no Batch logs link. Now joined by the log dir's job id (`runJobId`); a plan's job with no row yet (`PLAN_ID` in the job env) is a row too. |
| 17 | Columns: by, buckets (+ "ran in" region note), planned, deleted, gone, overwritten, drift, state (+ why), started, elapsed, undo by | port | |
| 18 | Live progress from `progress/<bucket>.json` (bar, deletes, bytes, rate, roots), "planning…", "no progress file" | port | Capability `runFiles` (the run dir is readable through the deployment's `/v1/files`). |
| 19 | Links: plan → `/files`, log → `/files`, Batch logs ↗ | port | `/files` links need `runFiles`; Batch logs for both. |
| 20 | Stop (gcs: the STOP file) with a "stopping" state | port | `/api/sweep/stop` existed; the UI now sends the job id. |
| 21 | "Dispatch isn't configured" note (`jobs.configured === false`) | port | |
| 22 | Runs pager | port | |
| 23 | Run row anchors (`#run-…`) | port | |
| 24 | Scroll-spy hash over bands / dispatch / runs | obsolete | One runs section; the bands section is gone. |
| 25 | "refreshing…" indicator | port | |

### `PlanSweepPage.tsx` (cw, plan-first)

| # | Feature | Status | Notes |
|---|---|---|---|
| 26 | Plans list (item / run counts), pick a plan | port | Read-only `?plan=<id>` picker: closing a plan must not hide its runs (and their undo windows). |
| 27 | Create a named plan | obsolete | Staging opens the shared plan; named plans stay the admin API (`POST /api/plans`). |
| 28 | Close plan | port | Admin, armed; the next trash gesture opens a fresh plan. |
| 29 | Plan note | port | |
| 30 | Add prefixes (textarea) | obsolete | Staging is the trash gesture / `POST /api/plans/stage`; admin curation stays the API (`POST /api/plans/:id/items`). |
| 31 | Add sweep-marked | obsolete | Marks are retired. |
| 32 | Remove an item | already | Unstage. |
| 33 | Read-only note for non-admins | already | |
| 34 | Dispatch with a scan picker | already | |
| 35 | Runs: stop / undo / purge with their window rules, "undone" / "purged" tags, overwritten column | already (cw) → port (gcs) | gcs gets stop and undo (new `/api/sweep/undo`). Purge is obsolete on gcs: GCS soft delete expires the deleted generations itself once the retention passes, so there is nothing to purge (`purge_state` stays `'none'`, as the schema says). |

### Routes

| # | Route | Status | Notes |
|---|---|---|---|
| 36 | `/api/db/deletion_runs` (the runs list) | already | `/api/plans/staged` (and `/api/plans/:id`) carry the plan's runs. |
| 37 | `/api/sweep/jobs` | already → extended | Also lists `gcs-undo-*` jobs (`op`, `target`) and each job's `plan_id`; `/api/plan-sweep/jobs` lists its undo / purge ops the same way. |
| 38 | `/api/sweep/stop`, `/api/plan-sweep/{stop,undo,purge}` | already | |
| 39 | `/api/sweep/undo` | port | New, below. |
| 40 | `/api/sweep/purge` (gcs, before `a5fd26b5`) | obsolete | As #35. |

### Asked for alongside

| # | Feature | Status | Notes |
|---|---|---|---|
| 41 | Run detail + verification: decisions summary, planned vs logged, D1 totals, undo deadline, per-band rows | port | The old page spread these over its columns and `/files`; a run row now expands into them, with checks (logged = D1, planned = accounted). |
| 42 | Stage batches emptied by absorption | new | A collapsed line: "N prefixes absorbed into <later batch>" (from `admin_edits`). |

**Counts:** 16 port, 1 new, 14 already, 11 obsolete (#35 counts as a port, its gcs purge half as obsolete-in-place; #37 as already).

## Design

### Executor capabilities (`site/src/plans.ts`)

`EXEC_CAPS: Record<Store['executor'], ExecCaps>`:

| cap | `plan-sweep` (cw) | `sweep` (gcs) | meaning |
|---|---|---|---|
| `stop` | ✓ (cancels the job) | ✓ (STOP file) | a live run can be stopped |
| `undo` | ✓ | ✓ | a real run can be undone inside its window |
| `purge` | ✓ | — | a real run's deleted versions can be purged after the window |
| `bucketCut` | — | ✓ | a dispatch may name a subset of the plan's buckets |
| `runFiles` | — | ✓ | the run dir (plan / progress / summaries / logs) is readable through `/v1/files` (gcs's files proxy serves the data bucket; cw's serves its R2 index store, and its run dirs live in GCS) |

`runJobId(run)` = the job id that ran a run: the last segment of its log dir (gcs: `…/sweep/runs/<job>`, cw: `…/sweep/cw/runs/<job>` = its run id).

### `/api/sweep/undo` (gcs)

`POST { run_id }`, admin. Refuses (before touching GCP): a malformed run id (400), no such run (404), a dry run (400), a run not finished (409), already undone (409), no recorded undo window (409), the window closed (409). Otherwise submits `gcs-undo-<stamp>z`: the dispatch's job spec (`sweepJobSpec`: same image, job service account, machine, `CLOUDFLARE_ACCOUNT_ID` / `DATA_BUCKET` / `SITE_URL` / `USER`, secrets `SITE_TOKEN` + `CLOUDFLARE_API_TOKEN`) with `OP=undo` / `TARGET_RUN=<run_id>` and the script `dt-cloud sweep undo "$TARGET_RUN"` (D1 lookup → its own deadline check → restore → `record_undo`), the exit trap pinging `/api/sweep/jobs`. It runs in the run's buckets' region (from `deletion_runs.buckets`, else its bands' buckets). On submit it records `undo_state = 'partial'` on the run row, as plan-sweep's undo does; `record_undo` sets the final state (`full` / `partial`).

IAM: none new — the dispatch account already submits Batch jobs and acts as the job account; the job account already restores (`storage.objects.restore` is in `objectUser`, and a real run's preflight checks it) and records to D1.

### Run detail (`GET /api/plans/run?id=<run_id>`)

`{ run, bands }`: the D1 row and its `deletion_bands`. The client adds, when `runFiles`: `plan-summary.json` (planned), `<deleted|would-delete>-summary.json` (logged decisions per bucket: deletes, gone, overwritten, failed, drift dirs, interrupted roots, soft-delete days). `verifyRun` checks logged deletes / bytes / gone / overwritten against D1, and planned objects against what the run accounted for.

### Emptied batches

`planDetail` (staging deployments) adds `emptied`: each stage batch with no items left, with what became of what it staged — absorbed into a later batch (an insert audit whose `old_json.absorbed` names it), unstaged (a delete audit), or covered from the start (the gesture's `covered`). `/staged` renders each as one collapsed line among the batches.

## Done

- Server (`90edcb68`): `_lib/sweepUndo.ts` + `api/sweep/undo.ts`; `sweepJobSpec` / `submitSweepJob` / `undoScript` / `RUN_ID_RE` / `jobStampOf` in `_lib/sweepDispatch.ts` (the dispatch now also sets `PLAN_ID`); `sweepJobView` + undo jobs in `api/sweep/jobs.ts`; ops in `api/plan-sweep/jobs.ts`; `runDetail` + `emptiedBatches` in `_lib/plans.ts`, `GET /api/plans/run?id=` in `api/plans/[[path]].ts`.
- Client (`0c00b27d`): `src/runs.ts` (pure: `EXEC_CAPS`, `runJobId`, `joinRuns`, `plannedOf`, `sumProgress`, `loggedOf`, `verifyRun`, `runControls`, …), `src/StagedRuns.tsx` (runs table + run detail), `src/plans.ts` hooks (`usePlanList`, `useClosePlan`, `useRunDetail`, `useRunFiles`, `useStagedPlan(live, id)`, the bucket cut in `useDispatch`, `RunAction`), `src/StagedPage.tsx` (plan picker, close plan, bucket cut, emptied lines, the runs section).
- Tests: `functions/_lib/stagedRuns.test.ts` (undo gate, run buckets, the undo route end to end with a generated SA key and stubbed GCP — refusals before any GCP call, the exact submitted spec, the row update, a failed submit; `sweepJobView`; `/api/plans/run`; emptied-batch replay, pure and through `/api/plans/staged`), `functions/_lib/sweepDispatch.test.ts` (`sweepJobSpec`, `undoScript`, stamps / run ids), `src/runs.test.ts`.

### Notes

- `undo_state = 'partial'` on submit mirrors plan-sweep's undo; the row reads "partly undone" if the undo job dies before `record_undo` (the console shows "undoing" while the job is live). Re-running an undo is safe (already-live names are left alone).
- IAM: nothing new was granted. The job account restores through `objectUser` (`storage.objects.restore`, checked by every real run's preflight) and records to D1 with the same `cf-pages-token` secret a run uses; the dispatch account only submits the job.
- The gcs API guide in `CLAUDE.md` (deployment-branch text) doesn't list `POST /api/sweep/undo` / `GET /api/plans/run` yet.
