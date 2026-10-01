# Slack notifier: user-action feed + staleness alarms

Two asks, one component:
1. Near-real-time Slack notifications for site activity — **marks/claims** especially, maybe logins — with thrds-style message *editing* (update an existing message as info accretes, organize via threads) rather than one post per event.
2. The "push" half of pipeline alerting: the `run.sh` ERR trap (landed 8/28) covers *job fails loudly*; nothing yet covers *job never ran* / *snapshot stale* / *fate series missing*. That needs a prober that runs on its own clock.

## Architecture: no queue needed

"Can the www BE push events to a queue that drains into Slack?" — CF Queues does work from Pages Functions (producer binding; consumer must be a separate Worker, paid plan). But it's unnecessary here: **the events already land in D1**. Marks and claims are rows in the actions ledger with `ts` + `who`; the ledger *is* the queue, and a watermark over it is the consumer offset. No new write path in the site, no Queues dependency, at-least-once by construction, and replayable (re-render any day's digest from the ledger).

So: one tiny **scheduled Worker** (`oa-gcs-notifier`, same CF account, cron trigger every 1–5 min, free-plan compatible) that binds the same D1 database (`oa-gcs-usage-auth`) plus `SLACK_BOT_TOKEN`:

- **Drain**: `SELECT * FROM <ledger> WHERE ts > watermark ORDER BY ts` → group → post/update → advance watermark (single-row state table; the whole drain is idempotent under crash-retry because rendering is derived from the ledger, not from the events consumed).
- **Probe** (every ~15 min): `GET /data/scans.json` (newest date + `meta.json.published` age), `series.json` fate-key presence, D1 reachability, optionally CW snapshot freshness. Post an alarm on breach, and **edit it to "recovered ✅"** when the condition clears — the thrds-update model applies to alarms too (no flapping spam).

The only case where CF Queues earns its keep is sub-second latency or events that never touch D1 — neither applies. Revisit only if we grow such an event type.

## Message model (thrds-style)

State table `slack_msgs(topic_key TEXT PRIMARY KEY, channel, ts, thread_ts, fingerprint, updated)` — the notifier's memory of what it has posted where.

- **Marks/claims**: one message per `(user, utc-day)`, `chat.postMessage` on first action, `chat.update` as more arrive: "**Ahmed** marked 657 prefixes today — keep 16.9 Ti · sweep 31.0 Ti · 3 claims" (+ deep link to `/user/<id>`). Bursts (BulkBar posts hundreds of actions) collapse into one edit per drain tick, not hundreds of posts. A daily parent message ("marks — 8/28") can anchor a thread with per-user replies if the channel gets busy; start flat, promote to threads when volume demands.
- **Logins** (lower priority, maybe off by default): sessions aren't currently recorded as events — add a tiny append (email, ts, idp) in `/auth/sso` on successful sign-in (one D1 INSERT; also generally useful audit data). Then a "who signed in today" line the notifier edits in place. First-ever sign-in by a user is the genuinely interesting event — worth a distinct ping during the cleanup sprint ("🆕 julian signed in for the first time").
- **Channel routing**: `#gcs-usage` for real traffic, `#gcs-usage-staging` for development (both exist). CW alarms need a home — there is currently **no CW pipeline channel** (`#cw-quickwins`/`#cw-access-logs` are topic threads; the CW cron posts nothing anywhere). Proposal: `#cw-usage`, or fold CW staleness into `#gcs-usage` alarms.

## Non-goals / guardrails

- No per-event posts (burst spam), no DMs to users (nag pressure stays human, via Percy), no PII beyond name+handle in messages (established convention).
- The notifier reads the ledger and probes public/authed endpoints; it never writes marks. Its D1 writes are `slack_msgs` + its watermark (+ the sso login log, written by the site, not the notifier).
- Deploy as a separate `wrangler.toml` under `notifier/` (it's a Worker, not a Pages Function — Pages has no cron triggers).

## Build order

1. Worker skeleton + D1 binding + watermark + marks-per-user-per-day message w/ chat.update (staging channel).
2. Staleness probes + alarm/recovery editing.
3. `/auth/sso` login log + first-sign-in ping.
4. Threads promotion + CW freshness, if/when wanted.
