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

The preview performed on October 12 succeeded: six new Cloudflare resources plus the stack, no updates/deletes, and `schedules: []`. The detailed preview and saved plan are in ignored `tmp/r2-access-preview.txt` and `tmp/r2-access-preview-plan.json`. Provider warnings say certain configuration resources cannot be removed by destroy alone: explicitly disable cron through an apply before decommissioning the Worker/stack; do not assume destroy stops collection.

## Preview/apply handoff

Local passphrase-encrypted state lives in **`state/` beside this file**, matching the existing RAC m3 stack convention. Keep/back up that state and its passphrase; it is not disposable benchmark scratch. No `up` was run by the agent. The `wt/m3` environment supplies the RAC deployment token and existing state passphrase; do not substitute an OA token or the Observability-only runtime token.

From the repository root:

```bash
direnv exec wt/m3 pulumi -C infra/cf/r2-access preview --stack rac --diff --save-plan "$PWD/tmp/r2-access-preview-plan.json" --suppress-outputs
# Human-only, after reviewing the preview:
direnv exec wt/m3 pulumi -C infra/cf/r2-access up --stack rac --plan "$PWD/tmp/r2-access-preview-plan.json"
```

After creation, install the runtime secret interactively, using RAC deployment credentials (the prompted value is the dedicated Observability token):

```bash
direnv exec wt/m3 pnpm --dir infra/cf/r2-access exec wrangler secret put CF_OBSERVABILITY_TOKEN
```

Then set `disky-r2-access:secretReady: true` and `disky-r2-access:enabled: true` in `Pulumi.rac.yaml`, preview, and human-apply that second plan. The pilot token expires **October 18**; replace/renew it before expiry. Inspect a scheduled run's private `_SUCCESS.json` and sanitized Worker execution logs before treating this as an operating collector.

## Collection contract and limits

Hourly runs query six-hour windows ending two hours behind the scheduled hour; the daily replay covers six days, within the provider retention window. Each run has immutable raw pages and publishes `_SUCCESS.json` only after every configured bucket drains to an empty page. Partial runs without that marker are incomplete. Concurrent/replayed runs have different UUID prefixes, with no racing mutable watermark.

The pilot caps each invocation at 50 total pages, 500 events per page and 4 MiB per response. A cap, non-COMPLETED query, query sampling, count mismatch, stalled cursor or failed write fails the run; it must not publish a success marker. The provider's seven-day retention, late/best-effort delivery and unknown capture sampling still apply. A success marker means the API query drained, **not** that every access was logged. Historical gaps older than retention cannot be recovered.

This is deliberately a small-fleet collector. Do not reuse these limits blindly for `marin-na`'s traffic: measure first, then narrow windows or move high-volume drains into queued/durable jobs. A downstream overlay must deduplicate provider event IDs across run manifests before counting accesses, and must separate content reads from HEAD/list/write/delete operations. Raw archives contain sensitive keys, actors and IPs; they must not flow to the public R2 demo. See [collector contract] and [deployment isolation].

[collector contract]: ../../../specs/r2-access-logging.md
[deployment isolation]: ../../../specs/deployment-isolation.md
