# Parallel-finalize memory: two real-data failures worth CP'ing

Findings from the marin-gcs-usage fleet burn-in (2026-08-16), running the depth-partitioned finalize with `-j 14` on a 61GB / 16-core node against **marin-us-central2** (298.5M rows, 280.5M files, 268 parts / 27 unsorted). Both bugs are invisible to unit fixtures and to the *serial* finalize — they need real fan-out and real interleaving to show up.

Two fork commits are ready to cherry-pick (both tagged `[CP→disk-tree upstream]`, full suite green):

- `fb6ab51` — bound run-merge memory (re-chunk many-run parts, share footer, scale priming by `jobs`)
- `de1dda3` — **coalesce row groups in the finalize workers** ← this is the one that matters

## 1. Row-group explosion in the depth workers (the node-killer)

`_finalize_depth_worker` wrote every incoming record batch as its own row group (`writer.write_batch(rb)`).

A parquet writer holds one `ColumnChunkMetaData` **per row group per column** in memory until the footer is written at `close()` — including min/max statistics, i.e. two full path strings for every string column. So the footer is O(row_groups × columns × path_len).

The killer input is the depth's `dir`↔`file` merge: it emits **one slice per alternation**, and at a given depth dirs and files interleave by path constantly. central2 has ~18M dirs among 280M files, so a single depth worker could emit tens of millions of one-to-few-row batches → tens of millions of row groups → multi-GB footer, ×14 workers.

Observed: **54GB anon RSS** across the workers (top worker 10.3GB) within ~18 min, `available` → 0. The node livelocked so hard the OOM killer never ran and SSH stopped answering; it needed an EC2 reboot, which cost us the preserved eu-west4 parts in `/tmp`.

The serial path was always immune because the parent's `emit()` coalesces to exactly `_FLUSH_ROWS` before writing — which is precisely why parallelizing surfaced this. Fix: workers buffer identically. Output bytes are unaffected (the parent re-batches the temps regardless), so byte-identity for any `jobs` still holds.

**Suggested invariant, worth a test in DT too:** row-group count must be a function of *rows*, never of merge slices. Fork test: `test_finalize_worker_coalesces_row_groups` — hand-built dir/file parts whose paths fully interleave (`d000` / `d000!`, `'!' < '/'`), call `_finalize_depth_worker` directly with `_FLUSH_ROWS` monkeypatched small, assert `num_row_groups == ceil(rows/_FLUSH_ROWS)`. Note it must call the worker **in-process**: a `monkeypatch` of `_FLUSH_ROWS` does not reach a spawn worker, so an end-to-end test silently checks the parent's coalescing instead.

## 2. Run-merge reader buffers (real, but not what bit us here)

Each run reader of an unsorted part buffers one decoded row group. Parts are written with `_FLUSH_ROWS`-sized (262K-row) groups, so that's ~50MB per reader — thousands of runs would be tens of GB. `fb6ab51`:

- `_RECHUNK_RUNS = 16`: beyond 16 runs, re-chunk the part once (sequential, O(batch) memory) to `rb_rows`-sized row groups so reader buffers match the merge budget; temp removed in `finally`.
- `_rows_range(metadata=)`: share the parsed footer across a part's run readers (re-parsing a many-row-group footer per reader is its own multiplier).
- `_PRIME_ROWS // jobs`: concurrent depth workers split one global priming budget instead of each claiming 4M rows.

