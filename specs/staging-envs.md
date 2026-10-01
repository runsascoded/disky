# Staging environments — real-login server + selectable DB backend

## Goal

A deployed **staging server** you can actually sign into (CF Access, real identity) and exercise end-to-end — marks, claims, the actions ledger, the agent-token flow — without touching prod data. And, as a variant, the ability to point that same running app at the **real prod DB** to test live user actions. The database backing a session should be **selectable from the UI** (an *environment switcher* in the account menu), not baked into a separate deployment per backend.

Today we only have:
- **Prod**: `gcs.oa.dev` (CF Pages `oa-gcs-usage`), bound to the one D1 `oa-gcs-usage-auth`.
- **Local dev**: `./dev` (vite + `wrangler pages dev`) against a **local miniflare D1**, refreshable from prod with **`./refresh-db`** (`./dev --refresh`) — see that script. This already covers "mode 1, locally" (a recent prod copy, RW, isolated), but has no real login (the `DEV_IDENTITY` stub auto-auths).

## The four modes

| # | Mode | Backing DB | Login | Notes |
|---|------|-----------|-------|-------|
| 1 | **Recent prod copy, RW, isolated** | `DB_STAGING` (D1) | real | The default staging backend. Refreshed from prod on a schedule / on demand. Safe sandbox — writes are thrown away on next refresh. |
| 2 | **Live prod, real actions** | `DB_PROD` (D1) | real | RW against real data. **Staff-gated**, off by default, loud persistent "● LIVE PROD" banner. For testing that a user action really lands. |
| 3 | **Prod copy + pending migrations** | `DB_MIGRATE` (D1) | real | A *variant of 1*: `DB_STAGING` clone with the in-flight migration(s) applied on top, to test schema changes against real-shaped data before they merge. |
| 4 | **Synthetic fixture** | none (in-memory / SQLite file) | stub | **No D1, and not a deployed env.** For tests that assert exact state before/after an action. Spun up only on localhost for local tests (CI can do the same). Lives with the test harness, not the server. |

Notes on the edges:
- **3 vs 1**: keep 3 as a labelled variant ("staging + WIP migrations"), backed by its own D1 so it doesn't perturb the plain copy. Only promote it to a first-class env if we routinely test migrations against a *fresh* prod copy independently of the working staging copy.
- **4**: a prod copy (1) is almost always more useful than synthetic for manual/e2e work, because real data shape and edge cases matter. Synthetic earns its place **only** where a test needs a known, exact starting state to assert deltas — so it belongs to the test suite (a seeded SQLite fixture / miniflare `--local` D1), explicitly **not** a shared D1 and **not** a deployed environment.

## Architecture

The enabling fact: **D1 bindings are per-deployment, but one deployment can bind several D1s.** So a single staging deployment binds `DB_STAGING` + `DB_PROD` + `DB_MIGRATE`, and the Functions choose which binding to use **per request**, from a signal the UI sets. All D1 access already funnels through the Functions, so this is one chokepoint, not a scatter.

1. **One staging Pages deployment** — its own hostname (e.g. `staging.gcs.oa.dev`, or a stable `pages.dev` alias) behind a **CF Access app** (reuse the existing team domain / add the hostname to the current Access app). This is what gives a **real login**.
2. **Multiple D1 bindings** in the staging `wrangler.toml`: `DB_STAGING`, `DB_PROD`, `DB_MIGRATE`. Create `oa-gcs-usage-auth-staging` (+ `…-migrate`) and seed via export/import.
3. **DB accessor indirection** — the crux. Route every D1 access through one accessor in `functions/_lib/` that reads an `env` cookie (`staging` | `prod` | `migrate`, default `staging`) and returns the matching binding. `prod` is refused unless the session has the staff scope. Everything downstream (`/api/*`) is unchanged — it asks the accessor for "the DB", not `env.DB`.
4. **Environment switcher** — a small control in the **account menu** (the avatar+name chip top-right). Sets the `env` cookie, reloads, and paints a persistent banner when `env` ≠ the safe default (loudest for `prod`). This is the "DB configurable via the user chip" idea; the idiomatic name for the widget is an *environment switcher* (cf. Vercel's env dropdown, GitHub's org switcher, AWS's account/region picker).
5. **Refresh job** — repopulate `DB_STAGING` (and re-clone+migrate `DB_MIGRATE`) from prod on a schedule (cron Worker) and/or an admin "refresh from prod" button, reusing the `wrangler d1 export | d1 execute` mechanic that `./refresh-db` already does locally.

## Guardrails

- `env=prod` is **staff-only** and **off by default**; selecting it requires an explicit action and shows a persistent banner. Never make prod the default backend for the staging host.
- The staging Access app should gate to the same staff set as prod (or tighter) — the copy still contains real emails, hashed IPs, and agent-token hashes.
- The `refresh-db` dump / any export stays in gitignored scratch, never committed, never pushed local→prod.
- One-directional: staging never writes back to prod. There is no "promote staging DB to prod" path here.

## What it takes (rough)

1. CF: staging Pages project + hostname + Access app (or add hostname to the existing app). *(dashboard/infra)*
2. Create `oa-gcs-usage-auth-staging` (+ `…-migrate`) D1; bind all three in staging `wrangler.toml`; seed via export/import.
3. `functions/_lib` DB accessor keyed on the `env` cookie; default `staging`; `prod` staff-gated. *(small, one chokepoint)*
4. Env switcher in the account menu + the non-default banner. *(FE)*
5. Refresh job (cron Worker or a `pnpm` script) for `DB_STAGING` / `DB_MIGRATE`.

Ship order: **staging deploy + `DB_STAGING` copy + real login first** (mode 1), then add the switcher for `prod` / `migrate`. The DB-accessor indirection (step 3) is the linchpin — once queries go through it, the rest is config + a bit of UI.

## Open questions

- **`env` scoping**: cookie (per-browser, simple) vs. a `?env=` URL param (shareable, but leaks into deep-links) — lean cookie, set only by the switcher.
- **Where the switcher lives**: inside the account-menu dropdown, or a always-visible chip next to it when `env` ≠ default. Probably: menu item to switch, always-on banner when non-default.
- **Migration testing cadence**: is `DB_MIGRATE` worth standing up now, or defer until we're mid-migration and just apply the WIP migration to `DB_STAGING` ad hoc?
- **CI e2e**: should CI spin up a miniflare `--local` D1 (mode 4 fixture, or a redacted mode-1 snapshot committed as a fixture) and run Playwright against the built app? Out of scope here but the accessor makes it easy.

## Related

- `./refresh-db` / `./dev --refresh` — the local half of mode 1 (already built).
- `site/wrangler.toml` — current single `DB` binding; the staging variant adds `DB_STAGING`/`DB_PROD`/`DB_MIGRATE`.
- `src/auth.ts` `DEV_IDENTITY` — the localhost stub that bypasses Access; staging uses the real Access session instead.
