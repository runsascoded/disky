# R2 access-log collector

Private, bounded RAC collector for `jc-taxes`. The same code can be instantiated for HCCS/OA using separate account configuration, stacks, credentials and archives. It also feeds the r2.rbw.sh demo's read lens: derived, public per-hour files of per-object request counts (below).

## Ownership

Wrangler owns the non-secret config and bundling; Pulumi owns the archive bucket, private bucket setting, retention rule, Worker, private Worker setting and cron settings. Do not run a live `wrangler deploy` over the Pulumi-owned Worker. Runtime `CF_OBSERVABILITY_TOKEN` is installed separately, not embedded in code/config or Pulumi inputs. Native source-bucket Data Access Logs enablement remains a dashboard prerequisite; the RAC pilot already has it enabled.

The first apply has cron **disabled**. The private `disky-r2-access-logs` archive is protected against stack deletion, with a **30-day deletion lifecycle on `runs/`**. Protection does not stop lifecycle expiration. Neither `r2.dev`, `workers.dev` nor preview URLs are enabled, and the HTTP handler always returns 404. The initial apply does not change `jc-taxes`, OA or HCCS resources.

## Build and verify

From the repository root, using its existing infrastructure environment:

```bash
WRANGLER_LOG_PATH="$PWD/tmp/r2-access-wrangler.log" pnpm --dir infra/cf/r2-access types
pnpm --dir infra/cf/r2-access typecheck
pnpm --dir infra/cf/r2-access test
WRANGLER_LOG_PATH="$PWD/tmp/r2-access-wrangler.log" pnpm --dir infra/cf/r2-access build
infra/.venv/bin/python -m pytest -q --disable-warnings infra/tests/test_r2_access_stack.py infra/tests/test_cf_stack.py
```

Applied 2026-10-17 with cron **off**: the previewed plan (six Cloudflare resources plus the stack, creates only, `schedules: []`) went through `up --plan`, and a follow-up preview showed 7 unchanged. The Worker exists without its runtime secret, so it cannot query anything yet, and no schedule invokes it. Preview/apply logs are in ignored `tmp/r2-access-{preview,up}.txt`. Provider warnings say certain configuration resources cannot be removed by destroy alone: explicitly disable cron through an apply before decommissioning the Worker/stack; do not assume destroy stops collection.

## Preview/apply handoff

Local passphrase-encrypted state lives in **`state/` beside this file**, matching the existing RAC m3 stack convention. Keep/back up that state and its passphrase; it is not disposable benchmark scratch. Every credential comes from **`wt/m3`'s environment**: the RAC deployment token, the state passphrase and the runtime Observability token (`CF_OBSERVABILITY_TOKEN`, no expiry; the root `.envrc` copy is the expiring pilot token). Do not substitute an OA token. Put any `env` *inside* `direnv exec`, never outside it.

From the repository root:

```bash
direnv exec wt/m3 pulumi -C infra/cf/r2-access preview --stack rac --diff --save-plan "$PWD/tmp/r2-access-preview-plan.json" --suppress-outputs
direnv exec wt/m3 pulumi -C infra/cf/r2-access up --stack rac --plan "$PWD/tmp/r2-access-preview-plan.json"
```

After creation, install the runtime secret from that same environment, piped so the value is never echoed or typed:

```bash
direnv exec wt/m3 sh -c 'printf %s "$CF_OBSERVABILITY_TOKEN" | pnpm --dir infra/cf/r2-access exec wrangler secret put CF_OBSERVABILITY_TOKEN'
```

