# Loading states: audit (2026-09-11) and what's left

Ryan demoed the site and hit two silent loads: toggling **not** on the owner picker showed the previous scope's map unchanged (no in-flight signal), and picking a diff start scan days back left a blank 380 px hole for 10–20 s. An audit of every fetch in `site/src` followed.

## Shipped

- **Map**: the held previous tree (`mapTree`) now dims under a centered "loading view…" marker while the new scope's tree is in flight (`mapStale`), and shows a corner "filling in…" pill while the full tree lands behind the depth-1 one or a refresh runs (`mapBusy`). Everything derived for the drawn map (`klcIdx`, `dateRange`, `catOrder`, the rules section) follows `mapTree`, so a hold no longer empties decorations under a still-showing map or unmounts the Ownership section. The deliberate no-`keepPreviousData` on the per-path subtree queries stays (a held sibling subtree under a new name breaks the graft — see the comment at `dataFor`).
- **Diff**: `placeholderData: keepPreviousData`; the section keeps its height (`.diff-slot`), the last diff dims under an "aligning A → B…" marker, the subtitle shows "aligning" instead of the previous pair's numbers; first load gets a skeleton of the same height.
- `Busy` (`src/Busy.tsx`) + `.busy-host` / `.busy-overlay` styles are the shared affordance for any widget that holds content across a load.
- `ChildrenTable`'s `segs` prop is memoized (a fresh array per render defeated its memos); the Treemap rollup's client state walk is cached by inputs (it re-walked the drilled subtree on every hover/outline render).

## Second pass (2026-09-11, same day)

- **Background polls visible**: a corner `Busy` pill on the marks feed (`actions`, 30 s), the sweep console's dispatches and runs tables (30 s), and the assignments matrix while a refetch is in flight.
- **Pending states everywhere a section used to be blank or a bare "loading…"**: `Skeleton` (reserves the widget's height under the marker) on the age chart, size-over-time (220 px), `/users` map (300 px), `/user/:id` estate and map, `/assignments`, `/marks`, `DbTable`, and the sweep console's plan, dispatches and runs tables.
- **Headline-first diff**: `/api/diff?summary=1` returns both sides' scoped totals without the row walk; `App` runs it as a companion query, so the +X / Δobjects line shows in a second or two while the rows align (the subtitle says "aligning the rows…" meanwhile).
- **One definition per query**: `useScans` (scan.ts) behind `useScan`, `/user*` and `/og`; `useRules` (rules.ts) for the map page and `/marks`; `useMyUser` on top of `useUserEmails`. `OgPage` reads through the cache instead of a serial DIY fetch chain.
- **Deep-link pursuit** capped at ~20 s (was 60) and re-armed on the drawn tree, not the raw one.
- **Dev instrumentation**: `window.__qc` (the QueryClient) in dev / `?spy=1`, so a console session can subscribe to the cache and see which query refetched and whether its data identity changed — the companion to the render spy.
- **Sweep console**: the per-bucket cut is folded into a `<details>` ("limit this run to some buckets"); the summary names the cut when one is set.

## Open

- The 30 s ledger polls still re-render `AppContent` (~300–400 ms each, measured with the spy over 85 s idle) even though both payloads are byte-identical (structural sharing keeps `data`'s identity). Which tracked result property flips is not yet pinned down — use `__qc` to watch the cache; if it's a tracked prop, `notifyOnChangeProps: ['data', 'error']` on `useMarks` / `useMarkTotals` is the fix. `/api/actions` is 2.1 MB per poll; a `since=<action_id>` delta (or a `head` HEAD probe before the GET) would cut the transfer.
- Web workers: not worth it — the trees are pixel-budgeted and the ledger is ~7 k rows.
