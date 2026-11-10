# Generation retirement and compaction publishing

Automatic cleanup collects verified compaction predecessors after a three-day grace. It keeps historical scan coverage in the current compacted generation; it does not impose a history horizon. Full-history storage still grows with changes.

## Protocol

1. Publish generation data, its reader markers, then `compaction.json`. The completion record inventories the compactor's output checksums.
2. Publish the source store's forwarding record and current pointer. Copy the new generation to each serving copy, verify size/checksum metadata there, and copy its forwarding record and current pointer last.
3. `dt-cloud generation-gc` requires matching current pointers, base scan rosters and completion records with nonempty output inventories across every declared copy. A missing completion record or absent/empty inventory refuses collection. Every inventoried output must exist with matching sizes. It preserves named current pointers, explicit deployment pins, candidates and their inputs, and the newest successors of pinned roots. Older interval-store generations without checksum inventories continue to use their existing lineage-specific cleanup, not this coordinated command.
4. Only a predecessor with a published forwarding chain reaching the common current generation in every copy is eligible. Unlinked rebuilds, unknown directories and source archives stay for manual review. A candidate whose source publication is inconsistent stays too.
5. The grace starts at the later of the predecessor's last write and its immediate successor's publication in any copy. A subsequent compaction does not restart an older predecessor's grace.
6. Execution holds both builder coordinator and compaction leases in the scratch bucket, with heartbeats. It writes the exact plan and per-copy deletion inventories to `<kind>/gc/<timestamp>-<uuid>/plan.json` in that scratch bucket before removing anything; failure to write the receipt aborts cleanup. It rechecks publication state and deletion inventories before deleting a generation. Lease loss or disagreement aborts cleanup. Logs retain the receipt URL and per-copy counts; a partial failure can be retried. The scratch bucket's lifecycle also bounds receipt retention.
7. Retain `successor.json` tombstones indefinitely: profiles and old links may still name retired roots. GCS soft-delete retention can delay physical reclamation after live objects disappear; this protocol does not change bucket retention.

Pins are deployment configuration, including secondary stores and both production and preview. A new pin must be configured before cleanup can run against its generation. Default dry runs never acquire a writer lease or delete objects; `-x` requires the scratch bucket.

## CW rollout

The primary follows `static-names/CURRENT.json`; meta pins `2026-10-18meta` in both environments. The scan's post `store-gc` step coordinates `gs://oa-cw-s3-usage-data` and `r2://oa-cw-s3-usage-index`, using `oa-cw-s3-usage-scratch` for leases and a 259,200-second grace. A failed or busy cleanup is recorded as a maintenance shortfall and a later scan retries it. Existing interval-store lineage cleanup stays enabled on the R2 interval store.

## Compaction R2 copy

The compacting worker retains the new generation on its local SSD after publishing it to GCS. Pass that root to `r2_publish_gen` rather than downloading the same bytes again through GCS's file reader. Before each local upload, check its size and MD5 against the published GCS inventory. R2's metadata checks, marker ordering, retries and pointer-last publication remain required. A standalone `compact-r2` recovery job still streams GCS when it has no local outputs.

`dt-cloud r2-copy-probe` measures both paths over the same bounded sample, with alternating AB/BA rounds, local checksum cost included. Source download time is reported separately because a compaction already has its outputs. It writes only UUID-owned temporary R2 keys, verifies every uploaded object's size and checksum metadata, and deletes those keys on success or failure.

## Validation and measurements

The paired Batch probe `cw-r2-copy-probe-20261107-b` succeeded in `us-east1` on the compactor's `n2-highmem-48` with eight local SSDs and 32 upload workers. Each measurement copied the same 35 objects (4,293,389,279 bytes). Downloads to the SSD took 10.298 seconds, reported separately; a real compaction already has its output files.

| Round | Path | Seconds | MB/s |
| --- | --- | ---: | ---: |
| 0 | GCS stream | 44.439 | 96.61 |
| 0 | Local files, including MD5 validation | 13.842 | 310.18 |
| 1 | Local files, including MD5 validation | 12.394 | 346.42 |
| 1 | GCS stream | 41.455 | 103.57 |

Mean elapsed time improved by 3.274×. At that ratio a 29-minute full-generation copy would take approximately nine minutes; that extrapolation is not a measured full compaction. Every upload passed its size/checksum-metadata check and all 140 owned temporary objects were deleted.

Seven obsolete test/canary image digests were deleted after active-job and deployment-reference checks: `compact-test`, `lean-test`, `region-test`, `cw-canary`, `cw-next`, `remeasure-f105` and `remeasure-ff6c`. The superseded benchmark-only image was also deleted after its job succeeded, for eight removed digests total. The previous production digest is retained as `rollback-20261107`. Registry cleanup is already active: keep `latest` and the ten newest builds; delete older versions after 14 days.

Remote validation passed 161 targeted tests on the image runtime (Cloud Build `b66ae357-5124-4959-93a3-a656c0cb8780`). A broader run covered static and interval compaction and publication ordering. The lineage follow-up passed 171 remote tests (Cloud Build `d3139e4c-9186-4482-a9e2-90095cdfe481`).

The first cleanup held both builder leases, retained forwarding tombstones, and preserved current generations, rollback grace, meta pins and unknown directories. Post-cleanup cold serving probes passed all 16 requests. A manual metadata scan published the new footprint at `/meta`. Private per-generation storage measurements, deletion receipts and source manifests remain under ignored `tmp/`.

## Interval lineage follow-up

The original interval lineage walker missed `backfill-churn.json` parents. The corrected walker recognizes churn and opaque-object-version migrations, includes a replacement generation's publication time in its parent's grace, refuses deletion if CURRENT changes during planning, and creates a missing forwarding tombstone before deleting a backfill parent's data. Existing aliases survive collection; pins, rollback grace and unrelated generations remain protected.

CS3 means O(changes), rather than O(scans): retaining observed history is compatible with that goal. Optional scan thinning further reduces historical resolution and storage through a separate policy. Its implementation is tracked in `specs/scan-thinning.md`.
