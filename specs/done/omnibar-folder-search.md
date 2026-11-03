# Omnibar folder search

Status: built on `cloud` (2026-10-22).

## Ask

Ryan, after a demo: "`⌘k grug` to select a specific folder i was looking at in the TM … struck out in omnibar". Of the options (1: surface every directory matching the search as omnibar results; 2: path-segment autocomplete for the filter box; 3: something else), he picked 1.

## What it does

On the map page, typing in the omnibar (`⌘K`) adds two groups to the page's actions:

- **Map:** "Filter map by "grug"" (from one character): sets `?f=` to the query, as typing it into the filter box would.
- **Folders & files** (from two characters): the folders and files whose own names match, largest first, each labelled by its name, then its parent path (elided from the middle), with "folder · 1.2 TB · 3,400 objects" or "file · 50 MB" below. Picking a folder drills the map to it; picking a file opens it in the leaf viewer under its folder (`openHref`). When the server can't list every match (a rollup: too many), a last row says so, and picking it filters the map instead.

The matches are the map's own: the filter's match roots (`allMatchRoots`, the same answer `/api/filter-cover` and the map's matched set use) at the map's scan (`?d=`, or a union view's `?at=`), in the map's filter syntax, so a result's size is what the map shows for it. The search runs over the whole map, not just the drilled folder. On an indexed-only deployment whose newest scan isn't in the static name index yet (the ingest appends it after the scan publishes), the omnibar searches the newest covered scan at or before the map's (`searchScan`, over `/api/filter-scans`, as the filtered map opens on), and each result says "as of scan <id>"; found in CIC on r2.rbw.sh, 2026-10-22, where 10-22 wasn't indexed yet.

## API

`GET /api/name-matches?date=<scan>|d=<slug>&q=<filter>[&qs=<syntax>][&path=<P>][&limit=<n>]` (`site/functions/api/name-matches.ts`)
→ `{date, path, q, items: [{path, kind, b, o}], total, complete, reason?}`

- `items`: the `limit` (default 20, at most 100) largest match roots under `path` (default: the whole map); `kind` from one budgeted point lookup over just those (`pathTotals`, 16 / 32 groups), `null` when it didn't fit (the client then treats it as a folder).
- `total`: every match root; `complete: false` with `reason` as the cover's (rollup, partial, approximate, folders-only scan).
- Union views: each ready member's at its resolved scan, merged largest first (`total` summed); below the root, the path's member answers.
- Edge-cached like the cover (`name-matches` key, `MATCHES_V`), viewer-gated, refused for path-restricted links (`RESTRICTED_BLOCKED`), and counted in `usage.ts`'s `ROUTES`.

`filter-cover` and `name-matches` share their front half: `_lib/matchQuery.ts` (`matchQuery(ctx, route, negError?)`: store and path store, params, query parse and indexed-only refusal, viewer gate, scan resolution, hex-run rule, indexed gate). The `withPathStore` route audit (`pathStoreRoutes.test.ts`) accepts `matchQuery` as the wrap.

## Client

`site/src/folderSearch.ts`: `useFolderSearch` registers two `useOmnibarEndpoint`s (`map:filter`, sync, priority 90; `map:matches`, async, priority 80, `sort: 'none'` to keep the server's size order, 10 per page); `matchLabel`, `matchEntries` (unit-tested). Registered in `App.tsx` beside `drillTo` / `openObject`, so it exists only on the map. A refused or failed search lists nothing; the filter row still applies the query.

## Not done

- Option 2 (path-segment autocomplete in the filter box): not built.
- Ranking is by size only: an exact-name match isn't boosted over a larger partial one.
