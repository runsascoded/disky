# mgu → DT unification at fleet scale: layer-2 as the index tiers, sliced cascade, hour-grained access, as-of

Written 2026-09-06 from the mgu session (Marin GCS Usage, `~/c/oa/marin-gcs-usage`; spec workflow — this session asks, the DT session implements, ideally landing each item as its own commit so mgu can CP or adopt). Context and the mgu-side design: `marin-gcs-usage/specs/view-serving.md` (the "from scratch" serving design) and `specs/webdata-memory.md` (why the daily job OOMs at 124 GB).

## Why

mgu diverged from DT's aggregation in 2026-08: at the time layer-2 lacked class pivots and mean mtime, so mgu kept its own DuckDB SQL (`viz.py`: `dir_stats` → `dir_attr` → `ptu`, a per-`(path, team, usr)` descendant-inclusive rollup) and, to keep that rollup small enough, indexed **dirs only** — files exist in mgu's index as a count. Both of the original blockers have since shipped in DT (`--pivot-sum`, `--mean-mtime`), DT's cascade is level-wise (which is what mgu needs for memory), and DT's layer-2 has file rows. The divergence now costs: a 220M-row dirs-only index that can't address files, an aggregation that fills a 128 GB node with hash-agg state, and two implementations of the same rollup. Direction (Ryan, 2026-09-06): **adapt upstream to mgu's scale and adapt mgu to consume upstream — best of both, one implementation.** mgu keeps only its overlays (attribution identities, ledger/marks, storage-class pricing, the CF site).

Fleet numbers to design against (2026-09-05): 613M objects, 179M dirs (96 % of them single-object; `datakit/store*` alone is 164M dirs), 3 PiB, 6 buckets, listing ≈ 10.5 GB of parquet. Target: work at 10× that.

## Asks, in the order mgu needs them

### A. Fleet-scale cascade (needed first; gates everything below)

`find/aggregate_duckdb.py` builds the level tables in an **in-memory** DuckDB (`duckdb.connect()` + `memory_limit` + `temp_directory`). Intermediates spill, but base tables in an in-memory database don't page out, so at 613M input rows the level tables themselves must fit RAM.

1. **File-backed database option**: `connect(<path on the spill disk>)` so level tables live in the buffer pool and page to disk under `memory_limit`. Expose as `--db <path>` (default: temp file under `temp_dir`). Expected: peak RSS ≈ `memory_limit`, wall time up somewhat on the spill phases (local NVMe on the Batch node makes that cheap).
2. **Prefix-partitioned cascade** (`--partition-depth k`, optional): a dir and all its descendants share their depth-k prefix, so run the cascade per distinct depth-k prefix (memory ∝ that partition's rows), then cascade the k-level stubs to the root. No shuffle if the input listing shards are already prefix-contiguous (mgu's `bulk-list` streams are). Partitions can run sequentially on one node or as separate tasks — this is the knob that makes RAM / time / $ a user choice at 10× scale.
3. **Gate**: mgu's 2026-09-05 listing on the `mgu` EC2 node (61 GB RAM, 16 cores, needs disk) or a Batch node — report peak RSS, wall time, and row counts per level. The 9/4 mgu run for reference: object-level aggs 21 min, dir rollup 5 min on an n2-highmem-16 with DuckDB capped at 86 GB.

