# Import aggregation extensions: class-pivot sums + weighted-mean mtime

Written 2026-08-14 from the marin session (spec workflow; follow-on to
`done/import-a2a-findings.md`). Context: marin's Batch job now runs `disk-tree bulk-list` for
listings (swapped 2026-08-14; `bucket_list.py` deleted), and the next retirement target is its
`webdata` aggregation. Blocker: marin's layer-3 nodes carry two per-node stats that DT's
layer-2 can't express today — **per-storage-class bytes** (`cb`, drives $-pricing and the
class lens) and **weighted-mean created date** (`d`, drives the age color mode). Without them
the webdata rebase would need a second full pass over layer-1, defeating the point.

Both generalize cleanly (S3 has storage classes; mtime-weighted means are store-agnostic), so
they belong in the engine as opt-in aggregation extensions, not marin overlays.

## 1. `--pivot-sum` — per-category byte sums as columns

CLI: `disk-tree import … --pivot-sum storage_class_id` (repeatable for other columns).

- For each distinct value `v` of the pivoted layer-1 column, emit a layer-2 column
  `sum_<col>_<v>` (e.g. `sum_storage_class_id_2`) = sum of `size_bytes` over descendant
  files with that value, per path row (files: own value; dirs: bottom-up sum — same cascade
  as `size`).
- Distinct-value cardinality guard: error if > ~32 distinct values (this is for enums like
  storage class, not free-form columns).
- Omitted → no columns (zero cost; schema unchanged for existing consumers).
- Both engines (pandas + DuckDB), byte-identical, like `n_files`.
- marin mapping: `cb` = the nonzero non-STANDARD sums per node (its `SII_CLASS_IDS` id space
  is already what `bulk-list` writes).

## 2. `--mean-mtime` — size-weighted mean mtime per node

CLI flag (default off). Emits layer-2 column `mtime_mean` = `sum(mtime_i × size_i) /
sum(size_i)` over descendant **files** (zero-byte files excluded from both terms, matching
the weighted-sum semantics marin uses for its `d`).

- Files: own mtime. Dirs: cascade of the two partial sums (`Σ mtime·size`, `Σ size`) then
  divide at the end — do NOT average averages.
- marin mapping: `d` = `mtime_mean` converted to epoch-days at slice time.
- (Existing `mtime` = max stays untouched.)

## Non-goals

- Top-K aggregations (marin's per-node top-5 users `us`) — genuinely consumer/domain (needs
  the attribution join first); marin computes it in its overlay pass.
- Arbitrary expression aggregation — two targeted, tested extensions beat a mini-DSL.

## Acceptance

- `import -e duckdb --pivot-sum storage_class_id --mean-mtime` over
  `listing/2026-08-14/marin-us-west4/` reproduces marin's production per-node `cb` sums and
  `d` (to epoch-day rounding) for the bucket root + depth-1 dirs — extend the a2a harness
  from `done/import-a2a-findings.md`.
- Pandas/DuckDB parity tests (same fixtures as `n_files`).
- No flags → byte-identical output to today (regression: existing expected-parquet tests
  unchanged).

## Consumer follow-up (marin side, not DT)

With these landed, marin's `webdata` splits into: per-bucket `disk-tree import` (layer-2 with
`n_files`, class sums, `mtime_mean`) + an overlay pass (attribution join → `tm`/`us`/`sh`,
pricing, JSON slices). Then `webdata`'s DuckDB aggregation + `listing.py`'s SII shim retire
(DT `listing.py` already has the SII schema shim from item A.1). Full-fleet gate: 588M-row
run on `mgu` before the production job flips.

## Derived score: stale TiB-years (follow-up, cheap once `--mean-mtime` lands)

marin's fsutil (`lib/rigging/src/rigging/fsutil/usage.py:211-217`, Russell Power 2026-08) ranks
deletion candidates by **TiB × years-since-last-write** — a good single-scalar "big and cold"
triage metric. Two upgrades available to us that fsutil structurally can't do (it has no
persistence and no access data):

1. **Weighted, not max**: fsutil uses `max(last_modified)` over the prefix, so one hot file
   resets an entire prefix's staleness. Size-weighted mean mtime (`--mean-mtime`, above) is the
   honest basis; optionally also carry `mtime_max` for a "provably all-cold" variant.
2. **Read-recency ("atime")**: joining the `dt access` plane's per-prefix `max(ts)` gives
   time-since-last-*read* — the actually-correct coldness signal (shuffle scratch is written
   constantly but read once; archives are written once but read hourly; mtime scoring gets both
   wrong). GCS-only for now (usage logs); CAIOS returns `NotImplemented` for bucket logging
   (probed 2026-08-15), so CW scoring stays mtime-based.

Not an aggregation-engine change: the score is a query-time derivation over layer-2 (+ access
agg join). Belongs in `dt access top` / report tooling, and trends across dated scans for free.

## Status

- [x] `--pivot-sum <col>` (all 3 engines incl. new `-e stream` + cardinality guard + tests)
- [x] `--mean-mtime` (all 3 engines + tests)
      — exactness note (implementation, 2026-08-15): `Σ mtime·size` overflows
      int64 (~1.7e21 at PB scale) and float summation is order-dependent, so
      every engine carries the partial as an exact integer (Python bigints /
      DuckDB HUGEINT) through the cascade and does one `double(wsum) /
      double(size)` division at the end — byte-identical `mtime_mean` across
      engines (`find/agg_ext.py`; locked by
      `test_mean_mtime_exact_at_scale_boundary`)
- [x] 3-engine identity + hand-computed exact-values tests
      (`tests/test_agg_extensions.py`); no-flags output byte-unchanged
- [ ] a2a extension vs marin production `cb`/`d` (marin session runs this)
