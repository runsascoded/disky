# disky.oa.dev: docs + fleet page + fleet card

From the root session, 2026-11-07. Ryan's go: "yes sg" to: a Pages project + DNS for `disky.oa.dev` on OA's Cloudflare account, its secrets, and pushes to `o/front-door`; docs public, fleet page behind sign-in.

## Status

November 10 live readback: Pages `disky-oa` still has no canonical deployment, only `disky-oa.pages.dev`; production/preview have the existing broker/session/job secrets but no `FLEET_TOKEN_*` bindings. GitHub has neither release-enable flag set. Slack search including bot messages still shows the November 6 hand-collected draft and Ryan's November 10 TODO thread, with no automated fleet rollup. The feature is not live. Nonsecret readback artifacts: root `tmp/cs3-audit/fleet-live-readiness-nov10.json`, `fleet-release-flags-nov10.jsonl` and `oa-repo-release-metadata.json`.

Next delivery should satisfy the requested four-deployment status first: newest scan/freshness, five recent outcome marks and serving build, with a maintained monthly parent and one nominal daily reply. `dt-cloud fleet-status` already renders the basic rows, but it is not a scheduled publisher. Dedicated source grants, daily/monthly persistent delivery, concern transitions and real dry-run review remain implementation/rollout work. Meta charts and the new web dashboard should not be additional prerequisites for the basic daily status; unavailable extra metrics must remain explicit. The old hourly image refresh alone does not satisfy this daily/monthly contract. [Release workflow proposal] handles PR/dev/prod automation separately.

- **Built on `cloud`** (`rw/fleet-site`, 2026-11-07): the app `fleet/`, its specs and CI job, and `packages/pages-kit` (what it shares with `site/`). `o/front-door` adds the deployment config and gated release/hourly workflows; CI passed at `b34c4165`.
- **Provisioned, verified 2026-11-09:** the `disky-oa` Pages project and fleet KV, Actions deployment/job credentials, separate Pages production/preview session/job/client secrets, and production-only bot token. The broker owner completed the two clients and direct secret installation after direct user approval. See [deployment record].
- **Open:** the four source read tokens and GCS/CW meta summary contract, first preview/production deployment, DNS, browser sign-in/card verification, and the first automated post. Live Cloudflare reports no deployment or custom-domain/DNS cutover; both GitHub release flags remain unset.

## Why

`#disky-admin` gets one status post for all four OA deployments (gcs, cw, hf, r2), edited in place, with an auto-updating image. No current site sees all four, and gcs figures can't be public. So a small site of its own: public docs, a signed-in fleet page, and the fleet card as a dOGI image (`/og/fleet.png`, signed like the staged card) that the Slack post and the page's `og:image` both use.

## Delivery scope

