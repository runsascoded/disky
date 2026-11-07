# Non-live bytes: what the provider holds (and bills) that the listing misses

## Goal

Every scan lists **current objects** only (`disk-tree bulk-list`). What a provider stores, and usually bills, includes more: soft-deleted objects, noncurrent versions, unfinished multipart uploads. A deployment shows none of it today. Deletions look final the day they run, and the bill keeps the bytes for days or indefinitely.

Wanted: a **non-live toggle**. Per deployment and per path, it shows those bytes as classes beside the live ones, with when each expires where a rule or retention says. Read-only: it measures and never deletes or changes anything.

## Motivating case (hf, 2026-10-18)

hf retired 4 scans' per-scan files, and the retire backed them up into the GCS data bucket (`oa-hf-usage-data`). Then the 73.8 MB of backups was deleted by hand. /meta dropped the next scan (GCS 133 → 62 MB), but the bucket's 7-day soft delete keeps those bytes, billed, until about 2026-10-25. Nothing on the site says so. cw has the same blind spot, smaller than it once was: versioning on `marin-us-east-02a` was enabled 2026-09-15 and **suspended 2026-09-16** (31 TiB of retained versions purged that day), so a delete there now writes a 0-byte marker and frees the object. What remains is markers, plus noncurrent versions of objects written during the enabled window (a live sample of 80K entries on 2026-10-18 found none; `cw-noncurrent-gc` expires stragglers). Its digest spec already names those bytes "pending reclaim" and calls them "a new data path, not a reshaping of existing JSON" ([cw-slack-digest]).

## Inventory

