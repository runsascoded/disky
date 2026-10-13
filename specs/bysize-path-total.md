# `bysize` keyed on the path's total

**Status:** implemented on `cloud` (writer, reader, `index-recut`); the gcs rollout (image, `index-sync` of the re-cut generations, deploy) is the gcs session's. The interim read-time fix (`completeSlices`) is removed: it made thresholded views ~4× slower, and the re-cut supersedes it.

## Problem

On a labeled store generation (gcs: one row per `(path, usr)`), the `bysize` sort is ordered by each row's own bucket, `⌊log2 size⌋`. A thresholded read keeps a row when *its* size clears the depth's threshold. That has two effects (specs/interval-store.md §7):
- a drawn directory loses its owner slices under the threshold;
- a directory whose every slice is under the threshold, but whose total clears it, isn't drawn.

On prod 2026-10-09 the root view at 1280×768 (threshold 39.46 GB) had 92 tiles short by 1.80 TB, and 6 tiles missing (1.0 TB). The six bucket tiles were short by 197 GB, `marin-eu-west4` alone by 98 GB. The worst tile was `marin-us-central1/data/datakit/normalized/nemotron_cc_v2_1`, which showed 2,232 of its 2,653 GB.

## The interim fix (removed): complete the multi-owner slices from `path`

A child's owners are a subset of its parent's, so only a multi-owner path can be short or missing, and only under a multi-owner parent. The `path` sort holds a parent's children band (one depth, one path range) contiguously. `completeSlices` works down from the view root, or from the store root, which always qualifies:
- It reads each multi-owner parent's whole children band.
- It replaces those children's rows with every slice of the children whose total clears the threshold.
- It descends into the multi-owner children that can still hold a child over the threshold.

Paths that the size read already shows with two slices are seeded into the first round, so there are fewer rounds than levels.

