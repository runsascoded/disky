# R2 access-log pilot and collector contract

Status: 2026-10-17. Gate 2 complete: window boundaries, delivery latency and ingestion sampling measured on RAC `jc-taxes` against known HEAD/LIST requests (below). The Worker collector treats the UTC hour as its unit: each hour's aggregate is a pure function of one complete drain (event ids deduplicated within it, never persisted outside raw pages), a re-drain replaces it, and one conditional `state.json` write publishes every invocation (see [High-volume (hourly) mode](#high-volume-hourly-mode); the deployed RAC Worker still runs the earlier per-day version until the next apply). `dt-cloud access r2-export` queries `[from, to)` exactly and reports measured capture sampling. A replacement RAC Observability token (no expiry) is in `wt/m3`'s env; the October 18 pilot token remains in the root `.envrc` until it expires. No OA or HCCS settings were changed by this work.

## Reference implementation

`dt-cloud access r2-query` performs a bounded private diagnostic query against the documented Cloudflare telemetry API. It filters dataset `r2` by bucket and requires timezone-qualified start/end timestamps; the window is `[from, to)` (it sends `from − 1`, see below). It preserves the complete provider envelope, including query status, sampling statistics and page counts; it never advances an ingest watermark. Prefer `$CF_OBSERVABILITY_TOKEN`, falling back to `$CLOUDFLARE_API_TOKEN`; Cloudflare currently specifies Workers Observability Write permission even for this query operation.

```bash
dt-cloud access r2-query -a "$CLOUDFLARE_ACCOUNT_ID" -b jc-taxes -f 2026-10-11T23:25:00Z -t 2026-10-12T00:00:00Z -o tmp/jc-taxes-events.json
```

This is a sample, not an export: a successful response with a 100-event cap is not evidence that the entire window was collected. Raw responses can contain object keys, actor identities and IPs; keep them private and out of Git, public build logs and demo artifacts.

`dt-cloud access r2-export` drains a bounded window using `$metadata.id` cursors until an empty page. It refuses non-COMPLETED queries, query-level sampling, page-count discrepancies, stalled cursors and page-cap exhaustion. It deduplicates event identities and refuses events outside the window. After the drain it runs a weighted-count calculation over the same window and reports `capture_sampling` (`unsampled` / `sampled` / `inconsistent`), `estimated_events` and `max_sample_interval`. It writes a new private archive (mode 0600; existing output refused, the calculation included) and prints only action counts and coverage status. It advances no watermark; the scheduled collector ([RAC collector stack]) does that. Large exports belong on remote nodes; the laptop drains stayed bounded (at most 1,805 events, 5 pages).

```bash
dt-cloud access r2-export -a "$CLOUDFLARE_ACCOUNT_ID" -b jc-taxes -f 2026-10-11T23:25:00Z -t 2026-10-11T23:40:00Z -l 1 -n 4 -o tmp/jc-taxes-paged.json
```

Live pagination proved `events.count` is the returned page's count, not the full query's total: the one-event pages each reported 1, while the drained window contained 2 distinct events. Query-level `statistics.abr_level=1` is not proof of unsampled capture: ingestion sampling is reported only by `sampleInterval` in calculation aggregates, never in event metadata (measured below).

## Measured query semantics (gate 2, 2026-10-17)

Ground truth: two bursts of LIST + HEAD requests (6 and 5) on `jc-taxes` with the RAC read-only key, client send/receive times recorded. Raw responses stay in ignored `tmp/r2-gate2/` (0600).