**Status (DT, 2026-09-06): A.1 + A.2 landed; A.3 pending** (needs the EC2 node / real listing — out of the DT session's reach). `find/aggregate_duckdb.py`, `cli/import_listing.py`; tests in `tests/test_aggregate_duckdb.py` + `tests/test_agg_extensions.py` (byte-identity against the single in-memory cascade over the identity fixture — `//` keys, unicode, root-level files — at k = 1/2/3/6, with pivot sums + `mtime_mean`, and through the CLI).

- `disk-tree import -e duckdb -d/--db PATH`: PATH is a `.duckdb` file (created if missing, **kept** — post-mortems can open it; a clean run leaves it with no tables) or an existing directory (a `disk-tree-agg-<uuid>.duckdb` is created inside and removed on success — pass the spill disk). `aggregate_listing_to_parquet(db=…)`; the caller's `con` is then only used to describe the listing. Caveat worth knowing before measuring: DuckDB ≥ 1.1 can also offload an in-memory database's tables to `temp_directory` under `memory_limit`, so the in-memory premise above is weaker than it reads — the gate run should compare `--db` vs. not at the same cap rather than assume.
- `-k/--partition-depth K`: `_build_partitioned`. Partition keys = distinct depth-K prefixes of the *canonical* path, discovered in one pass over the listing. Per key: the inputs table is built straight off the listing (never a whole `inputs` table) with a `name` range predicate `name = X OR X/ ≤ name < X0` DuckDB can push into parquet row-group stats — so with prefix-contiguous shards each partition reads ~its share; dirty keys (`a//b`, whose canonical sort position differs from the raw one) are gathered once into a side table and joined in by exact key. Ancestors *inside* the partition (depth ≥ K) are synthesized there; each cascade climbs to the root, leaving stub rows for the shallower ancestors, and one final *top* cascade covers rows shallower than K (files at depth < K, the shallow dirs' own `n_desc=1`). `dirs_all` accumulates every cascade's (level, path) rows and the existing `GROUP BY path` folds the stubs; `n_children` is SUMmed across cascades (a shallow dir's depth-K children each count from their own partition). Cost: 3 full listing scans (discovery, top, the final COPY's file leg) + K-pruned partition scans. Each partition also re-reads the listing for the file leg of the final COPY, so `partition_depth` is for small K (tens–hundreds of partitions), not one partition per dir. Stats dict gains `partitions` and `max_rss_mb` (peak RSS, also logged as the last `[agg]` line — the gate's number).
- Not done: running partitions as separate tasks (would need a `--partition X` selector + a stub-merge step over per-task `dirs_all` outputs; the accumulator design allows it, nothing exposes it). `reduce` doesn't expose the knobs (a capture is one machine's disk, not fleet scale).
- Fixed on the way: the cascade never dropped its terminating empty `level_N` table (harmless in-memory; visible in a kept `--db` file).

### B. Attribution slices as cascade group keys

mgu's rollup is per `(path, team, usr)`, not per path: every path row is split into attribution slices (team = employer/communal pool, usr = the person) so lenses and per-user totals are exact. The per-**dir** labels come from a deepest-prefix-wins join against an attribution table (`prefix → team, usr`, ~10⁴ prefixes) — mgu-side data, DT-side mechanism:

- `--label <parquet> --label-cols team,usr`: join labels onto leaf rows by deepest matching prefix (the join itself is generic: longest-prefix match of `path` against the label table's `prefix`), then carry the label columns as **additional group keys through the cascade**, so layer-2 rows are `(path, team, usr, size, n_files, pivots, mtime_wsum, …)` — a path appears once per distinct (team, usr) under it. Unlabeled leaves get NULL labels (mgu maps that to `unattributed`).
- Without `--label` nothing changes (one row per path, as today).

This is the whole of mgu's `dir_attr` + `ptu` — once DT does it, `viz.py`'s aggregation is a CLI invocation plus the JSON overlays.

**Status (DT, 2026-09-06): landed** — duckdb engine only (`aggregate_listing_to_parquet(label=, label_cols=)`, `disk-tree import -e duckdb -L/--label PARQUET [-c/--label-cols team,usr]`; pandas/stream engines raise). Tests: `tests/test_agg_extensions.py` (`test_label_*`): the full labeled layer-2 of a 7-file fixture hand-written row by row, Σ-over-slices == unlabeled row for every column, identity under `--partition-depth` / `--db`, root-default `''` prefix, validation.

- Label table: `prefix` + label columns (default: all others); prefixes are canonicalized like paths (`//` → `/`, trailing `/` stripped); duplicates, missing columns, and names colliding with layer-2 columns raise.
- Join (`_Labels.join`): one LEFT JOIN per distinct prefix *depth* in the table, matching the row's depth-d prefix (`_part_expr`, the same expression A.2 partitions on) by equality; `COALESCE` deepest-first = deepest-prefix-wins. A `''` prefix (depth 0) is the catch-all. With ~10⁴ prefixes at a handful of depths this is a few streaming hash probes over the listing, no unnest, no per-row prefix scan. Applied to files *and* synthesized dir rows (a dir's own `n_desc=1` lands in the dir's own slice).
- Cascade: `_build_dirs_cascade(group_cols=)` groups every level by `(parent, *labels)`; `n_children` is per `(path, labels-of-child)` so it is additive too. NULL is an ordinary label value.
- Output: label columns right after `path`; sorted `(depth, path, *labels NULLS FIRST)`; `kind` on every row; root `.` is one row per slice — `Scan` root stats are the Σ over slices. Without `--label` the output is byte-identical to before.
- Semantics to confirm against mgu's `ptu` (the a2a in Acceptance B): `n_children` per child-label slice (vs. per path, repeated on every slice); dir self-count in the dir's own slice; NULL labels for unlabeled (mgu maps to `unattributed`). Both choices are what keeps Σ-over-slices exact.
- Not done: DT's own server/UI don't understand sliced blobs (duplicate `path` per scan) — a labeled blob is for mgu's consumers; `import`'s pandas/stream engines don't take labels.

### C. Layer-2 written as index tiers

Consumers read layer-2 over HTTP range requests (mgu's Pages Functions; DT's own server could too), so the output wants to be **sorted parquet with small row groups**, in tiers:

| tier | rows | sort | floor |
|---|---|---|---|
| `dirs` | every dir row (× label slices) | `(depth, path)` | none |
| `objects` | every file row | `(path)` (pre-order; one range per subtree) | none |
| `coarse` | dir rows whose subtree `size ≥ F` | `(depth, path)` | `F = 2^(round(log2 total_size) − E)`, `E` default 24 (256 MiB at 3 PiB; ~2M of 220M paths) |

Options: `--tiers dirs,objects,coarse`, `--coarse-exp E`, `--row-group-rows 8192`, and `--sort-variant <cols>` to emit extra sorted copies (mgu needs `(usr, depth, path)` and `(team, depth, path)` of the dirs and coarse tiers — parquet has no secondary index, a sorted copy is one). Every row keeps `kind` ('dir' | 'file'). The floor is a tier boundary, never a loss: every kept row's sums are exact; the planner picks the coarsest tier whose floor is under the query's pixel threshold.

Also: `bulk-list` should record `started` / `finished` timestamps in `_SUCCESS.json` (today: `bucket`, `prefix`, `objects` only) — the scan's as-of instant for D below.

**Status (DT, 2026-09-06): landed** — `find/tiers.py` (`write_tiers`, `coarse_floor`), `disk-tree import -i/--tiers dirs,objects,coarse -O/--tiers-dir DIR [-E/--coarse-exp 24] [-r/--row-group-rows 8192] [-S/--sort-variant usr]…`; tests `tests/test_tiers.py`.

- Tiers are cut from the *finished* layer-2 blob (one filtered, sorted, spillable COPY each), so they are engine-agnostic (duckdb + stream; pandas has no blob on disk and refuses) and land beside `--out-dir`'s blob as `<scheme>-<bucket>.<tier>[-by-<cols>].parquet`. Sorts: `dirs`/`coarse` `(depth, path, *labels)`, `objects` `(path, *labels)`, NULLS FIRST; a sort variant `-S usr` leads with `usr` (`(usr, depth, path, *labels)`, file `…dirs-by-usr.parquet`) and is written for the dirs and coarse tiers. Every row keeps `kind`; every file carries `tier` and `sort` in its parquet KV metadata, `coarse` also `floor_bytes` / `coarse_exp` / `total_size` (so the planner reads the floor from the footer, as mgu's `index_schema.floor_bytes` does from D1).
- Row groups: `--row-group-rows` must be a positive multiple of 2048 — DuckDB's parquet writer cuts groups in vector steps, so "≤ N" is exact for multiples (5000 rows at 2048 → `[2048, 2048, 904]`) and silently overshoots otherwise (3000 → one 5000-row group); the 8192 default is fine.
- Floor: `2^(round(log2 total) − E)` with Python's `round` (banker's at an exact .5 — a measure-zero case), clamped at 1; `total` = Σ root slices. With label slices the floor applies **per row** (a path can keep one slice and drop another — each slice is its own cell in a lens); the kept rows are exact.
- `_SUCCESS.json` (both `bulk-list` modes) now carries `started` / `finished`: ISO-8601 UTC, `started` taken after the exists-policy check (a reused listing keeps its own window), `finished` just before the marker is written. Readers that took the marker as an exact dict were updated; `capture`'s marker already had `time`.
- Not done: writing tiers straight to a URL (`--tiers-dir` is local; sync the dir), and `reduce` doesn't expose tiers.

### D. Access plane: hour grain, as-of, per-scan state

`access/aggregate.py` rolls layer-1a up per `(bucket, path, UTC day, op)`. mgu needs to cut reads at the listing's as-of instant, and a day-grained row can't be split, so today's "as-of" is coarse by up to a day.

1. **Hour grain**: `date_trunc('hour', ts)` (column `hour TIMESTAMP` replacing `day DATE`); `last_ts` stays exact. Row count grows only for dirs read across many hours.
2. **`--as-of <ts>`** on the aggregate/consumer path: rows with `hour < as_of`. mgu re-aggregates its retained raw shards (`access/raw/`, lossless per-request rows) once so history follows the same rule.
3. **Per-scan state** (`dt access state --as-of T --live <dirs tier> --prev <state>`): one row per `(bucket, dir)` that exists in the scan's listing — `last_ts, read_ops, read_bytes` (+ per-op sums) — built incrementally from the previous state (semi-joined to the live dirs) plus the shards in `[prev, T)`. Reads that belong to since-deleted dirs drop out; cost is O(live dirs) + O(new rows), not O(everything since logging began). Measured on mgu 2026-09-05: 64.3M dirs ever read, of which 52.0M (81 %) no longer exist.
4. The state joins into the cascade as a **max-aggregated column** (`--max-col last_ts` from a side table keyed by path): subtree-max last-read per row, which mgu emits as `a`.

**Status (DT, 2026-09-06): landed (all four)** — `access/aggregate.py`, new `access/state.py`, `cli/access.py`, `find/aggregate_duckdb.py` (`_Side`), `cli/import_listing.py`; tests `tests/test_access.py` (`test_agg_hour_grain_*`, `test_agg_as_of_*`, `test_state_*`) and `tests/test_agg_extensions.py` (`test_side_*`).

1. `hour TIMESTAMP` (UTC, naive — `SET TimeZone='UTC'` before `date_trunc`) replaces `day DATE` in layer-2a; sort `(bucket, depth, path, hour, op)`; stats `hours` replaces `days`; the file carries `grain: hour` in its parquet KV metadata. **BIC** for any 2a reader that named `day` — nothing in DT did (`access top`/`sizes` don't); mgu's consumers will. Re-aggregate retained raw shards to get hour-grained history.
2. `aggregate_access(as_of=)` / `dt access agg -a/--as-of T`: the cut is on the raw rows, `ts < T` — exact, not hour-coarse — so every emitted hour row satisfies `hour < T` and a mid-hour `T` keeps only the earlier part of that hour; `as_of` is recorded in the KV metadata. Naive `T` = UTC. An empty result raises rather than writing an empty 2a.
3. `dt access state -a T -l <dirs tier> [-l …] [-p <prev>] -o out <2a glob>` (`build_state`): one row per `(bucket, path)` dir in the live tiers with any history — `last_ts` (MAX over **GET**s: "read" = GET, the only op that moves object bytes out), `read_ops`/`read_bytes` (GET count/egress), and `n_ops_<op>`/`bytes_out_<op>`/`bytes_in_<op>` for every op in `schema.OPS`. The bucket comes from each tier's root `uri`. Incremental: prev rows semi-joined to live dirs (dead dirs drop; stats report `carried`/`dropped`) + 2a rows in `[prev.as_of, T)` (`hour` rows: a non-hour-aligned `T` here is coarse by < 1 h, unlike the exact cut in 2), folded per `(bucket, path)`; sorted `(bucket, path)`; KV metadata `as_of`/`prev_as_of`/`grain`. Building from scratch at `T` equals building incrementally (tested). `T` must be after `prev.as_of`.
4. `import -e duckdb -a/--side <state> -A/--max-col last_ts [-A read_ops]`: the side is joined by exact path (`.` ↔ root; a `bucket` column, if present, filters to the imported bucket) onto every file *and* synthesized dir row, and `_build_dirs_cascade(max_cols=)` folds it with MAX at every level (like `mtime`) — a dir's value is the max over itself and its subtree, NULL where nothing beneath carries one. Columns land after the extension columns, before `uri`. Composes with `--label` (each slice's max is over its own rows — the root's own read lands in the root's own slice) and `--partition-depth`. pandas/stream engines refuse.
- Not done: `n_requesters` is still the bounded overestimate the 2a docstring describes; `state` has no notion of retention (a shard glob is what you pass).

### E. Size histogram column (cheap once A–C exist)

Per path, a log2 histogram of descendant files by size — counts and bytes per bin (≈40 bins). It is additive through the cascade (children are disjoint sets), so it costs one vector-sum per level. `histogram.py` does the same idea for byte-weighted mtime per child at query time; this is the size axis, materialized. mgu wants it for an on-page "what sizes are the files under this path" chart that follows drills and scopes.

**Status (DT, 2026-09-06): landed** — `import -e duckdb -H/--size-hist` (`aggregate_listing_to_parquet(size_hist=True)`); `find/agg_ext.py` (`SIZE_HIST_BINS`, `size_bin`); tests `tests/test_agg_extensions.py` (`test_size_*`: every path's histogram equals a direct computation over the listing, edges at 2^k ± 1 up to 2^62, slices, partitions).

- Bins: 41. Bin 0 = zero-byte files; bin b (1 ≤ b ≤ 39) = sizes in [2^(b−1), 2^b) (i.e. `bit_length(size)`); bin 40 is open-ended (≥ 512 GiB). Output: `size_hist_n` and `size_hist_bytes`, `LIST(BIGINT)` of 41, placed after the pivot/`mtime_mean` columns (before `--max-col` columns and `uri`); a file's histogram is its own single bin. Per label slice under `--label`; byte-identical under `--partition-depth`.
- Mechanism: exactly the spec's — one count and one byte column per bin (82 `BIGINT`s) ride the existing `sum_cols` cascade and are packed into the two lists at the final COPY. The cost worth watching at fleet scale: 82 extra columns on every level table (~650 B/row uncompressed — DuckDB's per-column RLE/constant compression handles the all-zero-but-one pattern well, but it is not free). If that shows up in the A.3 gate, the alternative is a sparse `(parent, bin)` level-0 rollup packed to a list, which trades width for a 41× unnest.
- pandas/stream engines refuse the flag.

## Non-goals

Attribution *content* (identities, rules, W&B mining), the marks ledger and its fold, storage-class pricing, the CF site — all stay mgu-side. Naming: DT columns stay long-form (`read_ops`, not `ro`); mgu's wire abbreviations are its serializer's business.

## Acceptance

- A: the 2026-09-05 mgu listing cascades to completion under a fixed `memory_limit` well below input size, peak RSS reported; with `--partition-depth`, peak RSS ∝ largest partition.
- B: for a labeled leaf set, Σ over slices at every path equals the unlabeled path row; deepest-prefix semantics match mgu's `dir_attr` (mgu will diff its 2026-09-04 `ptu` against DT's output on the same inputs).
- C: tiers are sorted as declared, row groups ≤ N rows, `coarse` ⊂ `dirs` with exact sums; `_SUCCESS.json` carries timestamps.
- D: hour-grained shards; `--as-of` excludes exactly the rows at/after the instant; state file row count = live read dirs.
- E: histogram sums per level equal a direct computation over the listing for a sample of paths.

