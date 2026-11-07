# `-e stream -j N`: keyspace-partitioned parallel streaming

## Motivation

The stream engine is a single-threaded Python du-loop at ~40–75k rows/s — mgu's py-spy profile of the eu-west4 rerun (194M objects) shows the remaining cost is diffuse per-row Python (parts-writer ~20%, source decode ~13%, stack-walk ~15%, heap ~16%), no single lever left. The next ~10× is intra-bucket parallelism: partition the sorted keyspace into N contiguous ranges, stream each range in its own worker process, and monoid-merge the small set of directories whose subtrees span a partition boundary. ~Linear in cores, pure Python, composes with a future compiled core.

## Design

### Partitioning

- Pass-1 (`_scan_names`) already extracts each batch's first key; it now also records **checkpoints** `(raw_ordinal, first_key, n_bucket_rows)` per read batch (~1 per 64Ki rows — a few thousand entries at fleet scale). Checkpoints serve double duty:
  - **Boundary selection** (`_choose_boundaries`): sort all checkpoints by key, cumulative-sum weights, pick `jobs−1` boundary keys at even row quantiles (±one batch of balance). Boundaries are real keys; ranges are `[lo, hi)`.
  - **Intra-run seek** (`_clip_sources`): for a worker's range, each intersecting run's `[start, stop)` raw-ordinal window is tightened to the last checkpoint with key < `lo` / first checkpoint with key ≥ `hi` — so a worker whose range starts mid-run re-reads at most one batch per run, not the run prefix. Exact `[lo, hi)` filtering happens vectorized inside `_shard_rows` (new `lo`/`hi` params: whole-batch skip below `lo`, early `break` at `hi`, mask-filter at the edges).
- Pass-1 itself parallelizes per shard through the same process pool when `jobs > 1`.

### Worker semantics (`_run_partition`)

Each worker streams its range with the ordinary du-stack, writing per-`(depth, kind)` parts suffixed `.w{idx:03d}`. The subtree-interval property means a dir's keys are contiguous in the global order, so a dir needs cross-worker assembly **iff its subtree interval contains a partition boundary** — at most `depth` dirs per boundary side. Detection at pop time:

- **Left-spanning** (conservative): `lo is not None and dir.pfx < lo` — the subtree region starts before `lo`, so keys in an earlier partition *may* exist. False positives (no such keys) are harmless: the reduce merges a single segment.
- **Right-spanning**: any dir still on the stack at range EOF when `hi is not None`. (An interior pop is always right-complete — contiguity.)
- Invariant: a spanning dir's ancestors are spanning too (its `pfx` is prefixed by theirs; the stack nests). So all parent-child accounting among spanning dirs happens in the reduce, sequentially.

A spanning dir's pop **exports a partial accumulator segment** `(path, size, mtime, n_desc, n_files, n_children, pivot, mt_wsum)` instead of emitting a row, rolls nothing into its parent, and **retracts the push-time `n_children` increment** from the parent's segment — the reduce adds exactly 1 per spanning child, so children opened by multiple workers aren't double-counted. The root accumulator is always exported as a segment (path `''`).

### Reduce (parent process)

- Merge segments per path: Σ size / n_files / pivot / mt_wsum, max mtime, `n_desc = Σ − (k−1)` (one self-count survives), Σ n_children (spanning children retracted to 0 worker-side).
- Deepest-first, roll each spanning dir into its (also-spanning) parent exactly like `pop_emit` (`n_children += 1`), and build its output row.
- Rows land in per-depth **boundary parts** (`{depth:04d}-dir.b.parquet`, path-sorted); the root row (depth 0) always comes from here.

### Finalize

`_finalize_parts` now accepts **multiple parts per `(depth, kind)`**: per depth, dir parts (workers + boundary) are chained through the pairwise vectorized batch merge (`_merge_batches`, the generator form of the old `_merge_two_sorted`; degenerates to pass-through when ranges don't interleave), then merged against the files stream with the dir-before-file tiebreak. Worker file parts are strictly range-disjoint; worker *dir* parts can overlap adversarially at prefix-sibling boundaries (`store` vs `store-backup` across a split) — the merge handles it, and per-part measured sortedness (unchanged) still repairs pop-order inversions.

**Byte-determinism**: the finalize writer now slices its buffer at exactly `_FLUSH_ROWS` per row group, so the output parquet is byte-identical for any `-j` (previously row-group boundaries floated with upstream batch sizes).

### Failure / resume

Unchanged: worker or reduce failure → parts dir removed (mid-stream, unusable); finalize failure → parts + manifest preserved, rerun resumes at the merge. The process pool uses the spawn context; pool shutdown is in a `finally`.

## Interface

- `aggregate_stream(..., jobs: int = 1)` — `0` = all cores; `1` (default) = fully in-process, no multiprocessing (byte-path identical to before, modulo the root row moving to a boundary part and the deterministic finalize slicing).
- CLI: `disk-tree import -j/--jobs N` (stream engine only), plumbed through `import_bucket(jobs=...)`.

## Acceptance

- [x] Output parquet **byte-identical** (md5) across `-j 1` / `-j N` on fixtures including forced boundaries mid-subtree, at prefix-sibling inversions, on exact existing keys, and producing empty partitions; frame-identical to the pandas engine; pivot sums + `mtime_mean` covered under partitioning (6 new tests).
- [x] Full test suite green (256).
- [x] 10M-row synthetic smoke (`-p storage_class_id -m`): md5-identical at `-j 1/4/8/16`; stream pass 20s → ~5s at `-j 8` (incl. ~1–2s pool spawn); `-j 16` regresses on this laptop (P-core oversubscription) — pick `jobs ≈ physical cores`.
- [ ] mgu CPs and reruns a fleet bucket with `-j` as the at-scale acceptance (expected near-linear stream-pass speedup on 16 cores; at 194M rows the serial stream pass is ~63 min and the non-scaling phases — pass-1, finalize — are minutes).

## Implementation notes

- **Byte-determinism required `combine_chunks()`**: identical rows in identical row groups still produced different bytes across `-j` — parquet page boundaries are checked at the writer's chunk edges, so multi-chunk `write_table` calls leak upstream batch sizes into the encoding. Each row group is now combined to one contiguous chunk before writing (plus exact `_FLUSH_ROWS` slicing), making the layout a pure function of row content.
- **Checkpoint granularity is the pass-1 read-batch size** (`_SCAN_BATCH_ROWS`, default 64Ki, env-overridable via `DISK_TREE_SCAN_BATCH_ROWS` — spawned pass-1 workers see the env, not monkeypatched module attrs). `iter_batches` coalesces small row groups up to the batch size, so row-group-size tricks alone don't produce checkpoints.
- **Spawn context caveat**: workers use the `spawn` start method; calling `aggregate_stream(jobs>1)` from a non-importable `__main__` (heredoc/stdin scripts) breaks the pool. The CLI entry point and normal modules are fine.
- The left-spanning test (`pfx < lo`) is deliberately conservative — false positives export single-segment partials that reduce to the identical row; no correctness dependence on proving keys exist in the earlier partition.

## Follow-up (not this change)

Compiled/vectorized core (Rust/PyO3, or chunked-Arrow with depth-delta pops) for the per-core ~10× — see memory `stream-engine-10x-levers`.
