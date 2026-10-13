# Session log: optional, time-boxed user-action logging

When someone reports a bug, an admin opens their session's event log and sees what they did, what the site fetched, and what failed. It's for a few days of internal use: logging is off by default, an admin switches it on at `/admin`, and it turns itself off on a date, so it can't be left on by accident. Viewers see no notice.

This is an event log, not a screen recorder: no DOM snapshots, no form contents other than the path filter.

## Switch: `/admin`, stored in D1

A runtime switch, not a deploy var: the "Session log" section at the bottom of `/admin` has an on/off switch (`role="switch"` checkbox) and a "through" date (UTC).

- Turning it on with the date untouched runs it through the UTC day 7 days from now (`LIMITS.defaultDays`); the date can be moved, at most through the UTC day 31 days from now (`LIMITS.maxDays`; past that, or in the past, the API answers 400 and nothing changes). While on, changing the date shows a "Set date" button.
- It turns itself off at the end of its date: nothing is written, the stored end has just passed (`/admin` then says "Off: ended after …"). Expiry is checked on both sides: the server stops accepting batches, and an open tab stops logging at the instant (it reads `until` at boot).
- API (admin only, `requireAdmin`): `GET /api/session-log/switch` → `{enabled, until, reason, who, ts, defaultUntil, maxUntil}`; `PUT` the same path with `{enabled: true, until?}` (a `YYYY-MM-DD` runs through that UTC day, an ISO instant ends at it, empty = 7 days) or `{enabled: false}`.
- Storage: `session_log_switch`, one row (`id = 1`, `until_ms` NULL = off, `who`, `ts`), in the session-log migration itself (`0018` on cw, `0042` on gcs): no new migration, and it is strongly consistent, unlike `CACHE_KV` (a cache keyspace with a 30-day TTL, not bound on every deployment). No row = off. Each change also appends an `admin_edits` row (`tbl = 'session_log_switch'`, `pk = '1'`, `insert` / `update`, old and new row JSON), the same audit trail `/api/db` writes.
- Cost: `GET /api/session-log` (every page load) and each `POST` read the switch at most once per 60 s per isolate (`SWITCH_TTL_MS`, cached per D1 binding). The cache holds the end instant, not the answer, so expiry stays exact. An admin's `GET` / `PUT` bypasses and refreshes the cache, so a change applies at once in that isolate and within 60 s everywhere (an open tab whose next flush gets 503 stops).
- Without the migration the switch reads "not set up": the config says `{enabled: false, reason: 'not set up'}`, `/admin` shows that line and no form, and a `PUT` is 503.

## What is recorded

