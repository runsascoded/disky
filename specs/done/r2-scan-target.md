# `index --to r2://…` on a real 6.5M-file home scan: 3 bugs + read-back notes

Written by the `~/.disk` cleanup session (2026-09-09) after exercising the remote-scan-target path for real: `disk-tree index -C -q --to r2://disk-tree/scans /Users/ryan` with `AWS_PROFILE=cf` and `DISK_TREE_R2_ENDPOINT_URL=https://0dcad5654e9744de6616f74b8df4af63.r2.cloudflarestorage.com`. Boot disk was 98% full, x6 unmounted (so the newest prior `~` scans, ids 73/74, had unreachable blobs). Logs: `~/.disk/tmp/scan-home-r2*.log`.

## 1. R2 rejects s3fs multipart uploads — `fixed_upload_size=True` (blocker)

The root blob's multipart commit fails after the full 5.7-minute walk:

```
botocore.exceptions.ClientError: An error occurred (InvalidPart) when calling the CompleteMultipartUpload operation: All non-trailing parts must have the same length.
```

s3fs documents this exact case (`s3fs/core.py`: "Cloudflare R2 storage requires fixed_upload_size=True for multipart uploads"). `blobfs._s3fs` builds `S3FileSystem(client_kwargs={'endpoint_url': ...})` without it. The three chunk blobs (101 MB / 34 MB / 6.7 MB) happened to upload; the 12.8 MB root blob failed, leaving an **in-flight multipart upload** (billable parts) plus three **orphan chunk blobs** with no DB row. I aborted the upload and deleted the orphans by hand.

**Fix**: pass `fixed_upload_size=True` for the `r2` scheme (harmless for real S3 too, so unconditional is fine). Verified: re-running through a wrapper that monkeypatches `blobfs._s3fs` with that flag (`~/.disk/tmp/dt-r2.py`) completed — scan 123, blob `4b9ce0c5…`, 6,491,744 rows, 5 objects / 284 MB in `r2://disk-tree/scans`, nothing written locally.

**Also**: on a failed `hybrid.save`, clean up what was already written (chunk blobs + abort the multipart) so a failed remote scan doesn't leak storage. A `scans gc`/`scans dirs` pass that lists remote objects with no DB row would cover the leftover case.

## 2. `index` without `-C` crashes when the cached scan's blob is on an unmounted volume

`Scan.load_or_create` → `scan.df()` → `resolve_scan_blob` finds `11f2d3e8…` (scan 74, on x6) in no local dir, no remote dir, then falls back to the write dir (`r2://disk-tree/scans` under `--to`) and dies with `FileNotFoundError: disk-tree/scans/11f2d3e8-….parquet` before scanning anything. Same failure without `--to`: `du /Users/ryan` (freshest covering scan = 74) crashes the same way.

**Fix**: when the freshest scan's blob isn't reachable, fall back to the freshest scan whose blob *is* (for `du`/`load_or_create`), or at minimum raise a clear "blob `X` for scan N lives on an unmounted volume — mount it, or pass `-C`" instead of a raw `FileNotFoundError` with the wrong path.

## 3. The post-scan diff step crashes on the same unreachable blob, exit 1 after a successful scan

`build_previous(123)` picks scan 74 (`previous_scan` is by path+time only), `load_scan_table` → `FileNotFoundError`, and `index` exits 1 *after* the blob and DB row are committed — the summary line never prints, and a scheduled run reads as failed. **Fix**: `previous_scan` should skip scans whose blob isn't reachable (or `build_previous` should catch that and `err("… previous blob unreachable, skipping diff")`), and the diff step should never turn a persisted scan into a nonzero exit.

## 4. Smaller notes

- **Read-back needs the bucket in the search path.** `--to` "joins the search path for this run" only; afterwards `du /Users/ryan` can't resolve `4b9ce0c5…` unless `DISK_TREE_SCAN_DIRS=r2://disk-tree/scans` is set. Worth persisting remote write targets (e.g. record the dir/URL on the `scan` row, or a `remote_dirs` config entry) so a blob written to R2 stays resolvable by default. Note `DISK_TREE_SCAN_DIRS` *replaces* discovery, so setting it drops the local default dir unless listed too.
- `du -d1` off the R2 blob takes ~29 s (12.8 MB root + chunk reads); fine for a crisis, but worth checking it isn't fetching every chunk for a depth-1 query.
- `error_count`/`error_paths` are `null` on scan 123 even though gfind logged three `Interrupted system call` errors (WhatsApp `Group Containers` media dirs) and "gfind process exited with return code 1"; scans 64–74 recorded `error_count: 1`. Regression, or a hybrid/remote path that skips the error bookkeeping?
- `disk-tree` isn't on `PATH` outside the repo (direnv venv); the `~/.disk` charter now points at `~/c/disk-tree/.venv/bin/disk-tree`. A `uv tool install` / pipx-style global entry point would make the cleanup workspace self-sufficient.

## Landed (dt)

Bugs 1–3 fixed on `main`:

1. **`fixed_upload_size=True`** on the `r2` s3fs (`blobfs._s3fs`) — unconditional (harmless for real S3). Test `test_s3fs_sets_fixed_upload_size_for_r2` asserts the kwargs. *Deferred* (secondary, and now far less likely to trigger): defensive cleanup of already-written chunk blobs + multipart abort on a failed `hybrid.save`, and a `scans gc` pass that lists remote objects with no DB row.
2. **Unreachable-blob fallback.** New `config.blob_reachable(name)` (+ `_find_blob`) = "resolves to a real file/object", distinct from `resolve_scan_blob`'s write-dir fallback. `freshest_scan_covering` now gathers *all* covering scans and returns the newest whose blob is reachable (so `du`/`filter`/`vocab`/`histogram`/`repos` fall back to an older reachable scan instead of crashing; an explicit `-s ID` is still honored). `Scan.load_or_create` falls back to `Scan.load_reachable(path)`, and rescans fresh if none is reachable, with a clear `err(...)` either way.
3. **Diff step never fails a persisted scan.** `previous_scan` skips scans whose blob isn't reachable; `build_previous` wraps `build_pair` in try/except → `err(...)` + `None`, so a diff failure can't give `index`/`sync` a nonzero exit after the scan and row are committed.

Tests: `tests/test_unreachable_blob_fallback.py` (5), plus the two above. Full `du`/`filter`/`server`/`e2e`/`vocab`/`repos`/`histogram` sweep (233) green.

**Notes 4.1–4.4 deferred** (enhancements, not the reported bugs): persist remote write targets so an R2 blob stays resolvable without `DISK_TREE_SCAN_DIRS`; confirm `du -d1` off R2 isn't fetching every chunk; the `error_count`/`error_paths` null regression on remote/hybrid scans (needs a look at where the hybrid save path records error bookkeeping); a global `disk-tree` entry point for the cleanup workspace.
