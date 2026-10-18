# Access-log freshness: ingest off the scan's critical path, honest read data

From the gcs session (2026-10-18), after the `2026-10-18T0701` scan's 158-min `pre`. Ryan approved the plan ("yes plan sg go"). Shared code is `[cloud]`; the deployment schedules (Scheduler, profiles) belong to each deployment's session.

**Status:** Order 1 landed on `cloud`: `dt-cloud access ingest` is poller-safe (lease `access/state/.lease`, 8 h TTL: a held one logs and exits 0, an expired one is taken over; nothing pending = a listing or two per bucket, no DuckDB), `-P N` ingests N buckets at once under the one lease, every run reports stalled buckets (no CSV delivered in `-t` hours, default 24; `-j` JSON summary `{lease, buckets: [{…, newest_delivery}], stalled}`, `-x` exit 3), and `meta.access.through` (`YYYY-MM-DDTHH:00Z`, the hour of the newest `last_ts` after the as-of cut). Order 2 landed too: `options.access-ingest.poller: true` leaves the step out of the scan (`ScanProfile.enabled`; no `pre` task, `index` waits on the lists only and reads `options.index.access` as it stands). Off by default; each deployment flips it with its poller (Order 4). Next: Order 3 (C).

## Measurements (2026-10-18)

**`pre` is `dt-cloud access ingest`, and its time is linear in log bytes.** On the n2-highmem-8 it ran at a steady ~95–100 MB/s:

| Run | Usage-CSV bytes | Time |
|---|---|---|
| 10-17 | 345 GB | 3721 s |
| 10-18 | 958 GB | 9549 s |

- **Volume follows Marin's request traffic, and it swings a lot.** Per log-day: eu-west4 was 1.2–1.8 TB on 10-14/15, then 247 GB on 10-16. us-central2 was 4 GB on 10-13 and 349 GB on 10-17. One central2 hour is now 16 shards of ~1.8 GB.
- **`index` waits on `pre`.** It reads `access/agg` (`options.index.access`), so it waited 41 min on 10-17 and 2h18m on 10-18 after the list tasks were done.
- **GCS delivers usage logs ~9–16.5 h after their hour (median ~12.5 h).**
  - This is a file's `timeCreated` minus the hour in its name, over every pending file in all 7 buckets.
  - The lag is the same for buckets with ~0 bytes (us-east1, us-west1), so it comes from GCS delivery, not volume.
  - At 12:07 UTC the newest delivered hour was 00:00.
  - So the 07:00 scan's read data runs only to roughly the previous afternoon UTC.
- **The ingest is already incremental.** It keeps a per-bucket name watermark (`access/state/<bucket>.json`) and re-lists from `LAG_HOURS` before it. Running it more often needs no new state.

## Problems

1. **Scan wall time depends on traffic.** `pre` gates `index`, so a heavy log day delays the whole scan, and the delay will recur.
2. **Recent reads go missing (false "never read").** A path read only in the ~13–17 h before a scan carries no read in that scan. A path written recently and never read looks the same as data idle for months.
   - In `viz.py` the stored value is `a` = max last-read day, NULL = never read.
   - In `readLens.ts`, `readState` turns NULL into `never`, and `never` is the "sweep-interesting" color.
   - This matters for staging and deletion review, not only for the colors.
3. **The read window's end is never shown.** `meta.access.{from,to}` exists (epoch days, from `MIN/MAX(last_ts)`), but the UI doesn't say "reads through X". A scan doesn't say whether its read data is complete up to its own time.

## Plan

### A. Access ingest as its own job, run frequently

- **Schedule:** an hourly poller per deployment. Each run lists the pending CSVs past the watermark. If there are none, it exits in seconds. Otherwise it ingests them right away.
  - Same command (`dt-cloud access ingest`) and same state.
  - Small VM; a run processes the hours delivered since the last run.
  - Peak today is ~30–80 GB per delivered hour, i.e. 5–15 min at today's speed.
- **Lock:** one ingest at a time per deployment (a lease object next to `access/state/`), so a slow run and the next poll don't overlap.
- **Parallel when behind:** when a backlog builds (e.g. a traffic spike), fan out one task per bucket. Buckets are independent, and the state is already per bucket.
- **Scan side:** the scan no longer runs access-ingest. `index` reads whatever `access/agg` holds when it starts and records the newest log hour it saw (see B).
  - With that, `pre` leaves the critical path completely.
  - Optionally the scan triggers one last poll and waits a bounded time (e.g. ≤15 min) for it. Not on by default.
- **Alert:** if no new CSVs arrive for a logged bucket in more than ~24 h, alert in `#<deploy>-admin`. Logging may be off, or delivery may have stalled.

### B. Expose the read window's end

- Record `access.through` in `meta.json`: the newest log hour the scan's read data includes. Use the hour, not a day: `to` is a day, and the lag is about half a day.
- **Read lens:** show "reads logged through <ts>". When `through` is earlier than the scan time, show the gap as provisional: "reads after <ts> not yet delivered".
- **Older scans:** reads delivered later are not folded into a scan that is already published (its agg is filtered `< asof`).
  - When the latest ingested hour is past an old scan's time, that scan's read data could be marked "complete" vs "provisional (logs through X)".
  - Re-deriving old scans' read data after late deliveries is a separate decision. The interval store's read versions (`rd/`) may be the natural place. List it as open, not part of this change.

