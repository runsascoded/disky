# Activity feed: who did what in the app, in /admin and the admin channel

Feedback from the 10/20 presentation: when someone acts in the app (assigns a path to a user, stages a deletion, grants access, writes a chart note) nobody hears about it. More broadly, admins want visibility into how people use the app: what they open, which controls they use, where they drill.

The session log (`specs/done/session-log.md`) already records the browser side (page loads, URL changes, control clicks, the path filter, pickers, fetches, errors) while an admin switch is on, for at most 31 days, viewable at `/admin/sessions`. This spec adds:

1. **Server-side action events**: every write an identified actor makes, browser or agent token, recorded once, server-side.
2. **Bursts**: one person's activity with no gap over 15 minutes (`BATCH_GAP_S`, the staged-deletion rule), summarized.
3. **Sinks**: each burst is one Slack thread in the deployment's admin channel and/or one Discord message, edited in place as the burst grows.
4. **Per-kind configuration** in `/admin`: for each kind of event, whether to record it and how to report it.
5. **Always-on logging**: the session-log switch gets a "no end date" setting, and `/privacy` says what is recorded while logging is on.

## Event kinds

A kind is a stable string. Server kinds come from write requests; client kinds are the session log's.

| kind | source | default record | default notify |
|---|---|---|---|
| `assign` | `POST /api/actions` (owner set or cleared, prefix or object) | on | `line` |
| `note` | `POST/PUT/DELETE /api/annotations` | on | `line` |
| `stage` | staging writes (`/api/plans/*`) | on | `count` (its own thread already lists each; the burst links to it) |
| `deletion` | `/api/sweep/*`, `/api/plan-sweep/*` (dispatch, stop, undo, purge) | on | `line` |
| `access` | `/api/auth/*` writes (grants minted, edited, rotated, revoked, disabled; access requests approved / denied; the sign-in allowlist) and `/api/og/*` (preview links made / revoked) | on | `line` |
| `admin` | `/api/db/*` writes (the admin console's table edits), the session-log switch, the activity config | on | `line` |
| `token` | `POST /api/token` (an agent token minted) | on | `line` |
| `profile` | profile writes (name, avatar) | on | `count` |
| `write` | any other successful non-GET by an identified actor | on | `count` |
| `view` | client `start` (a page load) | on | `count` |
| `nav` | client `nav` (any URL change: route, query params, `?section=`) | on | `count` |
| `click` | client `click` | on | `count` |
| `filter` | client `filter` | on | `count` |
| `pick` | client `pick` | on | `count` |
| `error` | client `error` / `reject` / `console`, and `api` with status 0 or ≥ 500 | on | `count` |
| `api` | client `api` (other fetches) | on | `off` |
| `prefetch`, `viewport`, `vis` | client | on | `off` |

- **record** (`log`): off drops the kind before storage. For a client kind, the server drops those events from each session-log batch. For a server kind, no event row is written.
- **notify**: `off` (not in Slack/Discord), `count` (a count in the burst's summary), `line` (a count, plus one line per event in the burst's reply for that kind).
- Session-log POSTs and pure reads sent as POST (`/api/session-log`, `/api/footer-cache`, `/api/digest-image`, `/api/static-bench`, `/api/prefixes`, sign-in flows) are never `write` events.

### Configuration: `activity_kinds`

One row per kind that differs from its default (`kind` PK, `log` 0/1, `notify` `off|count|line`, `who`, `ts`). No row = the default above. Each change appends an `admin_edits` row (`tbl = 'activity_kinds'`), as the session-log switch does. Read at most once per 60 s per isolate (the switch's `SWITCH_TTL_MS` pattern); an admin's write refreshes that isolate at once.

`/admin` → "Activity" section: a table, one row per kind (kind, what it covers, record checkbox, notify select), next to the session-log switch.

## Server events

Captured in `_middleware.ts`, after the route answers: a non-GET request that got a 2xx, from an identified actor (`identify`: an email session, a grant session or an agent token), whose route maps to a server kind (`serverKind`). The body is read from a clone before the route consumes it (JSON only). The middleware records:

- `ts`, `kind`, `actor` (email, else the grant's name, else `grant:<id>`), `via` (`session` / `grant`), `method`, `route` (the path, secret params masked as the session log masks them).
- `body`: the request body summary the session log uses for `api` events (per top-level key: an array's length, a `kind`/`op`/`action`/`mode` string, else the value's type).
- `detail`: optional, set by the route with `noteActivity(ctx, …)` on Pages' per-request `ctx.data` (the middleware and the route each get their own copy of the `Request`, so a request-keyed map doesn't reach across; `data` is shared). Routes without one get a generic line: the auth package's writes say what they did (`authLine`: "minted a grant", "revoked grant g1", "approved access request r9", …), anything else `METHOD /path` with the body summary.

Routes that note detail:
- `/api/actions` (`assign`): per action, `` `pattern` → owner`` (`→ nobody` for a clear), the memo if any (≤ 80 chars), `object: true` for an exact key. The middleware then adds the size at the latest scan (`activitySize.ts`: `stagedStats`, as the staged threads size a plan; best-effort, unsized if the index can't be read) to `detail.bytes` and the line.
- `/api/annotations` (`note`): added / edited / removed, the note's date and text (≤ 120 chars).
- `/api/token` (`token`): minted / rotated / revoked their agent token.
- `/api/og/*` (`access`): a preview link made (its page path) or revoked (never the token).
- `/api/db/*` (`admin`): the table, the key and the action; an update shows the column and its new value (≤ 60 chars).
- `/api/session-log/switch` and `/api/activity/kinds/*` (`admin`): what the switch / kind was set to.
- Staging and the sweep routes have generic lines for now (follow-up: the plan id and run mode as detail, so the summary links the plan's thread).

Recording runs in `waitUntil` and is best-effort: a failure is logged and never fails the request.

## Bursts

A burst is one actor's events with no gap over `BATCH_GAP_S` (15 min). Server events and client batches from the same actor join the same burst: client batches are keyed by the session's email, or `sid:<sid>` for an anonymous viewer on a `PUBLIC_READ` deployment.

### Storage (migration `activity-feed`)

- `activity_events`: `id`, `burst_id`, `ts`, `kind`, `actor`, `via`, `method`, `route`, `line`, `detail` (JSON). Server events only; client events stay in `session_log_batches`.
- `activity_bursts`: `id`, `actor`, `via`, `origin` (for links), `first_ts`, `last_ts`, `counts` (JSON `{kind: n}`, bumped atomically per kind with `json_set`), `pages` (JSON: the 20 most-viewed paths, from `view`/`nav`), `sids` (JSON: the session-log sessions it covers), `rendered_ms`, `dirty`.
- `activity_posts`: `(burst_id, sink, part)` → `msg_id` (Slack ts / Discord message id), `claimed_ms`. `part` is `parent` or a `line` kind's reply. The first renderer claims a row (`INSERT OR IGNORE … 'pending'`) and posts; later ones edit the posted message; a part still `pending` elsewhere is skipped, and a claim older than 60 s is retaken (its poster failed). A failed post releases its claim. So two isolates never open two threads for one burst.
- `activity_kinds`: above.
- No foreign keys. Retention: `SESSION_LOG_RETAIN_DAYS` (default 30) covers the bursts, events and posts, in the session log's hourly purge (`maybePurge` → `purgeActivity`).

## Sinks

A burst is reported once any of its events has `notify` other than `off`. Each sink is optional and is skipped when it isn't configured.

### Slack: `SLACK_BOT_TOKEN` + `SLACK_ADMIN_CHANNEL` (already set on gcs and cw)

- **Parent** (the summary, re-rendered on each update):
  - who (plain text, never a mention), how (browser / agent token), the span (`10:02–10:41Z`);
  - counts by kind (`12 assignments · 2 notes · 31 views · 58 URL changes · 40 clicks`);
  - highlights:
    - the largest assignment by bytes (`marin-us-east5/checkpoints/ (1.2 PB) → dlwh`);
    - owners affected;
    - access granted;
    - errors seen;
    - the most-viewed pages;
  - links to the burst in `/admin/activity` and its sessions in `/admin/sessions`, plus the staged plan's thread when `stage` events are in it.
- **Replies**: one per `line` kind in the burst, listing its events (≤ 30 lines, then `+N more`), edited in place while the burst continues. A new burst is a new thread.
- **Rate**: an event with notify `line` renders at once. Count-only changes render at most every 20 s per burst. If one arrives inside the window, the request waits in `waitUntil` until the window ends, re-reads the burst and renders it if it's still dirty, so the last count always lands.

### Discord: `DISCORD_ADMIN_WEBHOOK` (a secret; a channel webhook URL)

- One message per burst (`POST …?wait=true` returns its id), edited in place (`PATCH /webhooks/<id>/<token>/messages/<msg>`). Content: the same summary, then each `line` kind's list, cut to fit Discord's 2,000-character limit, with `+N more` and the `/admin/activity` link.
- Same rate rule as Slack.

### Mentions

None in this version: posts name people in plain text, as the staged threads do, and the admin channel is where people find out. Mentioning the assigner or assignee needs each person's Slack / Discord id, and some users (e.g. Stanford) are in neither. Mentions are a follow-up: a per-kind option that mentions the target when the deployment knows their id.

## Always-on session logging

- The switch's "no end date" is gcs's (`f20a4c20`, adopted on `cloud`): `PUT {enabled: true, indefinite: true}` stores `until_ms = FOREVER_MS` (2100-01-01; any stored value at or past it reads as indefinite), shown as "on, no end date". Turning it on with a date keeps today's behaviour. (`cloud` first had its own year-10000 `NO_END`; dropped for gcs's, which was already live.)
- `GET /api/session-log` answers `on` (the switch is on for everyone signed in) and `retainDays` to anyone, signed in or not. While it's on (any end date), `/privacy` carries a "usage log" paragraph: what is recorded, that it never records other fields' text or keys, who sees it (the admins), and for how long. `/privacy` always says that changes people make are recorded and the admins told.
- The session-log `POST` also feeds the actor's burst: per batch, the counts of the client kinds whose `record` is on, the paths viewed, the `sid`.

## `/admin/activity`

Admin only (`src/ActivityPage.tsx`; the `/admin` table is `src/ActivityKinds.tsx`). A list of bursts, newest first: who, last active, started, via, counts by kind. A row's last-active time opens it (`?burst=<id>`): its sessions in `/admin/sessions`, its Slack thread, the pages viewed, and its server events (time, kind, line). Filters: who (substring, `?u=`), kind (`?k=`), since; kept in the URL. Also in the omnibar's pages.

## Admin API

`/api/activity/*`, admin only: `GET kinds` (each kind's config, its default, whether Slack / Discord are configured), `PUT kinds/<kind>` `{log, notify}`, `GET bursts?actor=&kind=&since=&limit=`, `GET bursts/<id>` (the burst, its server events, its Slack thread's permalink).

## Deployments

`[cloud]` code; each deployment applies the `activity-feed` migration (`cloud`'s own D1 is the r2 demo's, which has no write actions; it gets the table, and the feed stays quiet).

| deployment | Slack | Discord |
|---|---|---|
| gcs | `#gcs-admin` (configured) | `DISCORD_ADMIN_WEBHOOK`: Ryan creates the webhook (which server / channel: open) |
| cw | `#cw-admin` (configured) | as gcs |
| hf, r2 | not configured: a gap in their deployment specs | not configured |

Each deployment: merge `cloud`, apply `site/migrations/activity-feed.sql` to its D1 (Ryan's go), deploy. Before the migration the feed is a nop (every read and write treats a missing table as off), and the session log works as before. Optionally `wrangler pages secret put DISCORD_ADMIN_WEBHOOK`, and the session-log switch at `/admin` → "no end date".

## Verified

- `wrangler pages dev` over the built site with a local D1 (2026-10-21): a kinds `PUT`, the switch set to "no end date", an agent-token mint and a session-log batch landed in one burst, each server event with its route's line (`ctx.data` reaching the middleware), and a kind turned off was dropped from the next batch before storage. `/admin/activity` and `/privacy` checked in Chrome. The `/admin` table is covered by a render test; Slack / Discord against a stubbed `fetch`.

## Tests

- Pure parts, pinned with exact output: route → kind mapping, body summary, burst assignment (gap edge cases, client + server events joining), the summary and reply renderers for Slack and Discord (truncation, `+N more`), the kinds config merge (defaults + rows).
- `sqliteD1` for the storage: events → bursts → counts; config changes; retention.
- Sinks with a stubbed `fetch`: first event posts a parent; a `line` event posts a reply then edits it; count-only updates are throttled and the trailing render lands; a Slack failure doesn't fail the request.
