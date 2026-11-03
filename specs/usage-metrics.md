# Usage metrics: who drives load and spend

Status: phases 1–3 built (2026-10-21; shared code, so `[cloud]`, gcs's wiring in its own commits). Phase 1 and `/admin/usage` are on dev.gcs.oa.dev; the `cf-usage` step reaches the daily job with the next image rebuild. Prod is not deployed, so prod's Functions requests carry no usage point yet.

## As built

- `site/functions/_lib/usage.ts`: the `Meter`, the route table (`ROUTES`; `usage.test.ts` pins that every Function file maps into it), caller resolution, the point and the binding instrumentation. The env's R2 / D1 / KV bindings are wrapped once per isolate (the env object is the isolate's); each call charges the meter of the request it runs in, found through `AsyncLocalStorage`. That needs the `nodejs_als` compatibility flag in the deployment's `wrangler.toml` (gcs has it). The module is imported by a computed specifier, so a deployment without the flag still builds: its points carry route, caller, status and store, with zero counters and cache `none`.
- `via` = `token` for a grant presented as `Authorization: Bearer`, `grant` for a share link's cookie session. The caller is the grant's name, else its note, with emails masked and cut to the 96-byte index. `bot` uses a user-agent regex (`BOT_UA`); the site had no earlier heuristic.
- `blob8` = `prod` on `PROD_HOST`, else `dev`: gcs's dev stack writes to prod's dataset.
- R2 counts are Class B reads (`get` + `head`). R2 bytes are the ranges actually returned.
- `double8` = D1 rows written (the result's `meta.rows_written`), added with phase 3.
- `dt-cloud cf-usage` (`cloud/src/dt_cloud/cf_usage.py`): one GraphQL query per dataset, `$CF_ANALYTICS_TOKEN`, verified against the live schema (2026-10-21).
  - **Pages scripts.** `pagesFunctionsInvocationsAdaptiveGroups` never names a script by its project: a project's two scripts are `pages-worker--<n>-production` and `pages-worker--<n>-preview` (gcs: `pages-worker--16777874`). Only the Pages API maps one to the other (`production_script_name`), and the analytics token can't read it. So the stem is configuration: `CF_PAGES_SCRIPT` in the wrangler config's `[vars]` (or `-s`). The query filters on `scriptName_in` and groups by hour; the report and snapshots split production and preview. Without a stem, the account's total is used, with a note.
  - **CPU.** No dataset has a CPU sum for a Pages script (`workersInvocationsAdaptive.cpuTimeUs` covers Workers only). The Functions dataset has CPU quantiles p25…p99.9 (µs) and `wallTime` (µs). Each hourly group's mean is the quantile function integrated by trapezoids (0 at p=0, p99.9 held to p=1), capped at the group's wall time. The tail carries the mean: on 2026-10-14, gcs prod's p50 was 33 ms, its p90 3.2 s and its mean about 1 s, so requests × p50 under-counted about 30×. The open ends bias the estimate low.
  - `-o ROOT` (repeatable): one snapshot per day, written for the window's days any root lacks (`-f` rewrites all of them). Attribution in snapshots covers both hosts.
- **Snapshots** (`snapshots/usage/<date>.json`, `cf_usage.snapshot`, `version: 1`): `date`, `project`; `units` (every `PRICES` unit, storage as the day's GB level); `usd` (list $ per unit before the included amounts, a storage unit's day being a 30th of its month); `plan` (per unit: `label`, `usd` per `per`, monthly `included`, `storage`); `functions` (`{production, preview}` → `{requests, cpu_ms}`); `by_caller` (`caller`, `via`) and `by_route` (`route`), each with the `AE_SUMS` (`requests`, `wall_ms`, `r2_reads`, `r2_bytes`, `d1_queries`, `d1_rows_read`, `kv_reads`, `d1_rows_written`), top 100 by requests plus `(other)`; `notes`. Integral numbers are ints and the rest are rounded to 6 places, so a rewrite gives the same bytes.
- **Job step** `cf-usage` (`scan_steps.py`, post stage, after `digest`): not core, because a core step would need every store profile to skip it. A deployment lists it in `steps` once its token is a secret. It runs `dt-cloud cf-usage -c site/wrangler.toml -d 7 -o <data>/snapshots/usage [-o $R2_ARCHIVE/snapshots/usage]`, so a missed day is backfilled by the next run. It is skipped without `CF_ANALYTICS_TOKEN` or on REPROC. It is soft: a failure fails only its phase. The image now carries `site/wrangler.toml`. gcs: `steps: [..., cf-usage]`, `secrets: CF_ANALYTICS_TOKEN: gcs-cf-analytics-token`, the same secret in `job/batch-submit.sh`, and the same write in `job/run.sh` (the one-VM fallback) after the Discord digest.
- **`/api/usage`** (admin, `?days=1..92`, default 30): the newest documents from the served store (`INDEX_R2` under `SERVED_R2`), oldest first, version 1 only. `/data/*` serves only `snapshots/<scan id>/`.
- **`/admin/usage`** (`UsagePage.tsx`, `usageModel.ts`, its own chunk; linked from `/admin` and the ☰ menu). URL state: `days` (7 / 30), `attr` (caller / route), `um` (ranking metric). It shows:
  - tiles: the overage $, the list $, and D1 rows written's share of the overage;
  - a units table: used, included pro rata, over, over $ and list $, with D1 rows written marked;
  - stacked daily bars of list $ by product, D1 rows written at the base, with a per-day tooltip;
  - the attribution table, top 12 plus `(other)`, with each row's share of the ranking metric and the coverage of billed requests by usage points.
- **First numbers (gcs, 30 days to 2026-10-21):** D1 rows written are about three quarters of list $ and about half of the overage. They are the job's `index-sync` footer writes through D1's HTTP API, so the site's attribution doesn't see them; `specs/d1-footer-writes.md` works on that. R2 storage is next. Billed usage before 2026-10-01 (the path store's launch) is near zero.


## Why

We have no view of what drives a deployment's Cloudflare load and bill. Today the only signals are Cloudflare's dashboard totals and our own scan costs. There is no way to answer:
- "the sheet-sync token is half our R2 Class B ops"
- "the agent API's `/api/subtree` drives the D1 rows read"
- "warm-cache after each scan costs $X/mo"

Requests come from browsers (sessions), share links, agent and CLI tokens, the job's grants (snapshot job, sheet sync, session monitor), crawlers and anonymous OG fetches.

## Two sources

1. **Cloudflare's billed totals**, via the GraphQL Analytics API, per account and day:

   | Product | Dataset | Measures | By |
   |---|---|---|---|
   | Pages Functions | `pagesFunctionsInvocationsAdaptiveGroups` | requests, errors, CPU time | script, status |
   | R2 | `r2OperationsAdaptiveGroups` | Class A/B ops | bucket, action |
   | R2 | `r2StorageAdaptiveGroups` | storage | bucket |
   | D1 | `d1AnalyticsAdaptiveGroups` | rows read/written, queries | database |
   | KV | `kvOperationsAdaptiveGroups`, `kvStorageAdaptiveGroups` | ops, storage | namespace |

   These are the units the bill uses. They have no notion of caller or route.
2. **Our attribution**, through Workers Analytics Engine: one data point per Functions request, written by the root middleware (`site/functions/_middleware.ts` `onRequest`, after the response, via `waitUntil`). One dataset per deployment (binding `USAGE`, dataset `disky_usage_<deployment>`).

   | Field | Content |
   |---|---|
   | `blob1` route | normalized pattern, e.g. `/api/subtree`, `/data/*`, `/assets/*`, `page`; never raw paths (cardinality, privacy) |
   | `blob2` method | |
   | `blob3` via | `session` / `grant` / `token` / `public` / `bot` |
   | `blob4` caller | the grant or token's **name or note**, the job grants by name, `session` for any signed-in browser. Never an email: the admin page joins to identities where it's allowed to show them |
   | `blob5` status class | `2xx`, `3xx`, `4xx`, `5xx` |
   | `blob6` cache | `edge-hit` / `kv-hit` / `miss` / `none` |
   | `blob7` store | `primary` / a `STORES_JSON` key |
   | `double1` | 1 (the count) |
   | `double2` | wall ms |
   | `double3` | R2 GETs made |
   | `double4` | R2 bytes read |
   | `double5` | D1 queries |
   | `double6` | D1 rows read, where the D1 result's `meta` gives it |
   | `double7` | KV reads |
   | index | `caller` |

   - **How the counters are filled:** routes report their caller and counts on a request-scoped meter, e.g. `meter(ctx)`, created in the middleware and threaded through `ctx`. The R2 and D1 seams that already exist bump it: the `INDEX_R2` wrappers in `_lib/index.ts`, the D1 helpers, and `edgeCache.ts` for the cache field. A route that reports nothing still gets a point, with `via` = what the gate identified, or `public`.
   - **Cost:** included in Workers Paid up to 10M data points/month (gcs is far below that). Points are sampled at high volume, so queries use `_sample_interval` (`SUM(_sample_interval * double3)`).

## Surfaces

- **`dt-cloud cf-usage [-d DAYS] [-j]`.** Pulls (1) for the account, filtered to the deployment's script/buckets/database/namespaces from its wrangler config, and (2) through the Analytics Engine SQL API. Prints a per-day table: billed units and list-price $, then the attribution by caller and by route.
  - Needs a CF API token with Account Analytics: Read. The existing `CLOUDFLARE_API_TOKEN` doesn't cover it (GraphQL: "not authorized for that account"), so a new read-only token is needed, as `$CF_ANALYTICS_TOKEN`. It is a secret and goes through the user.
  - Prices live in one table in code: R2 Class A/B, storage, D1 rows read/written, Functions requests/CPU, KV.
- **The daily job** runs it after `digest` and stores `snapshots/usage/<date>.json`, so `/admin/usage` reads a file, not a live API call per view. The same token is needed as a job secret.
- **`/admin/usage`** (admin):
  - totals vs the plan's included amounts and the overage $;
  - stacked bars by day: requests, R2 ops, D1 rows read, by caller (top N + other);
  - a route × caller table for a chosen window.
- **Instrumentation-only first:** phase 1 can ship before the token exists. The data points start accumulating, and Analytics Engine keeps 3 months.

## Open

- Deploy prod (phase 1's meter and `/admin/usage`) and rebuild the job image, so the 07:00 run writes the snapshots. The daily cron body (`PIN=1 DRY=1`, infra/gcp) only matters for `NF=0`; the Nextflow tasks read the profile's secrets.
- Other deployments: each adds `CF_PAGES_SCRIPT`, a `CF_ANALYTICS_TOKEN` secret, and `cf-usage` in `steps`. Until then, their deployment specs list it as a gap.
- Option: give the token Pages: Read, so `cf-usage` can resolve the stem from the Pages API instead of configuration.

## Phases

1. Middleware meter + Analytics Engine write + the `USAGE` binding (gcs `wrangler.toml`, prod and preview). Unit tests for the route normalizer, the caller resolution (session / grant name / token / public / bot) and the point shape. Deploy; verify points arrive with the SQL API, or on the CF dashboard if there is no token yet.
2. `dt-cloud cf-usage` (GraphQL + Analytics Engine SQL), with a fixture-tested parser and the price table.
3. The job step + `snapshots/usage/`, then `/admin/usage`.

## Parity

Shared code, so every deployment gets it unasked. Each deployment adds its own binding, dataset name and token, and the deployment specs list the binding as a gap until they do.
