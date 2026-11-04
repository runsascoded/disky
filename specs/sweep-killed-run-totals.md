# A killed real run's totals and undo

**Status:** not built. Next after `w/sweep-manifest-exec`. This predates that branch.

## Problem

A real run has a start row. If its job dies after deleting (out of memory, pre-emption) or its final `record_run` fails (a WARN), `reflectSweepRuns` closes the row with its zero totals and no `undo_deadline`. So:

- The row says 0 deleted after objects were deleted.
- `sweepUndo` refuses it (`the run recorded no undo window`), so the UI can't undo the run that most needs it. From the code, `dt-cloud sweep undo <run or log dir>` passes `deadline=None` and doesn't refuse; not yet confirmed on a bucket.
- Slack says "ended without a result". That's honest but gives no count.
- `-x` resume won't help: `subtract_settled` requires the drained run's `deleted-summary.json`, which a killed run doesn't write. What a killed run can resume from has to be established.

## Fix

1. When a job with a start row and no end record ends, close it from what it wrote: the journal parts under `deleted/`, else `progress/<bucket>.json`. Those are lower bounds if a part never uploaded: say so on the row and in Slack rather than presenting them as totals.
2. Set `undo_deadline` from the soft-delete window, counted from the job's end, the same `HOLD_SECS` rule as a finished run (`runReflect.ts`).
3. Whether `/staged`'s undo works on such a run follows from 2. Its log can be partial, so undo must say it restores only what the log names; blobs in a lost part need `sweep undo -B` (bulk, by window) or a manual restore.
4. Establish resume: either `subtract_settled` accepts a killed run's journal parts, or a re-dispatch from a fresh manifest is the answer (a generation already deleted just acknowledges again, and counts as attempted). Document which.

## Plan card wording

`renderParent`'s "Queue emptied: everything staged here is deleted" is the strongest claim and goes first: XML replies can't prove it, and neither can a skip-bearing run. "Last real run deleted …" follows. The card re-renders on every event without the run's summary, so it needs its own read of `plan-summary.json`, or a D1 column for the contract.

## Tests

- A job killed mid-run, with parts in `deleted/`: the closed row's totals equal the parts' sums, its `undo_deadline` is set, and Slack's text says partial, as exact equality.
- The same with no parts and only `progress/`.
- `sweepUndo` accepts that row. A CLI undo with a null deadline, on a scratch bucket with soft delete on (`oa-gcs-usage-scratch` has none).

## Until then

If a real run dies, don't trust its row: reconcile from `deleted/` and `progress/` by hand and tell Ryan.
