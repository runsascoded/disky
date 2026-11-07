# Delete preconditions: an object `version` each store can check atomically

**Status:** shared object-version pipeline, CW conditional deletion, reviewed-staging lifecycle and preservation analytics implemented locally, 2026-10-21; not deployed. Other providers' executor changes remain open. Discussion with Ryan began 10/18 in the controller session.

**CW audit and decision (2026-10-20; versioning-gate removal deployed 2026-10-21):** Versioning prevents TTL deletions from reclaiming bytes and can exhaust quota. Do not enable it as a prerequisite for CW deletion. Historical CW listings discarded ETags; those identities remain unknown. The new local implementation retains them and uses single-object conditional deletes; details and the rollout are in §5.

## Goal

A reviewed deletion must never remove an object that changed after it was reviewed. Each cloud offers an atomic check for this: a delete conditioned on a per-object value that changes whenever the object does. This spec:
- makes that value a first-class column, `version`, through the listing, the interval store (IS), staging and the executor;
- makes a failed check an error the user sees, not a retry;
- records why objects were preserved and completes only the specific reviewed staging state.

gcs has this today as `generation` (listing → `path-index -G` → IS → `ifGenerationMatch` in `sweep_exec.py`; a 412 is reported as `skipped_overwritten`). This spec generalizes it so cw (and later HF) adopt it.

## 1. The `version` value

**Definition:** a provider-specific validator retained with the reviewed object. A GCS generation identifies one exact write; an ETag validates content and can survive an identical rewrite. The common optional string field preserves these tokens without making their identity guarantees equivalent.

| store | value | source dtype | the delete's check | changes on an identical rewrite? |
|---|---|---|---|---|
| GCS | `generation` | int64 | `ifGenerationMatch` (per object; batch: per sub-request) | yes |
| S3 | ETag | string | `If-Match` on `DeleteObject`; per object `ETag` in `DeleteObjects` (AWS since [2025-09-16][s3-cond-del]) | not guaranteed; content, part layout and encryption affect the validator ([Object API][s3-object]) |
| CAIOS | ETag | string | conditional-delete SDK support verified; provider enforcement still needs a controlled canary | not a unique rewrite identity |
| R2 | ETag | string | unverified: does R2's `DeleteObject` honor `If-Match`? | as S3 |
| HF | file `oid` (git blob sha, or LFS sha256) | string | repo-level: `create_commit(parent_commit=…)` (§4) | no (content hash) |

An ETag is an opaque content validator, not a unique rewrite identity: identical rewrites and metadata changes can retain it. CW also checks reviewed size and mtime at LIST; an identical rewrite between LIST and DELETE remains a limitation of the provider's ETag precondition.

**Local implementation (2026-10-21):** canonical listings carry nullable string `version`, supplied by S3 LIST/boundary HEAD/shallow discovery ETags or exact decimal GCS generations. Legacy numeric `generation` remains readable. `disk_tree.object_version` provides the shared projection and file-row attachment used by imports; all three import engines and CW's index union retain it. The optional CRC32C tuple slot remains separate. Existing format-version metadata is unrelated.

The IS records the optional string field as `"object_version":{"name":"version","type":"string"}`. It is a change column: a different token opens a new interval even with unchanged size/mtime. Build, append, carries, base compaction, exports, identity lookups and parity checks preserve it. Old `"generation":true` stores retain their legacy schema/behavior. The GCS hold adapter converts canonical decimal strings back to native integer preconditions at its provider boundary.

`dt-cloud interval-store migrate-object-version -g OLD -O NEW -o STORE` folds an existing store into a new generation carrying the generic field, without raw listings or historical per-scan indexes. Existing native GCS generations become exact strings; unknown historical identities stay NULL. It leaves the source and CURRENT untouched. A later first-observed ETag opens its own interval rather than filling an older unknown interval retroactively. `-R` names borrowed source ranges where needed.

**Not the version:** HF's repo head commit. It changes whenever anything in the repo changes, so storing it per file would open a new IS version for every file of a repo on every commit, and the IS would stop growing by changes only.

## 2. Through the pipeline

1. **Listing.** The canonical layer-1 column (today `generation`, nullable). The S3 lister records `ETag` from `ListObjectsV2` (already in each response; no extra calls). HF's lister already reads `oid`.
2. **IS.** As `generation` today (`path-index -G`, `scans.json` `"generation": true`): carried on object versions, a change in it opens a new version, `interval-store verify` checks parity, `backfill-generation` fills it from raw listings. Generalize the column and flag names to `version`; gcs keeps `generation`.
3. **Staging.** A staged object records the `version` it was staged at (from the scan the stager was looking at).
4. **Execution.** The executor conditions each delete on the staged `version`. A failed check is reported per object (`skipped_overwritten`, as GCS today), never retried, never "fixed" by re-reading the current value.

## 3. Reviewed staging and preserved objects

A staging gesture approves a specific snapshot. The manifest excludes new and changed objects since that review; execution also preserves replacements and new keys encountered later. A verified successful real run consumes the original staging even if newer objects remain under its prefixes. The run page records these outcomes. Including newer data requires the ordinary review and new staging flow, without an in-place redo that expands the old approval.

