# Δ-mode diffs from the interval store's changes, not from two as-of views

**Status:** revision 3 (2026-11-01) supersedes the `changes` design below; kept for the record. Originally a proposal, 2026-10-31. Shared (`[cloud]`: `site/functions/_lib/view.ts`, `_lib/index.ts`, `cloud/src/dt_cloud/interval_store.py` / `interval_compact.py`). Nothing is implemented.

## Problem

`/api/diff` (`buildDiff`) reads each side as an as-of view at one **byte** floor, `max(rootA, rootB) · minArea / (w·h)`, then walks the two forests and looks up names one side lacks. Its cost is the subtree's size at that floor on both sides, whatever changed. The map's default diff area mode is Δ (`DiffTreemap.tsx`: cell area = |Δ|; `?dm=max` is the other), so in the default mode:

- **Unchanged subtrees cost as much as changed ones.** gcs prod, 2026-10-31, `marin-us-central1/tokenized/common_pile` (≈12 TB), full diff, cold:

  | range | wall | `total` | groups | rows |
  |---|---|---|---|---|
  | 10-30T0701 → 10-31T0702 | 4.1 s | 1.9 s (`groups` 2.4 s, `rows` 3.0 s summed) | 158 | 0 |
  | 10-30T1409 → 10-31T0702 | 9.2 s | 6.0 s | — | 0 |
  | 10-24T0702 → 10-31T0702 | 14.2 s | 10.9 s | — | 0 |

  `total_a == total_b` to the byte: nothing moved, and the answer needed every group either side holds over the floor.
- **Small movement inside a big subtree is folded away.** A byte floor set by the subtree's size is far coarser than the movement a Δ map draws: a subtree whose net Δ is 1e-5 of its size draws its movers only where each mover is itself over the byte floor. The rest lands in a residue.

The interval store already holds exactly what a Δ diff needs: a path differs between scans D1 < D2 iff it has a version boundary (an open or a close) in `(D1, D2]` (`interval-store.md` §2.6). Each appended run *is* its scans' boundaries, and a run's close records carry the closed version's full row (`interval_append.py`: "the closed version's row with its final `vt`"). So a Δ diff can be computed from boundary events alone, at a cost that scales with the changes under P, not with P's size.

## Design

### 1. Transitions

A **transition** is one path's change at one scan `b`: the version that closed at `b` (`old`, none if the path appeared) and the version that opened at `b` (`new`, none if it went away). In the store a change is a close at `vt = b` and an open at `vf = b` of the same path. A transition row joins the two, so **both sides of a change are on one row**: `depth, path, b, kind, size, n_files, c2, c3, c4` of `new` and `size_prev, n_files_prev, c2_prev, c3_prev, c4_prev` of `old` (NULL where that side is none; `kind` of whichever side exists). `b` is the boundary's **epoch** (the `vf`/`vt` value itself), not an index into `scans.json`: retiring scans (`scan retire`) or re-listing `scans.json` changes no row, no group bound and no cache key.

For path p and range `(D1, D2]`:
- p has no transition in the range ⇒ the same version is live at D1 and D2 (or p is absent at both): Δ = 0, never read.
- otherwise p's transitions in the range chain (each one's `old` is the previous one's `new`), so Δp = Σ (new − old) over them = `new` of the last − `old` of the first. A single transition gives Δ from its own row. Δ bytes, Δ objects and Δ per class all come from the rows, no as-of read.

### 2. Where the events are read

