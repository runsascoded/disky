# Read grants for the fleet dashboard

Status: deployment-owner handoff, 2026-11-09. Root owns the `disky.oa.dev` consumer and Pages secret placement; GCS, CW, HF and R2 own their source access policies and grant issuance. This handoff does not authorize a permission change or live rollout by itself. Ryan has authorized the fleet dashboard; preserve each deployment's source-policy review.

The consumer is built and verified, and the OA broker clients are provisioned. The four `FLEET_TOKEN_*` secrets are absent. Existing `fleet/functions/_lib/fleet.ts` reads `/api/health`, `/api/scan-runs` and `/api/series?store=meta` with each source's token. GCS/CW admin-gate the meta store; using an admin token would give the consumer unnecessary authority. No narrow fleet-summary endpoint currently exists in the checked-out deployment sources.

## Required handoff

| Owner | Canonical origin | Pages secret name | Remaining source work |
| --- | --- | --- | --- |
| GCS | `https://gcs.oa.dev` | `FLEET_TOKEN_GCS` | Dedicated read grant and narrow meta aggregate access |
| CW | `https://cw.oa.dev` | `FLEET_TOKEN_CW` | Dedicated read grant and narrow meta aggregate access |
| HF | `https://hf.oa.dev` | `FLEET_TOKEN_HF` | Dedicated read grant; verify all three reads |
| R2 | `https://r2.oa.dev` | `FLEET_TOKEN_R2` | Dedicated read grant; verify all three reads |

Each owner should first identify whether an existing fleet-specific grant has already been handed off. Do not mint a duplicate or reuse a job/admin grant silently. Supply new token material through a private local handoff or direct secret installation, never in Agent Mail, a spec, command output or a transcript. Report only the grant's nonsecret ID, audience, scopes, expiry policy, handoff location and verified endpoint statuses.

The consumer needs only fleet health/freshness, scan IDs/times/status/duration/cost/open phase issues, and meta-store byte totals over time. It needs no object names, ownership evidence, staging/deletion authority, auth logs or grant administration. Prefer a narrowly scoped summary route over granting ordinary viewers access to the whole meta store. Keep anonymous, public and path-restricted grants from acquiring access to private aggregate totals through a new route. If the owner chooses a new response contract, agree it with root before changing the consumer and cover it with exact route/gate tests.

## Acceptance and rollout ordering

1. Source owner proposes the minimal scope/route and source changes, with tests. Check a dedicated grant against every required read, and verify it cannot stage, delete, administer grants or read unrelated private metadata.
2. Source owner performs its normal reviewed rollout and records the revision/endpoint verification. Root installs the provided token in the intended Pages production and preview secret slots, preserving existing broker/session/job secrets.
3. Root deploys preview, verifies anonymous gates and OA staff sign-in, and reads a real fleet document. Missing metrics/errors remain explicit; successful HTTP alone is not complete data acceptance.
4. Root obtains the real `POST /api/fleet/slack?dry=1` payload and card for first-post review. Then create/verify the maintained post and enable the hourly updater. The dry response contains a signed private-card URL and must remain private.

The fuller daily nominal replies, concern threads and monthly history contract remains recorded in [fleet delivery scope]. None of those is implied by an hourly image refresh.

[fleet delivery scope]: disky-oa-dev.md
