# Static name index: thresholds and tunables

A reference list of every threshold and tunable constant in the static name index (search index) pipeline: build, runs/append, carries, compaction, Batch fan-out, and the site's readers. It is a snapshot of the code on the `gcs` branch (merged from `cloud` at `3fc8f99b`), with the K and run-parameter rows updated for `cloud` `72a6c427`. Line numbers will drift; the constant names won't.

Columns:
- **Default**: the code default. Where a deployment profile overrides it, the profile value appears in the "Override" column.
- **Defined**: `file:line`. Python paths are relative to `cloud/src/dt_cloud/`; TS paths are relative to `site/functions/_lib/`.
- **Override**: CLI flag, env var, profile field (`job/static-names.json` / `STATIC_NAMES_*`), or a generation's `meta.json`.
- **Baked?**:
  - **gen**: recorded in a generation's files and fixed until a rebuild or compaction.
  - **run**: chosen per invocation.
  - **deploy**: a reader-side setting, chosen per site deploy.
- **Bytes?**:
  - **bytes**: changing it changes the output files.
  - **perf**: it changes only time, memory or I/O; the output stays byte-identical.
  - **answers**: a reader-side value that changes which queries are answered, declined or sent to the heavy path. It writes no index bytes.

## Listing, intervals, catalog, suffix shards

| Name | Default | Defined | Override | What it controls | Baked? | Bytes? |
|---|---|---|---|---|---|---|
| `OPEN` | `4291747200` (2106-01-01) | `static_names.py:52` | — | The `vt` of a version that is still open. Every tier and reader treats it as "still live". | gen (format) | bytes |
| `INTERVAL_RG` | 65,536 | `static_names.py:57` | — | Row-group size of the interval (open-version) files. | gen | bytes |
| `SX_RG` | 8,192 | `static_names.py:58` | — | Row-group size of the suffix shards (`sx/`). It is the reader's decode unit and the "two row groups of slack" in every light-read bound. The anchors' `meta.json` also records it as its `rg` (`static_anchors.py:520`). | gen | bytes |
| `CODEC` | `zstd` | `static_names.py:59` | — | The codec for every parquet the index writes. | gen | bytes |
| Key ranges `k` (staged) | 128 | `static_names.py:1142` (`-k`) | CLI | Key ranges of the staged (Batch) base build. Each range is one DuckDB pass over every scan. Recorded in `ranges.json`. Appends read `k` from it (`static_runner.py:188`), and a compaction copies it (`static_compact.py:1239`). | gen | bytes |
| `RANGES` (local) | 8 | `static_local.py:67` | `static-names build -k` / `-r ranges.json` | The same as the row above, for a one-machine build. | gen | bytes |
| Shard `target_rows` | 50,000,000 | `static_names.py:1397` (`plan-shards -n`); `RUN_SHARD_ROWS` `static_append.py:48`; `SHARD_ROWS` `static_local.py:68` | CLI `-n` | Suffix rows per shard file. A three-character prefix is never split, so a literal's range always sits in one shard. Recorded in `shards.json`, and a compaction re-plans with the old plan's `target_rows` and task count (`static_compact.py:1280`). | gen | bytes |
| Shard tasks | 32 | `static_names.py:1398` (`plan-shards -t`) | CLI | Contiguous task groups of shards in the staged build. Recorded in `shards.json` `tasks`. | gen | perf (grouping only) |
| `V` (membership `max_rows`) | 100,000 | `static_local.py:71` `MAX_ROWS`; `catalog members -V` (required) `static_catalog.py:934` | `static-names build -V`; generation `catalog/meta.json` `membership.max_rows` | The catalog membership bound. A literal whose suffix range holds more than `V` rows is a catalog member: heavy, answered from the catalog cells or the drill (`cut_members` `rows > max_rows`, `static_catalog.py:908`). The census is cut at `V + 1` (`static_local.py:291`, `static_compact.py:107`). Runs copy `V` from the base catalog (`static_append.py:642,1005`, `static_local.py:804`), and so does a compaction (`static_compact.py:1291`). | gen | bytes |
| `max_bytes` | none | `static_catalog.py:931` (`members -B`) | CLI | An optional second membership bound, on decoded bytes. No build passes it. | gen | bytes |
| `CELL_RG` | 4,096 | `static_catalog.py:52` | — | Row-group size of `catalog/cells.parquet`. | gen | bytes |
| `CHUNK_ROWS` (catalog) | 1 << 24 | `static_catalog.py:375` | — | Suffix rows fed to `member_events` at a time. Events are additive across chunks. | run | perf |
| `CENSUS_PROC_MEM` | 4 GiB | `static_catalog.py:247` | — | The working set assumed per census process. `census_jobs` divides the DuckDB memory limit by it. | run | perf |
| Census `floor_rows` | 50,000 | `static_catalog.py:868,1236`; `static_anchors.py:1953` | CLI `-f` | The prefix floor of the diagnostic census commands (`V` reports). It has no effect on the build. | run | — |
| `hex_runs` rule `MIN,TAIL` | required; gcs `16,8` | `static_profile.py:90`; `hex_runs.parse_rule` `hex_runs.py:38` | profile `hex_runs` (`job/static-names.json:44`), `STATIC_NAMES_HEX_RUNS`, `static-names build -x` | Interiors of hex runs of at least `MIN` characters are not indexed, except the last `TAIL` characters. It applies to suffix rows, the catalog, the drill and the anchors. Recorded in `scans.json` and `catalog/meta.json` `hex_runs`. The runner refuses a profile rule that differs from the generation's (`static_runner.py:131`). | gen | bytes |
| `READ_ROWS` | 1 << 16 | `static_append.py:202` | — | Rows per read from an input shard in `merge_shards`. | run | perf |
| `RANGES_PER_JOB`, `MIN_RANGE_ROWS` | 4; 1 << 22 | `static_append.py:204-205` | — | `merge_shards` without a plan: ranges per job, and the fewest estimated rows worth a range of its own. | run | perf |
| `PIECE_RG` | 1 << 17 | `static_append.py:207` | — | Row groups of the intermediate pieces in `merge_shards`. | run | perf |
| `append_tasks` | 16 (gcs 16) | `static_profile.py:65` | `job/static-names.json:37`, `STATIC_NAMES_APPEND_TASKS` | Batch tasks for a run's append. Each task takes `ceil(k / append_tasks)` ranges (`static_runner.py:193`). | run | perf |
| `pipeline_depth` | 1 | `static_profile.py:69` | `STATIC_NAMES_PIPELINE_DEPTH` | Backlogged scans in flight at once (`runs add -c`). | run | perf |

