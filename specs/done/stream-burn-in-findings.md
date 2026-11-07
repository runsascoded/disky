# Stream-engine burn-in findings (marin-gcs-usage, 2026-08-15/16)

Real-data acceptance of the stream engine + aggregation extensions, run from the marin-gcs-usage fork (commits tagged `[CP→disk-tree upstream]` on its `main`). Two datasets: CW S3 92.77M objects (185.2M layer-2 rows), and the 6-bucket GCS fleet 593.8M objects.

## Bugs found (all fixed + test-locked in the fork; CP candidates)

1. **Row-group skip via raw run ordinals** — run-start ordinals were bucket-filtered indices; every run source re-read its shard from row 0 (~R×/2 read amplification on bin-packed shards). Now raw row indices mapped onto row-group boundaries; non-intersecting RGs skipped. Test: interleaved two-bucket shard (raw ≠ filtered ordinals), `row_group_size=2`.
2. **EMFILE + O(runs) memory in the merge** — `heapq.merge` primes every source at startup: fleet buckets have 5–10K runs → >1024 open `ParquetFile`s (all 6 parallel imports died with `Too many open files` in minutes) and a row-group read buffer per run (~20GB RSS). Fix: **lazy-open k-way merge** — sources sorted by first key (recorded in pass-1), opened only when the merge horizon reaches their first key, dropped at exhaustion. Open sources bound by range-overlap depth: observed 393/1,633 runs (us-east1), 2,085/9,783 (us-central2). `stats['max_open_sources']` + stage line report the high-water.
3. **Disjoint-run concatenation mostly doesn't engage** for real bulk-list output: consecutive in-order ranges within a shard coalesce into one detected run with key *gaps*, and other shards' ranges land in the gaps — so runs interleave and the heap path is correct. Chain path retained for single-run inputs.
4. (Earlier, already CP'd/known: piecewise-run splitting, `head_object(Key='')`, canonical column order, epoch floor-vs-round.)

## DuckDB final-sort footguns (both hit at fleet scale)

- **Shared spill dir**: all imports ran with the same cwd → same relative `.tmp/`; concurrent sorts collided (`Could not read enough bytes from duckdb_temp_storage_*.tmp`). Per-import `-T` dirs required; consider defaulting `temp_directory` to a per-invocation tempdir in `aggregate_stream`.
- **`max_temp_directory_size` auto-caps at free-disk-at-launch**: a sort died at "failed to offload data block (41.4 GiB/41.4 GiB used)" despite ample disk later. Consider exposing/setting it explicitly alongside `memory_limit`.
- **Resumability gap**: sort failure discards the (expensive) pre-output parquet — a 63-min stream re-runs for a sort-only failure. Worth keeping the pre-output on failure + a resume path (or making the final sort restartable).

## Perf (m6g.4xlarge, arm64)

- CW 92.77M obj → 185.2M rows with `--mean-mtime --pivot-sum storage_class_id`: **32:42 wall** (pass-1 43s, merge 24.5 min @ ~63K files/s, final sort 7.4 min), 11.9GB peak RSS.
- us-central2 280.5M obj: stream phase 63 min (~74K files/s) *while sharing the node with 5 other imports*.
- py-spy: remaining merge-loop time is diffuse (writer ~20%, source decode ~13%, `_Acc.__init__` ~10%, stack-walk ~15%, heap ~16%); landed micro-levers (single-pivot fast path, hoisted `path+'/'`) gave ~1.34× (44.4K→59.5K files/s single-run).

## a2a vs marin production (the flip gate)

Stream layer-2 vs deployed `snapshots/2026-08-15/tree.json`, per bucket, root + all depth-1 children: `b`/`o` **exact**, `cb` (pivot sums) **exact**, `d` (from `mtime_mean`) within ±1 day. 728/728 checks on the 4 buckets compared so far (2 big buckets re-running after the sort collisions). Two benign semantic deltas, both explained:
- GCS folder-marker objects (`prefix/`): production counts them under the prefix; DT canonicalizes to a sibling file named `prefix` (±1 object/prefix; roots agree exactly).
- Production folds sub-threshold `"(files)"` into `(other ×N)` at slice time — site-layer behavior, not aggregation.

## Upstream status (disk-tree main, 2026-08-16)

- All 7 fork commits cherry-picked (`4f5201e..40d0e91`: empty-start fix, canonical column order, epoch floor, row-group skip, disjoint-run concat + micro-levers, stage-line fixup, lazy-open merge).
- Empty-start fix got its botocore-Stubber tests (`tests/test_bulk_s3.py`): origin start ⇒ no HEAD / no `StartAfter`, on both `stream_pages` and `stream_prefix`.
- DuckDB spill footguns addressed: both engines now default `temp_directory` to a fresh per-invocation tempdir (removed on success), and `import -x/--max-temp-size` exposes `max_temp_directory_size` (plumbed through both engines).
- Resumability: partial — a final-sort-only failure now preserves the streamed pre-output parquet and prints its path (test-locked), so the stream pass isn't re-run; an automated resume/re-sort path is still open.
