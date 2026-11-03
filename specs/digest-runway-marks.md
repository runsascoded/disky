# Digest quota marks by runway, as admin-page knobs

Status: design agreed with Ryan 2026-11-03 (phases 1–3); phase 2's emoji upload pending. Affects the `cw` digest template (`cloud/src/dt_cloud/digest_cw.py`, the daily replies), the site's OP renderer (`site/src/digest/pools.ts`, `cw.ts`, `codec.ts`) and `/admin/slack` (`site/src/SlackPage.tsx`).

## 1. Problem

Each pool's mark is set by fill alone (`_mark`: 🔴 at `mark_pct`, 🟠 within 10 points, 🟡 within 40, 🟢 below), and the clauses sort fullest first. A pool held flat near its quota on purpose (archival, fully used) reads as urgent; a pool at 60% filling several TiB a day reads as calm. What matters is how soon a pool runs out.

## 2. Runway (phase 1)

For each quota group (a bucket, or a zone's buckets summed), at a scan:

- `rate_w`: net change per day over the last `w ∈ {1, 3, 7}` days, from the latest scan at or before `t − w` (the template holds 30 days back: `Month.pre + lead + rows`; the site's `CwRows` the same).
- `growth` = max over the windows that have a scan (the fastest recent fill: a pool flat for the month that jumped yesterday still shows).
- `runway` = free ÷ growth, in days; ∞ when growth ≤ 0 or unknown; 0 when over quota.
- Mark: 🔴 runway < 3 d or over quota; 🟠 runway < 14 d or fill ≥ 95%; 🟡 runway < 30 d; 🟢 otherwise.
- Text: a clause whose runway is under 60 d gains `, +4.1 Ti/d, ≈8d to full` (commas: ` · ` separates the pools).
- **Order stays fixed** (config order, as before the circles): a reply series reads by position; the marks draw the eye.

Knobs: Python `quota_marks: runway` (beside `warning`, `circles`); the site's OP params `q=r` (beside `q=o` off, `q=c` circles). The quota alert keeps `alert_pct` for now.

## 3. Hue sweep (phase 2)

Runway → hue from green (120°, ≥ 60 d) to red (0°, ≤ 1 d), log-scaled, quantized to 8 steps, as 8 custom workspace emoji `:cwq-0:` … `:cwq-7:` (circles; Slack's built-in emoji only have 7 colors). A bot can't upload emoji (`admin.emoji.add` is Enterprise Grid only): they're added at slack.com/customize/emoji, driven in Chrome (Ryan's go covers the Save clicks) or by a reusable skill. Knob: `quota_marks: runway-hue` / `q=h`. Per-pool shapes dropped: the fixed order already identifies a pool.

## 4. Admin page knobs and push (phase 3)

`/admin/slack` already previews the OP under any params (`codec.ts`). It gains:

- **Live params**: the deployment's chosen params (OP and replies) stored where both the job and the site read them, so a toggle sticks and the next cron converges to it instead of reverting to `job/digest.yml` (`params` there becomes the default).
- **Replies preview**: the replies under the chosen params (today only Python renders them; the site gets a TS port, as it did for the OP).
- **Push**: for a chosen month, edit its OP (the site renders it and holds the bot token: `chat.update`) and its replies (a Batch job, `dt-cloud digest -m M -E`, or the site directly once replies render in TS), all of them or one day's reply. Admin-only, with a confirm showing what changes.

Live params: a D1 row (the admin page already writes D1; each change logged).

## 5. Phases

1. Runway marks + fixed order + rate text, Python and TS together, golden tests on both sides; cw flips to `runway`; re-edit November (`-E`).
2. Hue emoji (generate + upload), `runway-hue`.
3. Admin knobs: live params, replies preview, push.