## Drill: base build, measurement, rollups, write

| Name | Default | Defined | Override | What it controls | Baked? | Bytes? |
|---|---|---|---|---|---|---|
| **R** (read bound / heavy bar) | 100,000 | `static_local.py:72` `R_ROWS`; `static_drill.py:2443`; `static_roots.py:805,976,1242,1312,1542`; `static_anchors.py:64` `R_DEFAULT` | `static-names build -R`; `drill build -R`; `roots rollups -R`; generation `drill/meta.json` `R` | A `(q, dir)` is **heavy** when it holds strictly more than `R` root rows (`rollup_sql` `rows > R`, `static_roots.py:411`). Heavy directories get rollup cells. Light ones are answered by reading their match roots. | gen | bytes |
| **K** (kept children) | 256 | `static_local.py:72` `K_KEPT`; `static_drill.py:2437`; `static_roots.py:970,1237,1310`; `static_anchors.py:65` `K_DEFAULT` | `static-names build -K`; `drill build -K`; `roots rollups -K`; generation `drill/meta.json` `K` | Per heavy directory, the K children with the largest peak bytes (ties broken by name) are kept by name. The rest are summed into the remainder, which the reader shows as `(other)` (`static_roots.py:416-419`). | gen | bytes |
| **floor** (measurement floor) | 10,000 | `static_local.py:72` `FLOOR`; `static_drill.py:2431`; `static_roots.py:677,761` | `static-names build -f`; `drill build -f`; `roots measure -f`; generation `drill/meta.json` `floor` | `roots-measure/` keeps the directories with at least `floor` root rows. Runs use it to find newly heavy directories, and incremental compaction uses it to re-measure. Must be at most `R + 1`; otherwise the incremental compaction is refused and measures every root (`static_measure.py:148`). | gen | bytes (`roots-measure/`) |
| `ROOT_RG` | 8,192 | `static_roots.py:298` | — | Row-group size of the base drill's roots and rollups files (the Worker reads whole groups). The base drill records it as `meta.json` `rg`. | gen | bytes |
| `RUN_RG` | 2,048 | `static_drill.py:43` | — | Row-group size of a run's drill (`build_day` `rg`, `static_drill.py:2518`; `static_local.py:863`). It is smaller than the base's because the reader's bound adds 2·`rg` for every tier. A carry requires every input to have the same `rg` (`static_drill.py:2218`). | gen (per tier) | bytes |
| `IDX_RG` | 1,024 | `static_roots.py:462` | — | Entries per row group of the group index (`*-index.parquet`). Its `.top.parquet` has one entry per index row group. Recorded as `meta.json` `idx_rg`. | gen | bytes |
| `dispatch_rows` | `R + 2·rg` | `static_drill.py:2060`; `static_roots.py:1335` | derived | Recorded in `drill/meta.json`. The TS reader only checks it is a positive integer (`staticDrill.ts:354`) and recomputes the bound itself over all tiers (see Serving). | gen | bytes (meta) |
| `FILE_ROWS` | 50,000,000 | `static_drill.py:41` | — | Roots or rollup rows per data file. Files are cut at the next `q` boundary past this. | gen | bytes |
| `CHUNK_ROWS` (local drill) | 1 << 22 | `static_local.py:74` | `static-names build -c`, `add -c` | Suffix rows per member-loop chunk, and roots per measurement / rollup `q` range (`q_bounds`, `static_local.py:351`). This bounds the working set. The output is byte-identical at any `-c`. | run | perf |
| `-c` (staged drill build) | 1 << 24 | `static_drill.py:2428`; `build_day` default `static_drill.py:2067` | `drill build -c` (the runner passes none, `static_runner.py:371`) | Suffix rows per member-loop chunk in a run's drill on Batch. | run | perf |
| q-group `target_rows` (staged base) | 150,000,000 | `static_roots.py:1044` | `roots … -r` | Root rows per q-group in the staged base drill's rollup tasks. | run | perf |
| Subtree parts | 32 | `static_roots.py:766,1188` | `-n` | Subtree partitions (= Batch tasks) of the staged measure / rollups. | run | perf |
| `MEASURE_THREADS` | 4 | `static_local.py:404` | `$DT_MEASURE_THREADS` (`static_local.py:407`) | DuckDB threads per measurement process. `measure_ranges` runs `threads // MEASURE_THREADS` `q` ranges at a time, each in its own process (`static_local.py:432`). | run | perf |
| `MEASURE_HEAVY_X` | 4 | `static_local.py:414` | `$DT_MEASURE_HEAVY_X` (read at import) | The heavy measurement bar is `measure_heavy_rows` = `max(MEASURE_HEAVY_MIN_ROWS, round(X × median range's spilled root rows))` (`static_local.py:420`). The bar is measured in rows, not bytes. Heavy ranges are measured first, with `HEAVY_THREADS` threads each. | run | perf |
| `MEASURE_HEAVY_MIN_ROWS` | 1 << 25 (≈ 33.5M) | `static_local.py:415` | — | The floor of the heavy bar. | run | perf |
| `$DT_MEASURE_HEAVY_ROWS` | unset | `static_local.py:416,550` | env | Sets the heavy bar outright, replacing `measure_heavy_rows`. | run | perf |
| `HEAVY_THREADS` | 16 | `static_local.py:417` | — | DuckDB threads per heavy-range measurement process. | run | perf |
| `FETCH_THREADS` | 64 | `static_drill.py:64` | `drill build -j` | Concurrent ranged reads when a run's drill prefetches prior tiers' groups (≤ 1 turns prefetch off). | run | perf |
| `CACHE_RAM_FRACTION`, `MAX_CACHE_BYTES` | 1/8 of RAM; 2 GiB | `static_drill.py:92-93` | `drill build -C` | Most prefetched bytes held: `min(2 GiB, RAM/8)`. | run | perf |
| `COALESCE_GAP`, `COALESCE_SPAN` | 64 KiB; 8 MiB | `static_drill.py:95-96` | — | Prefetch coalescing: needed groups at most `GAP` apart become one request of at most `SPAN` bytes. | run | perf |
| `GROUP_CACHE`, `SPAN_CACHE` | 2,048; 512 | `static_drill.py:49,51` | — | Decoded row groups and index spans kept per group file. | run | perf |
| `STATE_RG`, `STATE_BATCH_ROWS` | 1 << 20; 1,000,000 | `static_drill.py:45,47` | — | Row groups and batch boundaries of the `state/` files, kept for identical parquet pages. | gen | bytes |
| `EXPLODE_PART_ROWS` | 1 << 23 | `static_drill.py:53` | — | `dr` rows per part of the ancestor aggregations (`_by_parts`). | run | perf |
| `NH_PART_ROWS` | 1 << 24 | `static_drill.py:56` | — | Touched directories per part of the newly-heavy join with `dcount`. | run | perf |
| `NEWLY_HEAVY_CHUNK_ROWS` | 1 << 23 | `static_drill.py:59` | — | Estimated root rows of newly heavy directories built at once. | run | perf |
| `SETTLE_CHUNK_ROWS` | 1 << 22 | `static_drill.py:61` | — | Settling input rows loaded at once. | run | perf |
| `WRITE_JOBS` | None (every core) | `static_drill.py:69` | — | Processes `write_kind` writes files with. Same bytes at any count. | run | perf |
| `SORT_WRITERS`, `COUNT_THREADS` | 4; 16 | `static_drill.py:71,73` | — | Concurrent sorted writers, and cursors counting sorted chunks. | run | perf |
| `SPILL_PARTS`, `SPILL_DIRECT` | 64; 512 | `static_roots.py:1113,1116` | — | Most partitions in a chunked spill (`ChunkSpill`), and the chunk count up to which each chunk gets its own partition. | run | perf |
| `BRUTE_CHILDREN` | 200,000 | `static_roots.py:1391` | — | `drill-brute` (the reference check) lists only the kept children above this many. | run | — |
| Drill DuckDB sizing | `-M` = vCPUs × 7,700 MiB / 2; `-p` = vCPUs | `static_runner.py:96-99` (`drill_duckdb_args`) | profile `machines.drill` | Half the drill VM's memory goes to DuckDB; the rest is left for Arrow and the Python caches. | run | perf |