Each batch carries a per-tab session id (`sid`, in `sessionStorage`, so it survives reloads of that tab), a per-page-load id (`pid`), a sequence number, and the build SHA (`VITE_BUILD_SHA`, `git rev-parse --short=10 HEAD` at build time). The server adds the signed-in identity (email and `via`, from the request's session, never from the client) and the user agent. Each event carries a client timestamp (`t`, epoch ms) and a kind (`k`):

| kind | fields | source |
|---|---|---|
| `start` | `u` (URL), `ref` (referrer origin) | page load |
| `nav` | `u` | `history.pushState` / `replaceState` / `popstate`, when the URL changed (route, `?d=`, `f=`, drills, `?o=`, …) |
| `click` | `n` (`data-log` name), `l` (label: `aria-label`, else text, ≤ 60 chars), `x` (checkbox state) | capture-phase click on an interactive control (button, link, menu item, option, tab, checkbox / radio, `[data-log]`) |
| `filter` | `n`, `q` | `input` on a `[data-log-input]` field, debounced 800 ms. Only the path filter carries it |
| `pick` | `n`, `v` | `change` on a `<select>`; `slog('pick')` from custom pickers (assign, scan) |
| `prefetch` | `w` (what), `p` (path) | hover-intent prefetches (`prefetchCover`) |
| `api` | `m` (method, if not GET), `u`, `s` (status, 0 = network), `ms`, `c` (`x-cache`), `qe` (`x-query-engine`), `err` (the JSON body's `error`, for non-2xx), `b` (request-body summary) | wrapped `window.fetch` |
| `error` | `msg`, `src` (file:line:col), `st` (stack) | `window` `error` |
| `reject` | `msg`, `st` | `unhandledrejection` |
| `console` | `msg` | `console.error` |
| `viewport` | `w`, `h`, `dpr` | boot and `resize` (debounced 500 ms) |
| `vis` | `s` (`visible` / `hidden`) | `visibilitychange` |
| `dropped` | `n` | events past the per-load cap |

### What is not recorded

- Form contents other than the path filter (memos, names, emails, dates, the admin console's fields): only `[data-log-input]` fields report `input`.
- Secrets: URL params `key`, `token`, `code`, `state`, `nonce`, `access_token`, `id_token`, `sig`, `signature`, `secret`, `password` are replaced by `…` in every recorded URL (`start`, `nav`, `api`). Cross-origin request URLs are recorded as origin + path, no query. Headers are never recorded.
- Request bodies: an assignment / staging POST records only a summary — per top-level key, an array's length, a `kind`/`op`/`action`/`mode` string, else the value's type (`{items: 12, kind: "assign", memo: "string"}`).
- No DOM snapshots, no keystrokes, no mouse movement.

Strings are truncated at log time (labels 60, filter 200, URLs 300, messages 300, stacks 1500).

## Transport

- Client-side buffer; flush every 10 s, when 200 events accumulate, on `visibilitychange → hidden`, and on `pagehide`. Interval flushes use `fetch(…, {keepalive: true})` (through the original, unwrapped `fetch`: the logger never logs itself); hide/pagehide use `navigator.sendBeacon`. Nothing awaits a flush and failures are dropped, never retried: the log can lose a batch, it can't slow the app.
- Caps: a batch's body ≤ 60 000 bytes (split across several posts otherwise; `sendBeacon` / `keepalive` share a 64 KiB in-flight budget); the buffer never exceeds 200 (it flushes there); ≤ 20 000 events per page load, past which events are counted, not kept (a `dropped` event per flush).
- Server: `POST /api/session-log` (`requireViewer`; anonymous `public` viewers are logged with no email on a `PUBLIC_READ` deploy). Body ≤ 64 KiB (413), ≤ 300 events (413), schema-validated (400), per session ≤ 20 000 events (429). Replies 204. A response of 503 (off / expired) stops the client.
- Boot: `GET /api/session-log` (`no-store`) answers `{enabled, until, reason}`; `enabled` only while the switch is on *and* the request has a viewer identity, so the sign-in page never logs. While off it costs that one request per page load; the logger itself is a lazily imported chunk, loaded only when on. Errors raised before the chunk arrives are held (≤ 20) and replayed into the log.

## Storage: D1

Compared:

| | (a) D1 table | (b) R2 object per batch | (c) Workers Analytics Engine |
|---|---|---|---|
| infra | none new: the `DB` binding exists on every app deployment; one additive migration | a bucket binding (gcs has `INDEX_R2`; others none) | a new dataset binding, plus a CF API token secret: AE is queried only over the SQL HTTP API, not from the binding |
| cost (a few days, ~100 users) | ~2 rows written per flush (batch row + session upsert, + index entries); a busy tab ≈ 1–2K rows/h — well inside the paid plan's 50M rows/month | Class A op per batch (~$4.50/M); listing for the session index is Class A too | free tier 100K points/day, then $0.25/M |
| write limits | ~1K writes/s per DB; fine at this volume | none in practice | 25 points per invocation (a batch of 200 events doesn't fit as one point each), 16 KB blobs per point |
| query ease | SQL: per-user, per-session, error counts in one indexed query | list prefix + GET + parse every object; the session list needs a separate index | SQL, but sampled under load (bad for "show me *this* session exactly"), and only via the API token |
| retention | a `DELETE … WHERE received_ts < ?` (indexed) | lifecycle rule (cheap, but infra outside the repo) | fixed 3 months, not configurable |

**Choice: D1.** No new infrastructure (the endpoint already lives next to the `DB` binding and the `requireAdmin` gate), exact (unsampled) per-session reads, and the session list is a plain indexed query. Volume is a short window of human clicks, far below D1's limits. One row per *batch* (events as a JSON array) rather than per event keeps rows written ~100× lower and the timeline a single `SELECT`.

### Schema (`migrations/cw/0018_session_log.sql`; gcs: the same file as `migrations/gcs/0042_session_log.sql`)

Additive only: three new tables, no foreign keys (so the purge can delete in any order).

- `session_log` — one row per `sid`: `email`, `via`, `build`, `ua`, `started_ms`, `last_ms` (client clock), `n_events`, `n_errors` (`error` + `reject` + `console` + `api` with `s` 0 or ≥ 500), `n_batches`, `first_url`, `received_ts` (epoch s, server clock: what retention keys on). Indexes on `received_ts`, `(email, last_ms)`.
- `session_log_batches` — PK `(sid, pid, seq)`; `received_ts`, `first_ms`, `last_ms`, `n`, `events` (JSON array as validated). A duplicate `(sid, pid, seq)` (a retried beacon) is ignored and counts nothing (`INSERT OR IGNORE`, the session row bumped only when a row was inserted).
- `session_log_switch` — the switch (above): `id` (= 1), `until_ms`, `who`, `ts`.

### Retention

`SESSION_LOG_RETAIN_DAYS` (default 30). The purge (`DELETE` from both tables where `received_ts` is older than the cutoff) runs from the endpoint, throttled to once per hour per isolate: on any `GET /api/session-log` (every page load hits it) and on each new page-load's first batch. It runs whether logging is on, off or expired, so the data ages out after the window closes as long as the site gets traffic. `SESSION_LOG_RETAIN_DAYS` is an optional `[vars]` value; unset = 30. A missing table (migration not applied) makes the purge a nop. To end it early: `DELETE FROM session_log_batches; DELETE FROM session_log;`.

## Viewer: `/admin/sessions`

Admin-only (`requireAdmin` on the APIs; the page links from `/admin`).

- `GET /api/session-log/sessions?email=&since=&errors=1&limit=` — newest first (`last_ms`), ≤ 500.
- `GET /api/session-log/sessions/<sid>` — the session row plus its events, flattened across batches, ordered by `t` (ties by `pid`, `seq`, position).

The list: user, start, duration, events, errors (highlighted), build; filters for user (substring), since (date) and errors-only, kept in the URL. The session view: a DIY SVG strip of the session's events on a time axis (ticks by kind, errors red, hover = floating-ui tooltip, click = jump to the row), then a timeline table (offset, kind, summary), errors highlighted, each row with "open this view" — the URL in effect at that moment (the last `start` / `nav` at or before it), opened in a new tab.

## No user-facing notice

Viewers see nothing: no About line, nothing on `/privacy` (the first version showed a line in both while on, and a permanent `/privacy` paragraph; both were removed, since this runs for a few days on internal users). The only UI is admin-only: the switch on `/admin` and the viewer at `/admin/sessions`.

## Overhead

Measured on the build (`pnpm -C site build`, vs. the spec commit):

- Main chunk: +2.7 KB raw / +1.2 KB gzip in the first version (the boot shim, the notice, the lazy route); the runtime switch dropped the notice and added the `/admin` switch (not re-measured). CSS +2.4 KB / +0.4 KB gzip. The logger (`sessionLog-*.js`, 7.7 KB / 3.5 KB gzip) loads only while on; the viewer (`SessionsPage-*.js`, 9.9 KB / 3.9 KB gzip) only on `/admin/sessions`.
- Off: one `GET /api/session-log` per page load, nothing else.
- On: `log()` ≈ 0.2 µs per event including its share of the per-flush serialization (node, M-series laptop); a wrapped `fetch` adds ≈ 1.7 µs per request. `sessionLog.test.ts` keeps a loose budget check (< 20 µs/event).

## Enable (gcs)

1. On the gcs branch: copy `site/migrations/cw/0018_session_log.sql` to `site/migrations/gcs/0042_session_log.sql` (it now carries `session_log_switch` too); apply: `wrangler d1 migrations apply oa-gcs-usage-auth --remote` (Ryan's go). Deploy the merged code. No `[vars]` needed (`SESSION_LOG_RETAIN_DAYS` only to change the 30-day default).
2. Signed in as an admin: `/admin` → "Session log" at the bottom → check "Logging" (the date defaults to 7 days out; move it first if wanted, ≤ 31 days). Uncheck to stop early; it also stops by itself after the date.
3. Check: `/api/session-log` → `{"enabled": true, …}` signed in (within 60 s on other isolates); browse; `/admin/sessions` lists the session. `SELECT * FROM admin_edits WHERE tbl = 'session_log_switch'` shows who toggled it.
