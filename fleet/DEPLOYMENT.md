# OA front door

This deployment serves public docs and a staff-only aggregate fleet page, not a scanned object store. Storage-deployment baseline items (its own scan job, interval/name indexes, meta store, staging and personal agent tokens) are therefore not applicable. The four underlying stores retain their own pipelines and access policy. The fleet page uses OA broker sessions, not public bearer grants.

## Resources created 2026-11-07

- Cloudflare account: `74981a43be0de7712369306c7b19133d` (OA).
- Pages project: `disky-oa`; production branch `front-door`; preview branch `dev`.
- KV namespace: `disky-oa-fleet`, `be540648c50f46c2b667de5b669de17b`, bound as `FLEET_KV`.
- Zone: `oa.dev`, `169959d6aafcbfd77764b8efafa3a509`.

Wrangler owns deployment bindings and nonsecret settings. These resources were created through the API, not a Pulumi apply; the shared dashboard component currently requires D1, which this app deliberately does not have. Do not apply that component unchanged or create an unused D1. This branch has no pending D1 migration recipes because it has no D1 databases; `site/schema.sql` remains the shared code's current schema, checked by CI.

## Release gates

The code merges `cloud`, preserving this branch's README index and hourly health workflow. `.github/workflows/fleet-deploy.yml` waits for successful CI, refuses stale commits, deploys preview, verifies its anonymous gates, and only then builds/deploys production. Both builds run on GitHub, not the laptop. Set repository variable `FLEET_DEPLOY_ENABLED=1` only after secret provisioning is approved and complete. `.github/workflows/fleet-update.yml` refreshes the persistent post hourly, separately gated by `FLEET_POST_ENABLED=1`; leave that off until source data, sign-in and the first post are verified. Its logs omit the signed card URL.

Ryan explicitly approved credential persistence on 2026-11-07. Provisioned: `CF_FRONT_DOOR_TOKEN` in this repo's Actions secrets; distinct production/preview `SESSION_SECRET` and `FLEET_JOB_TOKEN` in Pages (production job token also in Actions); `DISKY_SLACK_BOT_TOKEN` in Pages production only. Names and environment placement were verified without logging values. No secret values belong in Git, workflow output or chat. Never rotate existing secrets during setup.

The broker owner provisions `oa-disky` for `https://disky.oa.dev/auth/oa/callback`, and `oa-disky-dev` for the preview/localhost callbacks. Client secrets go directly to the corresponding Pages environment, never to the laptop. Read tokens for each underlying deployment must be explicitly provided for fleet use; do not silently reuse unrelated job or admin credentials.

DNS is not cut over until the deployed anonymous gates pass. Planned proxied CNAMEs are `disky.oa.dev` → `disky-oa.pages.dev`, and `dev.disky.oa.dev` → `dev.disky-oa.pages.dev`, registered as Pages custom domains. `/api/fleet` must return an anonymous 401, and unsigned `/og/fleet.png` must contain only the anonymous card.

## Open

- Broker clients and their corresponding Pages client secrets.
- Source read tokens, including a read-scoped way to get GCS/CW meta totals (not an admin grant).
- Preview and production deploys, DNS, browser sign-in and card verification.
- Hourly fleet update after the first post is verified; preview never receives the bot token.
- `/docs/disky` currently reads this branch's README, so it repeats the OA overview. Link to upstream for the engine guide until a separate project-README source is wired into the generic docs builder.