## Anchors (`names/`, `anchors/`, `anchors/start/`)

| Name | Default | Defined | Override | What it controls | Baked? | Bytes? |
|---|---|---|---|---|---|---|
| **R**, **K** (anchors) | 100,000; 256 | `static_anchors.py:64-65`; CLIs `static_anchors.py:1998/2003, 2052/2054, 2153/2159` | `-R`/`-K`; generation `anchors/meta.json` `R`, `K` | The same meaning as the drill's, for `q$` / `^q$` rollups. For `start`, `K` is the number of buckets kept per prefix and `R` is the member bound (`static_anchors.py:2087,2091`). | gen | bytes |
| `rg` (anchors meta) | `SX_RG` = 8,192 | `static_anchors.py:520,746` | — | Recorded for the readers' bound `R + 4·Σrg`. | gen | bytes (meta) |
| `dispatch_slack_groups` | 4 | `static_anchors.py:520` | — | Recorded in `anchors/meta.json`. No reader reads it: both readers hardcode the 4 (`static_anchors.py:1800`, `staticAnchors.ts:474`). | gen | bytes (meta) |
| `ROLLUP_RG` | 2,048 | `static_anchors.py:77` | — | Row groups of the anchor rollups. It is applied by temporarily setting `static_roots.ROOT_RG` (`static_anchors.py:496-501`). | gen | bytes |
| `NAME_SHARD_ROWS` | 50,000,000 | `static_anchors.py:75` | — | Target rows per name-index shard. A three-character prefix is never split. | gen | bytes |
| `NAME_SPILL_PARTS` | 64 | `static_anchors.py:177` | — | Partitions the parallel name index spills into. | run | perf |
| `MAX_ROWS` (py mirror) | 100,000 + 2·8,192 | `static_anchors.py:67` | — | The light whole-range read bound. Mirrors `staticFilter.ts` `MAX_ROWS`. | deploy | answers |
| `START_MAX_ROWS` | 400,000 + 2·8,192 | `static_anchors.py:70` | — | The read bound for `^q` and path terms. Mirrors `staticAnchors.ts:65`. | deploy | answers |
| `START_MAX_HITS` | 150,000 | `static_anchors.py:73` | — | The most first hits a `^q` read may hold. Mirrors `staticAnchors.ts:69`. | deploy | answers |
| `MAX_SHARDS` | 8 | `static_anchors.py:1547` | — | The most shards one key selects within. Past it, the key is bounded by the shards' planned rows. Mirrors `staticNames.ts:90`. | deploy | answers |
| `ANCHOR_THREADS` | 4 | `static_anchors.py:525` | — | DuckDB threads per shard process when a tier's rollups are built shard by shard. | run | perf |
| `ANCHOR_HEAVY_X` | 4 | `static_anchors.py:532` | `$DT_ANCHOR_HEAVY_X` (read at import) | A shard is heavy at `max(ANCHOR_HEAVY_MIN_ROWS, X × median shard rows)` (`heavy_rows`, `static_anchors.py:536`). The compaction VM deals heavy shards to helpers, which build each one alone on all threads. The bar is recorded in the plan as `heavy_rows` (`static_fanout.py:441`). | run | perf |
| `ANCHOR_HEAVY_MIN_ROWS` | 1 << 24 (≈ 16.8M) | `static_anchors.py:533` | — | The floor of the anchors' heavy bar. It is half the measurement's floor. | run | perf |
| `PROBE_BATCH_KEYS` | 1 << 16 | `static_anchors.py:933` | — | Keys per prior-probe query. Each batch costs one shard read. | run | perf |
| `PROBE_GAP_ROWS` | 1 << 22 | `static_anchors.py:935` | — | A probe batch is split where more than this many shard rows lie between two keys. | run | perf |
| `PROBE_READ_ROWS` | 1 << 22 | `static_anchors.py:937` | — | Rows of row groups read per probe batch. | run | perf |
| `ISLAND_REPORT_GROUPS`, `SPAN_GAP_GROUPS` | 64; 4 | `static_anchors.py:1003,1006` | — | Single-key batches covering at least 64 groups read only the meeting spans. Spans at most 4 groups apart are merged into one read. | run | perf |

