# Digest month threads: a rolling window, never an empty month

**Status:** proposal, 2026-11-01, approved by Ryan (implement on hf first, review previews before any deployment's live thread changes). Shared (`[cloud]`: `cloud/src/dt_cloud/digest.py`, the `digest_gcs.py` / `digest_cw.py` templates).

## Problem

A new month's thread opens nearly empty: the 11/1 OP's chart has one bar at the left edge of a 30-day axis, and the weekly summaries hold one partial week. The thread reads as if the deployment had no history.

## Design

The thread stays one per calendar month, its identity (title, month-to-date headline) unchanged. Its **window** becomes the longer of the last 30 days (inclusive of today, the latest scan's date) and the month to date: it starts at `min(month start, today − 29d)` (`window_start`). On the 1st that's Oct 3, on the 15th Oct 17, and from the month's 30th day on just the month. Its **baseline** is the last scan before its first day (a store younger than the window: its first scan).

- **Chart** (the hosted plot): x-range = `[window start, month end]`, points from the baseline on. Days before the month are drawn, with the current month shaded, a rule at the month boundary, and `← Oct | Nov →` above the panel.
- **Diff map** ("What changed"): the window's, baseline → latest, so on the 1st it shows the last 30 days' change.
- **Weekly summaries**: every ISO week overlapping the window, so a new thread starts with about four rows. Weeks that ended before the month are marked with the month of their last scan (e.g. `wk of 9/28 (Oct)`: that week is mostly October), and their link target is unchanged. A week holding only a store's first scan (nothing to measure from) is skipped.
- **Headline**: month-to-date stays the first figure. The window's change goes beside it: `+4.7 TiB month-to-date · −21.0 TiB last 30d`, over baseline → latest. A store younger than the window reads `since M/D`, and the figure is dropped when its baseline is month-to-date's own (it would only repeat it: hf's first month).
- **Previous month**: one link to last month's thread in the OP (`[← October](…)`): the previous month's converge state's OP, by `client.permalink`. None when that thread doesn't exist, and none on Discord.
- **Replies are unchanged**: one per scan day, in this month's thread only. No seeded, copied, rotated or deleted replies: every reply notifies followers, Slack can't reorder replies, and deletions erode history. The window lives entirely in the OP, which the digest already edits in place.

## Tests

- Window bounds for the 1st, the 15th, the 29th, 30th and last day of a month, and across a year boundary (Jan 1 → Dec 3).
- The weekly list on the 1st holds the previous weeks with the previous-month marker. The headline on the 1st shows both figures.
- Rendered previews (the OP text plus the plot PNG) for hf, gcs and cw on 11/1 and mid-month, attached to the review. **No Slack posts** until Ryan approves the previews. 11/15 hadn't happened at review time: the mid-month stand-ins are 10/15 (gcs, cw) and 10/31 (hf, whose history starts 10/15).

## Rollout

hf first (its digest template), then each deployment's next digest run after its merge. Converge state (the per-month JSON) needs no migration: the OP is re-rendered from the snapshots each run.
