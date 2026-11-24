# One monthly admin thread per deployment

Open question #92; Ryan's go 2026-11-24.

## Why

Today `nominal` (`cloud/src/dt_cloud/nominal.py`) groups a deployment's scan runs into threads in `#<deploy>-admin` by status: a thread opens on a status change (`clean`, `issues`, `failed`) and lasts until the next one. A quiet clean thread already keeps a single rolling runs reply (`trail`) that is edited in place. So the status grouping no longer buys anything, a quiet thread can run for weeks, and its head is a wall of text.

`#<deploy>-usage` already has the right shape: one thread a month, a rolling 30-day window in its opening post (OP), pinned, linked to the months either side. The admin channel should match. The OP covers the job's side (runs, outcomes, cost, run time, metadata size) the way the usage OP covers the data's side.

## The thread

- **One thread per UTC month per deployment** in `#<deploy>-admin`, opened by the month's first run report.
  - It is pinned, and the previous month's thread is unpinned (the digest's `pin_op`).
  - The previous month's OP gains a `→ <Month>` link, and the new OP links back with `← <Month>` (the same link line and relink tool as the digest, `digest relink`).
- **Sender:** `<title> · runs`, under the app's own icon.
  - An edit can't change a post's icon, so the sender doesn't carry status. Status lives in the text and the card.

### Opening post

The OP is short, and the card carries the detail:

```
✅ 12 consecutive clean runs · latest 11/24 07:17Z · 46m 40s · $0.17
This month: 41 runs · 38 clean · 2 issues · 1 failed · 12 scheduled / 29 manual
Cost: $6.31 month-to-date · $7.02 last 30d · run time 43m (7d median, +19%)
Metadata: 49.9 G · +3.06 G last 30d · All scans
← October · December →
[status card image]
```

- **Status line:** the newest finished run's outcome and the current streak (the head's text today), plus its time, duration, cost and output.
- **Month-to-date counts:** runs by outcome and by trigger.
- **Cost:** this month and over the last 30 days; run time as the 7-day median and its change, using the card's figures.
- **Metadata:** the `meta` store's size and its 30-day change, from that store's `/api/series`.
  - The data store's sizes stay in `#<deploy>-usage`.
- **Card:** today's status card (`/og/deploy.png`: the last 30 runs, rolling across the month boundary like the usage OP's window).

The text is rendered by the site, as the digest's OP is (`/api/digest-op`).
- A new route, `POST /api/scan-runs/month-op`, renders it from D1 `scan_runs` plus the meta store's series.
- The job's grant authenticates the call. The route returns `{text, card_url}`.
- `nominal` posts what the route returns, so the fleet app can share the renderer (pages-kit).
- `/admin/slack` previews it.

### Replies

- **One rolling runs reply per ISO week:** `wk of 11/23 · runs`, with today's `trail` line format, oldest first.
  - The reply is edited as each run starts and ends, including the live `⏳ running` line.
  - A new week starts a new reply.
  - A week of cw's runs (4 a day, about 28 lines) can pass Slack's 3,000-character section limit. The reply then splits into several sections in one message, not several messages.
- **A failed run, or one with issues, gets its own reply:**
  - posted under its outcome's sender (`run_sender`: outcome icon, trigger badge);
  - with its steps' top-level alerts folded in (today's `fold` path);
  - and also sent to the channel (`reply_broadcast`), so a failure still shows up in the channel without opening the thread.
  - Its line in the weekly reply links to that reply.
- **Run links:** each run's `/scans` record (`scan_runs.thread_url`) links to its line's reply: the weekly reply, or the run's own reply when it has one.

### Closed months

On the month's last report, or the first one of the next month:
- the OP's text is frozen, with final figures and no status line;
- its card becomes a frozen image of the month's final 30 runs.
- Signed card URLs last 7 days (`IMAGE_TTL_DAYS`), so a closed OP needs an image that doesn't expire.
  - Rec: at close, render the PNG once and publish it beside the digest's plots (`digest.pages_deploy`'s Pages project, a content-addressed path).
  - The alternative is a non-expiring signature for a closed month's card (the content is fixed once the month ends). That leaves a permanent public URL to the run data.

## The fleet message

`#disky-admin`'s fleet post (the `fleet/` app, disky.oa.dev) keeps its status strip.
- Each deployment's reply under the post shows the same figures as that deployment's OP, rendered by the same pages-kit function, with the card.
- It links to the deployment's month thread.
- That folds the per-deployment fleet work (#87) into this renderer: one set of figures, in both places.

## Migration

- New config `nominal.monthly: true`. It's opt-in while this rolls out and becomes the default once every deployment is on it (parity).
- **Cutover, on a deployment's first monthly report:**
  - the open status thread gets a final edit linking to the month thread;
  - the month thread opens, seeded with the month's runs so far (from D1);
  - existing runs' `thread_url`s are re-pointed to their weekly replies (the `link_threads` path).
- **Canaries:** hf and r2 first, then cw and gcs, each deployment shipping it under its standing go.
- **Once all four are monthly,** the status-segment code goes (`segment`, `quiet`, failure-thread heads) and the threads' state shrinks to month, OP, weekly replies and failure replies.

## Phases

1. Site: `POST /api/scan-runs/month-op` (renderer in pages-kit), `/admin/slack` preview, tests.
2. `nominal`: monthly mode behind `nominal.monthly`, with OP, weekly replies, failure replies, pin, relink, close and cutover. Tests: the state machine over a fixture month, cutover from a quiet thread and from a failure thread, closing the month, idempotent re-runs.
3. Canary on hf and r2, then cw and gcs.
4. Fleet: the per-deployment reply uses the shared renderer and links the month thread.
5. Remove the status-thread code.

## Decisions

- Closed-month card hosting (above). Rec: publish the PNG with the digest's plots.
- Weekly runs replies, rather than one reply per day or one for the whole month. Rec: weekly.
- Failure replies broadcast to the channel. Rec: yes.
