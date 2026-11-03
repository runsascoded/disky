# Static compaction: carry runs appended mid-fold onto the successor

## Why

A compaction folds generation `G`'s runs as of its plan into `G'`. When a scan's run lands on `G` meanwhile, the fold is voided:
- `compact_store` checks `G`'s newest manifest just before and after `successor.json`. A scan that landed deletes `G'` and reports `superseded`; the next compaction folds everything again.
- `compact -P` (adopting a race's output) refuses when `probe.json`'s runs differ from `G`'s.

With folds taking hours, this blocks scans, which wait on the lease. A lapsed lease is worse: the scan appends and voids the fold (gcs, 2026-10-24: c001's lease lapsed at ~19:03 with two races and the original run still producing it). It's a 12h-critical-path hazard: the fold and the scans contend instead of composing.

## Shape

When the fold is done and runs `r_{k+1} … r_m` landed on `G` after the plan (which folded `r_1 … r_k`), publish `G'` with those runs **carried**, not void:
1. **Same deltas:** each late run's content is a delta of scan `s_j` against the stack `base(G) ⊕ r_1 … r_{j−1}`. `G'`'s base is that stack folded through `r_k`, so the late runs' deltas against `G'` ⊕ the earlier late runs are the same rows.
   - The interval store, `cdelta` and the suffix shards are per scan and stack-relative in content only through the classes and canonicals.
   - The drill's `dcount` / rollup headers are stack-relative: a header's `b` is absolute through the run, and `dcount` holds fresh rows. Both stay valid over a base that already contains `r_1 … r_k`.
2. **Remaps:**
   - Class canonicals. A late run's `aliases.parquet` maps its members to canonicals at its scan; `G'`'s classes are the fold's. A late run's canonical that `G'` maps elsewhere needs the same re-keying that `cmap` does for prior tiers. Readers already go through `cmap`, so this may need nothing beyond `prior_key`.
   - `prior_key` / the manifest chain.
3. **Mechanism:** copy the late runs' dirs under `G'` (`runs/<key>/`, server side), write `G'`'s manifests for them in scan order (the newest last), then `successor.json` on `G`. Appends that land during the carry repeat the check, so the loop converges on a quiet moment.
4. **Proof obligation:** a test that `G'` + carried runs answers every query exactly as `G` + the same runs does, and that the next compaction of `G'` + carried runs is byte-identical to a fold of `G` + all runs. That's the existing `test_compacted_generation_is_the_rebuild` world with an append injected between plan and publish.

## Open

- Whether any run artifact embeds `G`'s identity or a tier index that `G'` shifts. Audit `static_append.publish` and the drill's `meta.json`.
- Merged runs (level ≥ 1) whose inputs straddle the plan's cut: carry refused, or the fold's plan made to end at a level boundary.
