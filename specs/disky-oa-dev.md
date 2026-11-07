# disky.oa.dev: docs + fleet page + fleet card

From the root session, 2026-11-07. Ryan's go: "yes sg" to: a Pages project + DNS for `disky.oa.dev` on OA's Cloudflare account, its secrets, and pushes to `o/front-door`; docs public, fleet page behind sign-in.

## Why

`#disky-admin` gets one status post for all four OA deployments (gcs, cw, hf, r2), edited in place, with an auto-updating image. No current site sees all four, and gcs figures can't be public. So a small site of its own: public docs, a signed-in fleet page, and the fleet card as a dOGI image (`/og/fleet.png`, signed like the staged card) that the Slack post and the page's `og:image` both use.

## Shape

- **Code on `cloud`, generic:** a new Pages app `fleet/` (Vite + React + TS, Functions), configured only by env. It knows nothing about OA. A deployment list is a var, `FLEET_JSON = [{"name": "gcs", "url": "https://gcs.oa.dev", "token": "FLEET_TOKEN_GCS", "color": "#EA4335"}, …]`, where `token` names the secret holding that deployment's read token. Reuse `site/functions/_lib/og/` (SVG card → resvg PNG, `sign.ts`) and `@open-athena/auth` by import or a shared package, not by copying.
- **Deployed from `o/front-door`:** it merges `cloud` the way a deployment branch does, and adds its `wrangler.toml` (OA's list, `disky.oa.dev`) beside the README index and `health.yml`.
- **Public:**
  - `/`: the docs. The front door's README (deployments, links, live preview cards) rendered as a page, plus the project README / `docs/` as more pages.
  - `/og.jpg`: the site's own card.
- **Signed in:** `/fleet`, `/api/fleet`. OA staff only, through OA's sign-in broker (`login.oa.dev`, `OA_LOGIN_*`, as `site/` does: `specs/oa-login-rp.md`). Prefer a signed session cookie (`SESSION_SECRET`) with no D1. Add a D1 only if the auth package needs one, and say so.
- **`/api/fleet`** fetches each deployment's `/api/health`, `/api/scan-runs?limit=…`, and its `/meta` store usage, with that deployment's token. It returns one JSON: per deployment, last scan (id, start, duration, status, open step issues, cost), the last N runs, and `/meta` bytes now / 1 d / 7 d ago plus a daily series. Cache it about 5 min at the edge (`private`/no-store to browsers).
- **`/fleet`:** the same data. The card on top, then a table per deployment.
- **The card** (`/og/fleet.png?sig=…`): a stacked SVG, one trace per deployment in its color, as in the layout mock (`tmp/fleet-mock-stacked.png`):
  - `/meta` size, log y
  - scan duration, with filled = ok and hollow = a step issue still open
  - cost per scan

  A full card needs a valid `sig` (HMAC under `SESSION_SECRET`, 7-day expiry, `IMAGE_TTL_DAYS`). Without one, it's an anonymous card with no numbers, never an error. The phone width is the design target.
- **Slack:** `POST /api/fleet/slack` (Bearer `FLEET_JOB_TOKEN`) builds the post's text and blocks: a one-line status per deployment, plus an `image` block with a freshly signed card URL (a new `v=` each run, so Slack refetches). It posts once, then `chat.update`s that message, with the ts kept in KV or a var. The bot is "Disky Admin Bot" (`DISKY_SLACK_BOT_TOKEN`), and the channel is `SLACK_FLEET_CHANNEL`. A GHA cron on `o/front-door` calls it hourly.

## Secrets / config (set in the root session with Ryan's go; never by the builder)

`SESSION_SECRET`, `OA_LOGIN_CLIENT_SECRET` (+ vars `OA_LOGIN_ISSUER`, `OA_LOGIN_CLIENT_ID`), `FLEET_TOKEN_{GCS,CW,HF,R2}` (read grants minted by each deployment's session; scope must reach `/meta` usage), `FLEET_JOB_TOKEN`, `DISKY_SLACK_BOT_TOKEN`.

## Open

- `/meta` usage on gcs and cw is `scope: admin` today, so a read grant gets 403. Either the fleet tokens carry a scope that reaches it, or `/meta` usage gets a read-scoped summary route. That's each deployment's call.
- gcs `cost_usd` is null on every run (likely its machine type is missing from `scan_runs.py`'s price table).
- Meta scans aren't in `/api/scan-runs` on r2 and cw.

## Tests

Functions are tested as in `site/`: the gate on every route (`/fleet` and `/api/fleet` 401 or redirect when signed out; `/` public; `/og/fleet.png` anonymous without `sig`); `/api/fleet` against stubbed deployments; the card's SVG by golden; the Slack builder's blocks by exact equality.