For ETag stores, also flag an `mtime` mismatch (Ryan, 10/18). It catches A→B→A rewrites where the final write was a single-part upload of the original bytes, so the ETag matches again. S3 has `x-amz-if-match-last-modified-time` only on directory buckets ([API ref][s3-delobj]), so this check is ours, before the delete, with a small race window. Deferred: ETag alone first.

## 4. HF

Deleting files from a repo is a commit, and the only atomic check is on the whole repo: `create_commit(..., parent_commit=H)` fails if the branch head moved past `H`. Executor:
1. read the branch head `H`;
2. check each staged file's `oid` at `H` against the manifest; any mismatch is reported as above and dropped from the batch;
3. commit the deletions with `parent_commit=H`;
4. if the commit is refused because the head moved, re-read the head and repeat from 1.

Step 4 is not the retry §2 forbids. It re-checks every staged file. A commit refused because some *other* file in the repo changed says nothing about the staged files, so without step 4 deletes in busy repos would almost always fail.

**Freeing bytes.** A deletion commit frees nothing: the files stay in history, and private storage (history included) counts against the org's quota. Bytes come back only with `super_squash_history` (rewrites the branch) or `permanently_delete_lfs_files`. LFS storage is deduplicated by content, so purging one `oid` removes it from every path and revision that shares it. A purge must first check that no live file at any kept revision references that `oid`. Design of the HF purge flow: open (§6).

## 5. cw adoption

### Prior path (audited 2026-10-20)

- `src/disk_tree/find/bulk_s3.py`: both paginated listing paths and the boundary `head_object` path discard response ETags. `BlobRow`, `entries_to_frame`, the empty-shard schema, and adaptive/non-adaptive worker buffers in `bulk.py` / `bulk_adaptive.py` have no ETag field. Existing shards cannot backfill historical ETags.
- CW imports listings into per-bucket layer-2 files, then runs `index-write`; it does not use GCS's direct listing → `path-index -G` path. Import/aggregation, `cloud/src/dt_cloud/index.py`'s union projection, and IS schemas must all preserve a nullable string `etag` on file rows. Numeric GCS `generation` support does not do this automatically.
- `sweep.py` builds manifests from those layer-2 files using name, size and mtime. Execution re-lists, compares size/mtime, then issues unconditional key-only `DeleteObjects`; it has a check/delete race. Staging's older-scan check compares mtime only. ETags must reach both manifest construction and execution, not just the IS.
- Bucket versioning is no longer required or queried by the executor. Recovery is not guaranteed. This is independent of the ETag migration and must not wait for it.

### Implementation and deployment sequence

1. Generalize the shared optional object-version field, then populate it from ETags in both LIST and HEAD through every listing writer, including empty shards and adaptive workers; preserve it through each import engine and `index-write`. This reuses the GCS carry-through transform; CW's additional missing stages are implementation gaps, not an inherent store-specific architecture. Do not coerce opaque ETags into numeric GCS generations.
2. Carry ETags through IS build, append, merge, compaction, served identity lookups, and parity verification. Retain the size/mtime checks: an ETag is not a unique rewrite identity.
3. Take a fresh ETag-capable scan. Existing CW shards contain no ETags, so there is no equivalent historical raw-listing backfill available. Historical identities remain NULL; do not label them with today's HEAD/LIST ETags. Build a new generation preserving those unknowns, verify it, then coordinate the live pointer cutover.
4. Implemented locally: CW manifests retain `version`; older-scan staging compares size, mtime and token. Execution refuses the whole manifest before any deletion if an identity is NULL, empty or wildcard, including old manifests without the column. Take a fresh scan and re-stage such items. A matching LIST is followed by `DeleteObject(IfMatch=<reviewed ETag>)`, with bounded workers. A 412 is `skipped_overwritten`; other provider delete failures are `delete_failed` and make the CLI fail. Decision logs exclude both from undo/purge. The reviewed token is never replaced or retried with a fresh token.
5. The implementation selects the single-object API accepted by the SDK. Actual CAIOS enforcement still requires validation before deployment; no source-object write/delete probe was performed during this implementation. A future batch optimization must verify per-object preconditions before use; it must never fall back to unconditional deletion.

Steps 1–2 are implemented locally and covered by listing/import, IS build/append/merge/compaction, migration/export and conditional-delete regressions. Step 3 and production rollout are pending. The reviewed-staging lifecycle and run analytics in §3 are implemented locally; shared adoption is described in `staged-review-loading.md`.

## 6. Open

- CAIOS and R2: verify actual `If-Match` enforcement with an owned canary before deploying conditional execution. The installed S3 SDK accepting the field and fake-client tests do not prove provider enforcement; no canary object has been created or deleted in this work. Never probe a wrong validator against a real staged object that must survive.
- HF purge flow (§4): squash vs per-`oid` purge, and the reference check.
- Whether the IS should also keep `mtime` per version for the §3 flag (it keeps `mtime` already as the version's `wts`/`d`; confirm it's the store's `updated`, not `created`).

[s3-cond-del]: https://aws.amazon.com/about-aws/whats-new/2025/09/amazon-s3-conditional-deletes-s3-general-purpose-buckets
[s3-object]: https://docs.amazonaws.cn/AmazonS3/latest/API/API_Object.html
[s3-delobj]: https://docs.aws.amazon.com/AmazonS3/latest/API/API_DeleteObject.html
