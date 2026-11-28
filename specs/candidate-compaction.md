# Candidate compactions: fold without holding appends, then catch up and promote

Status: built for the static name index (2026-11-28, cw session; `cloud/tests/test_candidate_adopt.py`); real-data acceptance is cw's next due compaction with `compact_candidate` on. Ryan's go: "sounds like the automation is worth building regardless", with compaction cadence and size-based triggers as follow-ups ("follow-ups sg").

## Why

A promoting compaction holds the compaction lease for its whole fold: every append to the generation waits (`Compacting`, exit 4), so search lags by every scan published during the fold. cw's c004 fold took 11 attempts over days; five scans queued behind it and search refused them until the catch-up after c004 published (2026-11-28). gcs's folds take 2.4–3.7 h, so a scan or two waits each time.

A candidate (`compact -N`, already built) folds without the lease and points nothing at its output; appends go on into the old generation. What's missing is the rest, done by hand so far: append the scans that landed meanwhile to the candidate, then `promote` it. And a candidate has no lease at all, so a second due compaction (every scan's step defers one while the policy fires) would start another fold that deletes the first's output.

## Design

1. **A candidate fold holds the compaction lease too, marked `candidate: true`.** `compact -N` adopts the lease its submitter wrote (as a promoting one does), so a second compaction of the same generation is refused (`Busy`). `compaction_holds(…, appends=True)` (what `Runner.wait_compaction` asks) skips a candidate's record: appends never wait on it.
2. **A whole candidate is never folded again.** `Runner.compact_stage`, when the generation's next (`next_gen`) holds `candidate.json` and `compaction.json`, submits nothing: the candidate is adopted (3) instead. A candidate without `compaction.json` (a fold that died) is replaced by the next fold, as before.
3. **Adoption** (`Runner.adopt`, the store-neutral loop; `static-names runs adopt`, and `runs merge` after its compaction stage when the profile's `compact_candidate` is set):
   1. `follow()` to the current generation `old`; its candidate `new` must be whole, else nothing to do.
   2. Catch up: the runner on `new` (`with_gen`) appends every scan `old` holds that `new` lacks (`run(old's newest, catch_up=True)`), under `new`'s own coordinator lease (`coordinator_prefix`: a candidate's is `<prefix>/<new>`, apart from the current generation's). Compaction is deferred on `new` while it is a candidate. Each append's R2 stage copies its run to `new` on R2.
   3. Take `old`'s coordinator lease (the one every scan's append holds). Busy (a scan's append is running): wait and retry, up to `ADOPT_WAIT_S`. Held: if `old` gained a scan since (2), release and go back to (2). Else run `static_merge promote [-r] <new>` as a Batch job (it holds the R2 credentials) and wait to its end, then release. `promote_gen` re-checks for a scan that landed as it moved the pointers.
   4. Bounded: `ADOPT_ROUNDS` catch-up rounds. Exit 0 with `{from, gen, rounds, appended}` once promoted; nothing promoted otherwise (a later `runs merge` resumes: the candidate stays whole and the catch-up is idempotent).
4. **After promotion** every runner follows `old/successor.json` to `new` (unchanged), and `new`'s coordinator lease is the store's again.

The candidate runs no compaction of its own while it is one, and its own carries run as for any generation.

## Config

- Static-names profile `compact_candidate: true` (default false; env `STATIC_NAMES_COMPACT_CANDIDATE`): the runner's compaction stage writes a candidate (`runs add -N`'s default), and `runs merge` adopts after its compaction stage. A scan's `runs add` never adopts (a catch-up of every scan since the fold doesn't fit a scan's task), so a deployment turning it on defers its compactions to a coordinator (`options.static-names.compact_cmd` running `runs merge`, as cw's does), or runs `runs adopt` on a schedule.
- `static-names runs adopt [-g GEN] [-n] [-r ROUNDS]`: the adoption alone (a coordinator died, or a hand-run `compact -N`).
- A second coordinator that finds the candidate's lease held (another is catching it up) stands down: `promoted: false`, exit 0.

## Not here (follow-ups)

- The interval store's `interval-store merge` adopting the same way (its `promote` exists; it needs `Runner.promote_job`).
- Size-based compaction policies (the runs' rows / bytes against the base's) and shadow candidate folds at other levels (l3 vs l5) for comparison: `compact-probe -U` (the race path) already folds into scratch without touching the store, so a shadow fold is a race at another level, not a candidate.
