# The default scan and search's catch-up: two modes, one per deployment

## Why

Ryan (via gcs, 2026-10-24): "search won't work for ~an hour around each scan" is not acceptable. A scan publishes its data (path store, D1 rows) before its static-names run is appended (about 1 h today: catalog ~30 m, drill ~45 m, anchors ~20 m, longer when a compaction holds the lease). Meanwhile the site opened on the new scan, and every `?f=` search there was refused (`scan-not-indexed`), as an error.

## Decision (Ryan, 2026-10-26)

Both behaviors exist; each deployment picks one with the site var `DEFAULT_SCAN`:

- **`newest`** (unset, the default; non-blocking): a view with no `?d=` opens on the newest published scan. On a scan the static name index doesn't cover yet, the search box is shown disabled, not hidden. Its tooltip (the site's floating-ui `Tooltip`, pinnable so its links can be clicked) says search catches up when the static name index appends this scan, and links:
  - the scan's run on `/scans` (`/scans/<run_id>`, the run that produced the scan; the run list until the runs load or when none recorded it);
  - the newest searchable scan: the same view with `?d=` set to it.
- **`searchable`** (blocking): a view with no `?d=` opens on the newest scan the static name index covers. A newer published scan is reachable explicitly (`?d=`, the picker, /scans) but isn't the default until search covers it. The view says so ("Showing 10/9 4:30a, the newest searchable scan; 10/9 8:36a is still being indexed.").

In both modes:
- A query link (`?f=`) that doesn't pin `?d=` opens on the newest scan that can serve it, the newest covered one ("Searching …, the newest indexed scan; … is still being indexed.").
- A pinned `?d=` the index doesn't cover keeps the server's refusal (`scan-not-indexed`), but the UI shows the disabled box with its tip, and the map slot a catch-up note with the same two links, instead of an error.
- The scan picker marks the scans newer than the newest covered one "· indexing" (still selectable). Older scans the index never covered (before its base) aren't marked.
- Without the static filter (no covered list) the default is the newest scan and the box is never disabled.

The name: `DEFAULT_SCAN` names the decision (which scan a view with no `?d=` opens on); its values say what that scan is. Secondary stores inherit it (`stores.ts` `INHERITED_VARS`). An unknown value is a config error (`DEFAULT_SCAN=x: want newest | searchable`), never a silent default.

## Shape (as built)

1. **One resolver:** `site/src/defaultScan.ts` (React-free): `defaultScanMode`, `prefersIndexed(mode, filtered)` (every view on `searchable`, a filtered one on either), `floatingScan` / `defaultScan` (the newest covered scan when the view prefers it, else the newest; nothing covered: the newest), `indexingScans` (the picker's marks), `searchOff` (the box's state).
2. **Server:** `functions/_lib/defaultScan.ts`: `scanMode(env)`, `coveredScans(env)` (the static store's scans, what `/api/filter-scans` returns and `indexedGate` checks) and `serverDefaultScan(env, scans, filtered)`. OG cards resolve "latest" through it (`og/data.ts` `resolveScan`, `og/diff.ts` `resolveDiffScans`, with `filtered` from the view's `f`): the map, users, assignments, series and diff cards. The covered list is read only when the view prefers it.
3. **Client:** `/api/filter-caps` carries `defaultScan: 'searchable'` when set (absent: `newest`). `filterCaps.ts` `useScanPreference(filtered)` feeds `useScan` on the map/table (`App.tsx`), `/users`, `/user/:id` and `/assignments`; nothing is picked until the caps answer (no fetch of a scan the mode may rule out). `/api/filter-scans` is now fetched per store (`store=`). `SearchCatchUp.tsx`: the tip and the map-slot note.
4. **Mutable state:** none new. The covered list comes from the static store's manifests, as `filter-scans` reads it. No pointer to move, so a deployment whose static-names step is skipped or lags keeps a consistent (older) default rather than a stuck pointer.
5. **Digests:** their links pin `?d=` to the scan they report on (`digest*.py`), so they're unchanged: on `newest` a digest posted before the append lands on the disabled box with its links; on `searchable` too (a pinned `?d=` is never redirected).

## Deployments

Every deployment is `newest` until its owner sets `DEFAULT_SCAN = "searchable"` in its wrangler config (`[vars]`, and `[env.preview.vars]` for its dev stack); `wrangler.example.toml`, `wrangler.r2.toml` and `wrangler.r2-dev.toml` carry the commented line.

## Not this spec

Driving the append itself from about 1 h to minutes: the catalog / drill / anchors fan-outs, following the compaction's. The gate makes the lag visible and navigable; that track makes it short.
