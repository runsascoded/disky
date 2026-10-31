# Δ-mode diffs from the interval store's changes, not from two as-of views

**Status:** proposal, 2026-10-31. Shared (`[cloud]`: `site/functions/_lib/view.ts`, `_lib/index.ts`, `cloud/src/dt_cloud/interval_store.py` / `interval_compact.py`). Nothing is implemented.

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

## Open items

1. **`changes` size on gcs.** If it approaches `path`'s (~19 GB), cut it with fewer columns or only for scans after a cutoff (older ranges refused, per (c)).
2. **G.** Which quantity `diffRoot` scales the Δ root to, so the server floor matches the client's pixels.
3. **Flat directories.** A range with millions of changed objects directly under P (a sweep) is still millions of events over a tiny floor: the size pruning keeps only those over the floor, but a sweep of equal-size objects can all be over it. Cap as views do (`truncated`) and say so.
