# Lens-aware time series + histograms

The To-do lens (and any filter/lens) hides the "Size over time" and "Bytes by
creation date" sections today, because their data sources can't scope:

- `series.json`: per-path totals per scan — no mark-state axis.
- `age.json`: `(day, top-level dir, team, user)` strata — far coarser than
  mark prefixes (deep) or user filter patterns.

Both are fixable, and the fixes generalize past the To-do view.

## 1. Fate-over-time in `series.json` (cheap, exact)

The actions ledger is a WAL with timestamps, so mark state *at any past
moment* is replayable: fate of byte at scan date D = most-recent mark with
`ts ≤ end-of-day(D)` on an ancestor-or-equal prefix. The daily `gcs-usage
series` step already re-folds every archived `tree.json`; extend it to also
replay the ledger per scan date and emit per-date `{keep, sweep, undecided}`
(KLC decomposed via the same `klcSplits` logic). The To-do lens then plots
undecided-over-time — the *burn-down chart* the review actually wants.

- Input: `/api/actions` (or a D1 export the job pulls) + archived trees.
- Cost: one fate walk per archived scan (~30 × marked-spine walk) — seconds.
- UI: `SizeOverTime` gains stacked fate bands when the lens is active.

## 2. On-the-fly filtered histograms from `dir-cache/age-days`

`listing/<date>/dir-cache/age-days.parquet` already exists (layer-2,
attribution-independent): per-dir written-day byte histograms. A new
`/api/age?date=&prefixes=…` (or `?q=<filter>`) endpoint can:

1. resolve the request to a prefix frontier (to-do walk server-side via the
   ledger, or the filter predicate against the path index),
2. range-read the matching `age-days` row groups (same hyparquet + stats
   pruning as `/api/subtree`),
3. return the summed per-day histogram.

That serves: To-do's creation-date histogram, filter-scoped (`?f=`,
regex) histograms, per-user pages (claims + attribution frontiers), and the
read-lens variant once access-log day-shards get the same per-dir treatment
(`access/agg/*` is close to this shape already).

- Check first: exact `age-days.parquet` schema/sort — it must be
  `(dir, day) → bytes` with dir-range row-group stats to prune. If it's not
  sorted for range reads, add a sorted copy to the webdata step (same
  pattern as `path-index.parquet`).
- Guardrails: cap the frontier size (fold small prefixes into their
  parents), 64MB range cap, per-`(date, frontier-hash)` edge cache.

## Order

1. `series.json` fate columns (pipeline + UI) — restores "over time" on
   To-do with exact history.
2. `/api/age` endpoint — restores histograms on To-do, and makes them work
   for arbitrary filters/lenses everywhere (delete the "hide under lens"
   special case in `App.tsx` as each section becomes scope-aware).
