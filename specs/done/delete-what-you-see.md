# `/staged` deletes exactly what's selected, at each prefix's staging scan

**Moved to `specs/done/` 2026-10-22** (gcs's spec sweep, Ryan's go): Done in code: `realRun` (`site/functions/_lib/executor.ts`, `executor.test.ts`), all four points. Not yet checked in a browser on a dev stack (a real delete can't be CIC'd).

## What went wrong (gcs, 2026-10-19 00:33Z)

Ryan deselected `gs://marin-us-east5/tokenized/dclm_baseline-0206f1/` (read 5 days earlier, the one prefix with a recent read), left the other 9 of the batch checked ("9 selected · 45 T"), and clicked real delete. The run deleted all 10, including 60,348 objects / 15.6 TB of dclm (soft-deleted, restorable until 2026-10-26; left deleted: a byte-identical copy exists in us-central1).

Why: dispatch ignores the row selection. A real run acts on the plan's whole staged item set, gated on a finished dry run of exactly that set (`realGate` / `planDigest`, `_lib/executor.ts`), at the dry run's scan, which the Dispatch "scan" picker chose (2026-10-15T1515) independently of when each prefix was staged (dclm's `as_of` 2026-10-13T1300). Selection looked like intent; it wasn't.

Ryan: "definitely shouldn't delete things that were DR'd then specifically excluded from deletion", and an exact prior dry run isn't important enough to gate on.

## Update (2026-11-04): a real run with no dry run deletes from its manifest, with no relist

Point (3)'s "lists afresh" (`sweep manifest` + `sweep execute --for-real`) is replaced on gcs: such a run is now `sweep manifest -o <run>/source` + `sweep execute-manifest -e xml -j 64 … --for-real <run>/source`, the reviewed run's parallel XML executor over the manifest's own generations. "What to delete" (a DR's log, or a manifest) is separate from "how to delete". The preflight is pin, no zero or negative generation, under an item, totals (deletes + skips == the summary's `eligible`), no repeated identity, permissions, soft delete. A null generation doesn't refuse the run (Ryan's call): that object is not deleted, there's no fallback to `created`, and it's logged per object as `skipped_no_generation`, with its objects and bytes in `deleted-summary.json`, `plan-summary.json` and the run's row on `/staged` (D1 has no column for it). A manifest with no generation at all refuses, and so does no `generation` column; a whole bucket of nulls while others have generations goes on, flagged `whole_bucket` and "not swept" on the run, and writes no `deletion_bands` row so its items stay staged. A prefix only partly skipped does write a band, so it leaves the queue as a prefix with preserved objects does. `execute-reviewed` still refuses a null. Differences from the listing path it replaces: no whole-dir drift skip (a directory that gained a key since the scan is still swept), and a key written beside a manifest key survives because only named generations are deleted. A dry run still re-lists, and predicts decisions the real run no longer makes the same way: it can skip a drifted dir the real run sweeps. cw's `plan-sweep` is unchanged.

## Update (2026-11-04): on both XML paths, `deleted` means attempted

A prefix with any object skipped for want of a generation stays staged (Ryan, 2026-11-04): the run writes no band for it, so its deletes are in the run's totals and `deleted/` log but not in its bands or prefix history, until a clean run writes its band. No such prefix is expected: the 36M-row manifest of 2026-11-04 has no null generation. The same run has no whole-dir drift skip (Ryan, 2026-11-04).

Measured on real GCS, with and without soft delete: a multi-delete item naming `VersionId` is replied `<Deleted>` with HTTP 200 for the live current generation, one already deleted, an overwritten old generation (the current survives), a wrong generation on a live key (the object survives), a key that never existed, and a repeat. So `execute-reviewed` and `execute-manifest` can't observe gone or overwritten: their `deleted` counts deletes attempted and acknowledged, and `/staged` shows those columns as "—" on these runs (older JSON and listing runs keep their numbers). Undo restores logged generations with `if_generation_match=0`; since the log can't tell ours from an object someone else deleted after the scan, undo can bring those back too (`/staged`'s recovery section says so).

## Update (2026-11-04): a job that failed before recording a run

`sweep manifest`, `execute`, `execute-reviewed` and `execute-manifest` write `failure.json` (`step`, `reason`, `ts`) to their output dir when they raise. `/api/sweep/jobs` shows it as the row's "why", and `sweepReflect` gives a job that ended unsuccessfully with no `deletion_runs` row a closed one (empty digest), so the Slack thread hears of it, with the reason. A step killed from outside leaves no file, and shows Batch's event.

## Status (2026-10-19, controller)

Built on `cloud`, all four points. Not yet seen in a browser (gcs: CIC on dev, without a real delete).
- **Selection (1):** `DispatchReq.items` (the checked rows' `digestLines`; absent = the whole plan, the Slack thread's buttons) → `planItems(…, only)` / `selectItems`: both executors' snapshots, `plan.json` and the run's digest hold exactly those; an item the plan doesn't hold refuses (400 `not in the plan: …`), an empty selection too (`nothing selected`). /staged sends the checked rows (in the checked buckets); both buttons are disabled until a row is checked ("dry-run N selected", "real delete N selected…"), and the armed confirm lists each prefix with its size.
- **Staging scans (2):** the scan picker is gone; a run lists from the newest scan, and the manifest's `as_of` holds keep each item to what its staging scan saw (already there: `sweep_manifest.load_holds`).
- **No dry-run gate (3):** `realRun` replaces `realGate` in `dispatchPlan` (the Slack thread still reads `realGate` for its buttons' state): only a run in flight refuses. A finished dry run of exactly the selection at that scan is reused (gcs `execute-reviewed`, its scan when none is given); otherwise gcs's real run lists afresh (`sweep manifest` + `sweep execute --for-real`), as cw's always did. The 10/19 cause: the real run reused a dry run's manifest of the whole plan, the selection never reaching the server.
- **Test (4):** `executor.test.ts`: three items dry-run, one unchecked, real → the executor acts on the other two, a fresh listing, the run's digest theirs.

## Change

1. **The selection is the run.** "dispatch dry-run" and "real delete…" act on the checked rows (all rows when none are checked — or disable both until something is checked; pick one and say so in the button). The confirm step lists the prefixes and their total ("delete 9 prefixes · 45 T: …"), not just a count.
2. **Each prefix at its own staging scan.** A run reads each item at its `as_of` scan (`plan_items.as_of`), so it deletes what the person who staged it saw; objects written since are left alone (re-stage to include them). The existing exact-generation pin then skips anything rewritten after that scan. The Dispatch scan picker goes away (an "as of" column already shows each item's scan).
3. **No exact-dry-run gate.** The real run's own preflight (re-list, generation match, `if_generation_match` deletes, drift skip) is the safety. A dry run is a review tool: offered, its result shown beside each prefix, but a real run doesn't require one of the identical set. If a dry run of the same selection exists, the confirm step links it.
4. **Never delete an excluded prefix.** A test: stage 10, select 9, dispatch real → the run's manifest has exactly those 9 (and the D1 run row records them).

Both executors (`sweep`, gcs; `plan-sweep`, cw) take the item list from the request, so this is `dispatchPlan` + the page, not per-deployment.

## Not changed

Generation-pinned deletes, drift skip, soft-delete undo (`dt-cloud sweep undo -p`), the per-prefix recovery column.