Honest caveat: this was my *first* hypothesis for the OOM and it was **wrong**. Measuring the preserved parts showed all 27 unsorted central2 parts are tiny (largest 1.7M rows / 60MB) and only one exceeds `_MAX_PART_RUNS` (125,684 runs). The changes are still correct and cheap insurance for buckets whose unsorted parts *are* large (eu-west4's 51M-row dir parts), but they fixed nothing on central2. Worth stating so the CP isn't credited with the wrong outcome.

## 3. Two operational gaps you may want to close upstream

- **`aggregate_stream(jobs>1)` requires an import-safe caller.** The worker pool uses spawn, so a driver script that calls it at module top level re-executes itself in all N workers and dies immediately. Cost us one failed resume. Worth a line in the docstring; a `RuntimeError` hint would be friendlier.
- **The CLI defeats parts-dir resume.** `disk-tree import` mints a fresh `NamedTemporaryFile` per invocation, so the `<out>.parts` resume token is never found and an expensive stream pass is repeated even though the parts survive. A `--out`/`--resume` flag (or a deterministic temp name derived from bucket+timestamp) would make the resume reachable from the CLI instead of only from Python drivers.

## Perf context

Stream pass at `-j 14`: **~6–7 min** for 298.5M rows (was 63 min serial). That makes the finalize the whole story now — the old *serial* finalize on this bucket ran **>1h53m without completing** before I killed it, i.e. ≥16× the stream phase it follows. Parallel-finalize numbers to follow once the fixed run lands; a finalize-only resume against the preserved parts is the fast validation loop (skips the stream pass entirely).

---

## Implementation notes (disk-tree, 2026-08-16)

**All five finalize commits cherry-picked**, not just the two flagged: `fb6ab51`/`de1dda3` patch functions upstream didn't have (`_finalize_depth_worker`, `_detect_runs`, `_rows_range`), so the run-merge + depth-parallel finalize (`9124c43`) and its two sort fixes (`025796a` slice-take / 2GiB offsets, `7232893` large_string round-trip) came along. All applied without conflict — upstream and the fork had already converged on the keyspace-partitioned stream (`31d2aa0` ≡ upstream `53a2d2e`). Full suite green; the CP'd `test_finalize_worker_coalesces_row_groups` invariant test came with it.

**New: the parallel finalize is now size-gated.** Re-running the 10M-row byte-identity smoke after the CP showed the *stream* phase kept its speedup while the finalize went 3s → 7s at both `-j 4` and `-j 8` — total regressed 12.4s → 15.6s (j4) and 9.8s → 15.3s (j8). Depth-worker spawn plus the per-depth temp round-trip costs more than depth parallelism saves when each depth is small. `_PARALLEL_FINALIZE_MIN_ROWS` (default 20M, env `DISK_TREE_PARALLEL_FINALIZE_MIN_ROWS`) keeps the finalize serial below that; j4/j8 came back to 10.6s/10.2s, i.e. better than pre-CP. **Output bytes are identical on both paths** (md5 `9f664083…` across `-j 1/4/8`, before and after), so the switch is invisible to consumers — and at your scale it never triggers. If the crossover measures differently on the fleet nodes, the env var moves it without a code change. A `finalize: N depth(s) across J worker(s)` / `finalize: N depth(s), serial (R rows < T threshold)` stage line now says which path ran.

**Both §3 gaps closed:**

- `aggregate_stream(jobs>1)` now catches `BrokenProcessPool` and re-raises a `RuntimeError` naming the cause and the cure (`if __name__ == '__main__':`, importable module, heredoc/`python -c`/stdin can't satisfy it, use `jobs=1` there). The spawn requirement is also in the docstring now.
- `disk-tree import -o/--out-dir DIR` aggregates into `<DIR>/<scheme>-<bucket>.parquet` instead of a per-invocation `NamedTemporaryFile`, so `<out>.parts` is where a rerun looks for it. Test `test_cli_out_dir_makes_resume_reachable` injects a finalize failure through `import_bucket`, asserts the token lands at the deterministic path, then reruns and asserts the resumed output equals the pandas engine's frame. (Chose a directory over a single `--out` so a multi-bucket invocation can't collide.)

Not adopted: nothing. The honest caveat in §2 (the run-merge changes fixed nothing on central2) is preserved in the CP'd commit message.

**Upstream also gained, since you last synced:** `disk-tree index -m/--mean-mtime` on the local walk, `mtime_mean` through `/api/scan`, `GET /api/histogram` + `disk-tree histogram` (byte-weighted mtime distributions per child), and three `@disk-tree/react` widgets — treemap age lens, `<StalenessScatter>` (log-log age×bytes with exact iso-TB·year diagonals), `<AgeHistograms>` (draggable reclaim threshold). See `specs/viz-widgets.md`; the widgets are accessor-based, so wiring them into the mgu site is accessors + fetch only.
