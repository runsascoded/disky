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

## Phase 2: one silent try before the wall (built)

On a deployment with the three `OA_LOGIN_*` set, an unauthenticated page visit goes to `/auth/oa?prompt=none&next=<path+query>` once. A broker holding a session sends the person back signed in with no page shown. Otherwise they land on the wall, where they were, with no error. Unconfigured deployments behave as before. The code is `site/functions/_lib/silentSignIn.ts`; its tests (`silentSignIn.test.ts`) run every case below through the middleware and the real routes.

**Seam: the middleware, not the client.** The wall is rendered by the SPA after whoami (`AuthGate`), so a client redirect would flash it and load the page twice. `_middleware.ts` makes the same decision ahead of the shell, ahead of `anonymousPage`. It reads no D1: a session is judged by its signature (`presentsCredentials`), so a signed-in visit pays nothing. A valid signature on a since-removed person gets the wall, with no attempt.

**A person's visit** is `GET` with `Sec-Fetch-Mode: navigate` and `Sec-Fetch-Dest: document`, and no `Sec-Purpose` / `Purpose` (prefetch, prerender). These are headers only browsers send. A crawler, unfurler, script or older browser sends none of them and gets exactly what it got before: no user-agent list, and no header means no attempt. `/staged` and `/runs/<id>` wall a person, so a person's visit tries; their unfurl shell still goes to every bot.

**Never an attempt:**

- `HEAD`, `POST`, any `fetch`, an iframe.
- `/api`, `/data`, `/assets`, `/auth`, `/og`, `/a`, `/v1`, `/slack`, `/signin`, `/privacy`, and a card page's `.../og`, even opened directly.
- `?key=` (share link), `?og=` (preview link), `?denied=` (request-access), `?sso=` (already answered).
- A `Bearer` header, or a valid session cookie.
- `PUBLIC_READ`, `AUTH_MODE = "public"`, or an `OG_ANONYMOUS` policy other than generic. A gated deployment with `OG_ANONYMOUS = "full"` (cw's kind) gets no attempt either until that's lifted; its pages are walled, its previews aren't.
- A path starting `//`, which `next` would refuse.
- Localhost, which is already the dev identity.

**One attempt.** The `oa_sso_skip` cookie (`HttpOnly`, `SameSite=Lax`) holds `attempt` for 5 minutes from the redirect, long enough to stop a repeat and short enough that a sign-in at the broker elsewhere takes on a later visit. The broker's answer comes back through the adapter as `<next>?sso=<why>` (`login_required`, the other no-interaction errors, `access_denied`; anything else is `error`), which the middleware refuses to bounce again, cookie or no cookie. `main.tsx` then strips the marker from the address bar, so it doesn't follow the person into a share or a sign-in's return URL. Nothing renders it: `signInError` reads only `?error=`.

**Cookies blocked.** The nonce cookie is gone too, so the adapter can't vouch for the state. The callback lands on a quiet `/signin?sso=<why>`, which never bounces. The person couldn't hold a session here anyway. A reload is one more bounce, never a loop.

**A silent exchange that fails** (the attempt cookie still present): `/?sso=error`, with the reason in the log rather than on the page. An explicit attempt's failure still lands on `/signin?error=oa:<reason>`, which explains itself.

**Someone the gate doesn't admit** with a broker session lands on `/?denied=<email>`, the wall's request-access form, like Google's denial. `next` is lost there, as it is for Google.

**After a deliberate sign-out** (`oa/auth`'s `specs/cross-service-sso.md`, "Selection and logout"): `POST /api/auth/logout` sets `oa_sso_skip=signout` for 400 days, as long as Chrome lets a cookie last, because the broker's session survives an app logout and an attempt would sign them straight back in. This isn't a duration so much as "until they resume": a start of `/auth/oa` without `prompt=none` (the wall's "Sign in with OA") clears it, and so does any completed sign-in, whichever path (the middleware clears it on any response minting `oa_auth`). There's no broker logout call and no "Sign out of OA" action in this phase.

**`next`** is the adapter's existing `safeNext`, unchanged: same-origin paths only.

## Config for dev.gcs.oa.dev

`oa-gcs-usage`'s `[env.preview.vars]` on the `gcs` branch: `OA_LOGIN_ISSUER = "https://login.oa.dev"`, `OA_LOGIN_CLIENT_ID = "disky-gcs-dev"`. The secret is minted in `oa/auth` and piped straight into the preview env (Ryan runs it, from `~/c/oa/auth`):

```bash
scripts/oidc-client-secret.mjs | direnv exec demo broker/scripts/oa-wrangler.sh pages secret put OA_LOGIN_CLIENT_SECRET --project-name oa-gcs-usage --env preview
```

Its stderr hash goes into `oa/auth`'s `broker/src/clients.ts` as client `disky-gcs-dev`.

## Done when

- On dev.gcs.oa.dev, "Sign in with OA" → login.oa.dev → Google (first time) → back signed in, with the app session minted by the gate as for Google.
- A second visit after clearing only dev.gcs.oa.dev's cookies signs in with no Google page (the broker's session carries it).
- `?prompt=none` with no broker session lands on the wall with `?sso=login_required` (phase 2), not a loop.
- Tests for the config gating (unset → no route / no button) and the callback's error mapping, as for Google's.
