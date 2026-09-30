# Render benchmarks: time-to-render per plot, measured, not felt

**Status:** phases 1–2 implemented 2026-09-30 (§5 has the as-built notes and the first baseline); 3–4 open. Base (`site/`). Ryan: *"we should have benchmarking tools/helpers so we can really measure time to render for each plot on the page"* — after a 10 s over-time load on cw-s3.oa.dev that was diagnosed by hand (memory `diff-perf`: a rejected `private` cache put, then batched lookups; 20 s → 5 s → 0.15–0.3 s), and with `path-store` about to change what every plot reads.

## 0. What exists

- **Server:** `serverTiming()` (`functions/_lib/edgeCache.ts:97`) is a `(phase, ms)` sink whose `header()` becomes `Server-Timing: fetch;dur=812,spans;dur=40,…` on `/api/subtree`, `/api/diff`, `/api/series`; `readRects` reports `spans`, `rgjson`, `ngroups`, `groups`, and the `IndexHandle` wraps `file.slice` to count `fetch`. Nothing reads the header client-side; nothing on `/api/scans`, `/api/age`, `/api/children`, `/v1/files`, `/data/*`.
- **Client:** the render spy (`src/dev/renderSpy.ts`, `?spy=1`) counts React commits and per-component render time; `e2e/renders.spec.ts` asserts *commit shapes* after clicks. It measures React work only — not the fetch, not the decode, not the paint — and nothing reports time-to-render per widget.
- **Widgets that paint from a fetch:** `Treemap.tsx` (`/api/subtree`), `DiffTreemap.tsx` (`/api/diff`), `SizeOverTime.tsx` (`/api/series`), `AgeChart.tsx` (age pyramid), the children table and the dTM table (share their map's fetch). Each has the same lifecycle: **request → response → model → first paint → settled** (deeper levels / tooltips / legends land in later commits).

## 1. The measurement

One vocabulary, from the User Timing API, so DevTools, Playwright and the page's own overlay all read the same marks:

```
<widget>:<key>:request      performance.mark at fetch start
<widget>:<key>:response     headers in (+ Server-Timing phases attached as detail)
<widget>:<key>:decoded      body parsed → the widget's model (rows, tree, points)
<widget>:<key>:painted      first commit that draws it (requestAnimationFrame after commit)
<widget>:<key>:settled      no commit touching the widget for 500 ms
```

`widget ∈ {treemap, dtm, series, age, table, dtable}`; `key` is the request key (path + date/pair + w/h + lens), so two treemap loads on one page are two series. `performance.measure` between consecutive marks gives the four spans (`wait`, `decode`, `layout+paint`, `settle`) and the total. Server phases ride along on the `response` mark's `detail`, parsed from `Server-Timing` (which the browser exposes on `PerformanceResourceTiming.serverTiming` for same-origin responses — no client parsing needed when the API is same-origin; the dev proxy case parses the header).

A tiny helper owns the marks: `src/perf.ts` — `perf.start(widget, key) → { response(res), decoded(), painted(), settled() }`; every widget's fetch hook calls it. No-op cost when nothing listens: marks are cheap and buffered by the browser; the helper is on always (not `?spy=1`-gated), because the interesting numbers are production's.

## 2. Surfaces

1. **In-page overlay, `?perf=1`:** a small fixed panel listing each widget's last load as a horizontal bar (wait | decode | paint | settle, coloured), the total in ms, the server phases beneath (from `Server-Timing`), cache hit/miss. Refreshes on each new load. This is what you look at on cw-s3.oa.dev when something feels slow — the 10 s over-time load would have read "wait 9,600 ms (server: overtime 9,400)" without a DevTools session.
2. **Console table:** `window.__perf.table()` prints the same as `console.table` rows; `window.__perf.entries()` returns them (what Playwright reads).
3. **Bench runner, `site/bench/`** (Playwright, like `e2e/`): `pnpm bench -- <base-url> <paths…> [--runs N] [--cold]` loads each URL N times (cold: a fresh context, `Cache-Control: no-cache` on the API; warm: same context), waits for every widget's `settled`, collects `__perf.entries()`, and prints one table per widget: p50/p95 per phase, plus the server phases — and writes `bench/out/<stamp>.json` so two runs diff (`pnpm bench:diff a.json b.json`: per widget/phase Δ and %). Targets: the local dev stack, `dev.*` and prod alike (auth: the bench takes a bearer token env for gated deploys, like `dt-cloud healthcheck`).
4. **CI smoke (later):** the r2.rbw.sh deploy's smoke step runs the bench once over three URLs and fails on a p95 regression above a per-widget budget (`bench/budgets.json`). Not in phase 1 — budgets need a baseline first.

## 3. Phases

1. **Marks + overlay + console** — DONE (§5). `src/perf.ts`, the four fetching widgets and both tables wired, `?perf=1` panel, unit tests on the helper's measure math with a fake `performance`. CIC'd on the dev stack over r2.rbw.sh's API; the marks agree with `PerformanceResourceTiming` to ~1 ms on uncontended responses.
2. **Bench runner + diff** — DONE (§5). `site/bench/`, `bench/README.md`, first baseline `bench/baselines/r2-2026-09-30.json`.
3. **Server phases everywhere:** `serverTiming()` on the remaining routes (`/api/scans`, `/api/age-pyramid`, `/api/children`, `/data/*` proxy), the D1 query time as its own phase, cache hit as `cache;desc=hit`. Also: keep `Server-Timing` on edge-cache *hits* — `cacheMatch` rebuilds the response from the stored body and headers, which never included it, so today only misses carry phases (the baseline shows them on exactly the root loads that miss).
4. **Budgets in CI** once two weeks of baselines exist.

## 4. Non-goals

- Not a replacement for the render spy (commit *shapes*); the bench measures *time*. Both read from `?spy=1`/`?perf=1` without interfering.
- Not synthetic micro-benchmarks of `squarify` or hyparquet decode; those belong in the packages' own test suites.
- No third-party RUM; nothing leaves the page unless the bench runner reads it.

## 5. As implemented (phases 1–2)

**Helper** — `site/src/perf.ts`. `perf.start(widget, key, twins?)` returns `{ track(promise), response(res), decoded(), painted(), settled(), empty(), fail() }`; `createPerf(deps)` takes the clock/mark/measure/rAF/timer seam so the unit tests (`perf.test.ts`, 9 specs) run on a hand-cranked fake. Marks are named per §1; measures are `wait`, `decode`, `paint`, `settle`, `total` (the spec's `layout+paint` is `paint`). Departures from §1, on purpose:

- **`settled` is stamped at the last commit's frame, not at the end of the quiet window** — `settle` and `total` don't carry the 500 ms the helper waited to be sure. A widget that paints once shows `settle 0`.
- **The widget reports its own commits**: `usePerfCommit(widget)` (one line in `Treemap`, `DiffTreemap`, `SizeOverTime`, `AgeChart`, `ChildrenTable`, `DiffTable`) calls `perf.commit(widget)` after every render of that component; `painted` is the next frame for every load of that widget that has `decoded` and no `painted` yet, and each commit re-arms the widget's 500 ms settle timer. A parent re-render counts as touching the widget, so on a page whose last commit re-renders everything the widgets settle together — coherent ("the page went quiet"), but a widget's `settle` can be the page's, not its own. A hidden tab never gets a frame: the settle timer then paints and settles at the commit time.
- **Twins**: the tables draw from their map's fetch, so `start('treemap', key, ['table'])` opens a `table` load under the same key that shares request/response/decoded and paints/settles on the table's own commits (`dtm` ↔ `dtable` likewise).
- **`empty()`**: a load that decodes to nothing drawable (an empty diff, an age pyramid with no rows — r2 has no age tier) never mounts its widget; it closes at decode with `empty: true` rather than sitting open forever. The diff summary fetch (`summary=1`) is not a plot and carries no marks.
- **`Server-Timing` is read off the `Response`** (works same-origin and through the dev proxy alike), together with `x-cache` (`hit`/`miss`/`kv`); `cache;desc=` is the fallback. `wait` is the fetch promise's resolution, so it includes any main-thread delay past TTFB (40–70 ms behind `responseStart` on the dev build while React renders the first arrivals).
- Keys: `<path>@<scan>|w<canvas>[<scope>][|d1|full|coarse]` for the map (`ctbk/gbfs@2026-09-30|w1280|d1`), `<path>@<from>→<to>|…` for the diff, `<prefix><scope>|n<scans>` for the series, `<path>@<scan>|b<budget>` for age.

**Surfaces** — `?perf=1` mounts `src/dev/PerfOverlay.tsx` from `Root.tsx` (lower-left, the help card's styling; one row per widget = its most recently active load; `↺` clears, `×` closes); `window.__perf.{entries,table,reset,subscribe}` always. The render spy is untouched and coexists.

**Bench** — `site/bench/{bench,diff,lib}.ts` + `lib.test.ts` (5 specs) + `README.md`; `pnpm bench -- <base> <paths…> [--runs N] [--cold] [--token T] [--out F] [--note TEXT]`, `pnpm bench:diff -- a.json b.json`. Run as TypeScript by Node ≥ 23.6 (no build step; `bench/tsconfig.json` type-checks it, `@types/node` added). Fixed 1280×800 viewport; warm = one context, cold = a context per load + `Cache-Control: no-cache` on `/api` and `/data` (the edge cache ignores it — `cacheMatch` keys on the URL — so "cold" is the browser, not the edge). Outputs go to `tmp/bench/` (untracked); baselines under `bench/baselines/`. A page without `window.__perf` fails fast after 10 s.

**First baseline** (`bench/baselines/r2-2026-09-30.json`): r2.rbw.sh's deploy predates the marks, so the run went through the prod bundle (`VITE_STORE=r2 VITE_AUTH_MODE=public pnpm build; API_ORIGIN=https://r2.rbw.sh pnpm preview`) — 3 warm runs over `/`, `/ctbk`, `/ctbk/gbfs`, p50 ms:

| widget | key | wait | decode | paint | settle | total | cache |
|---|---|---|---|---|---|---|---|
| treemap | `/@2026-09-30\|w1280` | 184 | 0.4 | 38 | 96 | 306 | hit |
| treemap | `/@2026-09-30\|w1280\|d1` | 5 | 1.5 | 29 | 145 | 180 | miss (server `total 368`: `pre 88`, `fetch 138`, `rows 104`) |
| treemap | `ctbk/gbfs@2026-09-30\|w1280` | 329 | 3.1 | 49 | 63 | 442 | hit |
| dtm | `/@2026-09-29→2026-09-30\|w1280` | 5 | 0.5 | 29 | 105 | 180 | miss (server `total 598`: `groups 272`, `walk 241`) |
| dtm | `ctbk/gbfs@2026-09-29→2026-09-30\|w1280` | 252 | 0.5 | 30 | 53 | 441 | hit |
| series | `ctbk/gbfs\|n11` | 160 | 0.2 | 7.5 | 276 | 443 | hit |
| age | (all) | 5 | ~1 | — | — | 6.6 | empty |

Two things it says already: the root-path `|d1` view and both root diffs miss the edge cache on every network load (the 5 ms p50 waits are the browser's own HTTP cache — the API answers `private, max-age=300` — and the p95s, 535 / 1024 / 888 ms, are the real misses), while every `/ctbk` view hits; and `settle` is the biggest span on a hit (the series' 276 ms is the page's last commit, not the chart's).
