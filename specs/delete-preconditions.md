# Delete preconditions: an object `version` each store can check atomically

**Status:** draft (2026-10-18). Nothing built yet. Discussion with Ryan on 10/18 in the controller session.

## Goal

A reviewed deletion must never remove an object that changed after it was reviewed. Each cloud offers an atomic check for this: a delete conditioned on a per-object value that changes whenever the object does. This spec:
- makes that value a first-class column, `version`, through the listing, the interval store (IS), staging and the executor;
- makes a failed check an error the user sees, not a retry;
- has `/staged` show objects that moved since they were staged, before anything runs.

gcs has this today as `generation` (listing → `path-index -G` → IS → `ifGenerationMatch` in `sweep_exec.py`; a 412 is reported as `skipped_overwritten`). This spec generalizes it so cw (and later HF) adopt it.

## 1. The `version` value

**Definition:** a per-object value that (a) changes whenever the object's content or identity changes, and (b) a delete can be conditioned on, atomically or through a check-then-commit the store makes atomic.

| store | value | dtype | the delete's check | changes on an identical rewrite? |
|---|---|---|---|---|
| GCS | `generation` | int64 | `ifGenerationMatch` (per object; batch: per sub-request) | yes |
| S3, CAIOS | ETag | string | `If-Match` on `DeleteObject`; per object `ETag` in `DeleteObjects` (AWS since [2025-09-16][s3-cond-del]) | single-part, unencrypted / SSE-S3: no (MD5 of the bytes); multipart or KMS/SSE-C: yes ([Object API][s3-object]) |
| R2 | ETag | string | unverified: does R2's `DeleteObject` honor `If-Match`? | as S3 |
| HF | file `oid` (git blob sha, or LFS sha256) | string | repo-level: `create_commit(parent_commit=…)` (§4) | no (content hash) |

Different bytes always give a different value in every store, so a check never lets a changed object through. Some stores also change the value on an identical rewrite, which only causes extra refusals.

**Naming:** base (`cloud`) code says `version`. A deploy may rename it to its store's word (`generation`, `etag`, `oid`) and give it its own dtype (gcs keeps int64). Those are intrinsic diffs between branches, resolved mechanically in merges ([[deploy-code-looks-standalone]]); gcs keeps `generation` throughout.

**Not the version:** HF's repo head commit. It changes whenever anything in the repo changes, so storing it per file would open a new IS version for every file of a repo on every commit, and the IS would stop growing by changes only.

## 2. Through the pipeline

1. **Listing.** The canonical layer-1 column (today `generation`, nullable). The S3 lister records `ETag` from `ListObjectsV2` (already in each response; no extra calls). HF's lister already reads `oid`.
2. **IS.** As `generation` today (`path-index -G`, `scans.json` `"generation": true`): carried on object versions, a change in it opens a new version, `interval-store verify` checks parity, `backfill-generation` fills it from raw listings. Generalize the column and flag names to `version`; gcs keeps `generation`.
3. **Staging.** A staged object records the `version` it was staged at (from the scan the stager was looking at).
4. **Execution.** The executor conditions each delete on the staged `version`. A failed check is reported per object (`skipped_overwritten`, as GCS today), never retried, never "fixed" by re-reading the current value.

## 3. `/staged`: moved since staged

Before any dry run or delete, `/staged` compares each staged object's `version` against the newest scan's, from the IS (no live calls). Objects that moved are flagged, and the user can include or exclude each from the batch (default: excluded). The execution-time check then only has to catch changes since the newest scan.

For ETag stores, also flag an `mtime` mismatch (Ryan, 10/18). It catches A→B→A rewrites where the final write was a single-part upload of the original bytes, so the ETag matches again. S3 has `x-amz-if-match-last-modified-time` only on directory buckets ([API ref][s3-delobj]), so this check is ours, before the delete, with a small race window. Deferred: ETag alone first.

## 4. HF

Deleting files from a repo is a commit, and the only atomic check is on the whole repo: `create_commit(..., parent_commit=H)` fails if the branch head moved past `H`. Executor:
1. read the branch head `H`;
2. check each staged file's `oid` at `H` against the manifest; any mismatch is reported as above and dropped from the batch;
3. commit the deletions with `parent_commit=H`;
4. if the commit is refused because the head moved, re-read the head and repeat from 1.

Step 4 is not the retry §2 forbids. It re-checks every staged file. A commit refused because some *other* file in the repo changed says nothing about the staged files, so without step 4 deletes in busy repos would almost always fail.

**Freeing bytes.** A deletion commit frees nothing: the files stay in history, and private storage (history included) counts against the org's quota. Bytes come back only with `super_squash_history` (rewrites the branch) or `permanently_delete_lfs_files`. LFS storage is deduplicated by content, so purging one `oid` removes it from every path and revision that shares it. A purge must first check that no live file at any kept revision references that `oid`. Design of the HF purge flow: open (§6).

## 5. cw adoption (the second deploy)

- Lister: keep `ETag` in the listing (string column; cw names it `etag`).
- IS: `path-index` carries it (cw's equivalent of `-G`); backfill from cw's kept listings where they exist.
- Executor: `DeleteObjects` with each object's `ETag`. Today the S3 executor's safety is the versioning guard (`sweep.versioning_enabled`, refused unless `Status=Enabled`). 02a is Suspended since 9/16, and enabling versioning held 31 TiB of TTL deletes. Proposed: the ETag check becomes the S3 executor's default safety, and versioning stays an optional undo. Decision for Ryan.
- Probe first: does CAIOS honor the per-object `ETag` in a `DeleteObjects` body? A single `DeleteObject` with `If-Match` returned 412 on a wrong ETag (probed 9/16). Needs a scratch bucket or prefix (writes).

## 6. Open

- R2: `If-Match` on deletes, single and batch. Probe.
- HF purge flow (§4): squash vs per-`oid` purge, and the reference check.
- Whether the IS should also keep `mtime` per version for the §3 flag (it keeps `mtime` already as the version's `wts`/`d`; confirm it's the store's `updated`, not `created`).
- `/staged` UI for moved objects: per-object include/exclude vs per-directory.

[s3-cond-del]: https://aws.amazon.com/about-aws/whats-new/2025/09/amazon-s3-conditional-deletes-s3-general-purpose-buckets
[s3-object]: https://docs.amazonaws.cn/AmazonS3/latest/API/API_Object.html
[s3-delobj]: https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObject.html