### C. Newly written data isn't "never read"

- **Last touch:** use `max(last read, last write)` as the lens's last touch.
  - A path's subtree already has `mtime` / `created` maxima in the store (`ptu`: `mtime_max`, `created_max`).
  - The lens colors by `max(a, write day)`.
  - "Never read" stays its own state: no read logged, and nothing written since logging began.
- **New state:** add "written after reads were logged" (write day after `access.through`). Its color is neutral, like `unlogged`, because there is no information about reads yet.
- **Staging and deletion review:**
  - A path counts as idle only when both its last read and its last write fall before `access.through`.
  - "Idle N days" is measured from `access.through`, not from the scan time.
  - Wherever the sweep / staging threshold is computed, it should use this rule.

- **As built (step 3, refined by Ryan 2026-10-18):** `site/src/readLens.ts`.
  - Lens states: `read` (read since its last write: read-age color), `written` (written since logging began and not read since that write: cross-hatched, filled by the write's age on the same scale), `never` (no read, last written before logging began: brick red), `unlogged` (gray). This replaces the neutral "written after reads were logged" color: a write after `through` is a `written` cell at the scale's new end.
  - Read mode shows a banner over the map: "last access data: <through>", plus "reads after <through> not yet delivered" when the scan postdates it. The window's new end is `max(to, through's day)`.
  - Idle rule (`idleness`): no verdict without `through` or on an unlogged path; a write on or after `through`'s day is not idle (a write on that day may follow its hour); otherwise idle `through − max(a ?? from, d ?? from)` days, a lower bound. Shown on `/staged` (sortable `idle` column) and in the map table's read cell. No automatic sweep/staging threshold exists on `cloud` or the deployment branches; staging is a reviewer's choice, so the rule is surfaced, not enforced.
  - The OG card draws no hatch: a `written` tile takes its write's age color.
- **Folder rule + a no-hatch tint (Ryan, 2026-10-18):** one new file used to hatch a big, well-read directory, because a directory's `a` / `d` are subtree maxima, possibly of different objects.
  - `hatch` tint (default): an object keeps the rule above (`written` when unread since its last write); a directory is `written` only when nothing under it has a logged read and something was written since logging began; a directory with any read takes its newest read's color. `readState(a, logged, range, { d, dir, tint })`; `dir` = the node's `k !== 'file'` (folds and pre-path-store nodes are directories) on the map, in its tooltip and the readAxis domain, and on the OG card.
  - `touch` tint: color = last touch (`lastTouch`: `max(read, write since logging began)`) on the same gradient, no hatch and no `written` state; `never` ("not touched") = neither; `unlogged` unchanged (reads alone).
  - Toggle: `?rt=` (`readTint.ts`, `enumParam`): `hatch` (default, omitted from the URL) | `touch`, as a `hatch | touch` pill pair in the read legend (styled like `log | lin | rank`). The legend items and the color picker's explainer follow the tint. `readDay` is the day a cell sits at under either tint (its color, the gradient brush's value, and the read axis's domain). The OG card always uses `hatch`. The idle rule is unchanged by the tint.
  - Age chart: its rows aren't paths but bytes written on day `d` under a directory last read on `a` (subtree max), so `hatch` applies the object rule: `a` absent or older than `d` means nothing under the directory was read since those bytes were written. Under `touch` a row stacks by its last touch. (Its read axis needs rows with `a`; the per-path `/api/age-pyramid` rows carry none yet, so it doesn't show there today.)

### D. Machine fallback: try both shapes at once (optional, after the timeout cut)

- **Today:** gcs's `index` falls back from n2d-highmem-32 to n2-highmem-32 after 3 min queued. That's `queue_timeout`, cut from 10 in gcs `54486e24`; a normal start takes ~1 min.
- **Option:** the head submits both shapes together. The first to reach RUNNING wins, and the other is cancelled at once.
  - **Race:** if both start, the loser must be cancelled before it writes. Safer still, each attempt writes to its own scratch path and the winner is promoted.
  - **Cost:** at most a few VM-minutes when both start together.
- Worth doing only if the 3-min timeout still costs real time.

## Region (answer to Ryan's question)

- **Where things run:** every gcs task runs in `us-central1` (`executor.region`).
  - Batch already spreads a job over all the region's zones (default `allowedLocations`), so a stockout in one zone is handled.
  - The n2d waits seen so far (10-18, and a ~30-min scratch wait) were stockouts across the region.
  - The list tasks also list the eu-west4 and us-central2 buckets from us-central1.
- **Another region:** that would need the job submitted in that region. Nextflow's google-batch `location` is set for the whole run, not per process.
  - The index's staged inputs and outputs live in `gs://oa-gcs-usage-dvx`, so they would cross regions, at a few dollars a scan.
  - Not worth it while the fallback to n2 is cheap.

## Order

1. `[cloud]`: A's poller command and lease, plus B's `access.through` in meta (small).
2. `[cloud]`: the scan profile skips access-ingest when a poller is configured, and `index` uses the agg as it stands.
3. `[cloud]`: C's lens state and the idle rule (site + staging).
4. Per deployment: hourly Scheduler (gcs via `infra/gcp` Pulumi, user-gated `pulumi up`), and drop `access-ingest` from `steps`.
5. D only if it's still needed.