Every tier gets one more served sort, **`changes`**: one transition row per (path, `b`), ordered `(b, ⌊log2 max(size, size_prev)⌋ desc, path)`, with `.groups.parquet` bounds on `b`, the size key and path. A range read takes the groups whose `b` meets `(D1, D2]`; inside them a size floor and a path range prune as `bysize` does. Not `us`, `last_read`, `wts`.
- **Base generation.** Its `path` sort can't be pruned by time (its open segment's `vf` ranges span every scan; §2.6 measured a median of 2,818 groups per diff that way), so it needs this sort. Cut by `cut_gen` (so `build`, compaction and `backfill-*` all write it) from the `pvl` pieces: closes at `vt` joined with opens at `vf = vt` of the same path. A version opened at the generation's first scan is no transition. Size: about one row per closed version plus one per version opened after the first scan with no predecessor. Measure on gcs's current generation before committing to it (open item 1).
- **Runs.** A run's `bysize` holds its opens and its close records as **separate** rows, so pruning it by size can keep one side of a change and drop the other. Each run therefore gets its own `changes` sort too, cut at `publish` and by a carry's merge from the run's deltas (close records joined with opens at the same `b`). A merged run spanning more scans than the range decodes some out-of-range rows, bounded by its own changes (≤ 2^(L−1) scans).

### 3. The floor in Δ mode

A changed path matters only if it can be a cell: |Δp| ≥ floor, and |Δp| ≤ max(size at D1, size at D2). Pruning works in two passes, because the size bound is on a path's endpoints and a path can have several transitions in the range:

1. **Candidates.** Read the transitions in `(D1, D2]` under P with `max(size, size_prev) ≥ floor`. A path none of whose transitions is selected had size < floor at D1 and at D2 (its first `old` and last `new` would have been selected otherwise), so |Δp| < floor: no cell, and its movement is in its changed ancestors' own transitions.
2. **Endpoints.** A candidate's Δ needs its first transition's `old` and its last one's `new`. The earliest selected transition is the first in the range when its `size_prev` ≥ floor (an earlier transition would have that as its `new`, so it would have been selected). Likewise the latest selected one is the last when its `size` ≥ floor. A candidate whose earliest selected transition has `size_prev` < floor (or none: it appeared), or whose latest has `size` < floor (or none), may have unselected transitions beyond them. Those candidates get their in-range transitions read by path, with no size bound: batched point reads per level into each tier's `path` sort (`readAsks`, as the walk's lookups today, bounded by `vtLe`/`vfGe`). The common cases (a single transition, or a path over the floor throughout) need no second pass.

Pruning the two sides separately would be wrong: p growing 1 KB → 10 GB at a floor between the two would read as "appeared" (+10 GB) if only the open were kept. The transition row and pass 2 both rule that out.

The floor is the Δ map's own: `G · minArea / (w·h)`, where G is the area the client gives the Δ root (`diffRoot` in `site/src`: confirm whether it is net |Δ P| or the children's gross Σ|Δ|; open item 2). Reading the depth-1 band's events first gives a lower bound on a gross G (Σ|Δ child|), so a floor from it reads at most a little more than needed and never misses a cell.