## Runs, carries (binary counter), merges

| Name | Default | Defined | Override | What it controls | Baked? | Bytes? |
|---|---|---|---|---|---|---|
| Run R/K/floor (local `add`) | from the base generation | `static_local.py:862` (drill), `static_local.py:836` (anchors) | base `drill/meta.json`, `anchors/meta.json` | A one-machine run inherits R/K/floor from the base. | gen | bytes |
| Run R/K/floor (Batch runner) | from the base generation | `static_drill.py` `build_cmd`, `static_anchors.py` `run_cmd` (`static_names.base_params`) | `drill build -R/-K/-f`, `anchors run -R/-K`: only the base's value is accepted | Since `72a6c427` the staged runs read the base's `drill/meta.json` / `anchors/meta.json`, as the local `add` does. A flag that disagrees is refused. | gen | bytes |
| Carry R/K | copied from the newest input tier | `static_drill.py` `merge_tiers` (drill); `static_merge.py:128-129` (anchors, from `dirs[-1]`) | — | A carry copies `R`, `K`, `rg`, `idx_rg` and `dispatch_rows`. A drill carry refuses inputs whose `rg`, or `R` and `K`, differ. | gen | bytes |
| `MERGE_CHUNK_ROWS` | 1 << 24 | `static_drill.py:2181` | — | Roots and rollup rows per merged-`q` range in a drill carry. Byte-identical at any value (`static_drill.py:2216`). | run | perf |
| `compact_level` L | 5 (`COMPACT_LEVEL`); gcs 5 | `static_profile.py:25,95`; `append_runner.py:80` | `job/static-names.json:45`, `STATIC_NAMES_COMPACT_LEVEL`, `add -C` | Binary counter: carries stop below level L. A stack holds at most 2^L − 1 carried scans, and a compaction is due after about 2^L scans. `none` means carries are unbounded and no compaction is ever due. | run (policy) | bytes (tier layout only; answers are the same) |
| `compact` policy | none (uses `compact_level`) | `static_profile.py:99`; `Policy` `append_runner.py:282` | `STATIC_NAMES_COMPACT`, `add -C`, `compact -C` (`append_runner.py:2477`) | One of `level:L`, `every:Nd`/`Ns`, `geometric:r`, `none` (see [static-compaction]). | run | tier layout |
| `LONG_MERGE_LEVEL` | 5 | `append_runner.py:130` | — | A merge whose carries reach this level runs as a long stage: STANDARD VM, `LONG_RUN_S`. | run | perf |
| `LONG_ROWS` | 1,000,000,000 | `static_merge.py:279` | — | Suffix rows in a carry's inputs above which the merge job runs long. | run | perf |
| `MEM_FRACTION`, `STREAM_RESERVE`, `SHARD_JOB_MEM` | 0.75; 4 GiB; 1.5 GiB | `static_merge.py:54,56,58` | — | Memory split among concurrent tier merges, memory per streaming merge, and memory per `merge_shards` worker. | run | perf |
| Tier merges at once | 4 | `static_merge.py:371` (`carry -j`) | CLI | Concurrent tier merges per carry. | run | perf |
| `TICK_S` | 30 | `static_merge.py:76` | — | Interval between a merge's progress log lines. | run | — |