The first deliverable implemented here is one persistent fleet post, with its text and signed image refreshed hourly. It does not yet implement the fuller channel workflow in [Ryan's November 6 draft]: one reply a day while nominal; anything concerning gets its own top-level post and thread; the next nominal update opens a new thread; and a month's thread looks back `max(29d, month-to-date)`. Daily replies, concern/incident thread transitions, monthly history selection and their persistent state remain open work. The current card plots seven days, and its reader retains 14 recent scan runs and 30 days of daily meta points, so it cannot yet supply that whole history contract.

Fixture previews verify formatting and rendering only. Approval of a first real post requires a deployed dry-run payload and card backed by the four explicitly provisioned source grants, with missing metrics and source errors reviewed; fixture output is not that payload.

## Shape

- **Code on `cloud`, generic:** the Pages app `fleet/` (Vite + React + TS, Functions), configured only by env. It knows nothing about OA. A deployment list is a var, `FLEET_JSON = [{"name": "gcs", "url": "https://gcs.oa.dev", "token": "FLEET_TOKEN_GCS", "color": "#EA4335"}, …]`, where `token` names the secret holding that deployment's read token (`fleet/wrangler.example.toml` shows every key).
- **Shared with `site/`, not copied:** `packages/pages-kit` (`@disk-tree/pages-kit`, TS source like the treemap package):
  - `og/render`: resvg SVG → PNG, moved from `site/functions/_lib/og/render.ts`.
  - `og/sign`: signed card URLs, moved from `site/functions/_lib/og/sign.ts`.
  - `og/text`: `esc`, `fmtB`/`fmtSI`, `niceTicks`, `spread`, Inter's widths, out of `card.ts`, `digestCard.ts`, `interMetrics.ts`.
  - `oa`: the broker's OIDC config, the `/auth/oa` start and callback handlers, and silent sign-in, out of `_lib/oidc.ts`, `_lib/silentSignIn.ts` and `auth/oa*.ts`. Each app supplies its gate and what it walls.

  `site/` keeps every module and export it had, as re-exports or thin wrappers, with its tests unchanged. The resvg wasm `fleet/` imports is `@resvg/resvg-wasm/index_bg.wasm`, the same bytes as `site/`'s vendored copy. Its card fonts are `site/public/_fonts/Inter-{400,600}.ttf`, which `fleet/vite.config.ts` serves and emits at the same paths.
- **Deployed from `o/front-door`:** it merges `cloud` the way a deployment branch does, and adds its `fleet/wrangler.toml` (OA's list, `disky.oa.dev`) beside the README index and `health.yml`. Build from `fleet/` (`pnpm build` there), and deploy from `fleet/` too (`wrangler pages deploy dist`): `pages deploy` bundles the `functions/` of the directory it runs in.
- **Public:**
  - `/`: the docs index, a Markdown file named at build by `FLEET_DOCS_INDEX` (repo-relative; default `fleet/docs/index.md`, a generic page). The front door points it at its README index.
  - `/docs/disky`: the project README, unless it is the index (front-door's `FLEET_DOCS_INDEX=README.md`), which `/` already serves.
  - `/docs/<name>`: any other `fleet/docs/*.md` or `docs/**/*.md` (the fallback `fleet/docs/index.md` only as the index), reached by the docs' links; the nav shows only the index ("disky") and "Fleet".

  Relative links resolve to the page that serves their file, else to the file on GitHub (`FLEET_REPO`, `FLEET_REF`, default `runsascoded/disky` at `cloud`).
  - `/og.jpg`: the site's own card (static, `fleet/public/og.jpg`; `fleet/scripts/og.mjs` draws it).
  - `/fleet`'s `og:image` is the anonymous fleet card (`fleet.html`, its own Vite entry). Absolute `og:` URLs need `FLEET_ORIGIN` at build.
- **Signed in:** `/fleet`, `/api/fleet`. OA staff only, through OA's sign-in broker (`login.oa.dev`, `OA_LOGIN_*`, `specs/oa-login-rp.md`), with the same silent attempt as `site/` on a signed-out visit to `/fleet`.
  - **No D1.** `@open-athena/auth`'s gate needs a grant store, but only share links and Bearer tokens read it. Here it's an empty one (`NO_GRANTS`: every lookup misses, every write refused). A session is a signed cookie under `SESSION_SECRET`, judged by its signature. The policy admits `STAFF_DOMAIN` (comma-separated), a var the spec didn't list.
  - A Bearer token or `?key=` link is refused, never looked up.
  - Locally, `DEV_EMAIL` (`.dev.vars` only) makes a localhost request that address.
- **`/api/fleet`** fetches each deployment's `/api/health`, `/api/scan-runs`, `/api/series?store=meta` (the `/meta` store's bytes per scan) and `/api/series` (the primary store's root: its `objects` per scan, which scan time tracks; the token's viewer scope reads it, no `series_scope`), with that deployment's token. `/api/scan-runs` takes no `limit`, so the fleet keeps the runs started in the last `HISTORY_DAYS` (30: the page's longest chart range; the card draws its last `WINDOW_DAYS`, 7) whatever a deployment's scan rate, plus its newest even when older, capped at `FLEET_RUNS` (default 200); maintenance runs (`compact`, `race`, `merge`) and no-op (`nop`) runs left out.
  - It returns one JSON (`functions/_lib/fleetTypes.ts`): `days` (`HISTORY_DAYS`), and per deployment:
    - `health`: newest scan, its age, gaps summed.
    - `last` and `runs`: id, scan, trigger, start, duration, status, `failed_at`, open step issues, cost, and `thread_url` (the run's reply in `#<d>-admin`) when the deployment's `/api/scan-runs` carries it.
    - `meta`: bytes now / 1 d / 7 d before the read, plus the newest scan per UTC day over 30 days.
    - `errors`: which part couldn't be read, and why (`HTTP 403`, `no token (FLEET_TOKEN_GCS unset)`, `scan runs are not set up`). A deployment that fails never fails the document.
  - An open step issue is a `phase_issues` entry with neither `recovery` nor `passing` (so "search behind" counts).
  - The response also carries the full card's signed URLs (`card.dark`, `card.light`).
  - The document is held 5 min per colo (`FLEET_TTL`; `?fresh` reads anew), and browsers get `private, no-store`.
- **`/fleet`:** the same data, laid out for the web rather than as the card (Ryan, 2026-11-19):
  - A summary row per deployment: its newest 10 runs as ✅ / ⚠️ / ❌ / ⏳ marks (the Slack strip's rule, `_lib/runOutcome.ts`), each linked to `/scans/<run>` with a tooltip (outcome, failed step or open issues, trigger, start, duration, cost); the newest scan (age, called out past 36 h; duration; cost); the last 7 days' cost; the `/meta` size and its 7-day change (green grew, red shrank); links to its `/scans`, its Map and the newest run's thread (when it has one). Under 720 px each row folds onto three lines.
  - History: the card's four panels as interactive SVG (`src/charts.tsx`, no chart library) over 7 or 30 days (`?range=30d`; 30 only when the document carries `days >= 30`): `/meta` size and primary objects (log y, the newest scan per day), scan duration per run (hollow: an open issue or a failure), and cost per scan as a row per deployment on its own scale, a bar per UTC day stacked from its runs, scheduled under hand-started. Hover (a tap on touch) reads a point out; a click (a second tap) opens its run.
  - Each deployment's runs below, collapsed: health, then its newest 30 runs with their threads, and a link to its own `/scans`.
  - Tooltips are `@floating-ui/react`. `FLEET_MOCK=1 pnpm dev` serves the page's API from the test fleet, to look at it with no Functions or tokens.
- **The card** (`/og/fleet.png?[theme=light&]v=…&sig=…`): stacked panels over the last 7 UTC days, one trace per deployment in its color, each labelled at its latest point in a right gutter, as in the layout mock:
  - `/meta` size, log y
  - objects in the primary store, log y (Ryan, 11-16: object counts now drive scan time)
  - scan duration, with filled = ok and hollow = a step issue still open, or the run failed
  - cost per UTC day (the day's runs summed, so a deployment scanning 4× a day is comparable with a daily one; complete days only, today being partial)

  Each panel's note names the deployments it has no data for. The card is designed 480 px wide (phone width) and rasterized at 2.5× (1200 px). It sits on its own opaque, bordered background, so it reads the same in Slack's light and dark themes. `theme=light` is for the page. Lines get a halo (the panel color on dark, the line color darkened on light, so hf's yellow holds on white), and labels are the color nudged toward the ink.

  A full card needs a valid `sig` (HMAC under `SESSION_SECRET`, 7-day expiry, `IMAGE_TTL_DAYS`). Without one, or with a bad or expired one, it's the anonymous card: the frame, the deployments' names and colors, and no numbers. It is never an error (`x-og-tier` says which, and why). With `FLEET_CARD_PUBLIC = "1"` every request gets the full card (`x-og-tier: full (public)`, held 5 min): disky.oa.dev sets it (Ryan, 11-16: nothing on the card is sensitive, gcs's cost per scan included; the fleet's estimated storage bill is, and the card doesn't show it).
- **Slack:** `POST /api/fleet/slack` (Bearer `FLEET_JOB_TOKEN`) reads the fleet anew and builds the post: a one-line status per deployment (`:white_check_mark:` / `:warning:` / `:x:` / `:hourglass_flowing_sand:` / `:grey_question:`, its newest 10 runs as an emoji strip, oldest first, not linked (Slack's phone apps print an emoji-labelled link as its bare URL), scan linked to its run, status, duration, cost, `/meta` size and its 7 d change, a stale-scan note past 36 h), its `/scans`, run thread and `#<d>-admin` channel (`channel` on its `FLEET_JSON` entry) as links, a section per deployment (Slack refuses a section over 3,000 characters), a context line, and the card attached as a PNG file (`disky-fleet.png`), not an `image` block: Slack's phone apps offer Download only on a file. Each run renders the card, uploads it, attaches it in the edit and deletes the one it replaced (`@open-athena/thrds` `replaceFile`; `FLEET_KV` keeps the post's ts and its card's file id). The bot needs `files:write` and `files:read`. A comparison under it (Ryan, 2026-11-20): a reply in the post's thread carries the same card split into an image per panel (`card.ts` `only`, `PANELS`: `disky-fleet-{meta,objects,duration,cost}.png`, each under the full header, 1.7× its stacked height, all on one x axis), which Slack's phones show as a swipeable row; `publishSplit` keeps its ts and files under `slack-fleet-split` in `FLEET_KV` and swaps them each run (`replaceFiles`). A new post gets a new reply. A failure there is the route's `split_error` and never fails the post.
  - It posts once, then `chat.update`s that message. The channel + ts are kept in the KV binding `FLEET_KV`, or pinned by the var `SLACK_FLEET_TS` (in `SLACK_FLEET_CHANNEL`, an id).
  - With neither, it refuses to post a new message each run (502, saying so). `?new=1` posts one, to pin. `?dry=1` returns the message without posting.
  - A kept post deleted by hand is posted anew, where KV can keep the new one.
  - The bot is "Disky Admin Bot" (`DISKY_SLACK_BOT_TOKEN`). A Worker's Cron Trigger calls it hourly (`fleet/cron/`, below); `fleet-update.yml` is its by-hand twin (`mode` `dry` / `new` / `update`).
- **The clock** (`fleet/cron/`, a Worker; Ryan, 11-16): GitHub's `schedule:` ran late or not at all under load, and is disabled after 60 days without repo activity. Its `SLACK_CRON` POSTs `FLEET_SLACK_URL` with `FLEET_JOB_TOKEN`; its `HEALTH_CRON` dispatches `HEALTH_WORKFLOW` (`owner/repo/file@ref`) with `GH_DISPATCH_TOKEN` (fine-grained, Actions: write on that repo), so the health checks still run on Actions with each deployment's own `dt-cloud`, but start on a reliable clock. A failed job throws, so the Workers dashboard marks the run failed. The deployment's `fleet/cron/wrangler.toml` holds the crons and vars; the deploy workflow installs the secrets.

## Secrets / config (set in the root session with Ryan's go; never by the builder)

- Secrets: `SESSION_SECRET`, `OA_LOGIN_CLIENT_SECRET`, `FLEET_TOKEN_{GCS,CW,HF,R2}` (read grants minted by each deployment's session; scope must reach `/meta` usage), `FLEET_JOB_TOKEN`, `DISKY_SLACK_BOT_TOKEN`.
- Vars: `FLEET_JSON`, `STAFF_DOMAIN` (`openathena.ai`), `OA_LOGIN_ISSUER`, `OA_LOGIN_CLIENT_ID`, `SLACK_FLEET_CHANNEL`; optional `FLEET_RUNS`, `SLACK_FLEET_TS`.
- Binding: KV `FLEET_KV` (recommended; without it, one `?new=1` post and `SLACK_FLEET_TS`).
- Build env: `FLEET_ORIGIN` (`https://disky.oa.dev`), `FLEET_DOCS_INDEX` (the front door's README index); optional `FLEET_REPO`, `FLEET_REF`.
- The broker's client for this app must register `https://disky.oa.dev/auth/oa/callback` (and the dev stack's origin).

## Open

- `/meta` usage on gcs and cw is `scope: admin` today, so a read grant gets 403. Keep that gate; the source-owner handoff calls for an allowlisted aggregate summary route under `fleet:read`, rather than an admin grant or broader access to the whole meta store. This spec's secret/config list is the existing source-token handoff: dedicated source grants must be explicitly provided and installed for fleet use, never copied from unrelated job credentials. No narrow fleet summary route or `fleet:read` handling was found in the checked-out `cloud`, `gcs`, `cw`, `hf` or `r2` Functions source on 2026-11-09. Until the contract is implemented and rolled out by the sources, gcs/cw's `/meta` trace is missing and the panel's note names them.
- gcs `cost_usd` is null on every run (likely its machine type is missing from `scan_runs.py`'s price table). The cost panel's note names it.
- Meta scans aren't in `/api/scan-runs` on r2 and cw.

## Tests

`fleet/functions/**/*.test.ts` and `fleet/src/*.test.ts` (vitest; CI's `fleet` job also parses the toml, typechecks `pages-kit`, and builds):
- `routes.test.ts`: the gate on every route.
  - `/` and the docs public. `/fleet` gets a silent broker attempt signed out, and the page (its wall) after it.
  - `/api/fleet` 401 with no session, another domain's session, or a Bearer token.
  - `/og/fleet.png` anonymous without, with a bad, or with an expired `sig`, and full with a valid one.
  - The Slack route's token, its post-then-update through KV, and its refusal without KV.
- `_lib/fleet.test.ts`: `/api/fleet`'s document against stubbed deployments, exactly, every failure mode included.
- `_lib/card.test.ts`: the card's SVG by golden (`fleet/tests/golden/`; `node scripts/golden.mjs [--png DIR]` regenerates them).
- `_lib/slack.test.ts`: the Slack builder's text and blocks, by exact equality.
- `_lib/wranglerExample.test.ts`: the example toml names every `Env` field.
- `src/docs.test.ts`: the docs' link rewriting.

For an offline review artifact without posting or changing the goldens, run from `fleet/`: `node scripts/preview.mjs --fixture --out ../tmp/fleet-preview`. It writes the exact Slack builder's JSON/text and full dark/light SVG/PNG cards, clearly marked as fixture data. `--doc FILE` instead takes an `/api/fleet` JSON response. The post itself carries no card URL (its card is an attached file); `POST /api/fleet/slack?dry=1` answers the message and the card's size.

[deployment record]: https://github.com/Open-Athena/disky/blob/front-door/fleet/DEPLOYMENT.md
[Ryan's November 6 draft]: https://openathena.slack.com/archives/C0CFMN7FYG4/p1793969930968119
[Release workflow proposal]: oa-release-workflow.md