`(other)` in Δ mode is a node's Δ less its drawn children's Δ: the changed children under the floor. It comes from the same events (each changed child's Δ is known, drawn or not), so it is exact. `?dm=max` (area = max(old, new)) needs unchanged children's sizes and stays on today's as-of reads.

### 4. Root's three requirements

**(a) Nothing changed under P ⇒ no row reads.** The decision is made from footers, not from P's row. Every tier's group index (the base's `changes`, each run's `bysize`) is planned with `b`/`vf`/`vt` in the range and the path range `[P/, P0)`. No group selected ⇒ an empty diff, from footers that are held per isolate and in the colo cache. P's own version alone is not enough: a move inside P (same total, same max mtime) leaves P's row unchanged while its children differ, and its children's events still select their groups. The totals (`total_a`, `total_b`, `objects_a`, `objects_b`) come from `readRootAgg` as today, two point reads rather than subtree reads, and a no-change response carries them like any other (rows empty, totals both sides), so the client's header and movement table still render. Acceptance: the 10-30 → 10-31 `common_pile` diff above answers with 0 row groups decoded (Server-Timing `ngroups;dur=0`).

**(b) A range across a compaction.** A request resolves `interval-store/CURRENT.json` once (`withPathStore`, held `IV_STATE_TTL` per isolate) and reads one generation's base plus that generation's runs, never a mix. A compacted generation's base holds every scan's history (dyadic segments over every stamp, §2.7), and its `changes` sort covers every boundary in it, so any `(D1, D2]` splits into the base's `b` block for scans ≤ the base's newest scan and the runs after it. When CURRENT moves between two requests, the second request reads the new base, whose `changes` sort holds the events the folded runs held. It is the same answer, because compaction is byte-for-byte a fresh build (`test_interval_compact.py`). The edge cache key already carries the generation (`ivGen`), so a cached body from the old generation is still correct (same scans, same answer) and expires as usual. Test: §5's parity fixture compacted between two diffs over a range that spans the compaction.

**(c) Paths or scans outside the interval store.** No reader fallback. A Δ diff whose scans the current generation doesn't hold, or whose generation has no `changes` sort, is refused (`ivRefusal`, 409 with the reason), not answered from per-scan stores or as-of views. The fix is the index: `changes` is cut for the current generation (`interval-store backfill-changes -g GEN`: the base's from its `pv` range files, each run the newest manifest lists from its `pvl` deltas, added in place beside the other served sorts with `changes.json` last; additive, so no new generation and no pointer move: nothing reads `changes` until the reader does, and no existing file changes), and scans before the store's first are out of range for diffs as they are for views. Lens, owner-pool and filtered (`q=`) diffs keep today's as-of path. They read the slice sorts and the static index, and §1's events are per path, not per slice. A slice-level change list is a later phase, not a fallback.

### 5. Tests