| Provider | Class | API | Expires | Listing cost |
|---|---|---|---|---|
| GCS | soft-deleted objects | `list_blobs(soft_deleted=True)` (`softDeleteTime`, `hardDeleteTime`, `size`) | `hardDeleteTime` (= soft delete + the bucket's `soft_delete_policy` retention) | ∝ soft-deleted objects (usually small; a big sweep makes it large for 7 d) |
| GCS | noncurrent versions | `list_blobs(versions=True)`, keep `timeDeleted`/non-latest generations | lifecycle `daysSinceNoncurrentTime` / `numNewerVersions` rules, if any | ∝ every version (a full second listing) |
| GCS | incomplete multipart uploads | XML API `ListMultipartUploads` (+ `ListParts` for sizes) | lifecycle `AbortIncompleteMultipartUpload` age | ∝ open uploads (small) |
| GCS | resumable uploads in flight | **not listable**: there's no API that enumerates them | the session's 7 days | — (stated as a known gap) |
| GCS | early-deletion minimums (Nearline 30 d, Coldline 90 d, Archive 365 d) | none needed: derived from the existing listing's `created` + `storage_class_id` at delete time | — | free, but it's a **charge with no bytes**: a cost line, not a byte class |
| S3 / CAIOS | noncurrent versions | `ListObjectVersions` `Versions[]` with `IsLatest=false` | `NoncurrentVersionExpiration.NoncurrentDays` after noncurrent-since (the next version's `LastModified`) | ∝ every version (the whole bucket, again) |
| S3 / CAIOS | delete markers | `ListObjectVersions` `DeleteMarkers[]` (0 bytes; count them) | `ExpiredObjectDeleteMarker` | same pass |
| S3 / CAIOS / R2 | incomplete multipart uploads | `ListMultipartUploads` + `ListParts` (part sizes) | `AbortIncompleteMultipartUpload.DaysAfterInitiation` after `Initiated` | ∝ open uploads × parts |
| R2 | (versioning / soft delete) | none: R2 has neither, only multipart uploads | — | — |
| HF | git history beyond the current tree | already listed: `@history/<file>/<oid>` + `@history/@unlisted` rows, storage class 6 ([bulk_hf]) | — | in the main listing |

HF is the precedent: held-but-not-current bytes as their own rows, beside the live ones, so the listing sums to the provider's number.

## Existing pieces to reuse

- **S3 versions:** `sweep.py` `_list_versions` (l.484) paginates `list_object_versions` under a prefix (`Versions[]` + `DeleteMarkers[]`, key/version markers). `versioning_enabled` (l.160) is the gate, and `purge_run` (l.541) is the one place that already reasons about markers and shadowed versions ([sweep]).
- **GCS soft-deleted:** `sweep_exec.py` lists `soft_deleted=True` with `fields="items(name,generation,softDeleteTime),nextPageToken"` (l.1055), and `_soft_delete_days` (l.1276) reads the bucket's retention. `sweep_recovery.py` `recovery_sample` reads `size`, `soft_delete_time` and `hard_delete_time` ([sweep_exec], [sweep_recovery]).
- **Rules:** `lifecycle.py` already snapshots every bucket's lifecycle rules per scan (S3 and GCS shapes), and `site/src/lifecycle.ts` normalizes and describes them (`describeRule`: "abort incomplete multipart uploads after 7 d"). That gives each class its expiry rule; the step only needs the per-object timestamps ([lifecycle], [lifecycle.ts]).

## Design

### The step

A new scan step, `non-live`, non-core (`core=False`): it runs only where a profile asks for it (`options.non-live.classes: [soft-deleted, versions, mpu]`), per source. It's read-only: list calls only, no metadata writes and no restores. It's soft, so a failure warns the run and never fails the scan. It runs beside `list`, not after `index`, since it reads the bucket, not the layer-2.

### Output: a per-scan sidecar, never the layer-2

`<layer2 dir>/<scan>/non-live.parquet`, one row per `(bucket, class, prefix)` aggregate:

| column | |
|---|---|
| `bucket`, `class` | `soft-deleted` / `noncurrent` / `delete-marker` / `mpu` |
| `prefix` | the aggregation key (phase 1: `""`; phase 2: the same dir rollup as the layer-2) |
| `bytes`, `objects` | |
| `oldest`, `newest` | the class's own clock: soft-delete time, noncurrent-since, initiated |
| `expires_min`, `expires_max` | from retention or a rule; null = no rule (it stays until someone acts) |

Plus `_SUCCESS.json`: the classes listed, the requests made, and the listing cost. These bytes never go into the layer-2, the path store, the interval store or the static names. Live views stay exactly what they are, and the toggle is an overlay.

### Site

Behind one toggle (a `?nl` param, off by default):
- the root view's meters gain the non-live classes, hatched, so a meter reads billed rather than live;
- a treemap lens hatches each dir's non-live bytes over its cell, like the storage-class lens;
- the children table gains a "non-live" column, with a tooltip by class and its expiry.

The interval store doesn't carry it (phase 3 decides if over-time should).

### Digest

A "pending reclaim" line: soft-deleted plus noncurrent bytes and the date the bulk expires. This is cw's digest framing C, second half.

## Status

**Phase 1 built (2026-10-18):** `dt-cloud non-live -c CLASSES [-E ENDPOINT] [-o OUT.parquet] URI` (`cloud/src/dt_cloud/non_live.py`) and the scan step `non-live` (`options.non-live.classes`, off unless listed in `steps`), writing `<layer2>non-live/<bucket>.parquet` per bucket source (per bucket, like the layer-2, not one file per scan). A class the source's provider can't have is refused. Expiry is filled for GCS soft-deleted (`hardDeleteTime`); S3 classes' expiry from lifecycle rules is phase 2. GCS `mpu` goes through the XML API and needs HMAC credentials. Checked live: `gs://oa-hf-usage-data` held 90.5 MB soft-deleted in 131 objects, expiring 2026-10-24 → 10-25. The step also merges each bucket's rows into the scan's snapshot `non-live.json` (`-j`), as a `pre` step (beside `list`), so the snapshot reaches R2 with the scan's others; the site's root view shows a "Not in this view" line under the meters, per class its bytes and expiry (`NonLive.tsx`, `nonLiveModel.ts`), when the scan has any. The `?nl` toggle is kept for phase 3's lens: phase 1's line is information, not a mode. Not yet: the digest's "pending reclaim" line (every deployment's digest output changes with it: its own commit).

## Phasing

1. **Totals per bucket**, `prefix = ""`. It's cheap where the classes are small (GCS soft delete, MPUs) and puts a number on the root meters and in the digest. hf (data bucket) and gcs first.
2. **Per-prefix aggregation**: the same dir rollup as the layer-2 (`import`'s aggregation over a listing of the non-live rows), so the table and the lens work at any drill depth.
3. **Site lens + over time**: the hatched treemap. Then decide whether the interval store, or a small sidecar series, carries non-live totals per scan, so the size chart can draw a billed band.

## Open questions

- **Listing cost on huge versioned buckets (cw, roughly 1 PB, tens of millions of objects):** a `ListObjectVersions` pass costs as much as the scan's own listing. Options:
  - run it weekly, not every scan;
  - list only the prefixes swept since the last pass (the sweep logs name them);
  - trust lifecycle math alone: bytes swept × `NoncurrentDays`, with no listing.
- **GCS versions vs soft delete:** on a bucket with both, a deleted object is a noncurrent version first and soft-deleted only once that version is deleted. Make the classes disjoint, by generation.
- **Early deletion:** model it as a cost line (`scan_cost` / the digest), not a byte class? It needs the delete events, meaning the scans' Δ of objects in cold classes, not a listing.
- **Resumable uploads (GCS):** document the gap. No API reaches them.
- **Attribution:** do non-live bytes join the owner lens (who deleted, whose versions)? Probably later. The sweep logs know the actor for sweeps.

[cw-slack-digest]: https://github.com/Open-Athena/disky/blob/cw/specs/done/cw-slack-digest.md
[bulk_hf]: ../src/disk_tree/find/bulk_hf.py
[sweep]: ../cloud/src/dt_cloud/sweep.py
[sweep_exec]: ../cloud/src/dt_cloud/sweep_exec.py
[sweep_recovery]: ../cloud/src/dt_cloud/sweep_recovery.py
[lifecycle]: ../cloud/src/dt_cloud/lifecycle.py
[lifecycle.ts]: ../site/src/lifecycle.ts

## Review (controller, 2026-10-18)

- **Constant size over time.** A per-scan `non-live.parquet` is O(#scans), and phase 2's per-prefix rollup is the size of the layer-2's dirs per scan: the redundancy `scan retire` (specs/lean-scans.md) just removed. Keep only the newest scan's sidecar (the toggle shows "now"), and carry per-scan **totals** (phase 1's rows, a few per bucket) for over-time; a per-prefix history only through the interval store, if ever.
- **Per-deploy code** ([[deploy-code-looks-standalone]]): `cloud` holds the step, the sidecar schema and the site toggle; each deployment wires only its provider's classes (gcs: soft-deleted; cw: markers + MPUs; hf: none beyond `@history`; r2: MPUs). No empty classes on a deploy whose store can't have them.
- **Phase 1 first, totals only**, on hf's data bucket and gcs (soft-deleted: the motivating 73.8 MB, and gcs's sweeps). cw's `ListObjectVersions` pass waits on the cost question.
- **Avoidable cases.** Retire backups now live in a lifecycle-expiring place (cw: a Coldline bucket, 180 d), not in a soft-delete bucket that's then emptied by hand. The toggle should make such double-billing visible, not normal.

