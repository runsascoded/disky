# Static name search: rare terms without a server

Can the below-catalog ("rare term") name search run from static files on R2/GCS, read by a Worker, with interactive latency and a bounded worst case? Measured 2026-10-08 on the consolidated store (70 scans, 2026-07-30 → 2026-10-08). **Yes, with a suffix-ordered, denormalized postings layout**: every rare query is one contiguous byte range of at most a few MB, exact on every date, and the only server work left is the daily build. With the **static catalog** (gen `2026-10-08c`, below) every `/names` literal on every scan of the generation is answered without the query box: one- and two-character literals and literals whose suffix range exceeds V = 100K rows from a 25 MB catalog, everything else from the suffix shards (≤ V rows read). The members' **map-filter drilldown** (the filtered view at any path, any date) reads their match roots or, for the few heavy directories, per-child rollups: ≤ ~116K rows per drill, verified 2,270/2,270 against brute force ("Drilldown for heavy terms").

## The query

Unchanged from `mega_names.answer`, because its first-hit rule is per row: a postings row (one version of one owner slice of a path) counts toward literal `q` on scan `D` when

1. its lowercase basename contains `q`;
2. it is live on `D`: `since(D) ≤ vf ≤ D < vt` (`vt` = its closure, 2106 while open; `since` = the scan's epoch start);
3. its lowercase parent path does not contain `q` (a matching ancestor already covers it);

and the answer is `sum(size)`, `sum(n_files)` per first path segment (bucket). Nothing joins rows to each other, so a reader that holds exactly the rows whose name contains `q` can answer any date by filtering.

## Why today's layout is slow cold

`m_nodes` is sorted by name. A rare term matches thousands to ~100K names scattered across 18 GB, so a cold query touches about one 256-row granule per name (see `mega-index.md`, "Below-T cost"). Granules of `m_nodes` the current plan touches, against the rows the answer needs:

| Term | Matching names | Rows needed (versions × occurrences) | Granules touched today | Smallest trigram list (names) |
|---|---:|---:|---:|---:|
| `gof` | 9,194 | 9,451 | 204 | 9,194 |
| `0.0.73` | 33 | 31,125 | 124 | 5,111 |
| `116.tok` | 1 | 26,226 | 103 | 6,120 |
| `11979` | 3,747 | 14,315 | 808 | 469,073 |
| `bb-` | 56,635 | 123,716 | 32,921 | 56,635 |
| `5418` | 33,932 | 109,281 | 14,639 | 514,133 |
| `48.parquet` | 42,402 | 104,216 | 37,427 | 495,135 |
| `nk080` | 96,312 | 96,724 | 69,936 | 849,154 |
| `rt-0003` | 17,636 | 166,608 | 888 | 1,817,184 |
| `s__marin-us-centr` | 1,830 | 228,729 | 894 | 7,653 |

(Full table for the 27 T-curve and probe terms: `job/ch-store/static-name-term-stats.sh` over `static-name-terms.txt`.) `nk080` needs 97K rows and reads ~70K granules (~18M rows); the cost is the scatter, not the result.

## The layout: suffix-ordered postings

One row per (suffix position of a name, postings version of that name): `(s, depth, path, usr, vf, vt, size, n_files)`, sorted by `s`, where `s` is the name's lowercase suffix from that position. Only suffixes of three or more characters are stored (one- and two-character literals are always in the catalog). The rows for literal `q` are exactly the contiguous range of suffixes starting with `q`; a name containing `q` twice contributes two rows of the same version, which the reader dedups by `(path, usr, vf)`.

Closures are folded in as `vt` (the version's interval `[vf, vt)`), so there is no second table and no join.

### Measured (prototype)

Prototype: every suffix starting with `541`, `nk0`, `48.` or `gof` (complete for any query starting with those three characters): 31.0M rows, built in ~105 s on the 32-vCPU VM (`static-name-proto.sql`), exported as zstd parquet, read from GCS by `static-name-query.py`, which fetches the footer once (in production a sidecar, below) and then **one ranged GET per query**:

| Term | Row groups (4K rows) | Bytes read | Rows read | Rows matching | Fetch (laptop → GCS) | Filter + sum (Python) | Exact on 4 dates |
|---|---:|---:|---:|---:|---:|---:|---|
| `5418` | 25 | 1.40 MB | 110,686 | 109,281 | 0.28 s | 0.62 s | yes |
| `nk080` | 24 | 3.07 MB | 104,658 | 96,724 | 0.33 s | 0.66 s | yes |
| `48.parquet` | 24 | 1.28 MB | 108,920 | 104,216 | 0.32 s | 0.59 s | yes |
| `gof` | 3 | 0.17 MB | 13,083 | 9,451 | 0.19 s | 0.07 s | yes |
| `54181` | 3 | 0.18 MB | 13,083 | 6,274 | 0.21 s | 0.07 s | yes |
| `48.parquet.crc` | 1 | 0.03 MB | 4,579 | 0 | 0.09 s | 0.01 s | yes |

"Exact" = equal per-bucket bytes and objects to `mega_names.answer(..., postings='m')` on 2026-10-06, 10-01, 09-15 and 08-15 (24/24). Today's ClickHouse path takes 3.8–3.9 s cold for `nk080`/`48.parquet` and over 5 s for `5418`. With 16K-row groups the over-read is larger (`gof` 0.35 MB) and the footer 5× smaller; 4–8K rows per group is the sweet spot.

### Full-scale size

From `name_spans` (all 139.6M names, 1.15B versions):

| | |
|---|---:|
| Name characters | 5.89B (mean 42, p99 125) |
| Suffix positions ≥ 3 characters | 5.61B |
| Suffix rows (Σ positions × versions) | **16.5B** (14.3 per version) |
| Parquet zstd, measured on the prototype | 25–28 B/row |
| **Estimated total** | ~410–460 GB at the prototype's 25–28 B/row; **built: 150.6 GB** (9.1 B/row, pyarrow zstd) |
| R2 storage at $0.015/GB-month | ~$2.3/month (as built) |

Almost all of each row is the `path` string. Storing a parent-path id with a dictionary would shrink it, but needs a second lookup for the first-hit test; not worth it at these prices.

### Bounding every query

The read for `q` is `Σ over names containing q of versions × occurrences` rows, all time. The cost-weighted census (`ch-mega-catalog-build -w rows`, `mega-index.md`) registers exactly the literals at or above a weight `V`; with the weight defined as these suffix-range rows, **every literal outside the catalog reads fewer than `V` rows**, about `V × 28` bytes: 2.8 MB at `V` = 100K, one range read. Two details:

- **Long queries.** The prototype truncates `s` to 24 characters; a non-member `q` longer than 24 could have a common 24-character prefix and an unbounded range. Store the full suffix (or ≥ 128 characters; names p99 is 125), which sorted zstd compresses well, so the range is always exactly `q`'s.
- **Multiplicity.** The weight counts a name containing `q` twice as two rows, matching what the range holds.

### Reading it from a Worker

- **Sidecar.** At 4K rows per group, 16.5B rows is ~4M row groups; their parquet footer metadata is ~1.3 KB each (measured), so files are split by suffix prefix (e.g. first two characters, a few hundred files of ~1–2 GB) and the per-group `(s_min, s_max, file, byte range)` index goes to D1 (or KV shards). That is the same footers-in-D1 pattern the path store already uses (`index-sync`), so a query is: one D1 lookup (the group range for `q`) → one R2 ranged GET (≤ a few MB) → decode, filter, sum.
- **Decode.** ~100K rows / 3 MB compressed decodes to ~25 MB with hyparquet, within a Worker's 128 MB; the CPU is the filter over ~100K rows (the prototype's 0.6 s is pure Python). Lambda or Cloud Run are not needed.
- **Worker → R2, measured 2026-10-08** (dev Pages Functions `/api/static-bench`, colo EWR, bucket `oa-gcs-usage-index` in ENAM; the prototype files under `proto/static/`):
  - one ranged GET, 56 KB–3 MB: first touch 130–840 ms, repeat typically 60–280 ms (outliers to ~840 ms); 4–6 ranges issued in parallel: ~230 ms total;
  - hyparquet decode (fzstd, 6 columns) of ~100–125K rows (`5418`, `48.parquet`, `nk080`): typically 150–430 ms, with spikes to 1.2–1.7 s; 4–22K rows: 10–75 ms;
  - footer parse: 9.0 MB (4K-row groups) takes ~0.9 s, 1.8 MB (16K) 0.2 s, which is why production reads the per-group index from D1 and never parses a footer;
  - so a ~100K-row term is ~0.3–0.7 s end to end (vs 3.8–5 s cold on ClickHouse); decode, not the fetch, dominates, and it scales with `V`.

### Daily upkeep

LSM-style, no server:

- **Delta files.** Each scan's opened versions (~2M/day) become suffix rows (×14.3 ≈ 30M rows, ~0.8 GB) in a small sorted delta file with its own sidecar; its closures (~1M/day, ≈15M suffix rows) are `(s, path, usr, vf, vt)` close records in the same file. A query reads its range in the base and in each delta (independent reads, issued in parallel, not dependent), and applies close records over base rows.
- **Compaction.** Weekly, merge deltas into the base for the affected prefixes (a sorted merge; DuckDB on GCP Batch), keeping at most ~7 deltas.
- **Inputs.** The day's opened and closed versions are what `ch-ingest` already computes; the same diff can come from consecutive path-index files with DuckDB on Batch, so ClickHouse is not required for upkeep either.
- **Initial build.** The prototype's 31M rows took ~105 s; the full 16.5B is ~530× that, roughly 3–6 hours of sort and export on the 32-vCPU VM or a Batch job, once.

## Pipeline: rebuilt from primary sources on GCP Batch

`dt-cloud static-names` (`cloud/src/dt_cloud/static_names.py`), driven by `job/static-names.sh` (us-east1, beside the data; the gcs job image pinned by digest; this checkout's committed `dt_cloud` tree staged content-addressed under `gs://oa-gcs-usage-dvx/static-names/src/<tree>/`), tracked by DVX stages in `static-names/<gen>/`. Nothing reads ClickHouse except the verification stages.

| Stage (DVX out) | What | Where it runs |
|---|---|---|
| `scans.json` | each scan date's newest `path` sort (`listing/<date>/[index/<gen>/]path-index.parquet`, the rule `ch-ingest` uses), pinned by GCS generation, size, md5/crc32c, with its source format (61 v1, 9 v2) | laptop (footer reads) |
| `ranges.json` | 256 `(depth, path)` key ranges of about equal input rows (row-group starts of the newest v1 and v2 scans, weighted by scan count), each a few conjunctive filters so DuckDB prunes every scan's row groups | laptop |
| `intervals.json` | per range: every scan's rows in the range, merged per key as `ch-ingest` merges them, then pyrmts' gaps-and-islands (a new version when any value changes, the weighted mean stamp compared to the second with banker's rounding, or the key was absent in between) → `intervals/r####.parquet` (`depth, path, usr, vf, vt` + values, sorted), `hist/` (suffix rows per 3-character prefix) and `digest/` (per scan: opened/closed counts and md5 sums) | Batch: 32 tasks × 8 ranges |
| `verify-intervals.json` | per scan, opened and closed counts and order-insensitive digests vs ClickHouse `m_nodes`/`m_closures` | the ch-store VM, read-only |
| `shards.json` | shard plan from the histograms: runs of 3-character prefixes of ≤ 50M suffix rows (a prefix is never split, so a literal's range is in one file), in 32 contiguous task groups | laptop |
| `suffixes.json` | `suffix-map` expands each range's intervals once into `(s, …, shard)` rows written per reduce task; `shards` partitions its task's rows by shard and writes each sorted `(s, path, usr, vf)` as `sx/s####.parquet` (8K-row groups, zstd, `usr` dictionary) plus its sidecar; `sidecar` concatenates `sidecar.parquet` `(file, rg, s_min, s_max, offset, length, rows)` (from the rows written, not truncated statistics) and checks the shards don't overlap | Batch: 32 + 32 tasks |
| `verify-answers.json` | `query` (sidecar → one ranged read per file → first-hit filter → per-bucket sums) vs `mega_names.answer(…, postings='m')` per (term, date) | laptop + VM |
| `r2.dvc` (side effect) | `r2-copy`: `sx/`, `sidecar/`, `sidecar.parquet`, `shards.json`, `scans.json` (and, from gen `2026-10-08c`, `catalog/{cells,index}.parquet`, `catalog/{meta,members}.json`) → R2 `oa-gcs-usage-index` under the same keys, skipping what is there with the same size and md5 | the ch-store VM (holds the R2 keys), `nice`d |

Outputs: `gs://oa-gcs-usage-dvx/static-names/<gen>/{scans,ranges,shards}.json, intervals/, hist/, digest/, sx/, sidecar/, sidecar.parquet`; the shuffle (`sxmap/`, 180 GB for 2026-10-08, and its `sxmap-done/` markers) goes to the scratch bucket `gs://oa-gcs-usage-scratch/static-names/<gen>/` (us-east1, no soft delete, objects deleted at 7 days; mounted beside the data bucket in every Batch task), so deleting it never bills soft-deleted bytes (the suffixes stage reruns map and reduce together). Everything is written through pyarrow in fixed row-group sizes under a total sort order, so a rerun over the same inputs and image is byte-identical. `append -f GEN` adds the next scan to a range's intervals (open versions × the scan, a full join) and writes the day's opened/closed versions as `delta/<date>/r####.parquet`, the daily delta source.

Run: `cd static-names/<gen> && PATH=$REPO/.venv/bin:$PATH dvx run verify-answers.json.dvc` (then `r2.dvc`).

### Validation runs (2026-10-08)

- **Local** (`cloud/tests/test_static_names.py`): intervals equal a sequential-ingest oracle (v1/v2 mix, duplicate v1 rows, absence gaps, rounding ties) at 1 and 4 ranges; ranges partition the key space; `append` is byte-identical to a rebuild (intervals and histogram files) with matching digests and delta; suffix rows equal brute force; the reader's per-bucket first-hit answers equal brute force for every date; the restated kernel equals pyrmts' `_intervals_sql`.
- **Intervals, real data**: ranges 0, 100, 200 (one task, n2-highmem-16) — 7.59M, 2.92M and 3.03M intervals in 383 s (first range: includes parsing all 70 footers), 247 s and 232 s. Per scan opened and closed counts and digests **equal ClickHouse on all 70 scans for all three ranges** (`nodes`/`closures` restricted to each range).
- **Append, real data**: ranges 0 and 100 built over 69 scans then appended 10-08 (range 0: 1,822 opened, 1,557 closed) are byte-identical (md5), intervals and histograms, to the 70-scan build.
- **Reproducibility**: the dev shards built twice by two code paths (one pass per task; then the map/reduce shuffle) are byte-identical, all 11 files and the sidecar.
- **Suffix shards, real data** (the three ranges' intervals, 13.5M versions → 513M suffix rows, 11 shards): 50M-row shards sort and write in ~55 s each; files are 12–17.6 B/row (pyarrow zstd), 6.9 GB for 513M rows; sidecar 62,674 row groups, 2.6 MB.

### Full build, 2026-10-08 (gen `2026-10-08`, 70 scans 07-30 → 10-08)

**Intervals**: one Batch job, 32 spot n2-highmem-16 tasks × 8 ranges, 38 min wall (2,291 s); 256 ranges at 150–841 s (median 193 s), 14.9 task-hours ≈ **$5 spot** (~$17 on demand). 1,153,480,980 intervals, 11.1 GB (9.6 B/version). **Verification: every scan's opened (1,153,480,980 in all) and closed (545,152,608) versions equal ClickHouse `m_nodes`/`m_closures` exactly, counts and digests, all 70 scans** (`verify-intervals.json`, 60 s on the VM).

**Suffix shards**: `suffix-map` (32 spot tasks × 8 ranges) 33 min wall (6,238 s of mapping in all; one range, 1,042 s, set the tail: tasks take 8 consecutive ranges, so the long-named podcast ranges cluster), then `shards` (32 spot tasks) 14 min wall; plan + sidecar merge on the laptop in seconds. **16,501,534,337 suffix rows** (= the plan's histogram total and the map's row count), **322 files, 150.6 GB (9.1 B/row)**, largest 1.47 GB (prefix `son`, 232M rows, the one prefix over the 50M target), 2,014,505 row groups; `sidecar.parquet` 38.9 MB. Batch for the whole build: at most ~45 spot VM-hours (32 VMs × each job's wall time, an upper bound) ≈ **$10–15**, plus ~3 on-demand VM-hours of dev runs.

**Answers**: `verify-answers.json`: **259 of 259 (term, date) pairs equal** `mega_names.answer(…, postings='m')` bucket by bucket (bytes and objects; buckets with zero on both sides omitted) — 37 literals (`5418`, `nk080`, `48.parquet`, `gof`, `11979`, `pio`, `54181`, `48.parquet.crc`, `0.0.73`, `116.tok`, `bb-`, `rt-0003`, `s__marin-us-centr`, `xican`, `_hypocris`, and 22 random 4–8-character substrings of hash-sampled names, `job/static-names/terms.txt`) on 2026-10-08, 10-06, 10-01, 09-30, 09-15, 08-15 and 07-30. Every literal's rows were one ranged read of one file: median 0.39 MB, max 3.07 MB (`nk080`); e.g. `5418` 15 row groups / 1.44 MB / 122,880 rows read for 109,281 matching, `s__marin-us-centr` 28 / 1.13 MB / 229,376 for 228,729, `48.parquet.crc` 1 / 65 KB.

**R2**: `r2.dvc` copied `sx/`, `sidecar.parquet`, `shards.json`, `scans.json` (325 objects, 150.6 GB) to `oa-gcs-usage-index` under `static-names/2026-10-08/` from the ch-store VM in 21 min (119 MB/s, 8 streams, `nice`d; the VM's load stayed under 1); a rerun finds 0 to copy (size and GCS md5 match). Egress ≈ 150 GB × ~$0.12 ≈ $18.

Gotcha: DVX `git_deps` make a stage stale when the file changes. The Batch driver (`job/static-names.sh`) was a git dep of the intervals/suffix stages, and a one-line fix to it made a later `dvx run r2.dvc` start rebuilding from intervals (it was caught after an all-skip intervals job). Only `static_names.py` is a content dep now; the image pin rides in the driver's committed text.

### Extrapolation written before the full build

- Intervals: 256 ranges × ~250 s ≈ 18 task-hours of n2-highmem-16 → 32 tasks ≈ 35–45 min wall; ~$20 on demand, ~$6 spot. Output ~10–15 GB (8 B/version measured).
- Suffix rows: the three dev ranges average 38 suffix rows per version (long podcast names); `name_spans` gives 16.5B in all. Files at 12–15 B/row → **~200–250 GB** (the prototype's ClickHouse export was 25–28 B/row; the estimate above of 410–460 GB is superseded).
- Suffix map: one expansion of all 1.15B versions; reduce: ~330 shards × ~1 min sort/write plus each task's partition pass, 32 tasks ≈ 30–45 min wall; ~$15–30 on demand.
- R2: ~200–250 GB at GCS egress ≈ $25–30 once; ~$3–4/month stored.

## Coalesced versions (gen `2026-10-08c`)

A name answer reads a version's key (`depth, path, usr`), liveness and two values, `size` and `n_files`; the intervals open a version on any change (`last_read`, the mean stamp, storage classes, …). `coalesce` merges a key's adjacent versions (`vt` = the next `vf`) while `(size, n_files)` hold, so the answers are unchanged (tested: shards over coalesced versions answer exactly as brute force over every version). It is a function of the intervals, so `2026-10-08c` reuses the verified `2026-10-08` intervals and the intervals' append-equals-rebuild property carries over: `coalesce_append` turns the intervals' daily `delta/` into the coalesced versions and their `cdelta/<date>/` (a key closed and reopened with the same values continues its version), byte-identical to coalescing the appended intervals (`test_coalesce_append_equals_rebuild`).

Measured over all 256 ranges (`coalesce-report.json`; `coalesce` stage 8 min on 32 spot tasks):

| | every change | `(size, n_files)` changes | ratio |
|---|---:|---:|---:|
| versions | 1,153,480,980 | 797,994,947 | 0.692 |
| suffix rows | 16,501,534,337 | 13,235,105,643 | 0.802 |
| 3-character prefixes ≥ 100K / 150K / 250K rows | 8,800 / 7,751 / 6,855 | 8,229 / 7,461 / 4,270 | |
| shard files, bytes | 322, 150.6 GB | 262, 135.7 GB | 0.90 |

Per named term (range rows): `s__marin-us-centr` 228,729 → 95,788; `1443` 182,473 → 143,039; `rt-0003` 166,608 → 111,055; `4431` 134,816 → 97,249; `bb-` 123,716 → 85,454; `5418` 109,281 → 80,049; `6437` 97,579 → 75,852; `00241` 52,833 → 42,361; `04521` 13,015 → 8,255; long-named podcast terms (`_(ep` 235,505, `our_3_` 230,436, `y_ba` 220,584, `nk080`, `pio`, `48.parquet`) are unchanged (one version per file). Total rows fall 19.8%, and the literals that are slow because their paths carry many versions fall 25–58%: 4 of the 10 named terms between 100K and 250K rows move under 100K. Adopted: `2026-10-08c` is the coalesced generation (shards: 13,235,105,643 rows, 262 files, 135.7 GB, 1,615,743 row groups, sidecar 30.0 MB; coalesce 8 min + map/shards 46 min on 32 spot tasks).

## Static catalog: every query without the box (gen `2026-10-08c`)

`dt-cloud static-names catalog …` (`cloud/src/dt_cloud/static_catalog.py`, run on Batch as `MODULE=static_catalog job/static-names.sh run …`).

### Membership

A lowercase literal `q` is a **member** iff

- it has one or two characters and occurs in some name (depth ≥ 1) of some version, or
- its suffix range (rows of the shards whose `s` starts with `q`: Σ over versions of `q`'s occurrences in the name, overlapping ones included) holds more than **V = 100,000** rows.

Everything else is answered by the static reader, which then reads at most V rows (≤ V + 2 × 8,192 counting whole row groups). Properties:

- **Factor-closed.** A member's prefixes and substrings of ≥ 3 characters are members (their ranges contain its rows one for one). So the members are found in one pass per shard over its sorted suffixes, level by level (`census`): the length-`L` prefixes with more than V rows, then, among their rows only, length `L + 1`. No separate census over names is needed.
- **Monotone.** Rows are never removed (a closure only sets `vt`), so appending scans only grows ranges: once a member, always a member. Membership at a generation is a function of its scans alone, so an append equals a rebuild.
- **No length cap.** Suffixes are stored whole and members are untruncated: at V = 100K the longest member has 39 characters and 929 members are longer than 24 (e.g. `xp_sft_qwen3_4b_selfinst`, 101,007 rows). The Worker's lookup is by the full lowercased literal.
- **Bucket names need no special case.** Buckets are depth-1 rows (parent `''`), so they are suffix rows like any name: `east5` on 2026-10-08 is the whole of `marin-us-east5` (842 PB) plus other buckets' first hits, from the static reader, brute force and ClickHouse alike. The Worker's `bucket-name` skip (it assumed bucket names were depth 0) can go.

**Choosing V.** The census kept every prefix with ≥ 50K rows (159,970 of them), so V can move without recounting:

| V (rows) | long members | Σ their range rows | longer than 24 characters | longest |
|---:|---:|---:|---:|---:|
| 50K | 159,959 | 90.1B | 4,646 | 67 |
| 75K | 103,918 | 86.7B | 1,213 | 46 |
| **100K** | **82,616** | **84.9B** | **929** | **39** |
| 125K | 58,458 | 82.2B | 441 | 29 |
| 150K | 49,685 | 81.0B | 111 | 28 |
| 250K | 31,985 | 77.5B | 80 | 28 |

The Worker's decode cost is the bound that matters: ≤ 100K rows decode in ≤ 0.5 s, 106–139K in 0.4–1.0 s, 172–246K in 0.8–3.4 s. A non-member at V = 100K reads ≤ ~116K rows. Decoded bytes per row vary little at this size: over the ranges of 80–100K rows the decoded size (`s` + `path` + `usr` + fixed columns) is 180 B/row at the median, 270 at p90 and 411 at the max (16.6 / 24.4 / 39.4 MB per range), a 2.3× spread, so a row bound is within ~2.3× of a byte bound and simpler for the Worker to reason about; the census also records `ubytes`, and `members -B` adds a byte bound if the Worker's measurements call for it. Lowering V later re-runs only `members` → `catalog` (the census holds down to 50K).

### Answers

Per member and date `D`, per bucket: Σ `size`, `n_files` over first hits live on `D` (`vf ≤ D < vt`, depth ≥ 1, the name contains `q`, the lowercase parent path does not) — exactly `Reader.answer` and `mega_names.answer`. The answers stage walks a shard's rows level by level as the census does, and for each member prefix `q` of a row emits the row as two events, `+(size, n_files)` at `vf` and `−` at `vt` (none while open), counted only on the version's first occurrence of `q` (`instr(name, q)` = the row's position, so no dedup state). Events summed per `(q, bucket, t)` and accumulated in time order give the cells `(q, bucket, vf, b, o)`, one per change (the shape of the ClickHouse catalog's `catalog_cells`). One- and two-character literals come from the versions directly (`short`: each distinct character and pair of a name that its parent lacks), per key range, summed across ranges in `assemble`. Events are additive, so a shard is fed in chunks of row groups cut on `(s, path)` (one suffix can fill a shard: `s0124` is 99.8% `ccess`, from `_SUCCESS` markers).

### Layout

`gs://oa-gcs-usage-dvx/static-names/2026-10-08c/catalog/` (and R2 `oa-gcs-usage-index`, same keys):

| Key | What |
|---|---|
| `cells.parquet` | `q` string, `bucket` string (dictionary), `vf` int64 (epoch seconds), `b`, `o` int64; zstd; sorted `(q, bucket, vf)` in code-point order; 4,096-row groups. Each member is led by one header row `(q, '', 0, rows, n)`: `rows` = its range rows (−1 for one or two characters), `n` = the cell rows that follow. A bucket's value on `D` is its newest cell with `vf ≤ D` (none: zero). |
| `index.parquet` | per row group of `cells.parquet`: `rg` int32, `q_min`, `q_max` (its exact first and last `q`), `offset`, `length` (its contiguous byte span), `rows` int32, `chunks` list<int64> (per column in file order `q, bucket, vf, b, o`: `data_page_offset, total_compressed_size, dictionary_page_offset` or 0 — what `decodeGroup` needs, so no footer is parsed) |
| `meta.json` | `gen`, `cells_rows`, `row_groups`, `members_short`, `members_long`, `max_len`, `bytes`, `index_bytes`, `cell_rg`, `membership.max_rows` (= V) |
| `members.json` | the membership summary (V, counts by length) |

Measured: **3,073,977 cell rows (82,616 long members + 16,897 one- and two-character literals, headers included), 25.2 MB in 751 row groups; `index.parquet` 59 KB.** A member holds at most 420 cells here; a lookup reads one or two row groups (~34 KB each). That is 0.02% of the shards' 135.7 GB.

### How the Worker should consume it

1. Per isolate (Cache API between isolates, like the shard group indexes): `catalog/meta.json` (V) and `catalog/index.parquet` (751 rows, 59 KB).
2. `q` = the lowercased literal (`datedNameRequest`), any length ≤ 512, never truncated.
3. **One or two characters** (`[...q].length ≤ 2`): the catalog, always. Not found → no name ever contained `q`: zero for every bucket on every date (exact).
4. **Three or more**: the shard group index first (as today). If its selected groups hold ≤ V rows, `q` cannot be a member (its exact range is no larger): read it statically. Otherwise look it up in the catalog: found → its cells; not found → a non-member whose exact range is ≤ V, so the static read is bounded (≤ V + 2 × 8,192 rows).
5. **Lookup:** groups `[a, b)` with `a` = first `g` with `q_max[g] ≥ q`, `b` = first `g` with `q_min[g] > q` (code-point `cmp`); `a == b` → not a member. Else one ranged GET of `cells.parquet` `[offset[a], offset[b−1] + length[b−1])`, decode those groups from `chunks` (columns `q` BYTE_ARRAY UTF8, `bucket` BYTE_ARRAY UTF8 dictionary-encoded, `vf`, `b`, `o` INT64; ZSTD), take the rows with `q` equal to the literal: the first must be the header `(q, '')`, else not a member.
6. **Answer** for each requested date: `D` = the scan id as epoch seconds (`scanMs / 1000`); per bucket the last cell with `vf ≤ D`; absent buckets are zero. Diffs (`from`) read the same cells at both dates.
7. The registry's `threshold_paths` is no longer needed for dispatch; any scan in the generation's `scans.json` is answerable. Today the Worker sends frozen and daily scans to the box (their per-scan indexes); their answers were not compared here, so moving them to the static path is a separate decision.
8. Requests for a scan newer than the generation still need the box until the daily append is wired (below).

### Daily append

`static_catalog.append(prev, base, deltas, V)` (with `static_names.coalesce_append` making the coalesced `cdelta/<date>/` from the intervals' `append` delta): the catalog of a base generation's shards plus the scans since, kept LSM-style beside the shards' own deltas.

- **Membership.** A prefix can cross V only if the base's row-group upper bound (from the sidecar) plus the deltas' opened suffix rows exceeds V; those candidates are found level by level over the deltas' suffix rows (O(delta)), and the ones not yet members are decided by an exact base count (only the two edge row groups' `s` column decoded).
- **Cells.** Existing members and every one- and two-character literal get the new scan's events (opened `+` at `D`, closed `−` at `D`); a new member's cells are built from its whole history (the base shards' rows in its range, ≤ V by definition, plus every delta's opens and closes); a short literal first seen gets its first cells. The catalog file is rewritten (it is small: 25 MB).
- **Equal to a rebuild:** `test_append_equals_rebuild` appends one and two scans at V = 2 and 5 (with literals crossing V on an append) and compares `cells.parquet` and `index.parquet` byte for byte with the catalog rebuilt over every scan.
- Not wired yet: a `catalog append` CLI/Batch stage after the daily scan, and the shards' own delta files and close records (the Worker reads only base shards today). Until then each new scan means a new generation (≈ the build below).

### Build and cost (2026-10-08c)

| Stage | Wall | Batch |
|---|---:|---|
| `census` (every prefix ≥ 50K rows; `s` and string lengths only) | 5 min | 32 spot n2-highmem-16 |
| `members` (V = 100K) | 28 s | laptop |
| `catalog-short` (1–2 character literals per key range) | 11 min | 32 spot × 8 ranges |
| `catalog-cells` (members' answers per shard) | first run ~3.6 h (see below); with the fix, 5.8 min for the 24 shards then left | 32 spot (+ a 24-task queue job) |
| `catalog` (assemble + index) | 2.5 min | 1 spot |

Spot n2-highmem-16 throughout. The whole 2026-10-08c build (coalesce, map/shards, census, short, answers, assemble, brute force) was ~165 spot VM-hours ≈ **$35–40**, of which ~115 VM-hours (~$27) were the first, slow answers run; at the fixed speed the catalog stages are ~10 VM-hours (~$3) and a whole generation from the intervals ~45 VM-hours (~$10). R2: `r2.dvc` copied the generation's 531 served objects (shards, sidecars, plan, scans, catalog; 135.7 GB) to `oa-gcs-usage-index` `static-names/2026-10-08c/` from the ch-store VM in 20.4 min; egress ≈ 136 GB × $0.12 ≈ $16.

**Why the first answers run was slow** (profiled on `s0058`, 50M rows, 650 members, 1,302 s): reading the shard's row groups with pyarrow through the gcsfuse mount was 1,070 s (one small GCS read per column chunk), deriving the lowercase name and parent (`regexp_extract`) 180 s, and the level loop itself (carry 23 s, events 25 s) under a minute; it was not rescanning per term or rereading row groups per cell (each shard is read once, all its members together). The fix (`e63d76e2`): download the shard to local SSD, read it with DuckDB in `(s, path)`-cut chunks, cut the parent by string slicing (the regex kept only for paths with a newline); 5–6× per shard, cells md5-equal. The worst shard (`s0101`: one 22-level chain of members carrying all 110M rows, 2.16B row-levels) is then bound by the level loop itself (~5.6M row-levels/s); carrying only `(row id, s)` through the levels would be the next lever.

### Verification

Terms (`job/static-names/catalog-terms.txt`, 118, from `catalog-terms.py`): the 37 named build terms, 17 one- and two-character literals, 9 bucket-name literals (`east5`, `us-east1`, `eu-west4`, `central2`, `us-central1`, `marin-us-c`, `west4`, `-us-`, `marin`), 4 hash-sampled members per length 3–12+ (up to 24 characters: `xp_sft_qwen3_4b_selfinst`), and 15 hash-sampled non-members with 80–100K range rows (the static reader's worst cases); dates 2026-10-08, 10-06, 10-01, 09-30, 09-15, 08-15, 07-30.

| Check | Result |
|---|---|
| `verify-catalog.json`: catalog/static dispatch (`catalog query -x`) vs **brute force** straight from each date's scan file (`catalog brute`: the first-hit rule over the scan's rows; v1 and v2 sources; 7 spot tasks, 16 min) | **826 / 826 (term, date) equal**; 65 terms from the catalog, 53 static; every static range ≤ V (max 99,754 rows; at most 106,496 rows / 3.1 MB read) |
| `verify-catalog-ch.json`: the same answers vs ClickHouse `mega_names.answer(…, postings='m')` (the 54 non-short terms with ranges ≤ 250K, plus the 37 named terms' 2026-10-08 answers) | **637 / 637 equal**; brute force and ClickHouse also agree on all 637 |
| `census-check.json`: the census's three-character prefixes (counted from the sorted shards) vs the coalesce stage's histograms (counted from the versions) | equal: 9,891 prefixes ≥ 50K, 13,089,236,781 rows |
| Local (`cloud/tests/test_static_catalog.py`) | census = brute force over every substring; at V = 1, 4, 8 membership is exactly "short and present, or > V rows", every member equals brute force on every date, every non-member ≤ V; answers fed in several chunks; brute-force SQL = the version oracle; append = rebuild byte for byte |

Lookup cost from the laptop (Python, GCS, cold process): a catalog answer 0.10 s median, 0.42 s max, one ranged read of ≤ 2 row groups.

## Drilldown for heavy terms (gen `2026-10-08c`)

The catalog answers a member per bucket. The map filter needs it at **any** path P and date D (or D1 → D2): the term's match roots under P, summed by child of P. A **match root** is a first-hit row (a version of one owner slice of a path, depth ≥ 1, its lowercase name contains `q`, its lowercase parent path does not, live on D). If P's own lowercase path contains `q`, a root at or above P covers the whole subtree and the filtered view is the plain one. Light terms (≤ V range rows) are read from the suffix range directly; this section is the members: the 82,616 long ones and the 16,897 one- and two-character literals. Code: `cloud/src/dt_cloud/static_roots.py` (`dt-cloud static-names roots …`), tests `cloud/tests/test_static_roots.py`.

### Measured: how many roots

`roots measure` (long members, per shard: the catalog's level loop keeping the first-hit rows, aggregated per `(q, path)`, then per directory bottom-up) and `roots measure-short` (short literals, per subtree partition of the coalesced versions); `roots-measure.json` is the report. 32 spot tasks each, ~35 min wall for the long members (~2–3 min a shard); the short pass ~5 min except two partitions holding a giant depth-2 subtree each (2.3B and 4.8B roots, 50 and 40 min). About 12 VM-hours ≈ $3.

| | long members | one- and two-character literals |
|---|---:|---:|
| members | 82,616 | 16,897 |
| root rows (versions × owner slices, all time) | **78,830,388,514** | **12,971,156,040** |
| distinct root paths | 78,827,915,889 | 12,968,888,282 |
| open on 2026-10-08 | 38.5B | 7.84B |
| per member, q50 / q90 / q99 / q99.9 / max | 177,554 / 841,618 / 17.4M / 108M / 228.2M (`son`) | 79 / 17,407 / 18.9M / 117.9M / 361.5M (`.`) |
| members with more than 100K roots | 78,361 | 986 |
| (q, dir) with more than 100K roots under dir, by dir depth 1 / 2 / 3 / 4 / 5 / 6 / 7 / 8 / 9+ | 79,054 / 77,598 / 49,841 / 48,044 / 51,063 / 44,867 / 7,269 / 3,292 / 228 (361,256 in all) | 3,280 / 6,887 / 7,863 / 8,296 / 9,981 / 9,500 / 1,211 / 528 / 56 (47,602) |
| children of those dirs, median (depth ≤ 5) / max | 2–7 / 10,459,665 | 3–57 / 20,850,474 |

Nearly every root is a single-version file, so rows ≈ paths. Top long members by roots: `son` 228.2M, `.jso`/`.json`/`.js`/`jso`/`json` ~227M, `.gz` 117.8M, `.jsonl`/`onl`/`sonl`/`jsonl` ~114M, `cess`/`ess` ~112M, then the `.jsonl.gz` chain (~110M), `_wa`/`war`/`_ex`/`example`… (~108–110M, the `*_warc_examples*` files). Top short: `.` 361.5M, `.j`/`j`/`js` ~227M, `so` 223M, `on` 205M, `z` 194M.

Roots per drill level, for the heaviest (`son`, 228M): all 6 buckets hold more than 100K (median 10.9M); 1,506 depth-2 directories hold some, 54 of them more than 100K; depth 3: 30,035 / 80; depth 4: 134,796 / 123; depth 5: 2.2M / 146; depth 6: 5.2M / 388. `example` (108M) sits in a handful of directories (33 at depth 2, 4 over 100K), `.parquet` (75M) in 140 depth-2 dirs (27 over 100K), `tmp` (13.7M) in 110 (4). So a drill below the first two or three levels is almost always a small read; the heavy directories are a thin top-of-tree layer per member.

**Duplication.** Nested members share roots: `.json`, `.jso`, `json`, `jso` and `.js` have almost the same 227M roots, the `.jsonl.gz` chain eleven identical sets of 109,739,217. Per-member digests of the root set (`roots digest`: count and two order-free sums of row hashes, per shard, 32 spot tasks, 13 min) group the 82,616 long members into **35,915 distinct root sets holding 34,226,403,531 rows** (−57%); the rest are aliases (`drill/aliases.parquet`: `q → canonical`, the set's least member in code-point order; aliases cross shards, e.g. `ch_0121.` → `atch_0121.`). Near-duplicates (`son` vs `.json`: 0.3% apart) are not shared; a base + delta encoding would be the next lever (a crude bound: members bucketed by root count within 1% hold 3.7B rows), at the cost of a second read per drill.

### Design

- **Roots files**, one set per kind: every canonical member's roots `(q, path, usr, vf, vt, size, n_files)` sorted `(q, path, usr, vf)`, zstd, `q`/`usr` dictionary-encoded, 8,192-row groups. A drill at P is the key range `[(q, P/), (q, P0))` (`0` = the code point after `/`): one ranged read per file (a member's roots are in one file).
- **Read bound R = 100,000 rows** (the catalog's V, so a drill decodes no more than a non-member's suffix range). A directory with more than R roots under it is **heavy**; heavy directories are closed upwards (an ancestor holds at least as many roots).
- **Rollups** for heavy `(q, dir)` only: per child of `dir` (its next path segment), the running Σ size, n_files of the roots at or under the child, one cell per change (`+` at `vf`, `−` at `vt`), the child's value on D being its newest cell with `vf ≤ D` — the catalog's cells keyed by directory instead of bucket. The **K = 256** children with the largest peak bytes (ties by name) are kept by name; every other child is summed into the **remainder** cells, exactly, so a date's kept children plus remainder equal the drill. A header row per `(q, dir)` carries the kept count, the root rows under `dir` and its children.
- **Dispatch from the indexes alone**: the roots index bounds the rows of `[(q, P/), (q, P0))` by whole row groups (≤ true rows + 2 × 8,192). If the bound is at most R + 2 × 8,192 = 116,384, read the roots (≤ that many rows); else the true count exceeds R, so `(q, P)` is heavy and has a rollup: read `[(q, P), (q, P\0))` of the rollups.
- **Short literals** use the same files (`drill/short-…`), built from the coalesced versions; their roots are not aliased: equal count signatures (rows, paths, open rows and paths, depth range) occur only among tiny sets, 1.6M of the 12.97B rows.

### Layout (GCS `gs://oa-gcs-usage-dvx/static-names/2026-10-08c/drill/`; R2 `oa-gcs-usage-index`, the same keys under `static-names/2026-10-08c/drill/`)

| Key | What |
|---|---|
| `meta.json` | `gen`, `R`, `K`, `rg` (8,192), `idx_rg` (1,024), `dispatch_rows` (R + 2·rg), and per set (`long_roots`, `long_rollups`, `short_roots`, `short_rollups`): files, row groups, rows, bytes, index bytes |
| `aliases.parquet` | `(q, canonical, shard, n)` for every long member (`n` = its roots); a member reads its canonical's rows (35,915 canonical of 82,616) |
| `long/roots/s####.parquet`, `short/roots/g###.parquet` | roots: `q` string (dict), `path` string, `usr` string (dict), `vf`, `vt` int64 epoch seconds (`vt` = 4291747200 while open), `size`, `n_files` int64; sorted `(q, path, usr, vf)`; 8,192-row groups |
| `long/rollups/s####.parquet`, `short/rollups/g###.parquet` | rollups: `q`, `dir`, `child` strings (dict), `kind` int8 (0 header: `child` '', `vf` = children kept, `b` = root rows under `dir`, `o` = children with roots; 1 a kept child's cells; 2 the remainder's cells, `child` ''), `vf`, `b`, `o` int64; sorted `(q, dir, kind, child, vf)`; 8,192-row groups |
| `{long,short}-{roots,rollups}-index.parquet` | per data row group: `file` (relative to `drill/`), `rg`, `q_min`, `k_min`, `q_max`, `k_max` (its exact first and last `(q, path)` or `(q, dir)`), `offset`, `length` (contiguous byte span), `rows`, `chunks` (per column `data_page_offset, total_compressed_size, dictionary_page_offset or 0`, as the catalog index); sorted `(q_min, k_min)`, the row groups disjoint and ordered; 1,024 entries per index row group |
| `{long,short}-{roots,rollups}-index.top.parquet` | per index row group: `rg`, `q_min`, `k_min` (its first entry's), `q_max`, `k_max` (its last entry's), `offset`, `length`, `rows` (Σ the data rows of its entries), `chunks` (as above, for the index file's columns) |

### How the Worker reads it

1. Per isolate (Cache API between isolates): `drill/meta.json`, `drill/aliases.parquet` (long members only), and the four `…-index.top.parquet` files.
2. `t` = the lowercased literal; if `t` occurs in P's lowercased path, answer the plain (unfiltered) view. Kind = short when `[...t].length ≤ 2`, else long; for long, `c = aliases[t] ?? t`.
3. Roots key range `lo = (c, P + '/')`, `hi = (c, P + '0')`, compared as `(q, key)` pairs in code-point order. In the roots top: `a` = first entry with `(q_max, k_max) ≥ lo`, `b` = first with `(q_min, k_min) ≥ hi`. If the top entries strictly between `a` and `b − 1` hold more than `dispatch_rows` rows, the directory is heavy (go to 5). Else one ranged GET of the index file over top entries `[a, b)`, decode, and select the index entries meeting `[lo, hi)` the same way; their `rows` sum is the bound.
4. Bound ≤ `dispatch_rows`: one ranged GET per data file (in practice one) over those groups' spans, decode with `chunks`, keep rows with `lo ≤ (q, path) < hi`; for each date D, rows with `vf ≤ D < vt` (D = scan epoch seconds) summed by `path`'s segment after `P/`.
5. Heavy: the same two-level lookup on the rollups for `[(c, P), (c, P + '\0'))`; the rows are the header, then per kept child its cells, then the remainder's. Per date, each child's (and the remainder's) newest cell with `vf ≤ D`, zero if none. The treemap shows the kept children plus one "N others" cell (header `o` − kept); the table lists the kept ones. Diffs read both dates from the same rows.
6. Absent from the catalog (a light term): the suffix range, as today.

The Python reference is `static_roots.Drill` over `GroupFile` (two-level; `gcs_drill` reads GCS the same way); `roots drill-query` prints its answers with the bytes and rows read.

### Build and cost (2026-10-08c)

| Stage (DVX out) | What | Wall | Batch |
|---|---|---:|---|
| `roots-measure.json` | the measurement above | 35 min (+ 2 straggler partitions) | 32 + 32 spot |
| `drill-aliases.json` | per-member digests → `drill/aliases.parquet` | 13 min | 32 spot |
| `drill-long.json` | per shard: canonical members' roots (level loop), sort, write roots + rollups + their per-file indexes; queue, biggest first | ~1.9 h (s0012: 893M roots, 1.4 h) | 32 spot; 28.5 task-hours |
| `drill-short.json` | `short-plan` (59 q-groups of ≤ 250M roots; `.` alone is 361M), `short-map` (per subtree partition, 16 `hash(path)` pieces each written on its own, rows split by q-group → the scratch bucket's `drill-short-map/`), `short-reduce` (per q-group: roots + rollups) | map 71 min (the 4.8B-root partition), reduce 23 min | 32 + 59 spot; 8.8 task-hours for the reduce |
| `drill.json` | `index`: concatenated two-level indexes, `meta.json` | ~10 min | 1 spot |
| `drill-answers.jsonl` | `drill-query` over the cases (Python reader, GCS ranged reads) | 4.4 min (455 cases × 5 dates) | laptop |
| `drill-brute.jsonl` | `drill-brute` per date straight from the scan file (`-k`: past 200K children only the drill's kept children, plus the exact total) | ~3–8 min per date | 5 spot |
| `verify-drill.json` | `drill-verify` | 3 s | laptop |
| `r2.dvc` | `r2-copy` (`R2_SERVED` now includes `drill/`): 652 objects, 299.4 GB, to `oa-gcs-usage-index` `static-names/2026-10-08c/drill/` | 55.7 min (~90 MB/s) | the ch-store VM, `nice`d |

| Set | Rows | Bytes | Row groups | Index / top | Heavy `(q, dir)` | Rollup cells |
|---|---:|---:|---:|---:|---:|---:|
| long roots (35,915 canonical members) | 34,226,403,531 | 215.1 GB (6.3 B/row) | 4,178,159 | 459 MB / 842 KB | 160,813 | 17,036,099 (102 MB) |
| short roots (16,897 literals) | 12,971,156,040 | 83.5 GB (6.4 B/row) | 1,583,420 | 176 MB / 321 KB | 47,602 | 6,866,923 (53 MB) |

**Queue and index guards (2026-10-09, after cw's silent hole, specs/cw-static-names.md "Drill incident").** The queue stages (`measure`, `digest`, `build`: `static_roots.Queue.drain`) no longer exit successfully while another task's claim is unfinished: after its pass, a task waits (polling `-W`, default 60 s) for every claimed shard's output, takes over a claim whose lease (`-l`, default 90 min) has expired and builds it, and fails (`QueueTimeout`, nonzero) after `-w` (default 2 × lease); a claim written by the same task index (a Batch retry of a preempted task) is taken over at once. `index` refuses (naming them) unless every shard holding a canonical member (`catalog/members.parquet` through `drill/aliases.parquet`) has `long/rollups-index/s####.parquet` and every q-group of `short-plan.json` has `short/rollups-index/g###.parquet`.

Root counts equal the measurement and the alias plan exactly. Bytes per row: the `path` string is ~90% of a row; zstd level 9 instead of the default saves ~10% (measured on a slice), not pursued.

**Cost.** About 85 spot VM-hours for measurement, digests, both builds, the brute force and the indexes (≈ $19; the long build alone 28.5 task-hours, the short map ~15, the reduce 8.8), including two wasted runs (an out-of-memory short partition retried before it was split into pieces; a brute force that tried to list 20M children per case). R2: 299.4 GB copied once (GCS egress ≈ $36), stored ≈ $4.5/month; the GCS copy ≈ $6/month.

### Verification

Cases (`job/static-names/drill-cases.jsonl`, from `drill-cases -n 1` over `drill-terms.txt`): 38 terms — the named heavy members (`son`, `.json`, `.gz`, `cess`, `example`, `.parquet`, `par`, `tmp`, `47.tmp`, `46_`, `train`, `json`, `jsonl`), four aliases whose canonical sits in another shard (`_chunk033_1980s-2040s.`, `079.json`, `ch_0121.`, `553_war`), nine hash-sampled members of 3–8 characters, and 12 short literals; per term and directory depth (1–11) the largest heavy and the largest light directory (≥ 10K roots); 455 cases, 251 answered from roots, 203 from rollups, 1 plain. Dates 2026-10-08, 10-01 (v2 scans: files and directories), 09-15, 08-15, 07-30 (v1: directories only).

| Check | Result |
|---|---|
| `verify-drill.json`: `drill-query` (two-level index → roots or rollup, the Worker's dispatch) vs **brute force** straight from each date's scan file | **2,270 / 2,270 (term, P, date) equal**; 1,090 of them non-empty (roots 344 long + 261 short, rollups 268 long + 217 short); 2.85M children compared on roots reads, 85,819 kept children on rollup reads; 53 cases with more than 200K children (up to 20.9M; checked as kept children + remainder = total), 65 rollups with a non-zero remainder |
| Read cost (laptop, GCS) | roots ≤ 114,688 rows / 2.8 MB (median 90,112 / 0.37 MB), 0.6 s median; rollups ≤ 16,384 rows / 180 KB, 0.34 s median; plus one index row group (~110 KB) per lookup |
| Local (`cloud/tests/test_static_roots.py`) | roots = every member's first-hit versions (chunked feeding included) and every short literal's; per-directory counts and the subtree partitions' sums = brute force; at R = 3, 10, 10⁶ with 4- and 8-row groups every (member, directory, date) view through the two-level index equals brute force (rollup kept children and remainder); digests equal ⇔ root sets equal, aliased reads = brute force; `drill-verify` passes and catches an altered reference |

## Indexed-only filter (`FILTER_INDEXED_ONLY=1`)

A deployment flag (gcs): the map's filter (`/api/subtree`, `/api/diff`, `/api/series`) accepts only what this index answers exactly — one literal substring of a file or folder name (no `/`, `*`, regex, exclusion, second term or `a|b`; quoted spaces are fine), unscoped (no owner pool, user lens or storage classes), on a scan the index covers — and never walks the path store for a filter. Everything else is a 400 `{ "error": <message>, "code": <code> }` before any read (`site/functions/_lib/indexedOnly.ts`):

| Code | When |
|---|---|
| `unsupported-regex` | `qs=regex`, or a `/…/` query |
| `unsupported-glob` | a `*` term |
| `unsupported-exclusion` | any `-term`, exclusions alone included |
| `unsupported-terms` | several terms, or alternatives (`a b`, `a|b`, two short terms) |
| `unsupported-slash` | a term holding `/` (also `/api/name-summary`'s `name`) |
| `unsupported-scope` | a filter with `o=`, `lens=` or `cl=` |
| `scan-not-indexed` | the literal's answer doesn't cover the scan: a scan outside the generation, or a heavy literal past the drill base (until the drill's per-scan append) (`/api/name-summary` keeps its own scan refusal) |

A view root whose path holds the literal is the plain view, on any scan. The series names uncovered scans (`unindexed: [dates]`, gaps) instead of reading the client's roots per scan. `GET /api/filter-caps` → `{ indexedOnly }` tells the filter box, which refuses the same forms inline (never sending them), shows the server's codes as its message, and lists only the supported form in its help. Unset (cw, the r2 demo, local), nothing changes.

## Acting on matches (`/api/filter-cover`)

Assigning or staging a search's matches is a main use of the filter. The bulk bar and the table's rows act on the matches under the view as the **fewest exact prefixes** (`site/functions/_lib/cover.ts`, `GET /api/filter-cover?date|d=&path=&q=[&qs=]`), never a row's whole prefix and never the drawn subset:

- **Roots**: every match root under P on D (`view.ts` `allMatchRoots`: phase 1 only, the static index's roots held per isolate). A rollup (a heavy literal under a heavy folder) or a partial / approximate search is `complete: false` with a plain-words `reason`, and nothing acts.
- **Collapse**: a folder A (depth ≥ 2, at or below P) is *full* iff the matched objects and bytes under it equal A's totals (`path` sort point lookups, every owner slice summed). Full folders are closed downward, so the cover — the outermost full folders plus the roots under none — is the unique minimum; it never covers a non-match (zero-byte objects included: objects are compared, bytes cross-check). Rounds run deepest depth first; a folder holding a non-match poisons its ancestors (never looked up). Budget: ≤ 60 row groups a round, ≤ 240 a cover; past it the remaining folders count as not full (still exact, not minimal; `unchecked`). One-object roots are looked up for their kind (object key vs folder prefix); an unplaced one is `kind: null` and never sent.
- **Actions**: both act on folder prefixes only — the ledger accepts `…/` patterns (`/api/actions`), and a plan item is canonicalized to `…/` (`plans.ts` `relPrefix`), so an object key would stage as `key/` and delete nothing. A lone matching file (in a folder that also holds non-matches) is therefore listed but not acted on, and the bar says so; staging never takes a whole bucket. Both go in batches (500 per POST); staging then folds its batches into one (`/api/plans/:id/batches/merge`). No cap below the response's 50K items. Under a filter a table row's checkbox, trash and assign act on that row's folder items only; a row with none has no checkbox.
- **Review**: the bar lists the items grouped by parent folder, heaviest first, each folder and each item untickable — the remedy for correct-but-unwanted substring matches (`tomat` in `…Rotten_Tomatoes…mp3`).

Example (gcs, 2026-10-09, `podcast_audio_top1000_60s_clips/Adam_Carolla_Show?f=tomat`): 10,787 match roots, every one an `.mp3` directly in `Adam_Carolla_Show/`, which holds other files too — so no collapse: one group of 10,787 files (10.3 GB) to review, none of which can be assigned or staged by itself (they're the false positives here anyway; unticking the group drops them).

`/names` resolves `?d=` against the **store's** scans, as the map does (`scanSlug.ts`; server-side `scanArg.ts` `indexedScan`): a day is its latest store scan. A store scan the static index doesn't hold yet is the "not in the name index yet" state (the API's 400 `scan-not-indexed`), never the index's nearest scan.

## Alternatives compared

| Design | Size | Round trips per rare query | Bytes per rare query | Verdict |
|---|---|---|---|---|
| **Suffix-ordered denormalized postings** (above) | ~430 GB | 2 (D1 sidecar, then one range per file) | ≤ `V` × 28 B (~3 MB) | **Recommended** |
| Suffix → name id, plus name-sorted postings | ~25 GB + 16 GB | 2 + one read per matching name (34K for `5418`) | small, but scattered | Today's cold problem again |
| Trigram posting lists (plain, or SQLite FTS5 `trigram` via HTTP-range VFS or D1) | ~5.9B positions; FTS5 in D1 needs 10+ shards at 10 GB each | 2–3, then postings scatter | Smallest trigram list is 0.5–1.8M names for `5418`, `48.parquet`, `rt-0003` (MBs of doclists), plus candidate verification | Not bounded by the result; still needs postings |
| FM-index over the names | ~3–6 GB | ≈ len(q) dependent rank lookups, then one per occurrence to locate | small per step | Fine in memory on a server; sequential round trips on R2 |
| Minimizer- or k-sampled suffixes (store 1 in k positions) | ~1/k | k ranges | each range is a shorter, possibly common, suffix: unbounded | Breaks the bound |

## What stays on a server

Nothing on the query path: catalog lookups are static (`mega-index.md`), rare terms read this layout. The daily build (ingest diff, catalog census over 139.6M names, delta files, weekly compaction) is batch work, ~15–30 minutes a day, which fits a GCP Batch job like the daily scan's; the census needs ~10–20 GB RAM briefly.

## Plan to production

1. ~~Weight and bound~~: done as the static catalog's membership (suffix-range rows > V = 100K, plus every one- and two-character literal), gen `2026-10-08c`.
2. ~~Full base build~~: done, `2026-10-08` (every version) and `2026-10-08c` (coalesced versions + catalog), verified, on R2.
3. **Worker reader** (hot-preview worktree): consume the catalog as in "How the Worker should consume it"; point `STATIC_GEN` at `2026-10-08c`; drop the `short`, `bucket-name` and `registry-weight` skips; measure cold/warm latency. Map-filter drilldown for members: "Drilldown for heavy terms", "How the Worker reads it".
4. **Daily append** as a Batch stage after the daily scan: intervals `append` → `coalesce_append` → the shards' delta files (+ close records, read by the Worker beside the base) and `static_catalog.append`; weekly compaction into a new base generation. The drilldown needs its own: a day's opened/closed versions give each member's new roots and closures (a delta roots file in the same layout, read beside the base), new heavy directories and members, and alias splits when two equal sets diverge — not built.
5. Retire the always-on VM (keep ClickHouse only for ad-hoc experiments, if at all).

## Files

- `job/ch-store/static-name-proto.sql`: the prototype build (prefixes `541`, `nk0`, `48.`, `gof`).
- `job/ch-store/static-name-query.py`: the one-range reader and per-date answers.
- `job/ch-store/static-name-term-stats.sh`, `static-name-terms.txt`: the per-term table.
- `cloud/src/dt_cloud/static_names.py` (`dt-cloud static-names …`), `cloud/tests/test_static_names.py`, `job/static-names.sh`, `job/static-names/{ch-answers.py,terms.txt}`, `static-names/<gen>/*.dvc` (the DVX stages).
- `cloud/src/dt_cloud/static_catalog.py` (`dt-cloud static-names catalog …`), `cloud/tests/test_static_catalog.py`, `job/static-names/{catalog-terms.py,catalog-terms.txt,catalog-ch-terms.txt}`.
- `cloud/src/dt_cloud/static_roots.py` (`dt-cloud static-names roots …`; Batch `MODULE=static_roots`), `cloud/tests/test_static_roots.py`, `job/static-names/{drill-terms.txt,drill-cases.jsonl}`; stages `static-names/2026-10-08c/{roots-measure,drill-aliases,drill-long,drill-short,drill}.json`, `drill-{answers,brute}.jsonl`, `verify-drill.json`.
- Built data: `gs://oa-gcs-usage-dvx/static-names/{2026-10-08,2026-10-08c}/` and R2 `oa-gcs-usage-index` `static-names/{2026-10-08,2026-10-08c}/`; intermediates in `gs://oa-gcs-usage-scratch/static-names/` (7-day expiry).
- Prototype data: `default.sx_names`, `default.sx_rows`, `default.sx_proto` on the VM; `gs://oa-gcs-usage-dvx/scratch/bench/ch-store/static/sx-{4096,16384}.parquet` (1.5 GiB).
