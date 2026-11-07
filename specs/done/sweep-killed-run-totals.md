# A killed real run's totals and undo

**Status:** built. Reaches prod with a site deploy and, for undo on such a run, a new `SWEEP_IMAGE` (see "Rollout").

## Problem

A real run has a start row. If its job dies after deleting (out of memory, pre-emption) or its final `record_run` fails (a WARN), `reflectSweepRuns` closed the row with zero totals and no `undo_deadline`. So the row said 0 deleted after objects were deleted, `sweepUndo` refused it, and Slack gave no count.

## Closing the row

`reflectSweepRuns` closes a terminal job's open real row from its run dir (`_lib/sweepTotals.ts`):

1. `deleted-summary.json`, which the executor writes once every bucket's counts are final: exact. A job that succeeded with only its final record failed then finishes like any other run.
2. Else each bucket's `progress/<bucket>.json`: a lower bound. Buckets come from the row's `buckets`, else `plan-summary.json`'s. A bucket with no file counts nothing.

**Why not the `deleted/` journal parts.** The spec first named them. A Worker would have to decode a parquet column over hundreds of files to sum decisions and bytes: a footer's row count mixes decisions and has no byte sum. They aren't more complete either. A journal part holds up to 65,536 rows spooled locally before it uploads, while a progress file is at most 30 s behind what was acknowledged. Either can be ahead of the other, so neither is the true count.

**Bounds.** One file at a time, each cut at `PROGRESS_BYTES` (a cut file doesn't parse), the whole read time-boxed to `READ_MS`, once per run since the row then closes. The reader never throws. On a failed read the row stays open for an hour of polls, then closes unread, at zero, still undoable. `/api/sweep/jobs` and the executor's `refresh` both pass the readers; with no store configured the row closes unread at once.

`undo_deadline` is the job's end (`updateTime`, capped at now) plus `HOLD_SECS`, as `runReflect.ts` does. `/staged`'s per-object note about retention stands: retention starts at each deletion, and `conservativeDeadline` already says so.

**No bands.** The row gets totals, not `deletion_bands`: a killed run's prefixes stay staged, and a re-dispatch settles them. Its per-prefix column stays empty.

**Saying so.** No D1 column: lower-boundness is read from the run dir, as `plan-summary.json`'s contract is.

- Slack: `runEvent` says "attempted at least …, from its progress files, which can lag what it did … Undo restores only what its log names", or the exact totals, or that what it wrote couldn't be read.
- Row: `totalsLowerBound` (a finished real run with the empty digest and no `deleted-summary.json`) gives `≥` in the table and "at least" in its detail, with both caveats.

## Undo

`undoGate` and `runControls` accept the row once `undo_deadline` is set; that follows from closing it.

The original note that `sweep undo` "passes `deadline=None` and doesn't refuse" was true of the deadline only. `undo_run` read `deleted-summary.json` first and failed with no such file, so it couldn't undo a killed run at all. Now, with no summary, it takes the buckets from `plan-summary.json`, restores what the uploaded parts name, says its log may be partial, and sets `log_partial` in its summary. `record_undo` then leaves `undo_state` at `partial` whatever the counts: a partial log can't say everything is back. A dry run, or a run with no `deleted/` log, still refuses.

**What the log can't cover (a re-dispatch's log names it only while its scan is the newest; after that, by hand).** A generation in a part that never uploaded stays deleted. `sweep undo -B` won't reach it, against this spec's first draft: its precheck sends any dir holding an unlogged soft-deleted object to the per-object path, which restores only logged rows. It needs a manual restore, or an undo of a re-dispatch of the same items at the same scan, whose log names it. `-B` also needs the deletion window: from a closed row's `started_ts` / `finished_ts`, since a killed run's progress files never reach `done`.

**Checked on a bucket.** On a temporary bucket with a 7-day soft delete, a run dir with `plan-summary.json`, one uploaded part and no summary, and objects deleted by generation, some logged and some not:

- The CLI, with a null deadline and no D1 row, didn't refuse: each logged object was restored, under a new generation, with its body intact.
- The objects left out of the log stayed deleted, and an object the run never touched kept its generation.
- A second undo answered `already_live` for each and changed no generation. The undo summary carried `log_partial: true`.
- A `-B` dry run sent the dir to the per-object path, naming the 2 unlogged soft-deleted objects.
- A restored object's old generation still lists as soft-deleted until retention ends. That's GCS's restore, and not a sign the undo failed.

Not exercised: the undo Batch job and route, a real killed executor (the run dir was built by hand from the executor's schema), `record_undo` against D1, and a real `-B` restore.

## Resume

A re-dispatch from a fresh output dir, not `-x`. `subtract_settled` reconciles a run's parts against its final summary, and refuses a killed run by name before any delete. Accepting journal parts would mean dropping that reconciliation, the only proof the parts are whole, to save requests that do no harm: generation deletes are idempotent, and an XML delete acknowledges a generation already gone. Nothing dispatches `-x` from `/staged` anyway. Since a killed run writes no bands, its prefixes are still staged and can simply be dispatched again.

Two things follow. A re-dispatch's repeats count as attempted, so its totals overlap the killed run's. And at a newer scan its manifest no longer names what was already deleted, so its log doesn't cover the killed run's lost parts.

## Plan card wording

"Queue emptied: everything staged here is deleted" lived in `notifyPlan`, not `renderParent`, as the message a stage into an emptied queue posts. It now claims no deletions (`QUEUE_EMPTIED`), which needs no read and holds for any contract.

"Last real run …" is `renderParent`'s, with `lastOutcome` from `lastRunOutcomeOf`: `notifyPlan` reads the last finished real run's `plan-summary.json`, and one byte of `deleted-summary.json` if it failed. It says attempted, recorded (unread) or deleted, "at least" for a lower bound, then its skips. Only `gcs-sweep-*` run dirs are read. No D1 column.

## A row outside the run reads as running

`/staged`'s `rowExecution` matched a live run by bucket. It now reads each live run's pinned `plan.json` and uses `liveTargetOf`: an item of the run, or under a prefix item. A plan not read yet claims no run, so a row can only fail toward not saying "Running".

## Rollout

- Site deploy: totals, Slack, the row, the card, row scoping.
- New `SWEEP_IMAGE`: undo on a run with no summary. On the current image, the undo job fails on the missing file, after the route has marked the row `partial`. The undo CLI from this branch works today.
- No migration.

## Open

A real killed executor: the checks above use a hand-built run dir. Run one on a scratch bucket that's killed mid-delete, the first time one is safe, to exercise what's on disk when a job really dies, before relying on this.
