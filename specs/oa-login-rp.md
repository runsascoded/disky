# Sign in through login.oa.dev (OA's broker), starting on dev.gcs.oa.dev

From the `oa/auth` session, 2026-11-03.

## Why

login.oa.dev is OA's sign-in broker: an OIDC provider (`@open-athena/auth/provider`) over Google, admitting only openathena.ai Workspace accounts (`hd` checked). An OA app that signs in through it gets SSO across OA apps: signed in at one, the next one signs you in with no page in between (a `prompt=none` round trip). disky's `dev.gcs.oa.dev` is the first app to register with it, as the test bed; prod and the other deployments follow once it works. Background: `~/c/oa/auth/specs/cross-service-sso.md`, `~/c/oa/auth/broker/README.md`.

## What

An additional sign-in path, beside the existing Google one, on deployments that configure it:

- `OA_LOGIN_ISSUER` (var, e.g. `https://login.oa.dev`), `OA_LOGIN_CLIENT_ID` (var), `OA_LOGIN_CLIENT_SECRET` (secret). Unset: the path doesn't exist (404 / no button), as Google's is dormant without its secrets today.
- `GET /auth/oa` → `oidcStart` and `GET /auth/oa/callback` → `oidcCallback`, from `@open-athena/auth/oidc`, the same as `functions/auth/google{,/callback}.ts`, with:
  - `provider: { authUrl: `${issuer}/oidc/authorize`, tokenUrl: `${issuer}/oidc/token`, jwksUrl: `${issuer}/oidc/jwks`, issuer, scope: 'openid email profile' }`
  - `redirectUri: `${origin}/auth/oa/callback`` (registered exactly at the broker: `https://dev.gcs.oa.dev/auth/oa/callback`)
  - no `hostedDomain` (the broker already enforced it); the gate's policy (`STAFF_DOMAIN`, `allowed_emails`) decides admission as for every other path.
  - Failure handling as Google's callback: a 4xx lands on `/signin?error=oa:<reason>`.
- The sign-in page: a "Sign in with OA" button (→ `/auth/oa?next=…`) when configured. Non-OA viewers (`allowed_emails`, share links, email codes) keep their existing paths; the broker admits only openathena.ai.
- The adapter already sends PKCE S256 and a nonce, which the broker requires. `prompt` / `account=choose` / `login_hint` pass through as they do for Google.

Phase 2 (after phase 1 works on dev): on an unauthenticated visit, try `/auth/oa?prompt=none&next=…` once before showing the sign-in page, and fall back to the page on `login_required` (`?sso=login_required`, no loop: one attempt per page load, e.g. a short-lived cookie). That is the zero-tap SSO the broker exists for.

## Config for dev.gcs.oa.dev

`oa-gcs-usage`'s `[env.preview.vars]` on the `gcs` branch: `OA_LOGIN_ISSUER = "https://login.oa.dev"`, `OA_LOGIN_CLIENT_ID = "disky-gcs-dev"`. The secret is minted in `oa/auth` and piped straight into the preview env (Ryan runs it, from `~/c/oa/auth`):

```bash
scripts/oidc-client-secret.mjs | direnv exec demo broker/scripts/oa-wrangler.sh pages secret put OA_LOGIN_CLIENT_SECRET --project-name oa-gcs-usage --env preview
```

Its stderr hash goes into `oa/auth`'s `broker/src/clients.ts` as client `disky-gcs-dev`.

## Done when

- On dev.gcs.oa.dev, "Sign in with OA" → login.oa.dev → Google (first time) → back signed in, with the app session minted by the gate as for Google.
- A second visit after clearing only dev.gcs.oa.dev's cookies signs in with no Google page (the broker's session carries it).
- `?prompt=none` with no broker session lands on `/signin?error=oa:login_required` (or phase 2's fallback), not a loop.
- Tests for the config gating (unset → no route / no button) and the callback's error mapping, as for Google's.