## Compaction

| Name | Default | Defined | Override | What it controls | Baked? | Bytes? |
|---|---|---|---|---|---|---|
| Compaction R/K/floor (drill) | copied from the source generation; K overridable | `static_compact.py` `_fold_gen` | `compact -k`, `compact-probe -k`, profile `keep` / `STATIC_NAMES_KEEP` (K only) | `fold_drill` runs with `dm["R"]`, `keep or dm["K"]` and `dm.get("floor", FLOOR)` from `G/drill/meta.json`. A K other than the tiers' measures and rolls up every root (`static_measure.blocker`), and keys its own checkpoint (`checkpoint_prefix`). | gen | bytes |
| Compaction R/K (anchors) | copied from the source generation; K overridable | `static_compact.py` `_fold_gen` | as the drill's | `build_base_anchors` with `am["R"]`, `keep or am["K"]` from `G/anchors/meta.json`. The anchors are rebuilt in full. | gen | bytes |
| Compaction `V` | copied | `static_compact.py:1291` | none | Membership `max_rows` from `G/catalog/meta.json`. | gen | bytes |
| Compaction ranges / shard plan | copied | `static_compact.py:1239,1280` | none | `ranges.json` is copied. Shards are re-planned with the old `target_rows` and task count. | gen | bytes |
| Incremental vs full measurement | incremental when allowed | `static_compact.py:397-400`; `static_measure.py:140-150` | — | Incremental (base ⊕ runs, each range only sorted) unless: a run's `dcount` isn't `fresh`, the base has no `roots-measure/`, or `floor > R + 1`. Incremental rollups carry the tiers' kept-children cells forward (`static_measure.py:153`). | run | perf (same bytes) |
| `-c` chunk rows | 1 << 22 | `static_merge.py:548` (`compact`), `:644` (`compact-probe`) | CLI | The fold's roots per measurement / rollup `q` range. | run | perf |
| `-j` fold jobs | `fold_jobs` = max(1, vCPUs ÷ `FOLD_VCPUS`) | `static_runner.py:103-108,151`; default 1 at `append_runner.py:2480` | profile `fold_jobs` (`static_profile.py:108`; 0 means sized from the machine), `STATIC_NAMES_FOLD_JOBS` | Drill-fold chunks folded at a time, as cursors of one DuckDB database. | run | perf |
| `FOLD_VCPUS` | 6 | `static_runner.py:103` | — | vCPUs per concurrent fold chunk. | run | perf |
| `COMBINE_PART_ROWS` | 1 << 26 | `static_compact.py:702` | — | Estimated root rows per part of a long-roots chunk's combine. | run | perf |
| `STAGE_HEADROOM`, `SX_STAGE_HEADROOM` | 4.0; 1.0 | `static_compact.py:225,125` | `stage_roots` mode `auto`/`on`/`off` (`static_compact.py:221`) | In `auto` mode, roots or shards are staged onto the work disk when its free bytes cover the staged bytes × (1 + headroom). | run | perf |
| `compact_jobs` | 1 | `static_profile.py:105` | `STATIC_NAMES_COMPACT_JOBS` | The interval store's compaction `-j`. Listed here because the same profile carries it. | run | perf |
| `COMPACT_START_S` | 3,600 s | `append_runner.py:708` | — | How long the submitter's compaction lease record lives before the job adopts it. Appends wait this long if the job never starts. | run | — |
| `GRACE_S`, `MIN_GRACE_S` | 3,600 s; 600 s | `append_runner.py:1412,1414` | `add -G`, `compact -G` | How long a superseded manifest's runs (or a compacted predecessor) are kept. Must be well above the readers' 5-minute manifest cache. | run | — |

