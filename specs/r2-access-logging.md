# R2 access-log pilot and collector contract

Status: 2026-10-17. Gate 2 complete: window boundaries, delivery latency and ingestion sampling measured on RAC `jc-taxes` against known HEAD/LIST requests (below). The Worker collector now re-queries an overlap before a durable watermark, deduplicates by event id into per-day aggregates, records per-hour sampling, and publishes through one conditional `state.json` write. `dt-cloud access r2-export` queries `[from, to)` exactly and reports measured capture sampling. A replacement RAC Observability token (no expiry) is in `wt/m3`'s env; the October 18 pilot token remains in the root `.envrc` until it expires. No OA or HCCS settings were changed by this work.

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
- **Late delivery.** All 11 requests arrived (0 lost). Each became queryable 13–25 s after its timestamp, out of order: a later HEAD appeared before two earlier ones. A late event shows up on a re-query of an overlapping window under its original timestamp, so a window queried too early misses it. The collector's 2-hour lag and 1-hour overlap re-query cover that with a wide margin, and the daily replay re-checks six days.
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

## Account isolation / HCCS handoff

RAC enables and collects RAC bucket logs with RAC credentials into RAC private storage. HCCS should deploy the same collector for `ctbk`/`crashes` using HCCS credentials and private destinations; its local project agent owns that deployment. OA similarly owns `marin-na` and its private logs, independently of CW/GCS serving.

`r2.rbw.sh` may consume explicitly approved derived public scan/access summaries from those silos. It must not receive raw request IPs, actors or private object keys just because the reference implementation is shared. Cross-account read-only source credentials for the existing public demo do not imply permission to publish raw logs.

## Next gates

1. Complete: RAC token and dashboard/API/pagination pilot verified.
2. Complete (2026-10-17): boundary, latency and sampling semantics measured (above); overlap/replay with event-id dedupe, per-hour sampling and a conditional `state.json` publish implemented in the Worker; `r2-export` uses `[from, to)` and reports capture sampling.
3. [RAC collector stack] applied 2026-10-17 with cron off (6 resources + stack created; re-preview unchanged); its runtime secret (the replacement token, no expiry, from `wt/m3`) installed and cron enabled 03:33Z (`13 * * * *` hourly, `17 7 * * *` daily replay; the apply changed only `collector-cron`). Remaining: verify one scheduled run's `state.json`, `_SUCCESS.json` and sanitized logs. The public demo's serving bucket is not a raw-log destination.
4. Hand the same contract to HCCS. OA: Data Access Logs were enabled on `marin-na` around 03:15Z on 2026-10-17 (dashboard, by Ryan). No OA collector exists yet; build it after reviewing OA's pricing, sampling and permissions, and measure `marin-na`'s volume before reusing the pilot's limits.

[Data Access Logs][logs], [Observability pricing][pricing], [query API][query], [datasets][datasets].

[logs]: https://developers.cloudflare.com/r2/buckets/data-access-logs/
[pricing]: https://developers.cloudflare.com/observability/pricing/
[query]: https://developers.cloudflare.com/api/resources/workers/subresources/observability/subresources/telemetry/methods/query/
[datasets]: https://developers.cloudflare.com/observability/logs/datasets/
[RAC collector stack]: ../infra/cf/r2-access/README.md
