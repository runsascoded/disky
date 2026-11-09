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

The broker owner completed `oa-disky` with callback `https://disky.oa.dev/auth/oa/callback`, and `oa-disky-dev` with `https://dev.disky.oa.dev/auth/oa/callback` and `http://localhost:3871/auth/oa/callback`, after direct user approval. Distinct client secrets went directly to `OA_LOGIN_CLIENT_SECRET` in the corresponding Pages production/preview environment; broker metadata and audit read-back passed. The broker owner's `specs/done/client-management-api.md` records the completion. Read tokens for each underlying deployment must be explicitly provided for fleet use; do not silently reuse unrelated job or admin credentials.

DNS is not cut over until the deployed anonymous gates pass. Planned proxied CNAMEs are `disky.oa.dev` → `disky-oa.pages.dev`, and `dev.disky.oa.dev` → `dev.disky-oa.pages.dev`, registered as Pages custom domains. `/api/fleet` must return an anonymous 401, and unsigned `/og/fleet.png` must contain only the anonymous card.

## Open

- Source read tokens, including a read-scoped way to get GCS/CW meta totals (not an admin grant).
- Preview and production deploys, DNS, browser sign-in and card verification.
- Hourly fleet update after the first post is verified; preview never receives the bot token.
- `/docs/disky` currently reads this branch's README, so it repeats the OA overview. Link to upstream for the engine guide until a separate project-README source is wired into the generic docs builder.

## Verified 2026-11-09

Cloudflare reports no deployments or canonical deployment for `disky-oa`, only its `disky-oa.pages.dev` domain, and no `disky.oa.dev` DNS record. Both Pages environments have `SESSION_SECRET`, `FLEET_JOB_TOKEN` and `OA_LOGIN_CLIENT_SECRET`; only production has `DISKY_SLACK_BOT_TOKEN`. Neither environment has a `FLEET_TOKEN_*` secret. These checks inspected names and types only, never secret values.

GitHub's repository-variable list is empty, so both release flags remain off. CI passed for `b34c4165037012d06b7179219c2357196263be33` (run `42425357564`); the scheduled fleet-update jobs are skipped. `#disky-admin` still contains the hand-collected November 6 draft and the bot join, with no automated fleet post. The fleet app/card are built in source, not live.

The November 9 readiness changes passed all 26 fleet tests, fleet/pages-kit type checks and the production build on `mgu`. `fleet/scripts/preview.mjs --fixture` generated clean dark/light 1200px PNGs and exact Slack JSON/text; both cards were visually inspected. These are fixture artifacts, not a live fleet dry run or an approved first post. No source grants, release flags, DNS or Slack state changed during verification.