The result is exact: on prod 10-09 the root and all six bucket views matched an independent per-path reference (pyarrow over the `path` sort's row groups) tile for tile, in bytes and in per-owner bytes.

**Cost** (read-only, from a laptop against prod D1 and GCS, cold):

| View | Groups | Bytes | Bands | Rounds | Wall |
|---|---:|---:|---:|---:|---:|
| root | 6 → 83 | 1.4 → 13.3 MB | 95 | 3 | 1.5 → 5.6–6.0 s |
| `marin-eu-west4` | 11 → 47 | 3.7 → 8.4 MB | 34 | 5 | 1.0 → 3.5–3.8 s |
| `marin-us-central1` | 12 → 50 | 8.0 → 13.8 MB | 67 | 3 | 0.9–1.3 → 3.2–3.5 s |
| `marin-us-central2` | 12 → 45 | 3.5 → 9.0 MB | 30 | 3 | 0.6–0.7 → 2.8–2.9 s |
| `marin-us-east1` | 18 → 37 | 1.5 → 5.5 MB | 12 | 4 | 0.8 → 2.6–2.8 s |
| `marin-us-east5` | 13 → 55 | 0.5 → 3.7 MB | 42 | 5 | 0.7 → 2.9–4.1 s |
| `marin-us-west4` | 11 → 17 | 0.7 → 3.1 MB | 4 | 2 | 0.7–0.8 → 1.7 s |

The rounds run in turn, and each one is a span query plus a fetch. Most of the bytes are the bands of wide multi-owner directories (`marin-us-central2/experiments` alone spans 11 groups). Re-reading only the drawn paths' slices would cost more: at the root that is 304 groups and 2.5M rows, past `decodeSpans`' caps, and it would still miss the 6 tiles.

## Proposal: bucket each row on its path's total

Cut `bysize` with the path's total beside every slice, and sort and bucket on that total:
- `tot = SUM(size) OVER (PARTITION BY depth, path)`, stored as an INT64 column. It is equal on every slice of a path, so it run-length encodes well.
- The sort key is `(⌊log2 tot⌋ desc, path, …labels)`. All slices of a path are then adjacent, in one bucket run.
- Group stats: `b_max` becomes `MAX(tot)`, the planner's floor test. Both `disk-tree tiers plan`'s `select_bysize` and `index_footer.extract` follow it.

The reader keeps a row when `tot ≥ thrAt(depth)` and returns every slice of every qualifying path, in one pass at today's cost. It detects the column in the schema (`tot` present) and skips `completeSlices` for those generations.

**`bysize-by-user` stays per slice.** A lens reads one owner's rows, so its threshold is on the slice and is already exact.

**Unlabeled stores (cw, the r2 demo)** have `tot = size`. They can skip the column: the reader already skips completion where there is no `usr`.

## Implementation

- **Writer** (`find/tiers.py` `write_tiers`, so `disk-tree tiers`, `import -i`, `dt-cloud path-index` and `index-write` all get it): on a labeled layer-2, the base `bysize` sort carries `tot`, the path's total, as its last column — the multi-slice paths' `SUM(size) … GROUP BY depth, path HAVING COUNT(*) > 1` (a spillable aggregate, small) left-joined back, `size` elsewhere; the window `SUM(size) OVER (PARTITION BY depth, path)` doesn't spill and failed at 89 GiB on a 605M-row gcs store — and orders by `(⌊log2 tot⌋ desc, path, …labels)`; metadata `sort = tot_bucket desc,path,usr`, `bucket = log2(tot)`. The sort variants (`bysize-by-user`) and unlabeled stores are unchanged (`size_bucket`, `bucket = log2`, no `tot`).
- **Group stats** (`find/groups.py`, `index_footer.py`): `SIZE_COLS = (tot, size, b)`, so `b_min`/`b_max` (the `.groups.json`, `.groups.parquet` and D1 rows) are `tot`'s on a re-cut `bysize`. `disk-tree tiers plan`'s `matched` tests `tot` where present.
- **Reader** (`readSizeRects`): a sort with a `tot` column (`keyedOnTotal`) keeps, outside a lens, every slice of each path whose decoded slices sum to at least `thrAt(depth)`. That sum is the path's `tot`: every group holding a slice of a qualifying path has `b_max ≥ tot ≥` the read's floor and a path range meeting the rect, so all its slices are decoded. `tot` itself is not fetched (no extra bytes). A lens still tests each slice (`MAX(tot) ≥ MAX(size)` keeps that sound). A generation cut per slice reads as before this change (per slice).
- **Tests**: `sliceComplete.test.ts` on `v2-slices` (now with `w/`: 1100 two-owner dirs whose 300 KiB slices fill whole groups) — 270 brute-force cases, the view, a lens case, and planner parity (`plans.json`, `disk-tree tiers plan` with `matched`). Mutations: a per-slice reader predicate fails 3 tests; group stats off `size` fail the brute-force cases. `cloud/tests/test_recut.py`: the re-cut reproduces the writer's `bysize`; `bysize_check` is exact on it and catches stats off `size` (673 of 1100 `w/` dirs).
- **Re-cut** (`dt-cloud index-recut [-c] SRC OUT`): from a generation's `path-index.parquet` into a new generation dir (refuses one that holds the sort), with `.groups.json` + `.groups.parquet`; `-c` runs `bysize_check` (root and depth-1 views at 1280×768, `minArea` 12, `atten` 2) against per-path sums over the `path` sort, and also reports what the per-slice cut missed. Then `index-sync -v bysize -g <gen>` moves only the `bysize` pointer; `path` and `bysize-user` keep their generation.

## Re-cut (2026-10-09)

Every v2 gcs scan's `bysize` was re-cut on Batch (`index-recut -c`, n2-highmem-16 spot, us-east1, ~11–14 min per scan, ~$2 in all) into a new generation dir `listing/<scan>/index/20261009T213000Z/` (`path-index-bysize.{parquet,groups.json,groups.parquet}`); the published generations are untouched. Check JSON per scan: `gs://oa-gcs-usage-dvx/scratch/bysize-recut/20261009T213000Z/check/<scan>.json`.

Each scan's root and six bucket views (1280×768, `minArea` 12, `atten` 2) match per-path sums over its `path` sort, path for path, in bytes and per owner: 77 views, 252,509 of 252,509 paths. The root reads 4–5 row groups. The per-slice cut had missed 2.1–3.5 TB of the root view per scan (89–96 tiles short, 2–8 missing) and 444–476 GB across the bucket views.

| scan | root paths | per-slice root miss | rows | groups |
|---|---:|---:|---:|---:|
| 2026-09-30 | 4,429 | 94 short + 5 missing, 3.05 TB | 777,121,949 | 94,864 |
| 2026-10-01 | 4,428 | 94 + 5, 3.05 TB | 778,376,239 | 95,017 |
| 2026-10-02 | 4,430 | 94 + 5, 3.05 TB | 780,100,151 | 95,228 |
| 2026-10-03 | 3,966 | 94 + 6, 3.21 TB | 781,501,174 | 95,399 |
| 2026-10-04 | 3,716 | 96 + 8, 3.53 TB | 783,060,056 | 95,589 |
| 2026-10-05 | 3,656 | 93 + 6, 2.96 TB | 729,424,167 | 89,042 |
| 2026-10-06 | 3,649 | 92 + 6, 2.80 TB | 606,131,879 | 73,991 |
| 2026-10-07 | 3,642 | 92 + 6, 2.80 TB | 607,022,468 | 74,100 |
| 2026-10-08 | 3,638 | 92 + 6, 2.81 TB | 608,328,372 | 74,259 |
| 2026-10-09 | 3,652 | 92 + 6, 2.80 TB | 609,231,660 | 74,370 |
| 2026-10-09T1236 | 3,387 | 89 + 2, 2.13 TB | 605,027,137 | 73,856 |

## Rollout (the gcs session)

1. Merge `cloud` into `gcs` and rebuild the job image, so the daily `path-index` cuts `tot` (a new scan needs nothing else).
2. Deploy the site from it. The reader is per generation: a per-slice `bysize` reads as before, a re-cut one exactly.
3. Point each scan's `bysize` at its re-cut generation, from a checkout with this change (its `index_footer` takes `b_max` from `tot`; an older one would sync per-slice stats and drop groups): `dt-cloud index-sync -b oa-gcs-usage-dvx -g 20261009T213000Z -v bysize <scan>` for each scan above. Only the `bysize` pointer moves; `path` and `bysize-user` keep their generation.
4. Later `index-gc` sweeps the old `bysize` rows and keeps both dirs (each is still pointed at). Until step 3 the re-cut dirs are unpointed: an `index-gc -F` would delete them once they are 2 days old (the daily job runs only `-r`).

## Open

- Whether the interval store (specs/interval-store.md, one row per path) replaces the per-scan sorts before this re-cut pays off.
