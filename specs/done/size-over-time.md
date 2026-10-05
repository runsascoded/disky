# Size (and mark/owner) over time

Time-series charts over the historical scans: **total bytes over time**, scoped to
the currently-drilled subpath and (ideally) filterable by regex; plus a companion
**mark/owner-coverage over time**. Both consume the per-date snapshots we already
publish (`/data/<date>/tree.json`) — the work is making per-date, per-subpath /
per-filter aggregates cheap enough to chart on the fly.

## What exists

- One immutable `tree.json` per scan date (the full aggregated tree; `b` bytes,
  `cb` class mix, `tm`/`us` attribution per node).
- `scans.json` — the list of dates.
- `<TimeSeries>` in `@disk-tree/react` — a DIY-SVG line/area chart, unused on the
  GCS site today.
- `/api/filter` — regex/substring re-aggregation over one loaded tree (matched
  bytes only, outermost matches). This is exactly the per-date compute a regex
  series needs, run once per date.

## The three cases, cheapest first

### 1. Subpath total over time (no regex) — precompute an index

The common case ("how has `checkpoints/` grown?") needs, per date, the byte total
of one prefix. Don't ship whole trees to the client for that.

**At webdata time, emit a compact cross-date index** — `/data/series.json`:

```
{ prefixes: ["marin-us-central1/checkpoints", …],   // above a byte-fraction floor
  dates:    ["2026-08-10", …],                       // ascending
  bytes:    { "<prefix>": [<b@date0>, <b@date1>, …] } }  // null where absent
```

- Limit `prefixes` to those above a fold floor (same idea as the treemap's
  `MIN_FRAC`) so the file stays small — you only chart prefixes big enough to see.
- The client fetches one small file and renders the current `?p=` prefix's series
  instantly; switching subpaths is a map lookup, no refetch.
- Below-floor prefixes fall through to case 2.

Emit it in `webdata` (it already has every date's tree in the snapshot bucket) or
a tiny `gcs-usage series` command that folds the archived trees. Immutable per
date, so it's append-only: only the newest date's column is added each run.

### 2. Arbitrary regex over time — on-the-fly, cached

Regex can't be precomputed. Add **`GET /api/series?q=<regex>&path=<prefix>`**:

- For each date: load `tree.json`, run the `/api/filter` re-aggregation, take the
  matched-bytes total (optionally under `path`). Return `[{date, bytes}]`.
- **Cache aggressively** — scans are immutable, so a `(date, path, q)` result never
  changes. Key a cache (Workers KV, or a D1 table) on that triple; first request
  computes, all later ones are instant. This is the LRU/cache instinct, made
  durable: the cache never needs eviction-for-correctness, only for size.
- **Fan-out** the per-date compute (the CFN-per-scan idea): a coordinator issues N
  subrequests (one per uncached date) to a `series-cell` function, awaits them,
  merges. Watch CF subrequest caps (50 free / 1000 paid per request) — chunk if N
  is large, and only fan out uncached dates (usually just the newest after the
  first run). Sequential-with-cache is an acceptable v1; fan-out is the latency win.

Loading + parsing a full `tree.json` per date server-side is the cost; the cache
makes it a one-time cost per (date, query).

### 3. Mark / owner coverage over time (#4)

"How much of the estate is decided (keep/sweep) or attributed over time?" Two
inputs, joined per date:

- The **ledger** (`/api/actions`) carries each action's `ts` — so "marks as of date
  D" = fold the ledger to rows with `ts ≤ end-of-D`. No per-date scan needed for
  the *marks*; they're a single time-ordered log.
- The **sizes** those marks cover come from the per-date tree (or the case-1 index):
  resolve each mark prefix to its bytes in that date's tree, sum by fate
  (keep/sweep/klc/undecided) or by owner/group.

So #4 = case-1 index (bytes per prefix per date) × ledger-folded-to-date. Chart
stacked areas: kept / swept / undecided bytes over time, or attributed vs lost.
Depends on #1's index landing first; the ledger side is cheap.

## UI

- A **"size over time"** panel under the treemap (reuse `<TimeSeries>`), wired to the
  current `?p=` (subpath) and `?f=` (filter/regex) — it recolors/rescopes with the
  same controls as the map, so drilling or filtering updates the chart.
- A **mark/owner-over-time** stacked-area panel in the mark flow (gated on
  `markMode`), fed by #4.

## Build order

1. ✅ **DONE** — `series.json` index (`gcs-usage series`) + the subpath chart (case 1).
2. `/api/series?q=` with durable per-`(date,path,q)` cache (case 2) — regex.
3. Fan-out the uncached-date compute if latency needs it.
4. Mark/owner-over-time (case 3) once the index exists.

## As built (case 1)

- **`gcs-usage series -r <root> -o series.json`** folds every archived `tree.json`
  into `{dates, prefixes, bytes: {prefix: [b@date…]}}`. A prefix is charted if it
  clears `--min-frac` (0.2% of fleet) in any scan **or** is within `--full-depth`
  (2 = bucket + one dir), so common shallow drill targets are always covered even
  when small (e.g. `ego-dex`, 0.06%). ~478 prefixes / 26 scans ≈ 205 KB.
  - Reads `gs://…/snapshots` on Batch; a local `-r http://localhost:3254/data`
    folds the dev-proxied trees for local generation.
- **`SizeOverTime`** fetches `<store.base>/series.json`, renders the current drill
  prefix's series (round unit-aligned y-ticks), and **falls back to the fleet
  total** (per-date `meta.json`) when the index is absent or the prefix is below
  the floor — so prod never regresses before the index ships.
- **Publish step (live in prod):** the daily snapshot job (`job/run.sh`) runs
  `gcs-usage series -r /gcs/$DATA/snapshots -o /gcs/$DATA/snapshots/series.json`
  right after publishing the snapshot JSONs — over the same FUSE mount, guarded
  so a failure never blocks the snapshot (the chart just falls back to the fleet
  total). It re-folds every archived tree each run (append-only in effect), so it
  self-heals. The `/data/series.json` → `snapshots/series.json` mapping is the
  generic one in `site/functions/data/[[path]].ts` (no function change needed).
  Requires the Batch image to carry the `series` command — see `job/build.sh`.
  - **Backfill:** the initial `snapshots/series.json` was generated locally
    (`gcs-usage series -r gs://oa-gcs-usage-dvx/snapshots -o series.json`, ADC
    read) and `gsutil cp`'d up, so the chart worked before the next daily run.
  - Locally, the vite `dev-series-index` middleware still serves `tmp/series.json`.

## Open questions

- Index granularity: byte-fraction floor vs a fixed depth cap vs top-N per bucket.
  Floor mirrors the treemap fold and is self-tuning — lean that way.
- Cache store: KV (simple, eventual) vs D1 (queryable, transactional). D1 already
  bound; a `series_cache(date, path, q, bytes)` table is easy and lets a sweep
  audit query it.
- Sub-daily scan ids (if GCS ever goes <1d) make `date` a full instant — key the
  index/cache on the scan id, not a calendar date.
