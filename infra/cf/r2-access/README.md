# R2 access-log collector

Private, bounded RAC pilot for `jc-taxes`. The same code can be instantiated for HCCS/OA using separate account configuration, stacks, credentials and archives. It is not the `r2.oa.dev` dashboard or a last-read overlay yet.

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

Windows are half-open `[from, to)` in milliseconds. The telemetry API excludes **both** timeframe bounds (measured; see the spec), so every query sends `from − 1`, and events outside the window are refused.

- **Hourly** (`HOURLY_CRON`): `to` = the scheduled hour − `LAG_HOURS` (2). `from` = the published watermark − `OVERLAP_HOURS` (1), or `to − LOOKBACK_HOURS` (6) on the first run. Measured delivery latency was 13–25 s, so the overlap catches late deliveries with a wide margin.
- **Daily** (`DAILY_CRON`): replays `REPLAY_HOURS` (144) up to `to`, within the provider's seven-day retention.
- Every `from` is clamped to `to − REPLAY_HOURS`. A watermark older than that floor is recorded as a **gap** in the state; it cannot be recovered.

Each run, in order:

1. Drains each bucket's window to an empty cursor page, writing immutable raw pages under `runs/<scheduled>/<run id>/<bucket>/NNNN.json`. These expire after 30 days.
2. Runs one weighted-count calculation per UTC hour of the window, *after* the drain. Each hour gets `observed` (distinct events archived), `estimated` (Σ row weights), `max_sample_interval` and `capture`. `capture` is `unsampled` (all weights 1 and estimated = observed), `sampled` (provider ingestion sampling, permanent) or `inconsistent` (e.g. delivered between the drain and the count; a later overlap or the daily replay re-checks it).
3. Merges events into **per-UTC-day aggregates**, deduplicated by provider event id: `{events: {id: [timestamp, action]}, actions, classes, hours}`. `classes` separates `content_read` (GET) from `metadata_read` (HEAD), `list`, `write`, `delete` and `other`. Changed days are written to **new immutable keys** `agg/<bucket>/<day>/<run id>.json`; an unchanged day writes nothing.
4. **Publishes** with one conditional write of `state.json` (`onlyIf` the etag it read). That write repoints each day to its aggregate and advances `collected_through` (the watermark) to `to`. A run that read an older state fails with `state_conflict` and publishes nothing. The very first state write is unconditional.
5. Writes `_SUCCESS.json`, the run manifest (windows, gaps, new events, hour sampling counts, raw page keys).

Readers start from `state.json` only. Aggregate keys it does not name are unpublished, from failed or superseded runs. A cap, non-COMPLETED query, query-time sampling (`abr_level` ≠ 1), count mismatch, stalled cursor, out-of-window event or failed write fails the run before step 4.

The pilot caps each invocation at 50 event pages, 500 events per page and 4 MiB per response; the daily replay adds 144 small calculation queries. A published watermark means the API queries drained, **not** that every access was logged: provider delivery is best effort, and `sampled` hours hold fewer events than requests. Historical gaps older than retention cannot be recovered. Superseded `agg/` versions and `state.json` are not lifecycle-expired; at pilot volume that is a few objects per day.

This is deliberately a small-fleet collector: day aggregates hold every event id in memory and in one object. Do not reuse these limits blindly for `marin-na`'s traffic: measure first, then narrow windows or move high-volume drains into queued/durable jobs. Raw archives contain sensitive keys, actors and IPs; they must not flow to the public R2 demo. See [collector contract] and [deployment isolation].

[collector contract]: ../../../specs/r2-access-logging.md
[deployment isolation]: ../../../specs/deployment-isolation.md