## Fan-out, Batch, leases

| Name | Default | Defined | Override | What it controls | Baked? | Bytes? |
|---|---|---|---|---|---|---|
| `measure_tasks` | 1 (gcs 16) | `static_profile.py:72` | `job/static-names.json:38`, `STATIC_NAMES_MEASURE_TASKS`, `compact -D` | The compaction's drill measurement runs over this many tasks. Ranges are dealt by spilled **bytes** (`static_fanout.py:280`), heaviest first, each to the least-loaded task (`static_merge.deal`, `static_merge.py:915`). | run | perf |
| `anchor_tasks` | 1 (gcs 16) | `static_profile.py:75` | `job/static-names.json:39`, `-A` | The compaction's base anchor rollups over helper tasks. Shards are dealt by footer rows. Heavy shards (`heavy_rows`) never go to share 0. | run | perf |
| `fold_tasks` | 1 (gcs 16) | `static_profile.py:78` | `job/static-names.json:40`, `-L` | The drill fold over helper tasks, one helper job per wave. | run | perf |
| Helper `poll_s`, `retries` | 30 s; 2 | `static_fanout.py:120-121` | — | Polling interval for unit `done.json` files, and the re-deals per wave. | run | perf |
| `PLAN_WAIT_S`, `PLAN_POLL_S` | 1,800 s; 10 s | `static_merge.py:1109-1110` | — | How long a presubmitted `anchor-shard` task waits for its plan to appear. | run | — |
| `machine`, `ssd_gb` | `n2-highmem-16`, 750 GB (gcs: 32-vCPU, 1,500 GB; 48-vCPU / 3,000 GB for drill and compact) | `static_profile.py:55-59`; `job/static-names.json:20-35` | `machines` / `ssd_gbs` per stage, `STATIC_NAMES_MACHINE(S)`, `STATIC_NAMES_SSD(S)` | Batch VM shape per stage. It also sizes the DuckDB `-M`/`-p` (`duckdb_args`: ¾ × 7,700 MiB per vCPU, `append_runner.py:1234`) and `fold_jobs`. | run | perf |
| `spot`, `standard_stages` | true; () (gcs: anchors, compact, drill) | `static_profile.py:61,64`; `job/static-names.json:36,46` | `STATIC_NAMES_SPOT`, `STATIC_NAMES_STANDARD_STAGES` | Spot vs STANDARD VMs. `LONG_STAGES` (`compact`, `anchors`) always run STANDARD (`append_runner.py:125`). | run | perf |
| `LEASE_S`, `MAX_RUN_S` | 4 h; 4 h | `append_runner.py:117,119` | — | The merge lease lifetime, and the longest run of a Batch task. | run | — |
| `LONG_RUN_S` | 16 h | `append_runner.py:126` | — | The run limit for long stages. | run | — |
| `HEARTBEAT_S`, `COORD_TTL_S` | 300 s; 900 s | `append_runner.py:136,487` | — | The merge status refresh interval and the coordinator lease TTL. | run | — |
| `POLL_RETRIES`, `POLL_RETRY_MAX_S` | 5; 120 s | `append_runner.py:1308-1309` | — | Retries of Batch status polls. | run | — |
| `MD5_WORKERS`, `R2_WORKERS` | 16; 32 | `append_runner.py:2210,2349` | — | Concurrency of verification and R2 copies. | run | perf |
| `drill_checkpoint` | unset (gcs: the scratch bucket) | `static_profile.py:83`; `job/static-names.json:51` | `STATIC_NAMES_DRILL_CHECKPOINT`, `drill build -S` | Where a run's drill saves each step, so a reclaimed spot task resumes after its last saved step. | run | perf |

