# Static compaction: incremental, not O(corpus)

From gcs's design exploration (`wt/gcs/tmp/static-incremental-measure.md`, 2026-10-24), reviewed against the code at `cloud`.

## Where the time goes (gcs `2026-10-08c → c001`)

- **Fold:** long-roots 3,054 s, canonical-roots 2,054 s, short-roots 1,471 s.
- **Measure:** ~6 h.
  - The sampled `measure-probe` shows the measure stage's wall clock is set by a few unsplittable single-`q` ranges of 100M–440M roots, which spill to temp. Everything else is cheap.
  - Rollups outweigh `dir_stats` about 2.5:1.
  - ef95df4d measures those heavy ranges first, on wide processes. Re-measure after it before sizing the rest.
- **Rest:** intervals, shards, catalog and anchors stay O(corpus) either way.

## Review of the claims

- **Overcounting: right.** A run's `dcount` and its newly-heavy sum `S` count every delta root, closes included (`tdir.rows`). The run's own header counts only opens (`h.hb + t.opens`). The overcount is conservative: a run may roll up a directory a rebuild wouldn't, never the reverse, and answers stay exact. That's why compaction is the reset.
- **Additivity: right**, with these caveats:
  - Root counts per `(q, dir)` are additive in new keys. The heavy / `floor` sets only grow.
  - Child event series append after every base event (every run event is at `t ≥` the run's scan).
  - Peaks are monotone.
  - The kept top-K changes only by a restatement, which runs already do. A new member's history is all new.
- **Byte identity for design 1 is not shown yet.** "Base cells ⊕ run cells by the newest full header, collapsed into the rebuild's form = the rebuild's cells" is the claim a test has to establish. A carry's merged drill isn't tested against a rebuild today, only answers are.
- **Design 2 trade-off.** It keeps the base drill forever and adds a tier. It saves the drill rewrite and its R2 copy, at the cost of reader fan-out. It's worth it only once design 1's numbers are in.

## Plan

1. **Exact `S` and `dcount` (done in this change).** A run counts the roots new to the stack: an existing member's opens, every root of a new member (`tdir.fresh`). Old runs keep their overcount, which is conservative. This yields fewer spurious newly-heavy directories, fewer base counts and probes, and is the enabling change for both designs.
2. **Additivity test (done).** `static_measure.incremental_measure(con, tiers, meas, members, out, floor=…)` writes a compaction's `roots-measure/` (`dirs/`, `short/dirs/`) from the base's and the runs' counts. `test_incremental_measure_is_the_rebuilds` checks it against the rebuild's, md5 for md5, over `test_static_compact`'s worlds: two level-0 runs (A, I), a run over a compacted base (A's `g.c002`) and one merged run (B).
   - **What a run persists, and why it suffices.** Per class canonical at the run and directory, a run keeps one of two things:
     - A heavy directory's header (rollups `kind` 0 / −1). Its `b` is the absolute root rows through the run: a delta header's `h.hb + t.opens`, a settled or newly heavy directory's full count of the combined roots. Once a directory has a header, every later run that touches it writes one, so the newest header's `b` is the total.
     - Any other touched directory's `fresh` rows in `dcount`. A merged tier sums them and drops those with a header. Newly heavy directories are not in `dcount`; their full header carries the total.
   - **The rule.** Per member (each tier's canonical through its `aliases.parquet`, a member it doesn't know being its own):
     - The newest run header's `b`, if any run has one.
     - Else the base's measured rows plus the runs' `dcount`s.
     - A directory a run touched that is below the base's floor (a possible floor crosser) is counted from the base drill's roots. Each such count is under `floor` rows; nested directories are counted from their topmost touched ancestor's rows.
     - The short literals are their own classes. `k` is the directory's segment count, as `dir_stats` sets it.
   - **The one addition.** Runs mark their drill's `meta.json` with `dcount: fresh` (`static_drill.DCOUNT_FRESH`). A merged tier carries it only when every input did. `incremental_measure` refuses runs without it, since a run built before 462fb643 counts closes in `dcount`. Nothing else was missing.
3. **Classes from digests.** Persist per-member delta digests in runs (close: `+h(new) − h(old)` in HUGEINT) and the base's digests (a one-time pass), so a compaction's classes come before any root is read. That removes the fold's digest pass.
4. **Design 1 measure and rollups.** Replace `measure_ranges` in a compaction with base ⊕ runs. Byte identity is checked by `test_compacted_generation_is_the_rebuild`, unchanged.
5. **Decide design 2** from the numbers after 4 and the staging levers.
