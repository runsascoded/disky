# Soft delete on S3-style buckets

Disky works out of the box on an S3-compatible store whose buckets keep deleted data, and says plainly when they don't. cw stays off: no bucket there has versioning enabled, and turning it on is not part of this.

## Why it stays off on cw

Versioning is bucket-wide. On 2026-09-15 it was enabled on `marin-us-east-02a`, and within about 16 h it retained about 30 TiB of deleted `tmp/ttl=*` files as noncurrent versions, which pushed the bucket to its quota (`specs/cw-sweep.md`, current policy of 2026-10-20). Any store that enables it needs a `NoncurrentVersionExpiration` rule first.

## What "retention" means on S3

GCS reports one number, its soft-delete retention. S3 derives it from `GetBucketVersioning` and `GetBucketLifecycleConfiguration` (`soft_delete.s3_retention_seconds`):

| Versioning | Enabled noncurrent rule | `retention_seconds` |
|---|---|---|
| `Enabled` | `NoncurrentDays = N` | `N * 86400` |
| `Enabled` | none | `null`: kept until purged |
| `Enabled` | only under some prefixes | the smallest `N` |
| `Suspended` or never enabled | any | `0`: deletes are permanent |

A `Suspended` bucket can still hold versions from when it was enabled, and `cw-noncurrent-gc` expires those after 1 day on `marin-us-east-02a`. It reads 0 because a new delete there isn't recoverable.

## What shipped

1. **Reading:** `dt-cloud soft-delete -b s3://NAME [-E ENDPOINT]` writes the same `snapshots/soft-delete/<date>.json`. `retention_seconds` may be null. `SNAPSHOT_VERSION` stays 1: gcs never writes null, and a reader treating only 0 as off reads null correctly. The `soft-delete` scan step's default includes S3 sources, over one endpoint, and cw's profile runs it.
2. **Minimum:** `/admin`'s setting applies to `plan-sweep`. With no row the default is per executor: 7 for `sweep`, **0 for `plan-sweep`**, so cw's behavior stays as it is. Above 0, `plan-sweep execute -s N --for-real` refuses a bucket retaining less, or one whose retention it can't read. At 0 an unreadable bucket still deletes and records no retention, because deletion must not require versioning permissions.
3. **Run record:** the dispatch and the job's summary record `min_soft_delete_days`, and the summary `soft_delete_days`, with `NO_EXPIRY_DAYS` (36500) for kept until purged, since NULL there means not recorded. `reflect` sets `undo_deadline` only for a retention that expires, and `purge_state` is `pending` only if the bucket retained something. `HOLD_SECS` is gone.
4. **Capabilities:** `EXEC_CAPS['plan-sweep'].softDelete` is true, so `/admin`, the runs table, run detail, the real-delete confirm and the Slack card use the same `softDelete.ts` text as `sweep`. On cw that reads "No undo: this deployment doesn't require soft delete, so treat these deletes as permanent."; a run with no undo keeps its undo control, disabled, with the reason.
5. **Undo:** the route refuses a run with no undo, or one on a bucket the newest reading shows off. Unlike the spec's first draft, `plan-sweep undo` itself doesn't read the bucket's settings first: S3 doesn't refuse restores the way GCS does, so a historical run on a now-`Suspended` bucket can still be undone by hand, and the job reports per key what it couldn't restore (`unrecoverable`).

## Existing runs

`cw-sweep-real-20261021-174639z` had `purge_state = pending` on a bucket that retained nothing; it was set to `none` on 2026-11-06, with Ryan's go. Migration `sweep-soft-delete` was applied to cw's D1 the same day.

## Test

Unit tests cover the retention table, the refusal and the reflection with fakes. Per Ryan, no scratch versioned bucket: nothing has exercised this against a real versioned CAIOS bucket.