## Serving (site `functions/_lib/`)

| Name | Default | Defined | Override | What the reader does with it | Bytes? |
|---|---|---|---|---|---|
| `MAX_ROWS` (filter) | 100,000 + 2·8,192 | `staticFilter.ts:107` | `opts.maxRows` | A literal whose suffix range is above this, counted at row-group granularity, is not read on the light path; it goes to `heavy` (the drill). Hardcoded to `V` = 100K, not read from `catalog/meta.json`. | answers |
| `defaultMaxRows` | `min(2·MAX_ROWS, MAX_ROWS + Σ runs' edge rows)` | `staticRuns.ts:269-273`; used at `staticFilter.ts:151` | — | Each run tier can add up to two groups of nonmatching boundary rows. The allowance grows by the actual edge rows, capped at twice the base allowance. | answers |
| `HELD_HITS` | 400,000 | `staticFilter.ts:110` | — | First hits held per isolate across literals. The oldest literal is dropped past this. | — |
| Drill bound (`DRILL_RULES.bound`) | `R + 2·Σ tiers' rg` | `staticDrill.ts:408` (Python mirror `static_drill.py:779`) | `drill/meta.json` `R`, `rg` | For a heavy literal at `P`: if the selected root groups across tiers hold ≤ bound rows, the reader returns the match roots. Otherwise it serves the heavy directory's rollup: K named children plus the `(other)` remainder. `R` is taken from the base tier (`metas[0]`). | answers |
| Anchors bound | `R + 4·Σ tiers' rg` | `staticAnchors.ts:474` (Python `static_anchors.py:1800`) | `anchors/meta.json` `R`, `rg` | The same split as the drill bound, for a scoped `q$` / `^q$`. | answers |
| `START_MAX_ROWS` | 400,000 + 2·8,192 | `staticAnchors.ts:65` | `opts.startMaxRows` | The whole-range or scoped read bound for `^q` and path terms. Past it the term is `term-too-common`. | answers |
| `START_MAX_HITS` | 150,000 | `staticAnchors.ts:69` | `opts.startMaxHits` | A `^q` read is abandoned past this many first hits. A whole-range read then counts as heavy; a scoped one is declined. | answers |
| `PATH_TAIL_MIN` | prefix 2, exact 1 | `indexedOnly.ts:89` | — | The shortest path-term tail with a name-index range (`slash-tail-short`). | answers |
| `MAX_SHARDS` | 8 | `staticNames.ts:90` (`maxShards` `:434`) | — | The most shards one key selects within. | answers |
| `HELD` (drill, anchors) | 400,000 | `staticDrill.ts:718`; `staticAnchors.ts:264` | — | Hits plus cells held per isolate across views. The oldest view is dropped past this. | — |
| Manifest / state TTL | 300,000 ms | `staticRuns.ts:81`; `staticDrill.ts:571` | `ttlMs` | How long a stack's manifest (tier list) is cached. This is the "5 min cache" that `GRACE_S` must exceed. | — |
| `MAX_ROWS` (`/api/name-summary`) | 400,000 | `nameSummaryStatic.ts:31` | `STATIC_MAX_ROWS` env (`nameSummaryStatic.ts:177`) | A non-member read above this is refused. Membership itself uses `V` from `catalog/meta.json` (`nameSummaryStatic.ts:100`). | answers |
| `RESPONSE_V` | 28 | `staticFilter.ts:425` | — | Version of the cached response shape. Bump it on any change to a static-filter body's shape. | cache keys |
| `FILTER_DETAILS_MS` | `"300"` (wrangler) | `site/wrangler.toml:163` | env | How long a static filter view waits for its roots' own rows before answering without them. | latency |
| `STATIC_GEN` | unset (follows `static-names/CURRENT.json`) | `site/wrangler.toml:60` comment; `auth.ts:128` | env | Pins a generation. | — |