- **Timestamps.** `timestamp` is the server receipt time in ms, between client send and receive. `$metadata.timestampNs` is the same value × 10⁶, and the ULID event id encodes the same millisecond. There is no separate ingest clock to filter on.
- **Boundaries: both ends exclusive.** An event at `t` is returned for `(t−1, t+1)` but not for `(t, t+1)`, `(t, t+1000)`, `(t−1, t)` or `(t−1000, t)`. Verified on two events, events view and calculations alike. `from == to` is rejected as a validation error. Two windows sharing an edge `T` would both miss an event at exactly `T`, so the collector and `r2-export` send `from − 1` to get exactly `[from, to)`.
- **Ordering and cursors.** Events come newest-first. Cursor pages (`offset` = last id, `offsetDirection: next`) drain to an empty page.
- **Late delivery.** All 11 requests arrived (0 lost). Each became queryable 13–25 s after its timestamp, out of order: a later HEAD appeared before two earlier ones. A late event shows up on a re-query of an overlapping window under its original timestamp, so a window queried too early misses it. The collector drains an hour only `LAG_HOURS` after its end, re-drains hours drained within `OVERLAP_HOURS` of their end, and re-verifies older hours round-robin within the six-day retention window: a wide margin.
- **Ingestion sampling is real and visible only in calculations.** Event rows carry no weight. A `count` calculation sums per-row weights, and `sampleInterval` is their mean per aggregate. Over 144 h the drain returned 1,750 `ListObjectsV2` rows while `count` was 1,795 (`sampleInterval` 1.02571 = 1795/1750). Bisected to specific LIST bursts (~180 requests in ~90 s): 10/14 12:30–13:30Z drained 169 rows vs `count` 178 (1.0533), and 17:30–18:30Z 165 vs 183 (1.1091). Quiet hours and the 11 ground-truth requests were exact (`sampleInterval` 1, count = drained). The weighted rows sat in per-second bursts, while `jc-taxes` sees only hundreds of events a day. The cause is undocumented, so treat any `sampleInterval` > 1 as missing events.
- **Query-time sampling is separate.** One 6-day calculation came back with `abr_level` 10, every count a multiple of 10 (a lone GET counted as 10); the identical query a minute later was `abr_level` 1. Both tools already refuse `abr_level` ≠ 1. Per-bucket `series` values in calculation responses are coarse estimates (2 HEADs shown as 20) and are never used.
- **Consequences.** Absence of an event in a `sampled` hour is weaker still than "no read observed". Report per-hour `capture`, never convert weighted estimates into per-object access records, and keep `sampled` hours out of any deletion-safety reasoning.

## Provider semantics and cost

Native logs cover S3/API/dashboard/Workers/public-domain operations, including read, write, list, multipart and delete requests. Delivery is asynchronous and best effort, with seven-day retention and no backfill before enablement. The current docs describe Observability queries, not guaranteed daily CSV delivery to a chosen bucket. A daily disk-tree ingest job would be our collector schedule, not a provider delivery guarantee.

Ingestion is pre-compression log-event/enrichment bytes, not monitored object bytes. Paid allowances are account-wide: 50 GB ingestion and 10 GB-month storage per billing cycle, then $0.25/GB and $0.10/GB-month. Pricing changes and effective dates must be checked before rollout. RAC's dashboard currently warns that exceeding 20M events/month without accepting new pricing causes sampling; no billing acceptance was performed by this agent.

Absence of an event means **no read observed since X**, subject to collection gaps and provider omissions; never label it “never read.” HEAD is metadata access, not content consumption. LIST does not mean every listed object was read. `content-length` is not necessarily transferred bytes; preserve absent/unknown values rather than filling them with zero.

The actual HEAD probe's `response.bytes` equaled the object's 2,695-byte size although HEAD downloaded no object body. This is a concrete reason not to interpret that field as downloaded content bytes. The event also contained object key, client IP, user agent and actor access-key identifier; those raw details were not copied into public artifacts. Verification screenshot: ignored `tmp/r2-pilot-events.jpg`, with event details collapsed.

## Complete collector gate

Before a scheduled collector can publish a collected-through timestamp:

- Validate actual R2 event fixtures, including S3 HEAD/GET/LIST, Writes, Workers and public-domain reads; retain immutable event IDs and event/ingest timestamps.
- Query bounded windows within retention, drain documented cursor pagination, and refuse incomplete/non-COMPLETED responses or unexplained count shortfalls. Test exact boundary semantics before assuming half-open windows.
- Re-query overlapping windows to tolerate late delivery, deduplicate by provider event identity, and publish aggregate artifacts atomically before advancing local state. Keep immutable raw private batches for replay.
- Record sampling, gaps, disabled intervals, retention overruns and provider delivery limitations. “Collector drained” is distinct from “provider recorded every event.” Do not turn sampled estimates into exact access counts or deletion safety claims.
- Normalize content reads separately from metadata reads, writes, listings and deletes. Reuse the access-overlay aggregation only after mapping object identity/time/operation explicitly; do not feed R2 JSON into the GCS CSV parser.

## High-volume (hourly) mode

