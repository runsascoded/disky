# Stream engine: depth-partitioned, sort-free finalize

## Motivation (fleet burn-in, 2026-08-16)

`marin-eu-west4` (194M objects → 369M layer-2 rows) has now failed its final `COPY … ORDER BY depth, path, kind` **twice** on mgu (m6g.4xlarge, 247G disk): the global external sort spills >100.6 GiB and hits DuckDB's `max_temp_directory_size` (auto-cap = 90% of free-disk-at-launch; second failure consumed the entire honestly-set cap). The stream pass itself is fine both times (63 min, ~74K files/s). Generic external sort spills ~2× data — at 588M-object fleet scale the sort, not the aggregation, is the resource cliff. `-x/--max-temp-size` (2e3b307) only tunes the cap; the spill itself is intrinsic to sorting. This spec removes the sort.

## Insight: within a depth, the stream is already sorted

The du-stack emits:
- **file rows** in global path order (they pass through in listing order) — so restricted to any fixed depth *d*, file rows appear in path order;
- **dir rows** in postorder — but at a fixed depth *d*, subtree key-intervals are disjoint and ordered, and a dir pops exactly when the stream leaves its interval — so depth-*d* dir rows also emit in path order.

The output contract is `ORDER BY depth, path, kind` (`'dir' < 'file'` tiebreak). So if the pre-output is **partitioned by (depth, kind)**, the final file is just: for each depth ascending, a **2-way ordered merge** of (dirs_d, files_d) — no sort anywhere.

## Design

1. **Writer**: replace the single pre-output `_Writer` with a per-(depth, kind) family — parquet writers opened lazily on first row for that key, ~2×max_depth open writers/FDs (max_depth ≲ 60 in practice; still bounded by `ulimit` trivially). Rows already carry `depth`; routing is one dict lookup. Same `_FLUSH_ROWS` row-group batching per writer (memory = open_writers × buffer, still small).
2. **Finalize**: for each depth ascending, stream both parts as record-batch streams and do a **vectorized boundary merge** into the single output `ParquetWriter`:
   - while both sides have batches: if `dirs.head_batch.last_key ≤ files.head_batch.first_key` → write the whole dirs batch (and vice versa); else split the earlier batch at `searchsorted` of the other's boundary key and write the prefix.
   - equal `path` across kinds (a key that is both file and dir name): dir first — bias the split (`side='right'` on the dirs side).
   - O(#batches) Python, O(rows) C, O(1) memory, zero spill.
3. **Drop the DuckDB dependency from the stream engine's finalize** entirely — `memory_limit` / `temp_dir` / `max_temp_size` become no-ops for `-e stream` (keep accepted-but-unused for CLI compat, or route them away).
4. **Failure resilience** (2e3b307 semantics carried over): on finalize failure keep the per-part directory; a retry re-merges without re-streaming. Parts also make the finalize trivially parallelizable later (per-depth workers) and are independently queryable (a depth-1 stats query reads one small part).

## Expected effect

- eu-west4: sort spill >100.6 GiB → **zero spill**; finalize becomes IO-bound (~read 12G + write 10G, minutes).
- Removes the last super-linear resource term in the stream engine: everything is now O(depth) state + O(1)-memory streaming passes.

## Testing

- Existing 3-engine identity + column-order tests already lock output bytes; they run through the new finalize unchanged.
- Add: a fixture where one path is both a file and dir name **at the same depth** (tiebreak split boundary), and a deep-chain fixture (many depths, one row each — exercises writer-family churn).
- Perf check on the CW 92.7M listing (mgu): expect final phase ≪ the current 7.4-min sort.

## Status

- [x] Implemented in DT upstream (2026-08-16); marin-gcs-usage fork will CP back and rerun eu-west4/fleet as acceptance.
- Interim workaround in the fork: retry eu-west4 with `-x` + ~150G freed disk (archiving July deduped listings to `gs://oa-gcs-usage-dvx/listing-archive/` first).

### Implementation notes (upstream)

- One correctness wrinkle the "dirs emit in path order" insight glosses over: within a depth,
  dir rows pop in **subtree-interval** order = sorted by `path + '/'`, which differs from plain
  `path` order exactly when a dir name is a proper prefix of a same-depth sibling whose next
  char is < `'/'` — e.g. sibling dirs `store` and `store-backup` (`'-' < '/'`): `store-backup/`'s
  subtree sorts *before* `store/`, so its dir row pops first, inverted. This is common
  (`.`/`-` suffix patterns), not exotic. Rather than trust the analysis, `_PartWriters`
  **measures** each part's sortedness (one string compare per row); the finalize sorts only the
  (depth, kind) parts that actually measured unsorted (`pyarrow.Table.sort_by`, bounded by that
  one slice) and streams everything else. File parts are always sorted (subsequence of the
  globally-sorted key stream).
- Parts land in `{out_parquet}.parts/` with a `manifest.json` (stats + per-part row counts +
  sortedness); the manifest doubles as the resume token — finalize-only failure preserves the
  dir, and a rerun with the same output path skips the stream pass entirely (test-locked).
  Mid-stream failure removes the partial parts (no manifest → unusable).
- `-e stream` now imports no DuckDB anywhere; `-M`/`-T`/`-x` are accepted-but-unused for it
  (help strings updated). 2M-row smoke: byte-identical to the duckdb engine with
  `--pivot-sum storage_class_id --mean-mtime`; finalize phase ~1s (vs 7.4 min DuckDB sort for
  185M rows on mgu — extrapolated ~1.5 min IO-bound).
- Bycatch, found by the identity smoke: DuckDB's `HUGEINT::DOUBLE` cast is not correctly
  rounded (1-ULP drift past 2^64) vs Python's `int → float`, so duckdb-engine `mtime_mean`
  silently diverged from pandas/stream at real scale. Fixed by routing the conversion through
  `::VARCHAR::DOUBLE` (DuckDB's string→double parse is correctly rounded); TFFP-locked with a
  65-bit fixture.