Then set `disky-r2-access:secretReady: true` and `disky-r2-access:enabled: true` in `Pulumi.rac.yaml`, rebuild, preview (expect only the cron trigger's `schedules` to change), and apply that second plan. Inspect the first scheduled run's private `state.json`, its `_SUCCESS.json` and the sanitized Worker execution logs before treating this as an operating collector.

## Collection contract and limits

**The UTC hour is the unit.** One mode serves a quiet bucket (RAC `jc-taxes`, hundreds of events a day) and a busy one (OA `marin-na`, ~80K an hour; sizing in the [collector contract]). An hour's aggregate is a pure function of one complete drain of that hour; a later drain of the same hour *replaces* it. Event ids are deduplicated within one drain only and never persisted outside the raw pages.

Windows are half-open `[from, to)` in milliseconds. The telemetry API excludes **both** timeframe bounds (measured; see the spec), so every query sends `from − 1`, and events outside the hour are refused.

One cron (`HOURLY_CRON`). Each invocation schedules hours in priority order, oldest first within each list, while its budget lasts:

1. **New** settled hours: `hour end + LAG_HOURS ≤ scheduled time`, from the bucket's `collected_through` on (or `LOOKBACK_HOURS` back on the first run).
2. **Early** re-drains: hours whose latest drain ran less than `OVERLAP_HOURS` after their end, once a re-drain would no longer be early. With `LAG_HOURS < OVERLAP_HOURS` every hour is drained twice (RAC: 1 and 2); with `LAG_HOURS ≥ OVERLAP_HOURS` once (measured delivery latency is 13–25 s, so either is safe).
3. **Re-verify**: up to `VERIFY_HOURS` already-collected hours within `RETENTION_HOURS` (the provider keeps 7 days), round-robin from the bucket's `verify_cursor`. This replaces the old 144-hour daily replay: a busy bucket's whole week no longer has to fit one invocation.

Per hour, sequentially (one hour's drain in memory at a time):

1. A weighted **count** (calculation view) for the hour. It sizes the hour's page reservation, `⌊estimated / PAGE_SIZE⌋ + 2` (the rows, the empty last page, one page of margin for late deliveries), and gives the sampling check. An hour whose reservation exceeds `MAX_PAGES` can never fit an invocation: **`hour_over_cap`**. One that exceeds what's left of this invocation's pages waits for the next invocation (as do hours past `MAX_HOURS`).
2. The **drain** to an empty cursor page, each page written immutably to `runs/<scheduled>/<run id>/<bucket>/<YYYY-MM-DDTHH>/NNNN.json` (private, expired after 30 days).
3. The **aggregate** (`schema_version` 4): `events` (distinct), `own`, per-`actions` and per-`classes` totals over every action, `sampling` (`observed`, `estimated`, `max_sample_interval`, `capture`), and `rows`: `[key, action, n, first_ts, last_ts]` per (object key, action) for **reads only** (`ROW_ACTIONS`: `GetObject`, `HeadObject`), times **truncated to the minute**. Writes, deletes and listings are only counted: a write's recency is the listing's `mtime`, and at write-heavy marin-na per-key write rows were 99.8% of the aggregate (~129 MB/day; reads alone are tens of KB; [compaction]). Requests made with one of `EXCLUDED_ACCESS_KEY_IDS` (a secret: the deployment's own scanner keys) count in the totals and the sampling check but never in `rows`. `capture` is `unsampled` (all weights 1 and estimated = observed), `sampled` (provider ingestion sampling, permanent) or `inconsistent` (count and drain disagree; a later drain re-checks it). A changed aggregate (by SHA-256) goes to a **new immutable key** `agg/<bucket>/<YYYY-MM-DD>/<HH>/<run id>.json`; an unchanged one writes nothing.
4. For each source in `PUBLIC_BUCKETS`, a changed hour's **public file** is rewritten on the `PUBLIC` binding at `<PUBLIC_PREFIX>/<bucket>/<YYYY-MM-DD>/<HH>.json`: `{schema_version, bucket, hour, capture, observed, estimated, max_sample_interval, excluded, rows: [{key, action, n, last_ts}]}`. No event ids, IPs, user agents, actors or access key ids; own requests left out (`excluded` counts them). Every drained hour gets one, empty or not, so consumers see coverage and sampling. The RAC stack publishes `jc-taxes` to `disk-tree-demo` under `access/r2/`, which the demo's daily ingest turns into the read lens (`dt-cloud access r2-agg`, then `path-index -x`; each layer-2a row carries `sampled`).

Then, once per invocation:

5. **Publish**: one conditional write of `state.json` (`onlyIf` the etag it read; the first is unconditional). Per bucket: `collected_through` (every earlier hour drained at least once, or recorded as a gap), `first_from`, `gaps`, `verify_cursor`, `hours` (`YYYY-MM-DDTHH` → `{agg, digest, drained_at, final, events, own, estimated, max_sample_interval, capture, pages}` within retention), `frozen` (hour → aggregate key, past retention) and, after an upgrade from version 1, `legacy_days`. A run that read an older state fails with `state_conflict` and publishes nothing; a public file it already rewrote is rewritten again by the next drain of that hour.
6. **`_SUCCESS.json`**, the run manifest: the budget used, and per bucket its gap, watermark, hours still waiting, error, and each drained hour's phase, events, capture, change and raw page keys.

`hour_over_cap` holds the bucket's watermark (later hours wait behind it; other buckets and phases proceed), is recorded in the manifest, and fails the invocation *after* the publish, so the Worker logs `r2-access failed` with that code every hour until the cap is raised (alert on it). Once the stuck hour leaves retention it becomes a recorded gap. A watermark older than `RETENTION_HOURS` likewise becomes a **gap**; it cannot be recovered.

A non-COMPLETED query, query-time sampling (`abr_level` ≠ 1), a page count mismatch, a stalled cursor, an out-of-hour event, an over-size page or a failed write fails the run before the publish. Logs carry only error codes and counts.

**Budget.** Each invocation's worst case is `4 + 3 × MAX_HOURS + 2 × MAX_PAGES` subrequests (state read/write, manifest and one batched delete of superseded aggregates; per hour its count, aggregate and public write; per page its query and raw write); `settings` refuses any configuration over the 1,000 we size against (Workers' per-invocation subrequest limit on the paid plan; binding calls are counted conservatively). `MAX_PAGES` ≤ 450, `PAGE_SIZE` ≤ 2,000, so one hour holds at most ~896K events. RAC: `PAGE_SIZE` 500, `MAX_PAGES` 50, `MAX_HOURS` 48, `VERIFY_HOURS` 6 (bound 248): the 144-hour window is re-verified about every day. Memory is one hour's drain (its distinct ids and per-key read rows) plus one page; see the spec's sizing table.

**Upgrading** from the version-1 collector (per-day aggregates, a daily replay): its `state.json` is read as version 2 (watermark and gaps kept, day aggregates named under `legacy_days`), and the hours it covered get per-hour aggregates as the re-verify reaches them. `DAILY_CRON` and `REPLAY_HOURS` are retired; the Pulumi program refuses a config that still sets them, and a deployment's config must add `RETENTION_HOURS`, `MAX_HOURS` and `VERIFY_HOURS`.

A published watermark means the API queries drained, **not** that every access was logged: provider delivery is best effort, and `sampled` hours hold fewer events than requests. A superseded `agg/` version is deleted (one batched call) after the state publish that replaces it; a failed delete leaves an orphan, never a dangling state. `agg/` and `state.json` are not lifecycle-expired; `state.json` grows by one `frozen` entry per hour (~40 bytes). Raw archives contain sensitive keys, actors and IPs; they must not flow to the public R2 demo. See [collector contract] and [deployment isolation].

[collector contract]: ../../../specs/r2-access-logging.md
[deployment isolation]: ../../../specs/deployment-isolation.md
[compaction]: ../../../specs/r2-access-compaction.md
