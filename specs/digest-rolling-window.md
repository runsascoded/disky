# Digest month threads: a rolling window, never an empty month

**Status:** proposal, 2026-11-01, approved by Ryan (implement on hf first, review previews before any deployment's live thread changes). Shared (`[cloud]`: `cloud/src/dt_cloud/digest.py`, the `digest_gcs.py` / `digest_cw.py` templates).

## Problem

A new month's thread opens nearly empty: the 11/1 OP's chart has one bar at the left edge of a 30-day axis, and the weekly summaries hold one partial week. The thread reads as if the deployment had no history.

## Design

The thread stays one per calendar month, its identity (title, month-to-date headline) unchanged. Its **window** becomes the longer of the last 30 days and the month to date:

- **Chart** (the hosted plot): x-range = `[min(month start, today − 30d), month end]`. Days before the month are drawn, with the current month shaded and a rule at the month boundary. On the 1st that's Oct 2 → Nov 30, by the 30th just November.
- **Weekly summaries**: every ISO week overlapping the window, so a new thread starts with about four rows. Weeks before the month are marked as previous-month (e.g. `wk of 10/12 (Oct)`), and their link target is unchanged.
- **Headline**: month-to-date stays the first figure. The 30-day change goes beside it: `+4.7 TiB month-to-date · +38 TiB last 30d`.
- **Previous month**: one link to last month's thread in the OP (`← October`).
- **Replies are unchanged**: one per scan day, in this month's thread only. No seeded, copied, rotated or deleted replies: every reply notifies followers, Slack can't reorder replies, and deletions erode history. The window lives entirely in the OP, which the digest already edits in place.

## Tests

- Window bounds for the 1st, the 15th and the last day of a month, and across a year boundary (Jan 1 → Dec 2).
- The weekly list on the 1st holds the previous weeks with the previous-month marker. The headline on the 1st shows both figures.
- Rendered previews (the OP text plus the plot PNG) for hf, gcs and cw on 11/1 and 11/15, attached to the review. **No Slack posts** until Ryan approves the previews.

## Rollout

hf first (its digest template), then each deployment's next digest run after its merge. Converge state (the per-month JSON) needs no migration: the OP is re-rendered from the snapshots each run.
