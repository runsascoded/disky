# Latency observability: which caches answered, how often they're warm, and a bench to track it

**Status:** proposal, 2026-10-10. Base (`[cloud]`, `site/`). Nothing is implemented. §1 audits what's there today, read from the code on `cloud` at `d84d9c64`. gcs prod switched its path reads to the interval store today (`gcs` `1db67e1a`: `PATH_STORE=intervals`, `INTERVAL_STORE_GEN=2026-10-09b`). Per-scan stores still answer the dates the interval store doesn't hold, plus lenses and pools where the generation has no slice sorts.

Ryan asked four things:

1. Is "which caches were used" reliably reported, and does anything read, keep or aggregate it? §1.
2. What fraction of real requests are cache-warm at the edge, the colo and the isolate? §2 designs the RUM that would tell us.
3. A latency benchmark suite: representative queries under controlled cache states, run on a schedule, with trends and regression alerts. §3.
4. Sequencing and cost. §4. The recommendation is §4.1.

## 0. The layers

From the outside in, a `/api/subtree` / `/api/diff` / `/api/series` / `/api/filter-cover` request goes through these caches.

| # | layer | where | what it holds | TTL | key |
|---|---|---|---|---|---|
| L0 | browser HTTP cache | client | the API response (`private, max-age=300`) | 5 min | URL |
| L1 | edge response cache, colo tier | Workers Cache API (`edgeCache.ts` `cacheMatch` / `cacheStore`) | the whole JSON answer | 1 day (a partial answer: `PARTIAL_TTL` 120 s) | `https://<ns>.cache/v<CACHE_V>/…` (`cacheKeyFor`) |
| L2 | edge response cache, KV tier | `CACHE_KV` (global) | the same body. A hit back-fills L1 | 30 days | SHA-256 of the L1 key |
| L3 | isolate memos | module-level `Map`s / `Lru`s in `_lib/index.ts` | `handles` (pointer row → handle, `HANDLE_TTL` 60 s), `ivStates` (interval-store state, 60 s), `ivFooters`, `footerCache` (12 MB LRU: decoded footer groups `fg:`/`fj:`), `groupCache` (24 MB LRU: decoded row groups, per-scan `Row[]` or interval `IvGroup`), in-flight dedupe (`groupsInFlight`, `footersInFlight`, `docsInFlight`), the static name/filter stores' `held` indexes | process lifetime / LRU | `storeKey\|date-or-iv\|variant\|gen\|rg` |
| L4 | colo Cache API, data tier | `caches.default` under `https://index-footer.cache/…` (`coloKey`) | interval-store state doc (`state=1`, 60 s); footers (`footer` raw bytes, `compact=1` decoded); interval footer groups (`fd=<n>&d=2`); interval decoded row groups (`rg=<n>`, gzip JSON, put in the background via `putColoGroup`, settled by the middleware's `waitUntil(coloPutsSettled())`); `cachedRange` byte ranges (footer groups and search sidecars); `index-blob.cache` group-manifest blobs; static-name/filter indexes (`cacheIndexes`, 7 days) | 1 day (`BLOB_CACHE_TTL`) except state | object key + `?part`, `@<store>/` for secondary stores, `?rev=` under `INTERVAL_STORE_REV` |
| L5 | origin | R2 (`INDEX_R2`: interval store, static indexes) or GCS through S3-compat (`makeStore`: per-scan sorts, `STORE_BUCKET`) | parquet ranges | — | — |
| D1 | metadata | `DB` | pointer rows (`index_schema`), `index_row_groups` (hot footers), ledger head, scans, auth | — | — |

Two asymmetries:

- **Data ranges by store.** A per-scan sort's data ranges (`fileFor` → `makeStore(env).get`) are **never** colo-cached: only L3 holds their decode. An interval store's data ranges aren't colo-cached either, but its **decoded** groups are (L4 `rg=`).
- **Footers by store.** A per-scan cold footer (`.groups.parquet`) is cached raw in L4 (`footer`, `cachedRange`) and decoded per isolate. An interval footer is cached decoded in L4 (`compact=1`, `fd=`).

## 1. Audit: what's reported today

### 1.1 Server-Timing, route by route

`serverTiming()` (`_lib/edgeCache.ts`) is the per-request sink. `withTrace` hangs it on the handle, so `_lib/index.ts` and `_lib/view.ts` phases land in it.

| route | Server-Timing on a miss | on an L1/L2 hit | cache marker |
|---|---|---|---|
| `/api/subtree` | `auth`, `pre`, `indexed`, `match`, `box`, and the reader's phases: `open`, `root`, `spans`, `fgroups`, `footer`, `fjson`, `rgjson`, `ngroups`, `groups`, `fetch`, `group`, `gcache`, `gshared`, `gcolo`, `gjson`, `rows`, `details`, `interiors`, `reads`, `rollup`, `search`, `static`, `total` | **none** | `x-cache: miss`, `x-cache-store: deferred\|awaited;dur=N\|skipped\|partial;ttl=N`, `x-cache-partial`, `x-cache-upgrade`, `x-query-engine` |
| `/api/diff` | the same, plus `walk`, `views`, `rootagg`, `floors`, `perask`, `askspan` | **none** | same |
| `/api/series` | `auth`, `scans`, `unindexed`, `gens`, `cache` (its L1/L2 lookup), `box`, `static`, `overtime`, `points` | **none** | `x-cache`, `x-query-engine` |
| `/api/filter-cover` | `auth`, `pre`, `indexed`, `match`, `roots`, `floor`, `cover` | **none** | `x-cache` |
| `/api/name-summary` (static path) | `catalog`, `extent`, `suffix`, `static` | n/a (no response cache) | `x-query-engine: name-summary-static`, `x-static-io` (JSON: plan, groups, bytes, rows, **`index: isolate\|cache\|footer`**). This is the only route that names the tier its index came from |
| `/api/og/*`, page cards | `data`, `render` | — | — |
| everything else: `/api/age-pyramid`, `/api/path-index`, `/api/owners`, `/api/estate`, `/api/prefixes`, `/api/assignments`, `/api/plans/*` (what `/staged` reads), `/api/store`, `/api/me`, `/api/whoami`, `/api/filter-scans`, `/api/filter-caps`, `/api/scan-runs/*`, `/data/*`, `/v1/*` | **none** | none |

What the reader phases report. A phase's value is summed over every call in the request, so concurrent calls add up and a phase can exceed the request's wall time. Count phases ride as `dur` too.

| phase | meaning | tier visible? |
|---|---|---|
| `gcache;dur=N` | N row groups served by L3 | yes (count) |
| `gshared;dur=N` | N row groups joined another read already in flight in this isolate | yes (count) |
| `gcolo;dur=N` | N interval row groups served decoded by L4 (`gjson` = their inflate+parse ms) | yes (count), interval only |
| `ngroups;dur=N` | row groups the plan selected | origin reads = `ngroups − gcache − gshared − gcolo`, by subtraction only, and only when one read ran |
| `fetch` | ms in origin range reads (L5), summed | no count, no bytes, no rounds |
| `footer`, `fjson` | ms opening footer groups | **no**: an L3-held group skips the timer, but an L4 hit and an origin fetch both land in `footer` with no marker |
| `open` | ms opening the handle: pointer row, state doc, footer | **no**: the `handles` / `ivStates` L3 hit, the `state=1` / `compact=1` L4 hit, and the R2/D1 round trips all fold in |
| `spans` | ms planning (a D1 query in `d1` mode, memory in `pq`/interval mode) | no D1 marker |
| `pre` | `ledgerHead`, `hasExtras`, `pathGens`, `indexedScan`: mostly D1 | D1 count and duration not reported |
| `search`, `static` | filter index work, with a `desc` (mode, candidate counts, hit count) | the static store's own index tier (`Io.index`) is **not** surfaced here, unlike name-summary |

### 1.2 The edge response cache on a hit

`cacheMatch` returns the stored body with `x-cache: hit` (L1) or `x-cache: kv` (L2, which also back-fills L1), and a `private` cache-control. The handler returns that response as is: **the request's own `auth` / `pre` / `match` timings are computed and dropped**, and the stored copy never carried the miss's Server-Timing, so nothing is replayed. `x-query-engine` (worker / box) is dropped too.

On a hit we therefore can't tell:

- how long the lookup took: an L2 hit is a KV read plus an L1 put, typically tens to hundreds of ms;
- how old the entry is;
- what the miss that made it cost.

`specs/render-bench.md` phase 3 already called this out ("keep `Server-Timing` on edge-cache hits"). It's still open.

### 1.3 Bugs and pitfalls in what is reported

1. **Name collision: `match`.** `subtree.ts` and `diff.ts` time their L1/L2 lookup as `match` (`st.time('match', cacheMatch(…))`). `view.ts` traces the filter's own match phase as `match` into the **same sink**. On a filtered miss, `match` = cache lookup + filter matching. `series.ts` names its lookup `cache`, which `src/perf.ts` reads as `cache;desc=` (the tier fallback). Three different names for the same thing, one of them shared with something else.
2. **The Workers clock only advances across I/O.** This is the Spectre mitigation, noted in `api/static-bench.ts` and `nameSummaryStatic.ts`'s `tick()`. The pure-CPU phases (`group` decode, `walk`, `rows` assembly, the JSON stringify after the last await) read near 0, and `total` misses the CPU tail after the last I/O. The `cpuTime` in Workers Logs is the only real CPU number: 2026-09-15, a 25-group root subtree took 2.6 s CPU against 0.3 s of I/O, and Server-Timing showed none of the 2.6 s. Any latency number read from Server-Timing alone undercounts cold decode.
3. **Summed concurrent phases.** `fetch`, `footer` and `group` sum across `RUN_READS` / `GROUP_READS` in flight. In `index.ts`, "fetch 5.0 s summed, 1.0 s wall". They are work totals, not critical path.
4. **No isolate or colo identity.** Nothing says whether the request landed on a fresh isolate (L3 empty, module init) or which colo answered. Only the `cf-ray` suffix gives the colo, and nothing records it.
5. **A dev stack shares its prod's KV** (comment in `subtree.ts`: the root key carries `ROOT_LABEL` for this reason). A dev request can be an L2 hit on prod's entry, and a dev miss writes prod's KV.

### 1.4 Who reads it

- **Client.** `src/perf.ts` parses `Server-Timing` and `x-cache` off every tracked widget response (the marks are always on). Only the `?perf=1` overlay (`dev/PerfOverlay.tsx`), `window.__perf`, and the Playwright bench (`site/bench/`) read the result, and none of them sends it anywhere.
- **Session log** (`src/sessionLog.ts`, D1 `session_log` / `session_log_events`). It is on only while an admin switch on `/admin` is open (≤ 31 days). Each `api` event keeps the redacted URL, status, client `ms`, `c` = `x-cache` and `qe` = `x-query-engine`. It keeps **no Server-Timing**, no layer counts, and no colo. It's per-session and identity-bearing (`email`, `ua`). It's a debugging tool, not an aggregate.
- **`dt-cloud probe`** (`cloud/src/dt_cloud/probe.py`). It replays four page loads (root, largest bucket, filter hit, filter miss) and records `status`, `ms`, `bytes`, `x-cache`, and Server-Timing `total` only. `-c` keys reads past L1/L2 with a random `minArea=12.xxxxxx`. `-o` appends records to a local path or `gs://`. **Nothing schedules it.** The gcs job runs `healthcheck` and `warm-cache`, not `probe`. Its query-set mode (`-Q`) scores filter correctness and keeps median timings.
- **`site/bench`** (`pnpm bench`): Playwright, per widget wait/decode/paint/settle. One baseline (`bench/baselines/r2-2026-09-30.json`, r2 only). Not scheduled.
- **Workers Logs** (`[observability] enabled = true` on gcs, r2, and the example). Each invocation keeps URL, status, `wallTime`, `cpuTime`, and `console.*` lines, retained for days, queryable in the dashboard. It's the only persisted latency data today. It has no cache fields, because nothing logs them.
- **Analytics Engine: not bound anywhere.** `writeDataPoint` appears nowhere in the code.

### 1.5 Visible / invisible, summarized

| layer | visible today? |
|---|---|
| L0 browser cache | client only (`perf.ts` sees ~5 ms waits). The server never sees these requests |
| L1 vs L2 vs miss | yes: `x-cache` on the four response-cached routes. Kept only by the session log (when on) and `probe` (when run) |
| L1/L2 lookup cost, entry age, the producing miss's cost | **invisible** (dropped on a hit) |
| L3 row groups | yes, as counts (`gcache`, `gshared`), on misses of the four routes |
| L3 handles / state / footers / static `held` indexes | **invisible** |
| L4 decoded interval row groups | yes, as a count (`gcolo`) |
| L4 state doc, footers (raw and decoded), footer groups, `cachedRange`, blob manifests | **invisible**: folded into `open` / `footer` ms with no hit marker |
| L4 static name/filter indexes | name-summary only (`x-static-io.index`). Invisible on filtered subtree/diff/series |
| L5 origin reads | ms only (`fetch`, summed). No count, bytes, rounds, or provider |
| D1 | **invisible** as such (inside `pre`, `spans`, `open`) |
| isolate fresh vs warm, colo | **invisible** |
| CPU | **invisible** in-request. Only Workers Logs `cpuTime` |
| everything on the other ~20 routes | **invisible** (no Server-Timing at all) |
| aggregate warm fractions | **nothing aggregated anywhere** |

## 2. RUM: what fraction of real requests is warm

### 2.1 Prerequisite: every response says what it touched (increment 1)

One compact header per API response, built by the same sink as Server-Timing. Counts are per tier: `i` = isolate (L3), `c` = colo (L4), `o` = origin (L5 / D1).

```
x-dt-layers: edge=miss, iso=7f3a;age=812;n=57, colo=SJC, store=iv, state=i, handle=c, footer=0i2c1o, rgroup=12i4c3o, range=3o;b=812345;r=2, d1=4;dur=38, static=c
```

- `edge` is the response cache result: `hit` (L1), `kv` (L2), `miss`, or `bypass` (§3.2). On a hit, `x-dt-layers` holds `edge`, `iso`, `colo` and `age=<s>` (the entry's age, from a stored `x-dt-stored` stamp), and the rest is in `x-dt-origin-timing` (below).
- `iso` is a per-isolate random id (minted at module load), the isolate's age in seconds and its request count. `n=1` means a fresh isolate. This gives isolate warmth with no extra machinery.
- `colo` is `request.cf.colo`.
- `store` is `iv` (interval store), `ps` (per-scan) or `both` (an interval read that fell back for some dates).
- `state` / `handle` / `footer` / `rgroup` / `range` / `static` are hit counts per tier: the `handles`/`ivStates` memo, `ivStateDoc`, `openIvFooter`/`readFooterBytes`/`ivFooterDoc`/`cachedRange`, `readGroupsCached`, and the static stores' `Io.index`. `range` also carries origin bytes (`b`) and the critical-path round count (`r`). `rgroup`'s `o` is counted directly, not by subtraction.
- `d1` is the D1 statement count, and their summed `meta.duration`. `D1Result.meta` carries it, so this needs no extra calls.

Server-Timing changes, in the same increment:

- The L1/L2 lookup phase becomes `ecache` on every route (fixes §1.3.1). The tier is `cache;desc=hit|kv|miss|bypass`, which is what `perf.ts`'s fallback already reads.
- `cacheStore` keeps the miss's Server-Timing and `x-query-engine` on the stored copy: as headers on the L1 response, and as KV **metadata** (`put(…, { metadata })` / `getWithMetadata`, ≤ 1 KB, so the stored header is truncated to the top phases). A hit replays them as `x-dt-origin-timing` and `x-query-engine`, and its own `Server-Timing` is `auth`, `pre`, `ecache`, `total`.
- `_middleware.ts` stamps `Server-Timing: mw;dur=<total>` and `x-dt-layers: iso=…, colo=…` on **every** `/api/*` and `/data/*` response that lacks them. This closes the ~20-route gap without touching each route. Routes then add their own phases where they matter: `age-pyramid`, `path-index`, `owners`, `plans`.
- The session log's `api` event also keeps `st` (Server-Timing, truncated to `TRUNC.msg`) and `ly` (`x-dt-layers`). That's one line in `src/sessionLog.ts`, plus `sessionsModel` rendering.
- CPU (§1.3.2) stays out of headers. When precise CPU-bound phase times are wanted, `?_t=precise` (bench-gated like §3.2) inserts a `tick()` after `group` decode and before the response serialize, so their wall time shows. Each tick costs one Cache API miss (~1 ms), so it's off by default.

### 2.2 The sink: Workers Analytics Engine

One data point per `/api/*` response, written from `_middleware.ts` after `ctx.next()`. It parses the response's `Server-Timing` and `x-dt-layers`, so routes stay unaware of it. `writeDataPoint` is fire-and-forget: no await, no added latency.

Binding: `[[analytics_engine_datasets]] binding = "RUM", dataset = "<deployment>_rum"` (one dataset per deployment; the dev stack writes its prod's dataset with `deploy=dev`). Pages Functions accept Analytics Engine bindings. Check this on the dev stack before the gcs rollout.

| slot | field |
|---|---|
| `index1` | route (`subtree`, `diff`, `series`, …). This is the sampling key, so a rare route isn't sampled away by a common one |
| `blob1…blob14` | deploy (`prod`/`dev`), build sha, status class (`2xx`/`4xx`/`5xx`), edge (`hit`/`kv`/`miss`/`bypass`), store (`iv`/`ps`/`both`), colo, engine (`worker`/`box`/`static`), view shape: depth bucket (`0`, `1`, `2-3`, `4-6`, `7+`), `full` vs first paint, filter syntax class (`none`, `plain`, `^q`, `q$`, `^q$`, `hex`, `glob`/`regex`: the parsed AST's kind, never the term), lens kind (`none`, `user`, `pool`, `class`, `by`), diff gap bucket (`0`, `1d`, `2-7d`, `8-30d`, `>30d`), partial (`0`/`1`), source (`rum` / `bench`) |
| `double1…` | total ms, `auth`, `pre`, `ecache`, `open`, `footer`, `groups`, `fetch`, `walk`/`rows` (route's main), `static`/`search`, rgroup i/c/o counts, footer c/o, origin bytes, origin rounds, d1 count, d1 ms, iso age s, iso n, response bytes, scan age days |

That's within AE's 20 blobs and 20 doubles. The exact slot order goes in `functions/_lib/rum.ts` as one table, so the query CLI shares it (a TS → JSON export the Python side reads, or a duplicated constant tested for equality).

**Sampling.** Write 100%. gcs and cw-s3 are internal dashboards serving thousands of API requests a day, three orders of magnitude under the included write quota. AE samples at read time on its own; every query weights by `_sample_interval`. If a deployment ever outgrows that, sample in the middleware by route, with `edge=hit` responses at 1/10 (they're uniform), and record the rate in a double so the weights stay right.

### 2.3 Privacy

The repo is public and `/privacy` says "No third-party analytics, advertising or tracking scripts". Analytics Engine is Cloudflare's own storage on the account that already hosts the site, D1 and Workers Logs. It is first-party ops telemetry, the same footing as Workers Logs, which already keeps every URL.

- **No identifiers.** No email, no session or grant id, no IP, no UA, no country. `colo` is the only location, and it names the data center.
- **No content.** No path, no filter term, no user id in a lens. Paths and terms describe bucket contents, which are behind sign-in. Only the shape buckets above go in. A deep slow view is found by its depth and class, then reproduced with the bench or the session log, which do hold URLs and are admin-only.
- `/privacy` gains one sentence under "What is collected": *"Operational timings: for each data request, how long it took and which caches answered, with no identity, page path or search term attached."*

### 2.4 Questions it answers, and how

Queries run over the AE SQL API (`https://api.cloudflare.com/client/v4/accounts/<id>/analytics_engine/sql`, a token with Account Analytics Read). A **`dt-cloud rum`** subcommand holds the canned queries and prints tables. That's the CLI, not ad-hoc curl, and it reuses the deployment's `CF_*` creds. A table cut, for example:

```sql
-- warm fractions and latency per route × edge state, last 7 days
SELECT index1 AS route, blob4 AS edge,
       SUM(_sample_interval) AS n,
       quantileExactWeighted(0.5)(double1, _sample_interval) AS p50_ms,
       quantileExactWeighted(0.9)(double1, _sample_interval) AS p90_ms
FROM gcs_rum
WHERE timestamp > NOW() - INTERVAL '7' DAY AND blob1 = 'prod' AND blob14 = 'rum'
GROUP BY route, edge ORDER BY n DESC
```

- **Edge-warm fraction** = `edge ∈ {hit, kv}` / all, per route. Also `kv` / (`hit` + `kv`): how often a viewer is first in their colo.
- **Isolate-warm**, over edge misses: Σ `rgroup.i` / Σ `rgroup` (row groups), the share of requests with `iso n = 1` (fresh isolates), and `handle`/`state` `i` rates.
- **Colo-warm**, over edge misses past L3: Σ `rgroup.c` / Σ (`rgroup.c` + `rgroup.o`), and `footer.c` / (`footer.c` + `footer.o`).
- **Latency by state**: p50/p90 total ms per route, split by `edge` × "any origin read" (`rgroup.o + footer.o > 0`) × store. This is the real-traffic counterpart of §3's controlled states.
- **Regressions by deploy**: the same cut grouped by build sha.

`dt-cloud rum --since 7d [--route subtree] [--by edge,store]` prints these. A weekly line ("edge-warm 71%, cold-miss p50 2.1 s, …") goes in the existing Slack/Discord digest. A `/admin/latency` page, which would need an AE-read token as a secret, is §4's last increment, and optional.

### 2.5 Client-side RUM (later, optional)

Server RUM measures the server's wall time. Perceived latency adds network, L0, and decode/paint, which `perf.ts` already measures on every load. A later increment could beacon `perf` entries (widget, wait/decode/paint/settle, `x-dt-layers.edge`) to `POST /api/rum` → the same dataset with `source=client`, under the same privacy rules. It isn't needed to answer Ryan's question, which is about the server caches.

## 3. The latency benchmark suite

### 3.1 Extend `dt-cloud probe`, don't add a tool

`probe` already has the fetch seam, target resolution, a `-Q` query-set mode, `-o gs://…` records, and `-c`. The suite becomes a third mode: `dt-cloud probe -B <latency.yml> [-s states] [-n samples] [-o prefix] [--compare prev.json]`. The Playwright `site/bench` stays the tool for *render* time. This suite measures the API.

### 3.2 Controlled cache states: an admin-only bypass

Today, `-c` defeats L1/L2 by a random `minArea`. The other trick is choosing an unused 128-px `w` bucket (`QUANT` in `_lib/view.ts`). Both have the same flaws:

- They still **write** the answer to L1 and to KV for 30 days. That's a month of dead ~350 KB entries per cold sample, and on a dev stack the writes land in prod's KV.
- They can't reach L3 or L4 at all.
- `minArea` changes the answer slightly, so it isn't the user's query.

Replace them with a request parameter, **`_c=<layers>`**: a comma list of layers to bypass, from `edge` (L1 + L2), `l1` (L1 only: read KV, skip L1), `iso` (L3), `colo` (L4) and `all`.

- **Gate.** Honored only for an identity with `admin`, or a new **`bench`** scope that an admin can grant to a token from the existing grant console. A personal agent token carries only the base scope, so it can't. Any other caller sending `_c` gets **403**, never a silently warm answer.
- **The key ignores it.** `_c` is stripped before `cacheKeyFor` and before the box forward, so the answer is byte-identical to the user's.
- **Bypass means neither read nor write.** `edge` serves with `keep=false` (`x-cache-store: skipped`). `colo` skips every `caches.default` match and put in `index.ts`, `staticNames.ts` and `staticFilter.ts`. `iso` skips every L3 get, put and in-flight join, so a bench request neither benefits from nor evicts real users' entries. A bypassed bench run leaves no trace in any cache.
- **Mechanism.** A per-request policy on `env` under a symbol (`env[CACHE_POLICY]`), the same pattern as `IV_SLICED` / `IV_ASOF`. It's read at each cache site through three helpers: `coloMatch` / `coloPut` around `colo()`, and an `Lru` / `shared` wrapper. `withPathStore` already shows how a per-request env flows through.
- **Echo.** The response's `x-dt-layers` shows the bypass (`edge=bypass`, `rgroup=0i0c25o`). The runner **asserts** the state it asked for was the state it got, and records a sample whose layers disagree (an `i` hit under `_c=iso`) as invalid rather than timing it. This is the main reason increment 1 comes first.

The states the suite runs. Request order is edge → L3 → L4 → origin:

| state | `_c` | models |
|---|---|---|
| **warm** | (none), after one priming request | a repeat view: L1 hit |
| **edge-miss** | `edge` | a new view on a warm isolate. Reader caches warm |
| **kv-hit** | `l1` | the first viewer in a colo for a warmed key |
| **iso-cold** | `edge,iso` | a fresh isolate in a warm colo (the common real cold case) |
| **colo-cold** | `edge,iso,colo` | the first request for a scan in a colo: every read from origin |

A truly **fresh isolate** (module init, JIT, the 128 MB heap empty) can't be forced from outside. `iso` models its data-cache state. The real cost of a new isolate comes from RUM (`iso n=1` vs `n>1` at the same edge/colo state).

Other ways to get colo-cold, and why not:

- **Bumping `INTERVAL_STORE_REV`** re-keys the interval store's L3/L4/L1 keys for *every* viewer. It's a deploy-wide cold start, not a bench state, and it doesn't touch per-scan caches.
- **Waiting out TTLs** isn't controllable.

### 3.3 The query catalogue

`bench/latency.yml` per deployment branch (gcs, cw-s3, r2). The query ids are shared. The parameters are roles **resolved from the deployment** at run time, as `probe.resolve_targets` does, so one catalogue spec serves every store, and each branch's file pins only what can't be resolved (filter terms).

Resolved roles:

- `@latest`, `@prev` (the scan before it);
- `@week` (≈ 7 days back);
- `@far` (the oldest scan the interval store holds);
- `@pre-iv` (a scan older than the store's base, so a per-scan read);
- `@bucket` (largest root child);
- `@deep` (follow the largest child to depth ≥ 6);
- `@wide` (the directory with the most children among the first three levels' largest);
- `@user` (top owner from `/api/owners`);
- `@pool` (a non-empty owner pool).

Each query is a page's request set or a single request. Concurrent sets (a page load, as `probe` does) are timed per request *and* as page wall time.

| id | request(s) | notes |
|---|---|---|
| `root-d1`, `root` | `/api/subtree?path=&depth=1`, then full | first paint and full |
| `bucket` | `/api/subtree?path=@bucket` (d1 + full) | |
| `deep` | `/api/subtree?path=@deep` | depth ≥ 6 drill |
| `wide` | `/api/subtree?path=@wide&depth=1` | flat dir, many children |
| `diff-near-root`, `diff-near-bucket` | `/api/diff?from=@prev&to=@latest` (d1 + full) | |
| `diff-week` | `/api/diff?from=@week&to=@latest&path=@bucket` | |
| `diff-far-root` | `/api/diff?from=@far&to=@latest` | widest walk the store holds |
| `diff-mixed` | `/api/diff?from=@pre-iv&to=@latest` | one side per-scan, one interval |
| `lens-user` | `/api/subtree?lens=user:@user` | slice sorts |
| `pool` | `/api/subtree?o=@pool` | ledger fold |
| `class` | `/api/subtree?cl=…` | class scope |
| `age` | `/api/age-pyramid?date=@latest&path=@bucket` | age lens |
| `read` | the root subtree's `reads` phase (read lens tiles ride the subtree) | reported as a phase, not a request |
| `series-root`, `series-bucket`, `series-filter` | `/api/series?path=…` (+ `q=`) | |
| `f-rare`, `f-common`, `f-prefix`, `f-suffix`, `f-hex` | `/api/subtree?q=…` (first paint + `full=1`) + `/api/filter-cover` | terms pinned per branch: rare (1–10 matches), common (≥ 100K), `^q`, `q$`, hex-ish (an 8+ hex run, `hexRuns.ts`) |
| `f-rare-diff`, `f-common-diff` | `/api/diff?q=…` | |
| `names-rare`, `names-common` | `/api/name-summary?name=…` (+ diff form) | `/names` |
| `staged` | the `/staged` page's `/api/plans…` requests | D1-bound |
| `pre-iv-root` | `/api/subtree?date=@pre-iv` | per-scan path (GCS) |

That's about 40 request shapes.

### 3.4 Samples and statistics

- **Order.** Per run: for each query × state, **5 samples** (warm: 7, they're cheap), interleaved round-robin across queries and states in a shuffled order seeded per run. A slow minute then spreads over everything instead of landing on one query.
- **Concurrency.** Requests within a page set fire concurrently as the browser's do. Sets themselves run one at a time.
- **Report per cell** (query × state × deployment):
  - median;
  - p10 / p90;
  - min;
  - the IQR as a percentage of the median;
  - `n_valid` (samples whose `x-dt-layers` matched the asked state);
  - the medians of the main phases;
  - origin rounds and bytes, colo, and store.

  Never a single sample. Today's ±30% single-browser numbers are what this replaces.
- **Run-to-run comparison** is median vs the trailing median of the last 7 runs' medians. A cell is a **regression** when that ratio is > 1.25 *and* the difference is above 2× the trailing IQR *and* it holds on two consecutive runs. A **step** (what a deploy does) is flagged on the first run when the build sha changed and ratio > 1.5.
- **Colo is recorded per sample.** A run whose samples landed on more than one colo reports per-colo cells, so they can't blend.

### 3.5 Per-scan vs interval store: separating layout from provider

The per-scan stores read GCS through S3-compat from Cloudflare (cross-provider). The interval store reads R2 through a binding. "Interval faster than per-scan" mixes two causes: fewer/better-planned reads (layout), and a nearer origin with lower per-read latency (placement). To separate them:

1. **Measure the provider term in every run.** Generalize `api/static-bench.ts` into an admin/bench-gated `GET /api/bench/origin?size=262144&n=16&par=1|8`. It times *n* same-size range GETs against a fixed object in each origin (one in `STORE_BUCKET` via `makeStore`, one in `INDEX_R2`) from the same isolate. It reports per-read p50/p90 and the 8-wide wall time. Run it at the start and end of each bench run, from the same colo.
2. **Report reads, not just ms.** With `range=…o;b=…;r=…`, each colo-cold sample records origin read count, bytes and critical-path rounds. A sample's normalized cost is `rounds × RTT_p50(provider) + bytes / throughput(provider)`, plus the rest (decode, D1, walk). The report shows each store's cold result **as measured** and **re-costed at the other provider's RTT/throughput**. Then "interval on R2 vs per-scan *as if* on R2" is a layout comparison.
3. **The clean A/B, once.** Copy one per-scan generation (a date's `path` + `bysize` sorts and their `.groups.parquet`) from GCS to R2. Serve it as a secondary store (`store=` overlay, `specs/done/multi-store.md`) whose `STORE_*` target is R2's S3 endpoint, and run the catalogue's per-scan queries against it.
   - per-scan-GCS vs per-scan-R2 is the pure provider effect;
   - per-scan-R2 vs interval-R2 is the pure layout effect.

   It's a one-off copy of a few GB. It validates step 2's model, after which step 2 alone suffices.

### 3.6 Where it runs, where results go

- **Schedule.** A GHA workflow per deployment branch (`latency-bench.yml`) holding a `bench`-scoped `SITE_TOKEN` secret:
  - nightly against prod, after the day's scan and `warm-cache`, so `warm` / `kv` reflect what viewers get;
  - on each deploy of the dev stack (`workflow_dispatch` from `site/deploy --dev`).

  The runner's location decides the colo. GH runners mostly land on US-east colos, while OA viewers are US-wide. The colo is recorded and a cell is never blended across colos. A second runner in us-central1 (the gcs Batch job's region, as a step after `warm-cache`) is optional.
- **Results.**
  - One JSON record per run to the deployment's private bucket (`gs://oa-gcs-usage-dvx/bench/latency/<ts>.json`; cw-s3's own; r2's under `INDEX_R2`), via the existing `-o` writer.
  - Each sample also goes to the RUM dataset as `source=bench`, so `dt-cloud rum` and the bench share one query path and trend charts.
  - The JSON is the source of truth for reruns and diffs (`--compare`).
- **Alerts.** On a regression (§3.4), post to the deployment's Slack digest channel (the existing bot; `thrds slack post`) with the cell, the medians, the build shas, and the run record's link. Failures (5xx, invalid states) alert too.

### 3.7 Cost

| item | estimate |
|---|---|
| Analytics Engine writes (RUM + bench) | ≪ the Workers Paid plan's included 10M data points/month (gcs: ~10⁴–10⁵/month). **$0** |
| AE reads (CLI, digest) | ≪ the included 1M queries/month. **$0** |
| Bench requests | ~40 shapes × 5 states × 5–7 samples ≈ 1,100 requests per run per deployment |
| Bench Workers CPU | Colo-cold and iso-cold samples decode for real, ~2–3 s CPU each: ~400/run ≈ 1,000 CPU-s/day ≈ 30M CPU-ms/month. That's about the plan's included CPU, so marginally **~$0.50/month** |
| GCS egress for per-scan colo-cold samples | ~5–20 MB each, ~100/run ≈ 1–2 GB/day ≈ **$4–7/month** at internet-egress rates. The biggest line item. Halve it by running the per-scan cells every other night |
| R2 | no egress. Class B reads, cents |
| GHA minutes | ~20–25 min/run per deployment, serial by design. Inside the free/private allowance at nightly cadence |
| Middleware overhead per request | header parse + one `writeDataPoint`: sub-ms, not awaited |

The pricing is as published for Workers Paid / Analytics Engine as of this writing; re-check before relying on it. Nothing here needs a new paid product.

## 4. Sequencing

| # | increment | size | contents |
|---|---|---|---|
| 1 | **Markers** | small: ~1 day | §2.1: the `x-dt-layers` counters at every cache site in `index.ts` / static stores; `iso` / `colo` / `store`; D1 count/ms via `meta.duration`; rename the lookup phase to `ecache` + `cache;desc=`; hit replay (`x-dt-origin-timing`, entry age, engine; KV metadata); middleware stamps on every `/api/*`; session log keeps `st` + `ly`; `probe` records the full Server-Timing + `x-dt-layers` instead of `total` only. Tests: `edgeCache.test.ts` (hit replays, KV metadata), `index.test.ts` / `intervalStore.test.ts` (counters per tier), the middleware |
| 2 | **Bypass** | small: ~1 day | §3.2: the `_c` policy, `bench` scope, 403 for others, key stripping, no-write semantics; `probe -s warm,edge,l1,iso,colo` replaces `-c`'s `minArea` trick |
| 3 | **RUM** | medium: 1–2 days | §2.2–2.4: the AE binding per deployment (`cf/` Pulumi has no AE resource to create: the binding names the dataset), middleware writer, `_lib/rum.ts` slot table, `dt-cloud rum`, the `/privacy` sentence, a weekly digest line |
| 4 | **Bench suite** | project: 3–5 days | §3.3–3.6: `latency.yml` + role resolution, `probe -B` with sampling/statistics/validation/`--compare`, records + `source=bench` rows, `latency-bench.yml` on each branch, Slack alerts, `/api/bench/origin`, the model re-costing |
| 5 | **The A/B copy** | small once 4 exists: ½ day + copy | §3.5.3 |
| 6 | optional | 1–2 days each | `/admin/latency` page over AE; client-side RUM beacon (§2.5); `?_t=precise` |

### 4.1 Recommendation: increment 1 first

Increment 1 is a day's work and, on its own, answers Ryan's first question. It turns the existing session log (switch it on for a week), `dt-cloud probe`, and every DevTools look into an exact account of which layer answered. That matters this week: gcs prod just moved its path reads to the interval store, and `gcolo` / `gcache` alone can't tell us whether its L4 footer/state copies are hitting.

Everything after builds on it:

- the bypass's state validation reads `x-dt-layers`;
- RUM parses it;
- the bench records it.

Increment 2 follows directly. Together they are ~2 days, and they make `probe` runs comparable with no more minArea tricks or KV pollution. RUM (3) is next, because "what fraction is warm" is the open question and only real traffic answers it. The scheduled bench (4) is the one real project, and it gets better from having two weeks of RUM to choose its budgets.

## Pending colo puts: a bound was measured and parked (2026-11-06)

`putColoGroup` has no backpressure, so a read can return with its copies still queued. Colo-cold (the state after every new generation), peak pending reached 30–190 MiB at p90, mostly JSON before gzip, and killed requests had last logged 83–135 MiB. So one bounded probe compared the same cold bursts with and without a bound on pending bytes (16 MiB, skipped before `JSON.stringify`), code swapped between aliases mid-run, one colo.

| Kind, at once | Requests each way | Memory kills, off | Memory kills, bound | Peak pending p90, off / bound (MiB) |
|---|---|---|---|---|
| plain 16 | 160 | 6 | 0 | 115–135 / 18 |
| plain 32 | 320 | 2 | 8 | 136–162 / 18 |
| scoped 12 | 120 | 0 | 0 | 119–190 / 18 |
| plain 64 | 448 | 176 (39.3%) | 66 (14.7%) | 131–158 / 18 |

- Plain 64: the bound won all 7 paired bursts (sign test p ≈ 0.016), in both swap phases, so it isn't an alias effect. Isolates killed: 22 → 8.
- Plain 16 and 32: 8/480 both ways, each count one isolate dying. No benefit shown at realistic concurrency.
- Answers: 888 of 888 pairs byte-identical. 31% of puts skipped.
- Second pass: plain median 4.0 → 5.6 s, scoped unchanged at 6.2 s; a third pass, to show recovery, wasn't run.
- The baseline moves with time: the unbounded build had no kills for about 50 minutes, then they began, cause unknown. Compare only within a paired run.
- Kills left under the bound last logged 16 MiB pending, with 6–12 requests in flight (median 8). The unbounded build also survived 349 MiB of nominal pending. So pending alone doesn't decide a kill; several reads decoding at once is the likely remainder, inferred from logs, since a Worker's heap isn't observable.

**Parked.** No benefit at realistic concurrency, a cost on the second pass, where map latency is the priority, and 16 MiB rests on an assumed baseline (128 limit − 36 fixed LRUs − about 16 for code), so it has no margin we can defend. If kills appear at real load, an isolate-wide cap on concurrent decodes is the candidate to measure, since it bounds the cause inferred above. Separately, `exceededCpu` kills (9 in the run) hit only scoped reads, at low pending; a bound doesn't address them.

## The zstd decoder: wasm behind a flag (2026-11-06)

`ZSTD_WASM = "1"` decodes pages in zstd's own decoder (`site/functions/_lib/zstd.ts`), a statically imported wasm module, instead of `fzstd`. Off by default, on no deployment yet. The module is `zstddec` 0.3.4's decoder (zstd 1.5.7) with one edit, its memory section's initial size, 258 pages to 64; about 54 KiB in the bundle, a fifth of `@bokuweb/zstd-wasm`'s, at the same speed in Node and with a named, decoder-only surface.

Memory, measured on Workers by the JS allocation at which one request dies (no API reports an isolate's heap):

- Requests die at about 248 MiB of JS, not the documented 128. The module's whole initial memory is charged, touched or not: the upstream 16 MiB moved the threshold by 12–16 MiB, the edited 4 MiB by about 4.
- `memory.grow` past about 256 MiB throws a catchable `RangeError`; past the threshold in JS, the request gets a 1102.
- A decode is synchronous and frees all it took, so the heap's need is one frame's, not a sum over reads. The largest column chunk in gcs's served sorts decompresses to about 3 MiB, and a frame past `WASM_FRAME_MAX` stays on fzstd, so the heap can't grow by construction. Over 30 minutes on one isolate, about 4M frames, among them the largest the module takes: linear memory stayed at its initial size, no growth, one instance.

Paired bursts, as the bound's: both codes on the same cold URLs and fresh group namespaces, codes swapped between aliases between phases, one colo. Kills are exceededMemory + exceededCpu, requests each way, medians within the code.

| Kind, at once | Requests each way | Kills, JS | Kills, wasm | Wall median, JS / wasm (s) | CPU median, JS / wasm (s) | Bursts wasm faster / slower |
|---|---|---|---|---|---|---|
| plain 16 | 224 | 3 + 0 | 0 + 0 | 12.4 / 7.9 | 9.2 / 5.2 | 13 / 1 |
| plain 32 | 384 | 7 + 0 | 11 + 0 | 16.0 / 8.5 | 8.8 / 4.9 | 12 / 0 |
| plain 64 | 768 | 73 + 0 | 67 + 0 | 15.9 / 11.9 | 8.4 / 4.4 | 11 / 1 |
| scoped 12 | 168 | 0 + 2 | 0 + 0 | 20.4 / 13.4 | 13.2 / 7.1 | 12 / 2 |

- Memory kills: 83 vs 78, with no difference by burst (plain 64: 5 bursts each way). Pending colo puts peaked no higher (plain 64 p90 146 vs 119 MiB). So wasm neither fixes the memory kills nor adds to them.
- Warm (second pass, nothing to decode): the same, as it should be. Answers: 1675 of 1675 pairs byte-identical.
- At one request at a time on a long-lived isolate the gain is larger: read CPU median 16.9 → 5.0 s, wall 20.2 → 6.9 s, and `exceededCpu` 6 of 97 reads vs 0 of 243 (the heaviest scoped diffs, which fzstd can't finish inside the 30 s budget). Fewer requests fit in the same 40 minutes on JS, so the counts differ.

Colo copies, for the deferred-copy decision: at that same concurrency, copies off takes wasm's read CPU from 5.0 to 2.9 s and wall from 6.9 to 4.9 s (JS: 16.9 to 14.6 s). A put costs about 20 ms, so copies go from about a seventh of a cold read's CPU to about two fifths.

## A broad term's phase 1, folded to what can be drawn (2026-11-07)

A broad literal under one bucket (`checkpoint` under `marin-us-east5`, `results` under `marin-us-central2`: about 97K match roots each, over 250K forest nodes) held every root's aggregate and every ancestor's sums (`Phase1Memo`) through phase 2. A diff held two, and was refused past `HELD_BYTES` (`diff-too-broad`). Both now fold phase 1 (`foldPhase1`, `view.ts`): a diff side since `752732e0`, the single-date map (first paint and full read) since this branch, as a diff with no other scan.

**What a read holds.** Per scan, at a canvas of w×h and tile budget k = ⌊w·h / `TILE_AREA`⌋:
- the k heaviest forest nodes over their level's threshold, in `capTiles`'s order (bytes, then shallowest, then path);
- the drawn roots phase 2 reads (`REGION_READS`, 24) and looks up (`ROOT_DETAILS`, 48);
- the `MATCH_LIST_CAP` (200) heaviest roots not held already, for the match list;
- exact totals, the match count, and per held node its children's count (`kids`), for the `(other)`s.

That is at most k + 72 aggregates plus 200 list entries for a map, and 2k + 144 plus 200 for a diff side, whatever the answer's size. A first pass sums bytes, objects and liveness per node: each hit into its root, each root's ancestors made once (a walk up stops at the first node it knows), then level by level upward. A second pass sums owners into the held nodes only.

**Why nothing drawn is lost.** `capTiles` keeps the k best of everything the read drew (forest and phase-2 rows) in a total order, then drops any node whose parent fell out. A forest node outside its scan's k best forest nodes has k forest nodes ahead of it, all in that set, so it is never kept. A node under its threshold is never a candidate. An ancestor ranks ahead of its descendants: it holds at least their bytes and its threshold is lower. Phase 2 reads and looks up the heaviest drawn roots, and those are held. The response lists every kept root, then the 200 heaviest of the rest, and those are among the held or the listed. Counts and totals come from the first pass, exact. Tests: every term, path, scan, canvas, tile budget and first paint gives the unfolded body byte for byte (`staticDrill.test.ts`). A budget one short fails that sweep. On real bytes (Node replay, phase 2 and detail budgets lifted so a body depends on bytes alone), folded and unfolded bodies match on 31 of 31 answered cases: 7 path/term pairs × 3 canvases × first paint and full, the rest refused or unrecorded alike. The cover (`allMatchRoots`) still reads unfolded: it lists every root.

**What a new canvas costs.** The fold is cached per isolate by canvas and threshold, beside the held answer. A broad answer is past what an isolate holds (`HELD_BYTES`), so it is re-read per request anyway and every request folds: two passes over the hits, 170–280 ms in Node at these sizes, where the unfolded memo cost about the same to build. For a held answer, a new canvas costs one fold instead of reusing the canvas-free memo.

**Memory on Workers**, by ballast: each trial a cold isolate and colo namespace holding N MiB, in 1 MiB chunks, for its life; a read's peak is the calibration request's survivable ballast (243–247 MiB) less its own, ±4 MiB per bisect. Single-date maps at 1152×2304, scan `2026-11-07T0701`:

| Map | Today: survived / peak (MiB) | Folded: survived / peak (MiB) |
|---|---|---|
| `marin-us-central2`, `results` | 37, 41 / 204–208 | 123, 123, 119, 119 / 122–126 |
| `marin-us-east5`, `checkpoint` | 37, 37 / 208 | 37, 41, 37, 37 / 204–208 |
| fleet root, `checkpoint` | 49, 74 / 171–196 | 37, 41, 37, 66 / 179–208 |

- The fleet root answers from rollups, not roots, so it doesn't fold; its spread is the method's noise.
- `checkpoint` under `marin-us-east5` didn't move. Phase stops (`x-evp-stop`) show where its peak is. Today: survived 132 after the answer's read, 115 after phase 1, 41 after phase 2, so the memo plus phase 2's decodes make the peak. Folded: 129 after the read, but 33–37 after phase 1, so the fold's own transient work makes the peak instead. Making each ancestor once didn't move it (33 before, 37 after). The fold's working arrays and path map are per node, and they come right after the answer's read, whose garbage is still uncollected. Typed, presized arrays are the candidate. Inferred, not measured.
- CPU (tail `cpuTime`, a request and its `waitUntil` work). Medians of 3, one request at a time; warm means the same namespace, the response cache salted, so the isolate's memos and the colo's copies are kept. `results`: cold 7.1 s today vs 7.3 s folded, warm 8.0 vs 3.9 s. `checkpoint` under `marin-us-east5`: cold 5.2 vs 9.3 s, warm 3.7 vs 5.2 s. The fleet root: cold 11.1 vs 10.4 s, warm 7.9 vs 8.2 s (no fold there). Both builds return the same first answer, but only the folded one's background upgrade then reads phase 2 (76 more path groups). Today's upgrade decodes nothing after the response, so it most likely fails and gives up silently (`upgradePartial` swallows the error). Inferred, not measured. In Node, per request, cold / warm: `checkpoint` 3.9 / 2.8–2.9 s folded vs 4.4 / 3.4–3.7 s unfolded; `results` 3.3 / 2.2–2.5 s vs 4.7 / 2.4–2.6 s.

**Paired page loads**, deciding whether both folds ship to gcs. A real browser (headless Chrome, 1280×800) loaded `/marin-us-east5?f=checkpoint` cold: a fresh namespace per round, so isolate memos are cleared and colo keys namespaced. Four builds from `o/gcs`, all with wasm zstd on and prod's flags at run time. **A**: today. **B**: + the diff fold (`752732e0`). **C**: + both folds (this branch). **D**: + the map fold alone (C with the diff fold off). Two streams ran at once, each interleaving the arms in fixed blocks, offset so that the two streams were always on different arms. 40 rounds per arm. Kills come from the deployments' tails, page CPU is Σ tail `cpuTime` over the round's requests, and "stops changing" is the last change in the drawn cell count.

| Arm | Rounds with a 503 | 503s | Killed (exceededMemory) | First map med / p90 (s) | Map stops changing med / p90 (s) | Page CPU med (s) | Diff on the page | Whole at end |
|---|---|---|---|---|---|---|---|---|
| A, today | 6 / 40 | 6 | 7 | 7.8 / 11.6 | 26.4 / 34.3 | 55.0 | refused (`diff-too-broad`) 40 | 40 / 40 |
| B, diff fold | 12 / 40 | 33 | 23 | 8.2 / 16.8 | 23.8 / 38.6 | 68.9 | rows 40 | 40 / 40 |
| C, both folds | 1 / 40 | 1 | 1 | 7.8 / 10.7 | 19.8 / 25.5 | 55.9 | rows 40 | 40 / 40 |
| D, map fold | 0 / 40 | 0 | 0 | 7.8 / 11.5 | 18.6 / 26.3 | 45.5 | refused 40 | 40 / 40 |

Sign tests over the 40 blocks (two-sided; discordant blocks only):
- **C vs A.** Rounds with a 503: 1 vs 6, p = 0.125. Rounds with a kill: 1 vs 7, p = 0.070. The map stopped changing sooner in 37 of 40 blocks, p < 0.001. C is no worse than A, and the diff answers.
- **B vs A.** The diff fold alone is worse: 12 vs 6 rounds with a 503 (p = 0.24), and 23 kills vs 7. Ship it only with the map fold. **B vs C**: 12 vs 1, p = 0.003.
- **D vs A.** 0 vs 6 rounds with a 503, p = 0.031. **C vs D**: 1 vs 0, p = 1. Answering the diff costs about 10 s of page CPU (55.9 vs 45.5 s median).
- **Plain guard** (`/marin-us-east5`, 26 rounds per arm): no 503s and no kills on any arm. Maps stopped changing at a median of 7.7–8.5 s on every arm (no block test below p = 0.3), and page CPU was 11.6–14.0 s.
- About a quarter of A's loads had a 503 on an earlier day. This day's A had 6 of 40, so compare only within a paired run.

**Open, untested.** Don't assume these fit:
- Owner-scoped, pool and `NOT` diffs still build the unfolded memo on both sides, and are refused past `HELD_BYTES` (`tooBroadToDiff`). The fold has no owner scope or exclusions yet.
- An answer that doesn't cover the other scan: the fold reads both scans' bytes from one answer, so such a diff stays unfolded and refused past the bound.
- The fold's own transient memory on the broadest answers (above).
