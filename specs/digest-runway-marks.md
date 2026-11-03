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

## 3. Hue sweep (phase 2, built)

Runway → one of 8 custom workspace emoji, `:runway-1:` (red, least runway) … `:runway-8:` (deep green), uploaded 2026-11-03 (circles: `#e01e1e #ec4e0c #f27c00 #f2a900 #ddc800 #9cc41a #4fae34 #1e8e4a`, hand-picked; `tmp/emoji/gen.py`). A bot can't upload emoji (`admin.emoji.add` is Enterprise Grid only): they were added at slack.com/customize/emoji in Chrome.

- Step N = 1 + the number of cut points (days to full) the runway reaches, `RUNWAY_HUE_D = (1.8, 3, 6, 10, 19, 33, 60)` (log-spaced, 60^(k/7)): over quota or under 1.8 d → 1; not filling or ≥ 60 d → 8.
- A pool ≥ 95% full is never calmer than step 3 (`RUNWAY_HUE_FULL_N`), as `runway` holds it at 🟠.
- The names are a plain 1–8 scale and carry no thresholds, so the cut points can move without re-uploading.
- Order and rate text as `runway`. Knobs: `quota_marks: runway-hue` (`digest_cw._runway_hue`); the OP's params `q=h` (`pools.ts` `runwayHue`); the admin page's quota select.

## 4. Admin page knobs and push (phase 3)

`/admin/slack` already previews the OP under any params (`codec.ts`). It gains:

- **Live params**: the deployment's chosen params (OP and replies) stored where both the job and the site read them, so a toggle sticks and the next cron converges to it instead of reverting to `job/digest.yml` (`params` there becomes the default).
- **Replies preview**: the replies under the chosen params (today only Python renders them; the site gets a TS port, as it did for the OP).
- **Push**: for a chosen month, edit its OP (the site renders it and holds the bot token: `chat.update`) and its replies (a Batch job, `dt-cloud digest -m M -E`, or the site directly once replies render in TS), all of them or one day's reply. Admin-only, with a confirm showing what changes.
- **Selectors (built):** the push re-applies exactly what's chosen, never a whole-thread sweep. `dt-cloud digest -o/--op` (the OP, its plot re-rendered), `-d/--reply DAY` (repeatable) and `-y/--replies A..B` (an inclusive UTC-day range) edit only those already-posted messages to their current rendering: no new replies, no provisional reply, no alert, no plot unless `-o`; `-n` lists each message's ts and new body (`digest.apply_slack`). From a laptop: `job/cw-digest-local` (the CLI with cw's config, this checkout's code and its secrets from Secret Manager), e.g. `job/cw-digest-local -m 2026-11 -y 2026-11-01..2026-11-02 -c <channel> -n`.

**Built (phase 3; D1 recipe `site/migrations/digest-settings.sql` pending Ryan's apply):**

- **Live settings:** D1 `digest_settings` (one row per template: `params`, `quota_marks`, who/when; each change logged in `admin_edits`). `GET /api/digest-settings?template=cw` (viewer: the job's grant), `PUT` (admin). The job reads it before rendering (`digest.live_settings` → `with_settings`), over `job/digest.yml`'s values; no row, no token or an unreachable site keep the file's. CLI: `-L/--file-settings` ignores the live row; `-p/--params` and `-q/--quota-marks` override both for one run (a DM try-out).
- **Re-apply:** `POST /api/digest-reapply {month, channel?, target, dry_run, params?, quota_marks?}` (admin) submits a Batch job on the scan image running `dt-cloud digest -T cw -C job/digest.yml` with the selectors (`target`: `op` → `-o`, a day or `A..B` → `-y`, `thread` → a full converge), `-n` for a preview, `-p`/`-q` for candidate settings; output at `gs://<data>/digest/reapply/<job>.txt`, runs in D1 `digest_reapply`. `channel` defaults to `DIGEST_CHANNEL`; any channel id (a DM) is allowed.
- **Admin page:** `/admin/slack` gains a "Live settings" panel (params, optionally from a candidate column; reply marks; save with a second click) and a "Re-apply" panel (channel, the OP / a day range / the whole thread, under the form's settings or the live ones; preview, then apply with a second click; recent runs with their status and output). `:runway-N:` marks draw as their circle colors.
- **Replies preview:** still Python-only: a re-apply's dry run shows the replies' new bodies.

## 5. Phases

1. Runway marks + fixed order + rate text, Python and TS together, golden tests on both sides; cw flips to `runway`; re-edit November (`-E`).
2. Hue emoji (generate + upload), `runway-hue` (built; cw stays on `runway` until Ryan picks from a preview).
3. Admin knobs: live params, push (built; replies preview via the re-apply dry run).
