# `import -e stream`: O(depth) streaming rollup over sorted listings

Status: proposed 2026-08-15. Motivation: the DuckDB cascade needed 3 attempts (63GB RSS pandas tail → 95GiB spill exhaustion → RSS overshoot OOM-kill) to aggregate a 92.7M-object CAIOS listing on a 64GB node — for a job whose intrinsic shape is "read 1.7GiB compressed, roll up a tree, write ~3GB". The GCS fleet regime is 588M rows (6×); the cascade design won't survive it on any reasonable node.

## Why the cascade is 2 OoMs above intrinsic

`aggregate_listing_to_parquet`'s level loop re-materializes every surviving row (full ~100B path strings) once per tree level (~550M row-writes at 92.7M inputs), runs hash group-bys keyed on long VARCHARs at each level (DuckDB's most memory-hungry operator, and the one that overshoots `memory_limit` — observed 63GB RSS under a 50GB cap), then external-sorts the ~100M-row union for the `(depth, path)` output order. Total: ~60GB RAM + ~100GB spill.

## The streaming formulation

Object-store listings are **lexicographically sorted by key**, and `bulk-list` shards are each internally sorted with recorded boundaries (`_SUCCESS.json`) — a k-way heap merge over shard readers yields globally sorted rows for free. Sorted-by-path is DFS order: every dir's subtree (`p/…`) is a contiguous key interval in byte order. So the aggregation is the classic `du` algorithm:

- Maintain a stack of open ancestor dirs, each with running `(size, mtime_max, n_desc, n_files, n_children)` accumulators (plus `--pivot-sum` / `--mean-mtime` partials when those land — all monoid sums, so they streams identically).
- For each file row: pop-and-emit stack entries that are not ancestors of the new path (their subtrees are complete — a dir can never reappear later in sorted order); push newly-entered dirs; fold the file into every open ancestor; write the file row through to the output.
- EOF: pop-and-emit the remaining stack (root last).

Working state: O(max depth) accumulators ≈ KBs. Peak memory = output buffering only.

## Output-order wrinkle (the one design decision)

The layer-2 contract sorts rows by `(depth, path)` for depth-predicate row-group pruning. Streaming emits files in path order and dirs in postorder — neither matches. Options:

1. **Buffer dirs, stream files, two-section output**: file rows stream out in path order to one parquet section/file; dir rows (~10M at CW scale, tiny relative to files) buffer in RAM (or spill trivially), sort by `(depth, path)` at the end, write first. If the contract can be "dirs sorted by (depth,path), then files sorted by path" with per-row-group depth stats, pruning still works (depth min/max per row group stays tight because path-order clusters depth reasonably). Cheapest; slightly weakens the contract.
2. **External-sort just the files by `(depth, path)`** as a final pass (DuckDB `COPY (SELECT … ORDER BY)` over the streamed pre-output) — keeps the contract byte-exact, costs one bounded sort of already-thin rows. Still OoM better than today (no cascade, no hash aggs).
3. **Per-depth partitioned write** (files fan out to one writer per depth, concat at end — `pqtk`-style zero-copy row-group concat applies). Exact contract, no sort, more file handles.

Recommend 2 for a2a-parity with existing consumers (server, tests unchanged), with 1 as the follow-up once consumers read via DuckDB globs anyway.

## Scope

- New engine `-e stream` in `disk-tree import`, same CLI surface, same stats dict, same parquet schema. Cross-engine identity vs duckdb/pandas on fixtures + a mid-size real listing joins the existing CI identity test.
- Input: listing parquet glob; per-shard sorted-ness verified cheaply (assert monotone within shard; k-way merge across shards). Fall back with a clear error (suggest `-e duckdb`) if shards aren't sorted (e.g. hand-built listings).
- Non-goal: replacing duckdb engine (still the right tool for ad-hoc SQL over listings, non-sorted inputs, and the access plane).

## Acceptance

- CW listing (92.7M rows): completes on a 16GB laptop, peak RSS < 5GB, no spill dir, ≤ 15 min.
- Fleet listing (588M rows): completes on `mgu` (64GB) without touching swap.
- Byte-identical layer-2 vs the duckdb engine under option 2's ordering (a2a harness), or documented contract delta under option 1.

## Status

- [x] `-e stream` engine — `find/aggregate_stream.py` (option 2: final bounded
      `ORDER BY depth, path, kind` sort; the `kind` tiebreaker matches the other
      engines' dirs-before-files stable order when a key is both file and dir name)
- [x] Implementation wrinkle found + locked in tests: `//` canonicalization does
      NOT preserve lexicographic order (raw `a//z` < `a/b` but canonical `a/z` >
      `a/b`), so a single-pass stack over raw order pops dirs prematurely. Fix: a
      names-only pre-scan flags shards containing dirty keys; those rows are
      collected, canonicalized, sorted, and joined into the k-way merge as one
      more sorted source. Clean keys (raw == canonical) preserve order trivially.
- [x] Cross-engine identity: 3-engine test on `_IDENTITY_LISTING` + multi-shard,
      interleaved-shard, unsorted-input, earlier-source-wins coverage
      (`tests/test_aggregate_stream.py`)
- [x] Local scale smoke: 2M rows / 4 shards → 3.8s, ~525k rows/s, byte-identical
      to duckdb engine (projects: 92.7M ≈ 3 min, 588M ≈ 20 min pure aggregation)
- [ ] CW listing (92.7M) real-data acceptance run — mgu owns
- [ ] Fleet listing (588M) acceptance run on `mgu` — mgu owns