- **Parity:** over the interval fixtures (`fixtures/iv`, `gen.py tiered`), every Δ-mode diff (every directory, every pair of scans, unscoped and each class scope) from events equals today's `buildDiff` in Δ mode at a floor ≤ the event floor, cell for cell (paths, Δ bytes, Δ objects, `(other)`): base-only ranges, run-only ranges, ranges straddling the base/run boundary, and a merged run partially covered.
- **Endpoints across the floor:** at a floor between 1 KB and 10 GB, a path growing 1 KB → 10 GB reads Δ = 10 GB − 1 KB, a path shrinking 10 GB → 1 KB reads −(10 GB − 1 KB), each as one transition and as two (1 KB → 2 KB → 10 GB, so the first transition is under the floor and pass 2 must find it), and a path that disappears and reappears inside the range.
- **No-change:** a subtree with no events selects 0 groups, also one where a file moved between two child dirs (P's row unchanged, children not).
- **Compaction:** the same diffs before and after `interval-store add` compacts, equal.
- **Across the v1 → v2 switch:** files first listed at the switch are no transitions (`listed_from`); a diff from a v1 scan to a later one draws each dir's Δ exactly, and a file changed after the switch takes its old side from that transition's `size_prev` (never reads as appearing).
- **Retire:** retiring the oldest scan leaves every `changes` row and every in-range diff unchanged.
- **Refusal:** a generation without `changes`, and a scan the generation lacks, give 409, never a per-scan read.

### 6. Rollout

1. `changes` in `cut_gen` (base) and in a run's `publish`/merge (runs) + `backfill-changes`; measure size and build time on gcs's current generation on Batch (open item 1).
2. Reader: an events read per tier (`readChanges` beside `readSizeRects`), Δ-mode `buildDiff` on it when `dm` is Δ and the scope is unscoped or class-only; `dm=max` and lens/pool/filter diffs unchanged. The client sends `dm` (today it is client-only state, so the server can't tell which area mode it is drawing for).
3. Each deployment: backfill `changes` into its current generation, `current -s`, then deploy. Bump the diff's response version (`bump-response-v-on-shape-change`): its rows change for the same key.

## Implementation notes (reader, 2026-10-31)

- `readChanges` (`_lib/index.ts`) reads each tier holding a scan in `(from, to]` (the base, the runs), footer groups and row groups pruned by scan, size and path bounds; `buildDiffChanges` (`_lib/diffChanges.ts`) is the Δ walk. `buildDiff` takes it when the route sets `fromChanges`: `dm=delta` asked explicitly (the client now sends `dm`; one that doesn't draws `max` areas from the same rows), unscoped (no lens, owner pool, class scope, filter, summary, union member), on a deployment with `DIFF_FROM_CHANGES=1` (set once its store is backfilled). The edge-cache key carries it (`&dm=c`).
- The floor is attenuated with depth as the as-of reads' (`thrAt`: `floor · atten^(d − dP − 1)`), so the Δ map draws the rows the walk does.
- Endpoints (pass 2): a candidate's `from` side is its first transition's old side when that transition is at the range's first scan or its old side clears its floor; its `to` side likewise (its last transition at the range's last scan, or a new side over the floor). Otherwise `readAsks` on the `path` sort at that scan, unless its parent is surely absent there; at most `LOOKUP_CAP` (240) of them, largest estimated |Δ| first, past which a side keeps its estimate (off by less than its floor: every unread transition was under it; `(other)` takes the difference) and the diff says `lookups_capped` / `truncated`. Adjacent scans need none: `g2`'s 8,000-file add (`bk/flat`) draws its 500 files over the floor with 0 lookups.
- `atten ≥ 1` only (`t=` lower reads the as-of walk): a parent's floor is then at most its children's.
- A scan the current generation doesn't hold is refused (`ChangesUnavailable`, 409), as a missing sort is.
- A side a parent is absent on holds none of its children (the as-of walk's reading of a one-sided parent; a scan's rows are hierarchical).
- Tests (`intervalStore.test.ts`, "Δ diffs from changes"): over `g2`, `g5` (base + run) and `g7` (unowned), every pair of scans at every directory to depth 2 and three canvas sizes, every changed row drawn is the as-of walk's row (read just under the Δ floor, no lookup budget), every changed walk row over its depth's floor under an expanded node is drawn, and every expanded node's children and `(other)` sum to its Δ; a subtree with no change decodes no `changes` row group; a generation without the sort is refused. `g1`'s synthetic scans aren't hierarchical, so they are not a parity fixture.

## Revision 2 (2026-10-31): measured on gcs, a planner over three reads

**Status:** the reader above is on `cloud` behind `DIFF_FROM_CHANGES`, on for gcs dev only; **prod held everywhere** (hf included).

### What gcs dev showed (cold, `tmp/dm-compare.py`, 3 pairs × 6 paths, against the as-of walk `dm=max`)

| range | Δ from `changes` | as-of walk |
|---|---|---|
| 1 scan | 1.8–8.8 s (`common_pile`, 0 rows: 88 groups, 8.8 s) | 1.8–3.1 s |
| ~1 day (several scans) | 8.7–21 s, 125–240 lookups | 1.3–3.2 s |
| 1 week | 18–28 s, two 1102s | 2.0–3.5 s |

Totals equal everywhere; rows equal on 1-scan and ~1-day ranges; week ranges differ by 1–79 rows where the lookup budget
left estimates. Three causes:
1. **The size order can't prune by path.** Inside a scan's block each size bucket's groups span the keyspace, so a
   subtree reads every group of the range over the floor, fleet-wide (spec (a) failing: `common_pile` 0 rows, 88 groups).
2. **Ancestors.** A change makes a transition on every ancestor, and the group filter tested a flat floor (the per-depth
   `thrAt` only per row): every big ancestor of any change was read.
3. **No caching.** `changes` groups are read raw from R2 and decoded per request (~40–80 ms a group in the dev runs);
   the as-of walk's groups come decoded from the colo cache (`coloGroup`; ~12 ms a group in the same runs).
Plus: lookups for multi-scan ranges are scattered `path` point reads (~50–70 ms each).

### Measured on gcs's base (`2026-10-09b.g3`, 453M transitions, 55,294 groups a sort; groups / rows)

| case (floor) | size order, flat | size order + group-level `thrAt` | `(b, path)` order | `(b, depth, path)` |
|---|---|---|---|---|
| `common_pile` 1 scan (any) | 6–39 / ≤132 | — | **3** | 12 |
| `common_pile` 1 week (any) | 37–408 / ≤174 | 37–64 | **23** | 138–166 |
| `marin-us-central1` 1 week (1 GB) | 37 / 12.8K | 34 / 2.1K | 60 | 149 |
| root 1 scan (128 MB) | 46 / 359K | **11 / 11K** | 1,133 | 791 |
| root 1 week (1 GB) | 51 / 340K | **28 / 9.6K** | 4,697 | 2,008 |

Rejected: keying the size order on each transition's |Δ| and reading at floor/k (k scans): marginal at k=1 (root 1
scan 1 GB: 5 / 375 vs 6 / 988), worse over a week (root 1 GB: 58 / 38.6K plus 33K endpoint lookups vs 28 / 9.6K); at
128 MB, 201K lookups. `(b, path)` is also the smaller file (3.07 GB vs 3.54 GB).

### The design

- **Group-level attenuated floor** (shipped first, reader-only): a group passes when `b_max ≥ floor·atten^(d_min − dP − 1)`.
- **A second sort, `changes-path`, `(b, path)`**: a subtree's transitions in a scan are contiguous (one or two groups).
  Cut wherever `changes` is (`cut_gen`, `cut_run`, `backfill-changes`).
- **Endpoints from `changes-path` by path** (the same file, contiguous per path), not `path` point reads.
- **Colo-cached decoded groups** for both sorts, as the as-of reads' (`coloGroup` / `putColoGroup`).
- **A planner per request**, from footers alone (held per isolate and in the colo): the as-of walk's plan (both sides'
  groups, as views plan), the size order's (group-level floor) and `changes-path`'s, each costed as **groups × a measured
  per-group cost for its source** (a colo-cached decoded group vs a raw R2 range read and decode; measured on dev from
  `Server-Timing` and kept as constants beside `GROUP_READS`), plus the lookups each implies. The cheapest is read; the
  answer says which (`tier`: `changes`, `changes-path`, or the walk's).
- **Union members** (r2, and every deployment whose meta store is in a main union): below the root a member diff is one
  store's, so `fromChanges` applies there; at the union root the members' diffs share a floor `T`, so `buildDiffChanges`
  takes a fixed `threshold` (no floor-lowering). The parity tests assert `tier`, so a silent fallback fails them.

### Order of work

1. The group-level floor (`8f3d79c6`, `r/changes-atten`); re-run the dev comparison to see what is left.
2. Colo-cached `changes` groups; re-run.
3. `changes-path` (cut + backfill) and endpoints from it.
4. The planner (with the measured per-source costs) and the union path.
5. Dev comparison across 1 scan / 1 day / 1 week at root, buckets and quiet subtrees: every case no slower than the walk
   (cold and warm), rows and totals equal; then prod per deployment.

## Revision 3 (2026-11-01): lazy descent over the interval store, with a planner; `changes` retired

The `changes` sorts answered exactly (every gcs dev case equal to the walk) but no ordering of them was cheap across
deployments: the size order can't prune by path, a path order can't prune broad ranges by size, and every change makes
a transition on each ancestor. Revision 2's planner over three readers would have tuned a gcs-specific cost model.

What replaces them reads only the interval store's `path` sort (`_lib/diffDescent.ts`, `DIFF_DESCENT`):
- **Descent.** A round reads, for the directories it expands, their children at both scans as `path` ranges at each
  depth's floor; a child read on one side takes the other from its own version when that version spans both scans,
  else its parent is read again unfloored on that side (a range, never a point lookup). A directory expands when it
  changed and its net |Δ| clears its children's floor (moves inside an unchanged directory aren't opened). Best first:
  one queue by |Δ| across depths, 64 parents a round, until `top` changed rows are drawn (`expansions_skipped`).
