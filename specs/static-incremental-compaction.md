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
3. **Classes from digests (done).** A compaction's classes (`al`) come from persisted digests before any root is read; the fold's per-chunk digest pass is gone.
   - **The digest.** The build's: per member, its combined roots' count and two hash sums mod 2⁶⁴ (`static_roots.ROW_HASH` = `hash(path, usr, vf, vt, size, n_files)`, and `hash(ROW_HASH, 'x')`).
   - **Generations** write `drill/digests-long.parquet` (`static_drill.DIGEST_SCHEMA`: `q, n, h1, h2` per member) in `static_local.finish_drill`, from table `dgq`. The base build's `dgq` is its digest pass (already there); a compaction's is `mtid` ⋈ `tdig`, as before. The staged path's `roots alias-plan` writes it too, from its per-shard digests. A generation built before this has none, so its first compaction falls back; the compacted one has them.
   - **Runs** write `drill/digests-long.parquet` (`DIGEST_DELTA_SCHEMA`: `q, dn, d1, d2`) per class canonical at the run (`static_drill.day_digests`, in `build_day`'s long kind) and mark `meta.json` `digests: delta` (`DIGESTS_DELTA`). The run's delta roots (`dr`) are first combined as the stack combines them (smallest `vt` per `(q, path, usr, vf)`). Then per row:
     - a new member's (`newq`), its whole history, closed rows included: `+h(row)`, `dn + 1`;
     - an existing member's open (`vf = D`): `+h(row)`, `dn + 1`;
     - an existing member's close record (`vf < D`, `vt` set; the version's own `size`/`n_files`): `−h(row with vt = OPEN) + h(row)`, `dn` unchanged.
     - An existing member's row that is neither (an open version with `vf ≠ D`) has no exact change: the run writes no file and no mark (logged), so a compaction over it falls back.
   - **Merged runs** (`merge_tiers`) sum their inputs' changes, re-keyed to the merged canonicals through `cmap` as `dcount` is (`merge_digests`); the mark only when every input has it (`kind_files` lists the file when marked).
   - **Deviation from the plan:** the persisted changes and digests are `UBIGINT` residues mod 2⁶⁴, not `HUGEINT`. Parquet has no 128-bit integer, and the arithmetic is modular anyway: `(d1 + 2⁶⁴ − x) mod 2⁶⁴` is the same class as the signed sum.
   - **The fold** (`static_compact._long_roots`): `digests_blocker` (a base without the file, a run without the mark) else `member_digests`. Per member: the base's `(n, h1, h2)` (0 if the base doesn't know it), plus each run's change to the member's class there (`mt`'s canonical per tier, from its `aliases.parquet`), the sums mod 2⁶⁴. Per tuple: one digest (members of one tuple disagreeing falls back too) → `tdig`. `al` is then `min(q) OVER (PARTITION BY n, h1, h2)`, as before.
   - **The check.** Each chunk still combines its tuples' roots (`troots`, the spills unchanged). Instead of digesting them, it compares each tuple's row count with `tdig`'s, a cheap `GROUP BY tid`. A tuple that differs takes its digest from its roots, and the log and the drill result's `fixed` (members) say so. So a change that wasn't exact (e.g. a close of a root the stack didn't hold open, which a run can't see without reading the stack) costs a recount, never wrong classes. A count-preserving hash error (a close record whose `size` differs from its open version's) isn't caught, but it can't happen as `append_open` writes them.
   - **The switch** is logged (`drill classes: from the digests …` / `every root's digest (<why>)`) and lands in the drill doc's `classes` (`digests` | `roots`) beside `measure`.
   - **Tests:** `test_compactions_find_their_classes_from_digests` pins that every fixture compaction took the new path with nothing fixed. `test_digests_are_the_digest_passes` checks `member_digests` against the rebuild's `digests-long.parquet`, and its `al` against the rebuild's `aliases.parquet`, over A, A's `g.c002`, B, C and I. `test_a_tier_without_digests_finds_classes_from_every_root_to_the_same_bytes` strips C's run marks or its base's file and gets the same bytes. `test_a_wrong_change_is_corrected_from_the_roots` corrupts one class's `dn` and gets the same bytes, with `fixed` = that class's members. `test_a_runs_change_is_its_classes_after_less_before` pins the delta rule. The byte-identity tests now cover `drill/digests-long.parquet`, compaction vs rebuild.
4. **Design 1 measure and rollups (done).** A compaction whose runs are all `dcount: fresh` no longer measures or rolls up its roots.
   - **The switch.** `static_compact.fold_drill(meas=<G>/roots-measure)` asks `static_measure.blocker`: a run without the marker, a base without its measurement, or `floor > R + 1` (the measurement would miss heavy directories) falls back to measuring every root, as before. The log says which path it took (`drill measurement: incremental …` / `every root (<why>)`), and so does the drill doc's `measure` (`incremental` | `roots`), which lands in the compaction summary.
   - **Per range.** `finish_drill(incremental=(tiers, meas))` runs `measure_ranges(measure=False)`: each range's spill is loaded and written sorted (`<kind>-sorted/g<g>.parquet`, what `build_drill` streams), and `dir_stats` and `rollup_sql` are skipped. Fan-out helpers get `measure: false` in their plan and sort only.
   - **Measurement.** `roots-measure/` comes from `incremental_measure` (step 2).
   - **Rollups.** `static_measure.incremental_rollups`, per kind, per `(c, dir)` (`c` a canonical of the fold's classes, re-keyed per tier as the measurement is; short: the literal). It applies `merge_tiers`' rule over base ⊕ runs: the cells of the newest tier with a full header and of every later tier (the base's header counts as full). These are collapsed into the rebuild's form: one `kind` 0 header with the newest header's `vf` (kept children) and `o` (children), and `b` from the new measurement's rows; then the kept children's and the remainder's cells. Heavy is the measurement's (rows > R): a header on a directory that isn't heavy is dropped, and a heavy directory with no header in any tier raises. It logs the headers touched by runs, restated in full, dropped, and those whose newest `b` differs from the measurement's (0 under `fresh`).
   - **Tests.** `test_compacted_generation_is_the_rebuild` and the compact-probe, resume and fan-out tests are unchanged and now run the incremental path (every fixture run is fresh). `test_compactions_measure_and_roll_up_incrementally` pins that they did. `test_a_run_counting_closes_measures_every_root_to_the_same_bytes` strips the marker from C's runs and gets the same bytes through the fallback.
   - **What it saves.** It saves `_measure_one`'s `measure_rollups` (`dir_stats` plus `rollup_sql`), which the c001 probe puts at most of a heavy range's time and which spilled 20–37 GiB of temp per heavy range. Loading each range and writing it sorted remain. The base's rollups are read once more, as a parquet scan joined to the class map. Not yet measured live: the next gcs compaction after its runs are all fresh (runs built after 462fb643) is the first.
5. **Decide design 2** from the numbers after 4 and the staging levers.