## Interactions

- **R, K, floor, rg must agree across a stack.** The reader takes `R` from the base tier and sums `rg` over every tier (`staticDrill.ts:408`, `staticAnchors.ts:474`). Each run's rollups use the K it was built with, and incremental compaction folds those cells forward. A run built with a different R or K than its base would yield a stack whose heavy/light split and kept-children sets disagree between tiers. Since `72a6c427`, runs (local and Batch) take R/K/floor from the base, a drill carry refuses mixed R/K inputs, and a compaction at another K measures every root.
- **`floor ≤ R + 1`** is required for incremental compaction (`static_measure.py:148`). Above it, the compaction falls back to measuring every root.
- **V and the light bound.** The build's `V` (`catalog/meta.json`) and `staticFilter.ts` `MAX_ROWS` (`100,000 + 2·SX_RG`) must agree: every non-member's range has to fit the light read. The filter hardcodes 100K. `nameSummaryStatic.ts` reads `V` from the meta, but its refusal cap is a separate 400K.
- **Slack groups.** The light path, drill and anchors bounds all assume suffix groups of `SX_RG` = 8,192 rows (`2 * 8192` in `staticFilter.ts:107`, `staticAnchors.ts:65`, `static_anchors.py:67,70`). Run drills use `RUN_RG` = 2,048 so that 2·`rg` per tier stays small. Anchors record `dispatch_slack_groups: 4`, but both readers hardcode the 4. Drill `meta.json` records `dispatch_rows` (one tier's `R + 2·rg`), but the TS reader recomputes the bound over all tiers.
- **hex_runs** must be the same for the base and every run. The runner refuses a profile rule that differs from the generation's (`static_runner.py:131`). The reader marks a run whose `catalog/meta.json` rule disagrees as unusable (`staticRuns.ts:153-159`).
- **Python/TS mirrors** that must be changed together:
  - `MAX_ROWS`, `START_MAX_ROWS`, `START_MAX_HITS` (`static_anchors.py:67-73` ↔ `staticFilter.ts:107`, `staticAnchors.ts:65,69`)
  - `MAX_SHARDS` (`static_anchors.py:1547` ↔ `staticNames.ts:90`)
  - the drill and anchors bounds (`static_drill.py:779`/`static_anchors.py:1800` ↔ `staticDrill.ts:408`/`staticAnchors.ts:474`)
- **Duplicated defaults.**
  - R = 100,000 is hardcoded in about 9 places.
  - K = 256 is hardcoded in about 7: `static_local.py:72`, `static_anchors.py:65`, `static_drill.py:2437`, `static_roots.py:970,1237,1310`, plus the anchors CLIs, which use `K_DEFAULT`.
  - floor = 10,000 is hardcoded in 4 places (`static_local.py:72`, `static_drill.py:2431`, `static_roots.py:677,761`).
  - Chunk-row defaults disagree: 1 << 22 locally and in compaction, 1 << 24 for the Batch drill build, merges and the catalog. Output bytes are not affected.
  - The heavy bars use different floors: measurement 1 << 25, anchors 1 << 24.
- **`-K` means different things.** It is `--keep` (K) on `static-names build`, `drill build`, `roots …` and `anchors …`, but `--compact-now` on `static-names add`, `static_merge compact` and `runs add`. The compaction commands' `keep` override is `-k`.
- **Env vars do not reach Batch helpers.** `$DT_MEASURE_HEAVY_X`, `$DT_MEASURE_HEAVY_ROWS`, `$DT_MEASURE_THREADS` and `$DT_ANCHOR_HEAVY_X` are read in-process, and the `*_X` pair at import time. `job_spec` forwards only `STATIC_NAMES_*`, R2 and record vars (`append_runner.py:1239-1250`), so setting them on the submitter does not reach Batch tasks or fan-out helpers. The anchors' heavy bar is computed on the compaction VM and travels in the plan. The measurement bar is computed in each process.
- **K changes at compaction** (`2a42f9e0`): `-k` / profile `keep` sets the new generation's drill and anchors K; the measurement then runs over every root, and the checkpoint is keyed by the K (a race seeded across a K change copies only steps through `fold/spilled`). Runs appended afterwards follow the new generation's K.

[static-compaction]: static-compaction.md
