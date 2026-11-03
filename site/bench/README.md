# Render bench

Time-to-render per plot, measured (spec: `specs/render-bench.md`). Every fetching widget stamps User Timing marks — `<widget>:<key>:request|response|decoded|painted|settled` (`src/perf.ts`) — with the edge's `Server-Timing` phases attached to `response`. Three readers share them:

- **`?perf=1`** on any page: a panel (lower-left) with each widget's last load as a `wait | decode | paint | settle` bar, its total, the server phases and the cache tier.
- **`window.__perf.table()`** in the console (`__perf.entries()` for the rows).
- **This runner**, which loads URLs under Playwright and folds `__perf.entries()` to p50/p95.

## Run

```bash
cd site
pnpm bench -- https://r2.rbw.sh / /ctbk /ctbk/gbfs --runs 3          # warm: one context, caches warm after run 1
pnpm bench -- https://r2.rbw.sh / --runs 3 --cold                     # cold: fresh context per load + Cache-Control: no-cache on /api, /data
pnpm bench -- http://localhost:3266 /ctbk/gbfs                        # the dev stack
BENCH_TOKEN=… pnpm bench -- https://<gated-deploy> / --runs 2         # a gated deploy (or --token, or $SITE_TOKEN)
pnpm bench:diff -- bench/baselines/r2-2026-09-30.json tmp/bench/20261001-1200.json
```

Options: `--runs N` (default 1), `--cold`, `--token T`, `--out FILE` (default `tmp/bench/<stamp>.json`, untracked), `--timeout S` (per load, default 60), `--headed`. The viewport is fixed at 1280×800 so the map's canvas budget (`w1280` in every key) matches across runs. Needs Node ≥ 23.6 (the scripts are run as TypeScript directly) and the Playwright chromium `pnpm test:e2e` uses (`pwi` to install — never bare `playwright install`).

A run waits until every load has `settled` (or failed) and nothing has changed for a second, so a widget whose fetch starts late (the age chart waits for the scan list) still counts. A load that never settles is reported as open and the run marked `TIMED OUT`; its numbers are still folded.

## Output

One table per widget, one row per request key (path @ scan | canvas width | scope, plus `|d1` for the depth-1 first paint of the map and diff map): `p50/p95` ms per phase, `total` (request → settled), the cache tiers seen (`hit×2 miss×1`) and each `Server-Timing` phase's p50. `bench:diff` prints, per widget / key / phase, the p50 on each side and the Δ in ms and %; positive is slower.

`settle` is the span from the first paint to the *last* commit before 500 ms of quiet — it excludes the quiet window itself, so a widget that paints once shows `settle 0`.

## Baselines

`baselines/` holds committed runs against the public demo: `r2-<date>.json`. Diff a candidate against the latest before calling a change a regression, and add a new baseline when the deploy or the index format moves.

# View memory (`view-mem.ts`)

The memory of `/api/subtree` renders in Node against a deployment's live stores: what an isolate holds while a view renders and while its `waitUntil` work (the edge's background upgrade) runs, what it keeps afterwards, and, with `--snap`, a heap snapshot of who holds it. Workers allow an isolate 128 MB, and a render past that fails as a 1102 with no stack, so this is where to look first. Run it on a dev node, never a laptop: one heavy view peaks at several hundred MB.

```bash
# bundle (light), then ship the one file to the node
esbuild bench/view-mem.ts --bundle --platform=node --format=esm --outfile=view-mem.mjs
python3 -c "import tomllib, json; json.dump(tomllib.load(open('wrangler.toml', 'rb'))['vars'], open('vars.json', 'w'))"
# on the node, with R2_* / CLOUDFLARE_* / D1_DB_ID in the environment (read-only)
node --expose-gc [--max-old-space-size=MB] view-mem.mjs --vars vars.json [--var K=V]… [--snap MB] [--snap-end] --out RUN URL…
```

`tsc -p bench/tsconfig.view-mem.json` typechecks it. It is an esbuild entry, not run by type stripping like `bench.ts`.

Each URL prints one JSON line: status, the body's md5, ms, the peak RSS, heap, external and ArrayBuffer memory during the response and during its `waitUntil` work, and the heap kept after a collection. The colo cache, KV and analytics are empty stand-ins, so every render is cold apart from the isolate's own caches, which carry from URL to URL as on the edge. Heap peaks include uncollected garbage. To tell whether a view fits, find the smallest `--max-old-space-size` it completes under. Node's heap runs larger than workerd's, which compresses pointers.
