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

### 1. Boundary events

An **event** is a version row at one of its boundaries: an open at `vf` (unless `vf` is the generation's first scan), or a close at `vt` (unless `vt` is `OPEN`). For path p and range `(D1, D2]`:
- p has no event in the range ⇒ the same version is live at D1 and D2 (or p is absent from both): Δ = 0, never read.
- otherwise p's D1 value is the version its first close in the range closes (none if p's first event in the range is an open: p appeared), and its D2 value is the version its last open in the range opens (none if the last event is a close: p went away). Both rows carry `size`, `n_files`, `kind`, `c2..c4`: Δ bytes, Δ objects and Δ per class come from the two rows, no as-of read.

### 2. Where the events are read

- **Runs** (scans after the base generation). A run's served sorts hold only its scans' versions: opens (`vf` in its span) and close records. A run's `bysize` sort is therefore already a size-ordered change list. Read each run whose span meets `(D1, D2]`, groups by path range and size bound, rows kept when `vf` or `vt` is in the range. A merged run spanning more scans than the range decodes some out-of-range rows: bounded by the run's own changes (≤ 2^(L−1) scans).
- **Base generation** (scans folded into it). Its `path` sort can't be pruned by time: its open segment's `vf` ranges span every scan (§2.6 measured a median of 2,818 groups per diff that way). The base therefore gets one more served sort, **`changes`**: one row per event, ordered `(b, ⌊log2 size⌋ desc, path)` where `b` is the event's scan index in `scans.json`, with `.groups.parquet` bounds on `b`, size and path. A range read is one block of `b` values; inside it, a size floor and a path range prune as `bysize` does. Columns: what §1 needs plus the version key (`depth, path, vf, vt, kind, size, n_files, c2, c3, c4`). Not `us`, `last_read`, `wts`.
  - Built by `cut_gen` (so `build`, compaction and `backfill-*` all write it) from the same `pvl` pieces as `path`/`bysize`. Size: closed versions plus versions opened after the first scan, ×1 (an open and a close are two rows of different scans). Measure on gcs's current generation before committing to it (open item 1).

### 3. The floor in Δ mode

A changed path matters only if it can be a cell: |Δ| ≥ floor. |Δ p| ≤ max(size at D1, size at D2), so an event whose row size is under the floor can't be a cell; its movement is counted in its changed ancestors' own events. The size pruning of §2 is exact at any floor.

The floor is the Δ map's own: `G · minArea / (w·h)`, where G is the area the client gives the Δ root (`diffRoot` in `site/src`: confirm whether it is net |Δ P| or the children's gross Σ|Δ|; open item 2). Reading the depth-1 band's events first gives a lower bound on a gross G (Σ|Δ child|), so a floor from it reads at most a little more than needed and never misses a cell.

`(other)` in Δ mode is a node's Δ less its drawn children's Δ: the changed children under the floor. It comes from the same events (each changed child's Δ is known, drawn or not), so it is exact. `?dm=max` (area = max(old, new)) needs unchanged children's sizes and stays on today's as-of reads.

### 4. Root's three requirements

**(a) Nothing changed under P ⇒ no row reads.** The decision is made from footers, not from P's row. Every tier's group index (the base's `changes`, each run's `bysize`) is planned with `b`/`vf`/`vt` in the range and the path range `[P/, P0)`. No group selected ⇒ an empty diff, from footers that are held per isolate and in the colo cache. P's own version alone is not enough: a move inside P (same total, same max mtime) leaves P's row unchanged while its children differ, and its children's events still select their groups. The summary totals (`total_a`, `total_b`) come from `readRootAgg` as today: two point reads, not subtree reads. Acceptance: the 10-30 → 10-31 `common_pile` diff above answers with 0 row groups decoded (Server-Timing `ngroups;dur=0`).

**(b) A range across a compaction.** A request resolves `interval-store/CURRENT.json` once (`withPathStore`, held `IV_STATE_TTL` per isolate) and reads one generation's base plus that generation's runs, never a mix. A compacted generation's base holds every scan's history (dyadic segments over every stamp, §2.7), and its `changes` sort covers every boundary in it, so any `(D1, D2]` splits into the base's `b` block for scans ≤ the base's newest scan and the runs after it. When CURRENT moves between two requests, the second request reads the new base, whose `changes` sort holds the events the folded runs held. It is the same answer, because compaction is byte-for-byte a fresh build (`test_interval_compact.py`). The edge cache key already carries the generation (`ivGen`), so a cached body from the old generation is still correct (same scans, same answer) and expires as usual. Test: §5's parity fixture compacted between two diffs over a range that spans the compaction.

**(c) Paths or scans outside the interval store.** No reader fallback. A Δ diff whose scans the current generation doesn't hold, or whose generation has no `changes` sort, is refused (`ivRefusal`, 409 with the reason), not answered from per-scan stores or as-of views. The fix is the index: `changes` is cut for the current generation (a backfill run over its `pvl` range files, `interval-store backfill-changes -O NEW`, then `current -s NEW`), and scans before the store's first are out of range for diffs as they are for views. Lens, owner-pool and filtered (`q=`) diffs keep today's as-of path. They read the slice sorts and the static index, and §1's events are per path, not per slice. A slice-level change list is a later phase, not a fallback.

### 5. Tests

- **Parity:** over the interval fixtures (`fixtures/iv`, `gen.py tiered`), every Δ-mode diff (every directory, every pair of scans, unscoped and each class scope) from events equals today's `buildDiff` in Δ mode at a floor ≤ the event floor, cell for cell (paths, Δ bytes, Δ objects, `(other)`): base-only ranges, run-only ranges, ranges straddling the base/run boundary, and a merged run partially covered.
- **No-change:** a subtree with no events selects 0 groups, also one where a file moved between two child dirs (P's row unchanged, children not).
- **Compaction:** the same diffs before and after `interval-store add` compacts, equal.
- **Refusal:** a generation without `changes`, and a scan the generation lacks, give 409, never a per-scan read.

### 6. Rollout

1. `changes` in `cut_gen` + `backfill-changes`; measure size and build time on gcs's current generation on Batch (open item 1).
2. Reader: an events read per tier (`readChanges` beside `readSizeRects`), Δ-mode `buildDiff` on it when `dm` is Δ and the scope is unscoped or class-only; `dm=max` and lens/pool/filter diffs unchanged. The client sends `dm` (today it is client-only state, so the server can't tell which area mode it is drawing for).
3. Each deployment: backfill `changes` into its current generation, `current -s`, then deploy. Bump the diff's response version (`bump-response-v-on-shape-change`): its rows change for the same key.

## Open items

1. **`changes` size on gcs.** If it approaches `path`'s (~19 GB), cut it with fewer columns or only for scans after a cutoff (older ranges refused, per (c)).
2. **G.** Which quantity `diffRoot` scales the Δ root to, so the server floor matches the client's pixels.
3. **Flat directories.** A range with millions of changed objects directly under P (a sweep) is still millions of events over a tiny floor: the size pruning keeps only those over the floor, but a sweep of equal-size objects can all be over it. Cap as views do (`truncated`) and say so.
