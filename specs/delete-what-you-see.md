# `/staged` deletes exactly what's selected, at each prefix's staging scan

## What went wrong (gcs, 2026-10-19 00:33Z)

Ryan deselected `gs://marin-us-east5/tokenized/dclm_baseline-0206f1/` (read 5 days earlier, the one prefix with a recent read), left the other 9 of the batch checked ("9 selected · 45 T"), and clicked real delete. The run deleted all 10, including 60,348 objects / 15.6 TB of dclm (soft-deleted, restorable until 2026-10-26; left deleted: a byte-identical copy exists in us-central1).

Why: dispatch ignores the row selection. A real run acts on the plan's whole staged item set, gated on a finished dry run of exactly that set (`realGate` / `planDigest`, `_lib/executor.ts`), at the dry run's scan, which the Dispatch "scan" picker chose (2026-10-15T1515) independently of when each prefix was staged (dclm's `as_of` 2026-10-13T1300). Selection looked like intent; it wasn't.

Ryan: "definitely shouldn't delete things that were DR'd then specifically excluded from deletion", and an exact prior dry run isn't important enough to gate on.

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