Measured by the r2 session on OA's `marin-na` (2026-10-17, weighted calculation counts per UTC hour, 04–09Z): **~80K events/hour** (78–84K), ≈1.9M/day; PutObject ~59%, GetObject ~36%, ListObjectsV2 ~4%, DeleteObjects ~0.5%. `sampleInterval` was 1.02–1.04 in every hour, so every hour is `sampled` (the provider keeps ~96–98% of rows) and none can ever be `unsampled`. The first collector couldn't drain it: its hourly window plus overlap was ≈160K events against a 200K per-run page ceiling (100 × 2,000), the 144-hour daily replay ≈11.5M (~58× the ceiling), and each per-day aggregate kept every event id (~1.9M entries, tens of MB of JSON re-read and rewritten hourly, near a Worker's 128 MB). jc-taxes (RAC) sees hundreds of events a day.

The collector now has **one mode** for both, with the UTC hour as the unit (contract in the [RAC collector stack] README):

- An hour's aggregate is a pure function of one complete drain of that hour: event ids are deduplicated within the drain only (≤ one hour's events in memory), then counted per (object key, action) as `{n, first_ts, last_ts}` with times truncated to the minute, plus per-action/per-class totals, the deployment's own requests (`EXCLUDED_ACCESS_KEY_IDS`) counted apart, and the hour's sampling cell. No event ids persist outside the raw pages.
- A re-drain of an hour **replaces** its aggregate (a new immutable key, the state repointed; unchanged by digest writes nothing). That is how late delivery is absorbed; there is no cross-run id merge.
- Each invocation drains, oldest first and within a page/hour budget: new settled hours (`end + LAG_HOURS ≤ now`); then hours last drained "early" (within `OVERLAP_HOURS` of their end); then `VERIFY_HOURS` older hours round-robin within `RETENTION_HOURS`, which replaces the one-shot 144-hour replay. An hour that doesn't fit what's left waits for the next invocation. An hour that could never fit one (`⌊estimated / PAGE_SIZE⌋ + 2 > MAX_PAGES`, sized from the hour's count before draining) fails loudly with **`hour_over_cap`**, after publishing everything else, holding that bucket's watermark. Resuming a drain across invocations (or splitting an hour) was not built: at `PAGE_SIZE` 2,000 and `MAX_PAGES` 450 one hour holds ~896K events, 10× marin-na's peak.
- Public output for `PUBLIC_BUCKETS` is one file per hour, `<PUBLIC_PREFIX>/<bucket>/<YYYY-MM-DD>/<HH>.json`: per (key, action) `{key, action, n, last_ts}` plus the hour's `capture`, rewritten whenever the hour's aggregate is replaced, before the state publish. `dt-cloud access r2-agg` reads them straight into layer-2a leaf rows (`disk_tree.access.parsers.r2` → `aggregate_access_leaf`, the same parent synthesis as GCS, whose output is byte-identical) with a boolean `sampled` column (the hour's capture was `sampled` or `inconsistent`; GCS layer-2a has no such column). As-of rule: a row is kept only when its last request's minute ends by the scan's instant (`last_ts + 1 min ≤ cut`), exact for every kept row; only a row straddling the cut is lost whole, and scan instants are UTC midnights, so nothing straddles in practice. The scan's `day < D` rule in `viz` still applies downstream.

**Sizing** (`PAGE_SIZE` 2,000; raw bytes assume ~1.5 KB per event as returned by the events view, unmeasured; aggregates assume ~100 B per (key, action) row):

| events/h | pages/h (reserve) | subrequests/h | hours per invocation at `MAX_PAGES` 200 | peak memory (one hour) | raw archive/day (30-day TTL) | private aggregates/day |
|---|---|---|---|---|---|---|
| 100 (jc-taxes) | 1 (2) | 5 | `MAX_HOURS` | < 1 MB | ~4 MB | ~0.2 MB |
| 10K | 6 (7) | 15 | 28 | ~5 MB | ~360 MB | ≤ 24 MB |
| 84K (marin-na peak) | 43 (44) | 89 | 4 | ~30–50 MB (84K ids + ≤ 84K rows + one 3 MB page) | ~3 GB | ≤ 200 MB |
| 400K | 201 (202) | 405 | 0: needs `MAX_PAGES` ≥ 202 | ~150 MB: over the 128 MB isolate, split hours first | ~14 GB | ≤ 1 GB |

A steady-state marin-na invocation drains one new hour (89 subrequests, ~1–2 s of JSON parsing CPU) plus its re-verify hour(s); `MAX_PAGES` 200 leaves room to catch up ~4 hours per invocation after an outage. Private aggregates are not lifecycle-expired; a superseded version is deleted after the state publish that replaces it, so `agg/` holds one version per hour. Measured on marin-na's first run (2026-10-17, by r2): ~5.2 MB per hour, ~126 MB/day, ~46 GB a year; raw pages ~903 B/event (~1.7 GB/day, ~51 GB under the 30-day lifecycle). Compressing aggregates (or rolling closed days into parquet) is the open lever.

**OA's config** (`infra/cf/r2-access/wrangler.oa.json`, on the `r2` branch at 5886193b; not edited here) must, when it adopts this collector: drop `DAILY_CRON` and `REPLAY_HOURS` (the Pulumi program refuses them; one `HOURLY_CRON`), and set `RETENTION_HOURS` 144, `PAGE_SIZE` 2000, `MAX_PAGES` 200, `MAX_HOURS` 8, `VERIFY_HOURS` 1 (bound 3 + 24 + 400 = 427 subrequests), `MAX_PAGE_BYTES` 8388608, `LAG_HOURS` 2 with `OVERLAP_HOURS` 2 (one drain per hour at +2 h, final; re-checks come from re-verify; `LAG_HOURS` 1 doubles the drain cost), `LOOKBACK_HOURS` as wanted (≤ 144), and raise `limits.cpu_ms` (e.g. 60000; cron invocations allow up to 300000). With `VERIFY_HOURS` 1 each hour is re-verified about once before it leaves retention. Measure the raw page bytes per event on the first runs and revisit `MAX_PAGE_BYTES`.

## Account isolation / HCCS handoff

RAC enables and collects RAC bucket logs with RAC credentials into RAC private storage. HCCS should deploy the same collector for `ctbk`/`crashes` using HCCS credentials and private destinations; its local project agent owns that deployment. OA similarly owns `marin-na` and its private logs, independently of CW/GCS serving.

`r2.rbw.sh` may consume explicitly approved derived public scan/access summaries from those silos. It must not receive raw request IPs, actors or private object keys just because the reference implementation is shared. Cross-account read-only source credentials for the existing public demo do not imply permission to publish raw logs.

## Next gates

1. Complete: RAC token and dashboard/API/pagination pilot verified.
2. Complete (2026-10-17): boundary, latency and sampling semantics measured (above); overlap/replay with event-id dedupe, per-hour sampling and a conditional `state.json` publish implemented in the Worker; `r2-export` uses `[from, to)` and reports capture sampling.
3. [RAC collector stack] applied 2026-10-17 with cron off (6 resources + stack created; re-preview unchanged); its runtime secret (the replacement token, no expiry, from `wt/m3`) installed and cron enabled 03:33Z (`13 * * * *` hourly, `17 7 * * *` daily replay; the apply changed only `collector-cron`). Verified 2026-10-17 05:13Z (after fixing the 04:13Z run's `redirect: 'error'`, which workerd rejects): `state.json` published (`collected_through` 03:00Z, the first window 21:00Z–03:00Z: 6 hours, all `unsampled`, 0 events on a quiet bucket), `_SUCCESS.json` written, Worker log `r2-access archived`. The public demo's serving bucket is not a raw-log destination.
4. Hand the same contract to HCCS. OA: Data Access Logs were enabled on `marin-na` around 03:15Z on 2026-10-17 (dashboard, by Ryan). No OA collector exists yet; build it after reviewing OA's pricing, sampling and permissions, and measure `marin-na`'s volume before reusing the pilot's limits.
5. Hour-unit collector (this branch, not deployed): one hourly mode for quiet and busy buckets ([High-volume (hourly) mode](#high-volume-hourly-mode)). RAC's next apply replaces the Worker and drops the daily cron (`collector-cron` goes from two schedules to one); its version-1 `state.json` upgrades in place. OA's `wrangler.oa.json` needs the knobs listed above before its cron is enabled.

[Data Access Logs][logs], [Observability pricing][pricing], [query API][query], [datasets][datasets].

[logs]: https://developers.cloudflare.com/r2/buckets/data-access-logs/
[pricing]: https://developers.cloudflare.com/observability/pricing/
[query]: https://developers.cloudflare.com/api/resources/workers/subresources/observability/subresources/telemetry/methods/query/
[datasets]: https://developers.cloudflare.com/observability/logs/datasets/
[RAC collector stack]: ../infra/cf/r2-access/README.md
