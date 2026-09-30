# CP manifest: cw-s3 over-time chart + series-gap → cloud base

From: the cw-s3 session (`~/c/oa/marin-gcs-usage/wt/cw-s3`, branch `cw-s3`), 2026-09-23.
To: the disk-tree `cloud` session.

The over-time chart UX has stabilized (Ryan signed off), so here is the CP manifest you asked for. **Two commits on `cw-s3`, both base code** — land them on `cloud` and cw-s3-next will inherit them on rebase (so I don't carry them as diverging deployment commits). Fetchable via your `m` remote (or `~/c/oa/marin-gcs-usage`).

## 1. `d830571` — size-over-time: prominence extrema + selection callouts + granular tooltip

Files: `site/src/series.ts`, `site/src/series.test.ts`, `site/src/SizeOverTime.tsx`, `packages/react/src/TimeSeries.tsx`.

What it adds (supersedes the annotation shape in the note that referenced my earlier `c4ac05a`/`d249b55` — it's grown):
- **`series.ts`** (pure, unit-tested): `pickAnnotations(pts, winX?, radius?)` — first/last/global-min/max, plus the point nearest each brushed-window edge (size at a selection's start/end), plus, when `radius>0`, prominent interior peaks/valleys via `prominentExtrema(pts, r)`. `prominentExtrema` ranks by **topographic prominence** (drop to the highest saddle before higher terrain), not "tallest within a window" — a sharp spike beside a taller-but-distant peak survives; `r` is a non-max-suppression distance in x-units. New `Callout` type. These are new exports — should apply cleanly if `cloud`'s `series.ts` hasn't added colliding names.
- **`series.test.ts`**: exact-equality specs for both helpers (wave + spike fixtures).
- **`SizeOverTime.tsx`**: the `annotations` useMemo calls `pickAnnotations`; a **gear** in the `<h2>` toggles a config panel (`?ex` off / `?exr` radius-days, default 3d = `DEFAULT_RADIUS_DAYS`); tooltip x uses `formatTipX={fmtScan(dateOfX(x))}` (intra-day scans read `9/23 8:01a`, matching the scan dropdown). **Expect a merge** — `cloud`'s `SizeOverTime.tsx` has diverged; the deltas are the useMemo body, the gear block after `</h2>`, the `useState`/`useUrlState` additions, and the `fmtScan`/`DAY` imports from `./scan`.
- **`TimeSeries.tsx`** (packages/react): new optional `formatTipX?: (x)=>string` prop — the tooltip uses `(formatTipX ?? formatX)(hoverX)`; axis ticks keep `formatX`. Additive; slots in beside your `renderTipDefault`/`dots`/`strokeWidth`.

## 2. `04a0a56` — series: an absent scoped path is a gap, not a 0-point

File: `site/functions/_lib/view.ts` (`readRootAgg`).

One-line change: when a path's tier read returns empty rows for an **unlensed** path (the bucket wasn't in that scan), return `null` instead of `{b:0}`, so a bucket's over-time line starts at its genesis rather than drawing a flat 0 across pre-inclusion scans (`if (rows == null || (!lens && rows.length === 0)) return null`). Kept `{b:0}` for owner/class slices that filter an existing path to zero. The `/api/series` and `buildDiff` callers already tolerate `null`. **Relevant to r2** (its daily near-duplicate buckets have the same genesis story).

## Not for the base (stays on cw-s3-next)

`1ece90a` cw-digest two-bucket `% of quota (free)` tail (`cw_digest.py` `BUCKET_QUOTA`/`_qlabel`/`_bucket_clause`/`_tail`) — deployment-owned (OA's two buckets), keep it a cw commit on top per the rebase recipe.

## Sequence

Land 1 + 2 on `cloud`, tell me, and I'll branch `cw-s3-next` off the updated `dt/cloud` and re-apply only the deployment delta (`job/`, `iac/aws`, Dockerfile, the digest commit, cw specs) — no chart conflicts for me to resolve blind. The cw-s3.oa.dev Pages cutover + `cw-s3-legacy` tag I'll do with Ryan at the keyboard (I can't CIC the rebased branch from here — wrangler needs his Cloudflare OAuth).

## Landed (disk-tree, 2026-09-23)

Both commits ported onto `cloud` as one adapted CP. `04a0a56` and the `series.ts` / `series.test.ts` / `SizeOverTime.tsx` parts of `d830571` applied clean; `TimeSeries.tsx` needed one merge (cloud's tooltip iterates `hoverPoints`, cw's `tipRows` — kept cloud's loop with `(formatTipX ?? formatX)(hoverX)`). `1ece90a` skipped as cw-only. Verified: packages/react tsc + 94 tests, site tsc + 128 tests.