- **Planner.** After P's children (the first round), the descent continues only when at most `DESCENT_MAX_EXPANDABLE`
  (2) of them need expanding; otherwise `HandOff`, and `buildDiff` runs the as-of walk unchanged. `tier` names the
  reader; Server-Timing `plan` is the time to the decision.
- **`(other)` from the invariant** for every reader (`capDiffRows`): an expanded row's totals less its drawn children,
  objects-only ones kept; the walk's own `(other)` emits zero-byte objects too.
- **Cache versions.** `CACHE_V` 8 for the `(other)` changes; the descent's answers carry `DESCENT_V` in their key.

Acceptance (`dt-cloud diff-check -t descent,bysize,path`): gcs 18/18, hf 30/30, cw 17/17, cold and warm. The planner
kept the descent for quiet or narrow subtrees (gcs `common_pile`/`checkpoints` 0.4–0.6 s against the walk's 1.4–3.0 s;
hf `/`, spaces, buckets 149–524 ms) and handed broad ones to the walk at no measurable cost (`L1=0`). Kept and slower:
2 of cw's (+0.4, +0.6 s), deep and narrow; a running-cost bail-out was judged not worth adding.
- **Budget** (added after r2's union run): the planner can't see a deep, busy subtree from P's children (r2
  `marin-na`: L1–L6 0.2 s, then one L7 round of 0.8–3.5 s; 2.6–7.4 s against the walk's 0.5–1.5 s). Past
  `DESCENT_BUDGET_MS` (1,000) since it started, the descent hands off to the walk — checked before each round and raced
  against the round in flight — so its worst case is about the budget plus the walk. Server-Timing `handoff` says which
  bail-out declined (`breadth` / `budget`); `diff-check` prints it. Near the budget the reader that answers depends on
  timing: one request can come back from the descent once and from the walk another time. Both are exact and every
  expanded row sums, but the drawn rows can differ (the descent's finer floor), so which one an edge-cached body holds
  is whichever answered first. Accepted: it is a choice between two correct answers, not between right and wrong.
- **Work budget** (r2, after the time budget): a Worker's clock advances only across I/O, so the time budget never sees
  the descent's CPU, and a timer can't fire during it (r2 `marin-na`: 1.44 s of Server-Timing against 3.7 s wall).
  `DESCENT_BUDGET_ROWS` (5,000) counts the rows the descent's reads return, both sides and unfloored re-reads, checked
  after each read; past it, `HandOff('work')`. Server-Timing `descent-rows` carries the count (its dur). Set against gcs dev
  (its one kept case with work, `checkpoints` over a week: 3,706 children, ≈ 7.5K rows, 2.8 s against the walk's
  0.5 s, trips) and r2 `marin-na` (L4 alone 9,261 children); hf's and cw's kept cases are to confirm from their
  `descent-rows`. The time budget stays as a guard for I/O stalls.

`changes` has no reader: its write path (`cut_gen`, `cut_run`, the staged `cut`), `interval-store changes` /
`backfill-changes`, the reader (`readChanges`, `buildDiffChanges`, `DIFF_FROM_CHANGES`) and its fixtures are removed.
The files already backfilled on each deployment (gcs: 3.6 GiB on GCS and on R2) are left until Ryan's go to delete
them. Prod `DIFF_DESCENT`: per deployment, on Ryan's go.

## Open items

1. **`changes` size on gcs.** If it approaches `path`'s (~19 GB), cut it with fewer columns or only for scans after a cutoff (older ranges refused, per (c)).
2. **G.** Which quantity `diffRoot` scales the Δ root to, so the server floor matches the client's pixels.
3. **Flat directories.** A range with millions of changed objects directly under P (a sweep) is still millions of events over a tiny floor: the size pruning keeps only those over the floor, but a sweep of equal-size objects can all be over it. Cap as views do (`truncated`) and say so.