## Status (DT session, 2026-09-06)

Landed on branch `mgu-scale`, one commit per item so mgu can cherry-pick each (all additive to `find/aggregate_duckdb.py`, `find/agg_ext.py`, `find/tiers.py`, `find/bulk*.py`, `access/*`, `cli/import_listing.py`, `cli/access.py`; every new option is a no-op when absent and the existing 505 tests stayed byte-identical, 543 after):

| item | state | what's tested at small scale | still open |
|---|---|---|---|
| A | `--db`, `--partition-depth` landed | byte-identity vs the single in-memory cascade at k = 1/2/3/6, with extensions, through the CLI | **A.3 gate** — needs the EC2 node and the 9/5 listing: peak RSS (`max_rss_mb` in the stats / last `[agg]` line), wall, rows per level; compare `--db` on/off at one cap (DuckDB ≥ 1.1 can offload in-memory tables too); pick K |
| B | `--label` landed | hand-written labeled layer-2, Σ-over-slices exactness, partitions, root default | mgu's `ptu` a2a; the two semantic choices flagged under B (per-child-label `n_children`, dir self-count in its own slice) |
| C | tiers + `_SUCCESS.json` timestamps landed | sorted, ≤ N (multiples of 2048), coarse ⊂ dirs exact, KV metadata, variants over slices | tiers to a URL; `reduce` |
| D | hour grain, `--as-of`, `access state`, `--side/--max-col` landed | exact as-of cut, incremental == from-scratch state, dead dirs drop, subtree MAX under slices/partitions | mgu re-aggregates its raw shards at hour grain; `n_requesters` still a bound |
| E | `--size-hist` landed | every path == direct computation, 2^k ± 1 edges, slices, partitions | width cost (82 columns) at the A.3 gate |

Not moved to `specs/done/` because the A.3 gate is the acceptance for A and everything below it was built to that scale on paper only.
