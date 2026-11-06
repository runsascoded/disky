# Pages auth: gate the cloud deployment with `@open-athena/auth`

`disk-tree.pages.dev` (spec `done/cloud-reduce.md`) went live 2026-09-06 serving a full listing of Ryan's laptop to anyone — the first thing the real chain proved was that the static deployment needs a gate. It got a deny-all `_middleware.ts` within minutes; this spec replaces that with the real thing.

Asks (Ryan, 2026-09-06): "anyone with link can view" and/or a few of Ryan's own emails via SSO, using [`@open-athena/auth`] (`$oa/auth`).

## Shape: Tier 2, hosted in the Pages Functions

Per the package's two-tier model (`$oa/auth/specs/overview.md`), share links put this squarely in **Tier 2**: the app owns auth (HMAC session cookies + D1-backed grant tokens), and CF Access shrinks to an SSO IdP on exactly one path. Hosted in this project's own Pages Functions — the "clean consumer" shape the package recommends (no separate worker, no proxy hop; the demo at `auth.oa.dev` is the reference).

- **What's gated**: every `/api/*` route except `/api/capabilities` and `/api/auth/*`. The SPA shell, assets and `og.jpg` stay public — they reveal nothing, and the wall has to render from somewhere. A `?key=<token>` link is redeemed by the shell (`useKeyExchange`) before the first API call.
- **Identities**: SSO via CF Access (`/auth/sso`, the only Access-gated path); policy = an exact-email allowlist from the `ALLOWED_EMAILS` secret → scopes `view` + `admin`. Share links = grants with scope `view`, minted from the `/access` page (admin only), named after the recipient, optional expiry / redemption cap. Instant revocation: grant sessions re-join their row on every request.
- **Access log** on (`logViews: true`): who opened which link and looked at what — the point of named links. Disclosed on the wall and in the chip.
- **Flask unaffected**: `capabilities.auth` is `true` only from the Functions; the UI's gate is a pass-through when it's absent, so the local server and older peers never see a wall.

## Pieces

