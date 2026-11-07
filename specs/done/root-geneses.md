# Roots with different geneses

**Why.** From 2026-09-17 the CoreWeave scan covers two buckets (specs/cw-multi-bucket.md); `hero-checkpoints` has no history before that scan. The store root's size-over-time is a plain per-scan sum, so the day the second bucket entered the scan reads as +89 TiB of growth, and the root diff against any earlier scan presents the whole bucket as new bytes. gcs's six buckets happen to share a genesis, so nothing there ever showed it — but the same code serves both, and a seventh bucket would.

**Principle.** A root's *genesis* is data, not config: the first scan whose series has a point for it. The app derives it and draws the root view so that a root entering the scan is legible as coverage, never as growth. Inside a root there is one genesis and nothing changes.

## 1. `/api/series?split=roots` — one trace per root

Only for the unscoped store root (`path=''`, no `paths`/lens/owner/class scope; otherwise 400). Per indexed scan the reader already fetches every depth-1 row in one range read (`readRootAgg` sums them); `readRootRows` returns them instead, and the response carries both:

```
{ path: '', points: [{date,b,o}…],            // the total, as today
  roots: [{ path: '<bucket>', points: [{date,b,o}…] }…] }   // one per root, in order of first appearance, then bytes desc at the latest scan
```

Scans without tiers (the `meta.json` fallback) give the total as before and per-root points only when `meta.buckets` exists (multi-bucket scans); a single-bucket store's earlier scans attribute the total to the store's sole root when the *earliest indexed* scan has exactly one root — so cw's old scans stay on `marin-us-east-02a`'s trace. Pure parts in `functions/_lib/series.ts` (`rootPoints`, tested).

## 2. Chart (`SizeOverTime` + `TimeSeries`)

At the store root (no drill, no scope, no filter) the chart requests `split=roots` and shows:

- **Stacked bands, one per root** (default; `?ln` = overlaid lines instead — the toggle sits beside `fit / from 0`). A root's band starts at its genesis. Colours are the root's slot hue at the latest scan (largest root = slot 0), so they match the map. `TimeSeries` gains `stacked` (bands drawn between running sums; the tooltip lists each root and the total) and per-series `dashBeforeX` (that part of the line dashed).
- **The total** is the outline of the stack (its own line in `lines` mode), **dashed before the youngest genesis**, and the callouts (first/last/min/max) are the total's.
- **A legend row** under the chart names each root with its swatch; a root whose genesis is after the first scan reads `hero-checkpoints · since 9/17`.

Drilled, scoped or filtered views are unchanged (one trace).

## 3. Diff: "first scanned", not "added"

In the root diff, a depth-1 row is a root; `added` there means the root had no index rows on the older side — it entered the scan. `DiffTreemap` labels such cells `first scanned` and the crumb's movement arithmetic separates them: `start − removed + added ⊕ first-scanned = end`, so the day's real writes stay readable. Server rows are unchanged (`s: 'added'` at `d = 1` under `path=''` is the signal).

## 4. Tests

- `site/functions/_lib/series.test.ts`: `rootPoints` over indexed rows + meta-only scans (single-root attribution, `meta.buckets`).
- `site/src/series.test.ts`: `geneses`, `stackSeries` (running sums with a late root), `youngestGenesis`.
- The chart and diff changes are CIC'd on preview at desktop and phone width once a two-bucket scan exists; until then the root series has one root and the chart must render exactly as before (one band = the total).
