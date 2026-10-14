# R2 access-log pilot and collector contract

Status: 2026-10-12. Native Data Access Logs enabled on RAC `jc-taxes`; one bounded S3 LIST and HEAD succeeded after enabling. The user created a dedicated RAC Observability token (pilot expiry October 18) and saved it locally. API retrieval verified both `HeadObject` and `ListObjectsV2`; forced one-event pages recovered both followed by an empty page. The bounded Worker collector and Pulumi stack are implemented locally: 18 collector tests and nine infrastructure tests pass, type-check and dry-run build pass, and the real RAC preview proposes six new CF resources plus the stack, with no updates/deletes and cron disabled. No infrastructure apply occurred; no OA or HCCS logging settings changed. No scheduled collector is deployed.

## Reference implementation

`dt-cloud access r2-query` performs a bounded private diagnostic query against the documented Cloudflare telemetry API. It filters dataset `r2` by bucket and requires timezone-qualified start/end timestamps. It preserves the complete provider envelope, including query status, sampling statistics and page counts; it never advances an ingest watermark. Prefer `$CF_OBSERVABILITY_TOKEN`, falling back to `$CLOUDFLARE_API_TOKEN`; Cloudflare currently specifies Workers Observability Write permission even for this query operation.

```bash
dt-cloud access r2-query -a "$CLOUDFLARE_ACCOUNT_ID" -b jc-taxes -f 2026-10-11T23:25:00Z -t 2026-10-12T00:00:00Z -o tmp/jc-taxes-events.json
```

This is a sample, not an export: a successful response with a 100-event cap is not evidence that the entire window was collected. Raw responses can contain object keys, actor identities and IPs; keep them private and out of Git, public build logs and demo artifacts.

`dt-cloud access r2-export` drains a bounded window using `$metadata.id` cursors until an empty page. It refuses non-COMPLETED queries, query-level sampling, page-count discrepancies, stalled cursors and page-cap exhaustion. It deduplicates event identities, writes a new private archive (mode 0600; existing output refused), and prints only action counts/coverage status. This is not yet a scheduled collector, overlap store or watermark publisher. Large exports belong on remote nodes; the laptop test collected only the two known pilot events.

```bash
dt-cloud access r2-export -a "$CLOUDFLARE_ACCOUNT_ID" -b jc-taxes -f 2026-10-11T23:25:00Z -t 2026-10-11T23:40:00Z -l 1 -n 4 -o tmp/jc-taxes-paged.json
```

Live pagination proved `events.count` is the returned page's count, not the full query's total: the one-event pages each reported 1, while the drained window contained 2 distinct events. Query-level `statistics.abr_level=1` is not proof of unsampled capture: ingestion sampling is independently reported by `sampleInterval` in calculation aggregates, not the event metadata observed in this pilot. Export summaries therefore explicitly mark capture sampling as unknown and provider delivery as best effort.

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
2. Validate window boundary semantics and ingestion sampling via calculation queries; implement overlap/replay and durable publish state.
3. Human-review/apply the prepared [RAC collector stack], install its runtime secret separately, then preview/apply cron enablement. The initial preview leaves cron off, protects the new private archive and expires raw `runs/` objects after 30 days. Verify one scheduled run and replace/renew the seven-day pilot token before expiry; the public demo's serving bucket is not a raw-log destination.
4. Hand the same contract to HCCS, then pilot OA after reviewing account pricing/sampling and permissions.

[Data Access Logs][logs], [Observability pricing][pricing], [query API][query], [datasets][datasets].

[logs]: https://developers.cloudflare.com/r2/buckets/data-access-logs/
[pricing]: https://developers.cloudflare.com/observability/pricing/
[query]: https://developers.cloudflare.com/api/resources/workers/subresources/observability/subresources/telemetry/methods/query/
[datasets]: https://developers.cloudflare.com/observability/logs/datasets/
[RAC collector stack]: ../infra/cf/r2-access/README.md