- `ui/cfn/auth.ts` — `buildGate(deps)` (stores/secret/policy in, `Gate` out; tests inject the package's memory stores) and `gateFor(env, req)` (D1 + `SESSION_SECRET`, with the demo's localhost-only dev secret so `pnpm cfn:dev` needs no setup), `allowlistPolicy(ALLOWED_EMAILS)`, `gateApi(gate, req, next, audit)` — the pure middleware core.
- `ui/functions/_middleware.ts` — wires `gateApi` for `/api/*`; mounts the package's `authRoutes` at `/api/auth/*` (whoami / exchange / logout / grants / log).
- `ui/functions/auth/sso.ts` — the package's `ssoHandler` (verifies `Cf-Access-Jwt-Assertion` against `ACCESS_TEAM_DOMAIN` / `ACCESS_AUD`, signs in, bounces to `?next`).
- `ui/migrations/*.sql` — byte copies of the package's migrations (`d1 migrations apply`).
- `ui/wrangler.toml` — `[[d1_databases]]` `DB` → `disk-tree-auth`. Identity-bearing config (`ALLOWED_EMAILS`, `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD`, `SESSION_SECRET`) lives in Pages secrets, not in this public repo.
- `ui/src/auth.tsx` — the vendored half (per the package's "kernel packaged, presentation vendored"): `useWhoami`, `<Gate>` (no-op without `capabilities.auth`), the wall, the header chip; `ui/src/components/AccessPage.tsx` — mint / list / revoke / recent log, `/access`, admin only.
- `ui/cfn/tests/auth.test.ts` — over memory stores: anonymous → 401 on `/api/scans`, public paths untouched; a minted link exchanges into a session that reads scans; revoke → the same session 401s on its next request; a `view` grant can't mint (403); SSO email outside the allowlist is refused.

## Built + CIC'd locally (2026-09-06)

Everything above, plus `functions/auth/dev.ts` — a localhost-only stand-in for `/auth/sso` (`GET /auth/dev?email=…`, refused unless the host is localhost *and* no `ACCESS_TEAM_DOMAIN` is configured) so `pnpm cfn:dev --binding ALLOWED_EMAILS=…` can exercise the admin side. Five gate tests (`cfn/tests/auth.test.ts`); 23 vitest total.

Over `wrangler pages dev` + local D1 (`pnpm cfn:migrate:local`) and an emulated R2 seeded with the fixture scan: anonymous → the wall; `/auth/dev` as an allowlisted email → header chip + **Access** link; a stranger → 403 and a `deny · not-allowed` row in the log; `/access` minted "bob (test link)" (the `mint` row appears); the link redeemed over curl (whoami `kind: grant`, scans 200, `/api/auth/grants` 403) and in the browser (`?key=` stripped, chip shows the link's name); revoke → the redeemed session's next request 401s.

Two things the run found: the package's `admin` flag means "in `adminEmails`", not "has the admin scope" — the UI checks `hasScope(whoami, 'admin')`; and the header's identity probe raced the `?key=` exchange (the header mounts before the gate), so the chip showed the *previous* identity — `useWhoami` now holds until `useKeyExchange()` is done. Also: the pinned `dist` ships migrations `0001`–`0005` while its D1 adapter needs `0006`/`0007` (copied from the source clone; reported in `$oa/auth/specs/dist-migrations-stale.md`).

Deployed state meanwhile: the deny-all middleware from the incident is what's live; this code fails closed without the D1 binding, and `wrangler` refuses the placeholder `database_id` anyway.

## Deployed (2026-09-07)

1. Ryan added **D1 Write** to the `disk-tree-wrangler` token; `wrangler d1 create disk-tree-auth` → `c17a63c1-1d6f-443c-9d35-f1e9c5e674ac` (ENAM), the seven migrations applied `--remote`, id filled into `wrangler.toml`.
2. Zero Trust was already enabled on the personal account (team domain `runsascoded.cloudflareaccess.com`, one existing app). Added a self-hosted Access application **disk-tree** with the single destination `disk-tree.pages.dev/auth/sso` and a new reusable policy **disk-tree allowlist** (Allow · Include · Emails). Login methods: "accept all available identity providers" (the team's defaults). The app's AUD tag is in the secret, not here.
3. Pages secrets set from local files (never echoed): `SESSION_SECRET` (32 random bytes hex), `ALLOWED_EMAILS`, `ACCESS_TEAM_DOMAIN`, `ACCESS_AUD`.
4. `wrangler pages deploy` → the deny-all middleware is gone. Verified over curl: `/api/capabilities` 200 (`auth: true`), `/api/scans` and `/api/auth/whoami` 401, `/auth/sso` 302 to `runsascoded.cloudflareaccess.com/cdn-cgi/access/login/disk-tree.pages.dev?kid=<aud>…`, `/` 200 rendering the wall.

5. The team's existing **Google** login method refused a personal-domain address (`Error 403: org_internal`): its OAuth client lives in GCP project `1089043197530`, whose consent screen is *Internal* to some other Workspace (neither the runsascoded nor the OpenAthena account can see the project; HCCS is the likely owner, via the team's other app). Fix: a fresh OAuth client in Ryan's own `runsascoded-sso` project (consent screen External; redirect URI `https://runsascoded.cloudflareaccess.com/cdn-cgi/access/callback`), added to the team as a second IdP **Google (runsascoded)**, and the disk-tree app switched from "accept all IdPs" to exactly that one + One-time PIN — the HCCS app keeps the old provider. Client id/secret live in `.envrc` (`RAC_SSO_CLIENT_{ID,SECRET}`); the secret was pasted into the Cloudflare form by hand.

Verified live, 2026-09-07: SSO round trip (Google → `/auth/sso` → app, `signin` row in the log, **Access** button for the admin); `/access` minted "smoke test (delete me)"; the link redeemed over curl (`POST /api/auth/exchange {token}` → `kind: grant`, `/api/scans` 200, `/api/auth/grants` 403); revoke → the same cookie 401s on whoami and scans. The log shows every step (`signin` / `view` / `mint` / `redeem` / `revoke`).

To allow another email: add it to the Access policy **disk-tree allowlist** *and* to `ALLOWED_EMAILS` (both are exact-match lists).

[`@open-athena/auth`]: https://github.com/Open-Athena/auth
