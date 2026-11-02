# Digest: quota pressure at a glance

**Status:** proposal, 2026-11-02, direction approved by Ryan ("circles sg"); drafted by the cw session. Shared (`[cloud]`: `cloud/src/dt_cloud/digest.py`, `digest_cw.py`; the other templates where they have quotas). Builds on `digest-rolling-window.md`: same previews-before-posts rollout.

## Problem

cw's quota lines are hard to scan. The daily reply lists every pool in config order (`02a: 74.5% of 1P (232.2 Ti free) · hero 57 + rhoarnet 3 + 08a 35 Ti: 19.0% of 500Ti … · marinfold 0 + rno2a 100 Ti: 100.5% of 100Ti (-0.5 Ti free)`), so rno2a's overage on 11/1 sat last, unmarked. The OP plot draws only the primary bucket (02a) against its quota: a 100 TiB pool near full never appears in it.

## Design

### 1. Text: sorted by pressure, marked

- Pools (a bucket with a quota, or a zone's buckets sharing one) sort by % used, most full first.
- A status circle leads each pool's clause: 🔴 at or over `alert_pct` (cw: 90, the pending setting) or over quota; 🟠 within 10 points below it; nothing otherwise.
- Pools under 25% shrink to `name NN%` (no free figure); the full clause is kept for the rest.
- Over quota reads `(−0.5 Ti over)`, not `(-0.5 Ti free)`.
- The same order and marks in the OP's header line and every daily reply.

Example (11/1): `🔴 rno2a pool 100.5% (0.5 Ti over) · 🟠 06a 88.5% (11.5 Ti free) · 02a 74.5% (232 Ti free) · 08a pool 19% · 04a 0%`.

### 2. Optional: a custom gauge-emoji sweep

Stock circles give ~6 coarse colours. Like the trend-arrow avatars (`job/icons/arrows/gen-*.py`, angle + colour per Δ), a generated set `:q00:` … `:q100:` (+ `:qover:`) could encode the fraction itself: a ring filled clockwise to the %, coloured on a green → amber → red ramp keyed to `alert_pct`. That's 11–12 images, hosted beside the arrows; Slack custom emoji need a one-time upload by a workspace admin (Ryan). Pick circles first; the sweep is a drop-in swap of the glyph function.

### 3. OP plot: % of quota, one line per pool

- y = % of each pool's own quota (0 to max(105%, peak)), one line per pool; the quota is a single rule at 100%. A 1 PB pool and a 100 TiB pool share the axis honestly; no second y-axis, no panel per pool.
- Each line is labelled at its right end (`rno2a 68%`).
- Bands: 90–100% amber, above 100% red (thresholds from `alert_pct`).
- Pools that stayed under ~20% across the window are faint or dropped (listed in the legend as `also: 04a 0%, 08a pool 19%`).
- The treemap below stays the primary bucket's "what changed".
- Optional: a strip of horizontal quota bars under the title (one per pool, filled to its %, tick at `alert_pct`) for the current state; the lines carry the trend.

### 4. `/admin/slack`

The previews belong in the Slack control panel under construction (`/admin/slack-preview`, perhaps renamed `/admin/slack`): the OP text and plot for each variant (circles / gauge emoji; lines / lines + bars) rendered from the same snapshots, so Ryan picks before any thread changes.

## Tests

- Sort and marks: pools at 100.5 / 88.5 / 74.5 / 19 / 0% with `alert_pct` 90 → that order, 🔴 🟠 and three unmarked; an over-quota pool's `over` wording; no `alert_pct` → the marks key on 90.
- Zone pools aggregate before sorting (rno2a = marinfold + rno2a).
- Plot: a pool's line crosses 100% on the day it went over (cw's rno2a, late October); a pool under the faint threshold isn't drawn but is in the legend.
- Previews for cw (11/1, 11/2) on `/admin/slack`; no Slack posts until approved.

## Status

- Phase 1 (previews): the site's TS renderer has the quota line (`?dc=qc`) and the pools plot (`?dc=pp`) as `/admin/slack` options, default off (`site/src/digest/pools.ts`, `digestCard.ts` `poolsSvg`).
- Daily replies (Python, `digest_cw._tail`): config `quota_marks: circles` sorts the clauses fullest first, marks 🔴 at or over `mark_pct` (`alert_pct`, else 90; always past a quota) and 🟠 within 10 points below, shrinks a pool under 25% to `NN%`, and reads `(N Ti over)` past a quota (in either mode). The default `warning` keeps config order with `:warning:` at `alert_pct`. cw sets `circles`.
- Open: the OP's quota line and plot reach Slack with phase 2 of `slack-preview-page.md` (the job renders through the site); `alert_pct: 90` on cw waits on thrds' `post(broadcast=True)` (`~/c/thrds/specs/slack-reply-broadcast.md`); the gauge-emoji sweep (§2) is unstarted.
