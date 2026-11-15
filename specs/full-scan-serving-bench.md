# Full-scan serving: benchmark design A at fleet scale

Status: prototype built and measured, 2026-11-12 (§ Results); pack files, the slim index and small-RAM serving measured 2026-11-13 (§ Slim index, cold start, small RAM). Ryan's go ("go", after the index-rethink discussion). Prototype on a side branch (`w/full-scan-bench`), measured on the GCP VM `disky-full-scan-bench` (n2-highmem-16, us-east1-c, beside gcs's store), never on the laptop. Nothing here touches a deployment.

## Why

Every view is a pure function of its inputs: scans `S`/`E`, folder `D`, search `Q`, owner `U`, storage class `C`, staged state `L`, and the result budget `M` (SEDQUCLM). Today two indexes serve it:
- **The interval store** serves the map (`Q` empty).
- **The static name index** serves search.

Neither serves every combination. Search refuses several forms:
- a person lens or class scope: `unsupported-scope`;
- a common term in an owner pool: `owner-too-common`;
- path terms and short anchors.

Map thresholds under a lens have needed per-slice fixes.

Design A answers every combination with one bounded pass over the data held in RAM, with no index per axis:
1. match the search against the name dictionary;
2. make one depth-first pass over `D`'s rows;
3. keep the rows live at the scan and passing the filters;
4. add each one into its child's total.

Worst case is the fleet root, which reads everything. Latency is bounded by construction.

The ClickHouse box (`done/ch-postmortem.md`) was a server, but it rolled ancestors up with SQL joins: the `zarr.json` map took 153 s. A depth-first pass rolls them up with a stack. Whether a purpose-built scanner reaches memory bandwidth (my estimate: 0.2–0.5 s at the gcs root, all history) is what this measures.

The answer decides where design A sits on the storage-vs-query-compute curve, beside:
- **B:** succinct range structures over (suffix-array order × path order);
- **C:** B plus iceberg cells for heavy terms.

## Corpus

**gcs's interval store** (`CURRENT.json`'s generation, base + runs), the v2 scans only (2026-09-30 on). Before 09-30, scans are dirs-only.

Measured: generation `2026-10-09b.g3.c001` on GCS (`gs://oa-gcs-usage-dvx/interval-store/`, read in region from the bench VM, no egress), base of 132 scans + the newest manifest's two runs (`2026-11-12T0702.json`), 135 scans 2026-07-30 → 11-12T0702. Its `path` sort: 1,341,950,870 rows (25.2 GB) + 12.3M rows in the runs. `bench/full-scan/fetch-gen` copies only what the loader reads (24 GB in 30 s at ~790 MiB/s).

At 10-09 it held:
- 1,156,324,516 folded versions over 71 scans;
- `path` sort 16.79 GB;
- 609M open versions at the newest scan.

Owner slices come from its `slices` sort.

Read from GCS on a GCP VM in the bucket's region (Ryan, 2026-11-12), which settles open question #18 for this bench. Development ran on the repo's interval-store fixtures and on synthetic stores cut by the repo's own code (§ Results).

## The scanner (`bench/full-scan/`, Rust)

**Load** (one-time, timed and sized). Stream the store's `path` sort, base and runs combined as the reader does (`combineLive`: one row per version, the smallest `vt`), into packed columns in depth-first path order:
- `name` u32: id into a lowercase name dictionary;
- `depth` u8;
- `end` u32: the subtree's last row;
- `kind`;
- `size`;
- `vf` / `vt`: as scan ordinals (u16), not epoch seconds;
- the class bytes `c2`/`c3`/`c4`;
- owner slices: per-row (owner id, bytes) runs.

The name dictionary is one byte buffer plus offsets. Print bytes per column and the total resident size.

**Query:** `(scan | from,to), path, q, owners, classes, staged, depth, minFrac`. Answer:
- every node under `path` down to `depth` levels, whose filtered bytes are at least `minFrac` of the view's total;
- each node's exact filtered bytes and file count;
- one exact remainder per parent;
- a diff gives both sides and Δ.

Semantics:
- **Search:** one search language, the site's baseline (`specs/search-future.md`).
  - A term matches a file when some segment of its path contains the term, case-insensitive. A folder's match covers its subtree.
  - AND / OR / NOT combine terms per file.
  - Also measured: regex per name, and path terms, as "free with A?" data points. Neither is shipped.
- **U:** sum only the selected owners' slices; "all but X" is all minus X.
- **C:** only the selected class bytes.
- **L (staged):** a set of path prefixes, read from a deployment's staged rows. The phase-1 bench takes it as a file.

**Parallelism:** split `D`'s row range into chunks across threads. Each chunk starts from its first row's ancestor chain, so the match stack is known without a scan from the root. The name match runs over the dictionary in parallel first, giving a bitset per term.

### As built (`bench/full-scan/src/`)

**Load** (`load.rs`). The served `path` sort is ordered `(depth, path, vf)`, so a subtree is a range per depth, not one range; the loader re-sorts into pre-order:
1. **One depth at a time.** Level `d`'s rows come from the row groups whose `depth` statistics hold `d` (base and every listed run), decoded in parallel. Each row's parent is its path's prefix, looked up by 128-bit hash in level `d − 1`'s map; only two levels' maps are held, never the path strings. Names are interned into one dictionary.
2. **`combineLive`.** A level's rows are sorted by `(path hash, kind, vf)`; rows of one version (base + a run's close record) become one, with the smallest `vt`. Object versions become rows. A directory's versions (rolled-up totals) are kept aside in `dir_*` columns, and the directory gets one structural row.
3. **Pre-order.** Each level's nodes are ordered by `(parent's position, name + "/")` in byte order: segment by segment, so `a/b` and its subtree come before `a/b-c`. Subtree row counts are summed bottom-up, positions assigned top-down, every version scattered to its row; `end` is position + subtree − 1.
4. **Checkpoints.** Every 4,096 rows, the row's ancestor chain (from parent links, before they're dropped), for the threads' chunk seeds and for paths.

Columns: `name` u32, `depth` u8, `end` u32, `flags` u8 (object, another version of the previous row's path, class 1–4, complex), `size` u64, `vf`/`vt` u16 scan ordinals, `owner` u16, plus the directory versions (`dir_vf`, `dir_vt` u16, `dir_size`, `dir_files` u64) and a side table for the few objects whose owner or class bytes aren't one whole slice. The dictionary is one lowercase buffer (`\n` + each name + `\n`) with u64 offsets, plus the original spelling of names whose lowercase differs.

Owners come from the `path` sort's `us` (the folded owner slices, `interval-store.md` §2.2: `''`, a name, or JSON `[["usr", bytes], …]`), not from joining the `slices` sort at load: for an object they carry the same slices, and `oracle/slices_check.py` checks the two agree per (bucket, owner) at a scan. gcs names its class columns `nearline_bytes` / `coldline_bytes` / `archive_bytes` (classes 2–4); the loader and the oracle take either spelling.

**Query** (`query.rs`, `scan.rs`), as specified, with these exact rules (the oracle's too):
- Bytes are object versions' (`kind = 'file'`) live at the scan; directory rows only give structure.
- A name term is a lowercase substring of a segment; `^q`, `q$`, `^q$` anchor it to the segment (`\nq`, `q\n`, `\nq\n` in the buffer). A path term `a/b` holds when the lowercase path does (a small automaton per depth: ends-with, equals…, starts-with). A regex is searched in each lowercase segment. Up to 16 atoms; the expression is a lookup table over their bits.
- An object's owner slices are its `us` (`''` → no owner; a name → all bytes; JSON → each entry plus the unnamed rest when positive); a class's slice is `c2..c4` where positive, class 1 the rest. A file counts when it holds a selected slice; its bytes are the selected slices'. Owners and classes together gate each other per file (a file with several of both is an error; gcs has none).
- Node `x` is kept when nonempty and `max(bytes_a, bytes_b) · 10⁶ ≥ min_ppm · max(total_a, total_b)`; every kept node above the deepest level, and the folder, reports its rest (signed: directory totals that don't add up can leave a negative one).

**The pass:** threads take chunks of the folder's rows (8 per thread, ≥ 64K rows). A chunk seeds its stack from the checkpoint before it, replayed to its first row: the search masks of every ancestor, and its open nodes in the depth window (those are partials). A new path at depth `d` pops the open nodes at depth ≥ `d` (each rolled into its parent, and emitted) and pushes itself if within the window; a live, passing object version adds to the deepest open node. A node wholly inside a chunk is emitted complete, and dropped there if it is under the threshold's lower bound: `min_ppm` × the bytes all chunks have summed so far, which only grows. Partials are summed across chunks. The loop is monomorphized over (search, diff, filters).

**Two ways for an unfiltered view** (`q`, `U`, `C`, `L` all empty): the pass above, summing object versions; or `dirs: true`, reading each node's own row (a directory's rolled-up totals at the scan, an object's bytes) in a walk of the depth window only, which never reads below the deepest level. The bench runs every unfiltered view and diff both ways (`@dirs`); the oracle checks each against its own SQL.

## Oracle

DuckDB over the same store's rows, on the bench node, for every bench query: per-node sums by the same semantics, written as plain SQL with no shared code. Each answer must equal the oracle's exactly: same nodes, bytes and counts. A disagreement fails the run.

Built: `bench/full-scan/oracle/oracle.py` (exit 1 on any difference). It combines the base's and the runs' rows itself (runs grouped by `(depth, path, vf)` to the smallest `vt`, then joined to the base), and answers a query by SQL over the object versions: each search atom over `string_split(lower(path), '/')` (a path term over `lower(path)`), owner slices parsed from `us` by `from_json`, class slices from the class columns, a node per ancestor prefix (`string_split(path, '/')[1:d]`), the threshold in `HAVING`, remainders in Python. The directory-total view (below) has its own SQL over every version's own row. `oracle/slices_check.py` checks the `us` the scanner reads against the `slices` sort.

## Bench

Run on the GCP VM `disky-full-scan-bench` (n2-highmem-16: 16 vCPU = 8 cores × 2 threads, x86, 125 GB; us-east1-c, spot), which reads gcs's store in region. `mgu` was planned but its AWS SSO session had expired, so development moved to the VM too.
- Develop and tune on the VM.
- Run the final recorded gcs bench as a Batch job via `dt-cloud batch submit -c full-scan-bench` (`bench/full-scan/batch/submit`: cost labels from `$DISKY_LABELS`, the region checked against the buckets, spot with retries, Batch's cleanup, repeatable; `batch/task.sh` is the task). It reruns the VM run's query file and requires every answer to equal the VM run's oracle-checked ones.
- Each job's load time (copy, re-sort, pack) is reported as the restart cost a serving box would pay (`phases.jsonl`: `setup`, `copy`, `bench`, whose first line holds the load's own phases).

For each query:
- **Cold:** first touch after load. Page cache is irrelevant since everything is in RAM, but report it.
- **Warm:** median of 5.
- **Threads:** 1, and all 16.
- **Reported:** wall time, rows scanned, bytes touched.

The query set (≥ 60), drawn from real usage where we have it (`warm-cache`'s views, the probe's, the 105-query filter bench in `serving-options.md`):
- **Folders:** the fleet root, each bucket root, and drilled folders at depth 3 and 6.
- **Search:**
  - none;
  - rare terms (≤ 1K matches);
  - common terms (`zarr.json`, `.npy`, `ckpt`, `node_modules`);
  - two-term AND, OR and NOT;
  - one regex;
  - one path term.
- **U:** none, one owner, owned, unowned, all-but-two.
- **C:** none, one class.
- **L:** none, staged only.
- **Time:** newest scan; a scan a month back; a one-day diff; a month diff.

Output: `bench/full-scan/results/<date>.jsonl` (one line per query and run) and a summary table in this spec.

## Results (2026-11-12)

Raw lines: `bench/full-scan/results/2026-11-12.jsonl` (one per store load and per query × thread count: cold, the five warm runs, rows, bytes touched, nodes, totals). Tables below come from `bench/full-scan/summarize.py`.

### Verified against the oracle

Every answer the scanner gave equals the oracle's exactly (`oracle/oracle.py`; nodes, bytes, file counts, remainders):
- **gcs:** 96 / 96 queries (the 69 of the mix plus 27 `@dirs` twins), 0 different. The oracle combined 717,920,013 object versions itself, the scanner's object-row count to the version.
- **Fixtures:** all nine interval-store generations (`site/functions/_lib/fixtures/iv/interval-store/g1`–`g9`: base-only, base + runs, merged-run revisions, unowned, JSON owner slices, inconsistent directory totals), 9,504 queries from `fsb plan -x` (every scan and consecutive diff × every folder × each filter, at depths 1–3 and four thresholds, 1,584 of them `@dirs`), 0 different (`tests/test_fixtures.py`, `check-fixtures`).
- **Synthetic:** 98 / 98 at each scale (x450, x900, x1800; below), and 71 / 71 at two small ones during development. The oracle caught a generator bug: at x900, two runs drew the same random name, so the base held 549 duplicate `(depth, path, vf)` rows, which the scanner combined as `combineLive` does and the oracle (trusting the base to hold each version once) counted twice. Names now carry a counter; the rerun has none.
- **Reruns:** the gcs mix on the final code (the faster loader) gave the oracle-checked answers exactly, 96 / 96; so did the `@dirs` queries after the walk's last change (27 / 27) and x450's mix (98 / 98).
- Each answer also equals itself across thread counts (1 and 16) and across the five warm runs (`fsb bench` fails otherwise).
- **Owners:** the `us` the scanner reads equals gcs's `slices` sort on objects: per (bucket, owner), bytes and objects, at the newest scan (133 cells) and a month back (121 cells), base + runs combined on each side (`oracle/slices_check.py`).
- On gcs every unfiltered view's two answers (object sums, directory rows) are equal too: 27 / 27 pairs (`oracle/dirs_vs_files.py`), so gcs's directory totals add up exactly at these scans.

### gcs

Generation `2026-10-09b.g3.c001` + 2 runs, 135 scans; on the n2-highmem-16 VM (8 cores × 2 threads, x86). Queries at the newest scan (`2026-11-12T0702`) unless named; "a month back" is `2026-10-13T0702`; diffs are one day (`2026-11-11T2052` →) and a month. `depth` 3 below the folder, `min_ppm` 500 (each node ≥ 0.05% of the view).

**Load (the restart cost).** 1,354,236,524 version rows read (base 1,341,950,870 + runs 12,285,654), 6,034,826 of them another tier's copy of a version (`combineLive`). Packed: 967,126,225 rows = 717,920,013 object versions + 249,206,212 directories (holding 630,281,685 directory versions aside), 948,770,533 paths, 172,027,281 distinct names, 40 owners, 0 objects with more than one owner or class slice. Copy from GCS 30 s; load 359 s (read and combine the levels 228 s, dictionary 64 s, pre-order 28 s, scatter 39 s); peak RSS 110 GB (the load's working set, not the index's; it fits the 125 GB VM with little to spare, so the loader's peak is what sizes a loading box today). The first, serial version of the pre-order took 280 s of a 786 s load.

| Store | Versions read | Rows (objects / dirs) | Resident | B / object version | Load | Root rows/s/thread | Root 1 thread | Root 16 threads | All queries, 16 threads: p50 / p90 / max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gcs | 1,354,236,524 | 967,126,225 (717,920,013 / 249,206,212) | 48.29 GB | 67.3 | 360 s | 300M | 3,220 ms | 453 ms | 211 / 1,243 / 2,201 ms |

Selected queries (all 96: `results/2026-11-12.jsonl`; the timing lines: `results/gcs-bench-2026-11-12.txt`). "Of which dictionary" is the search's dictionary pass (and the path lookups) inside the 16-thread time.

| Query (gcs) | Rows read | 1 thread: warm (cold) | 16 threads: warm (cold) | of which dictionary | Nodes |
|---|---:|---:|---:|---:|---:|
| `root` | 967,126,225 | 3,220 (3,228) ms | 453 (456) ms | 0.0 ms | 290 |
| `root@dirs` | 292,096 | 9.8 (11.6) ms | 1.5 (2.2) ms | 0.0 ms | 290 |
| `root-depth6` | 967,126,225 | 4,045 (4,066) ms | 593 (597) ms | 0.1 ms | 939 |
| `root-depth6@dirs` | 254,491,097 | 3,536 (3,563) ms | 470 (515) ms | 0.0 ms | 939 |
| `bucket0` | 372,104,748 | 1,207 (1,208) ms | 180 (174) ms | 0.1 ms | 372 |
| `bucket0@dirs` | 1,749,369 | 26.1 (26.9) ms | 9.3 (9.6) ms | 0.1 ms | 372 |
| `bucket3@dirs` | 31,212,875 | 275 (274) ms | 49.9 (49.6) ms | 0.1 ms | 353 |
| `d3-0` | 312,386,455 | 973 (974) ms | 146 (140) ms | 0.1 ms | 286 |
| `d6-0` | 4,194,340 | 35.7 (36.2) ms | 11.1 (11.0) ms | 0.5 ms | 9 |
| `root-month` | 967,126,225 | 3,210 (3,208) ms | 451 (438) ms | 0.0 ms | 401 |
| `diff-day-root` | 967,126,225 | 3,550 (3,577) ms | 551 (541) ms | 0.0 ms | 287 |
| `diff-day-root@dirs` | 292,096 | 18.0 (14.7) ms | 2.7 (2.6) ms | 0.0 ms | 287 |
| `diff-month-root` | 967,126,225 | 3,618 (3,617) ms | 555 (539) ms | 0.0 ms | 425 |
| `rare0` | 967,126,225 | 8,391 (8,028) ms | 1,177 (1,165) ms | 90.7 ms | 3 |
| `common-zarr.json` | 967,126,225 | 8,374 (8,446) ms | 1,220 (1,187) ms | 89.4 ms | 28 |
| `and` | 967,126,225 | 9,996 (9,760) ms | 1,338 (1,316) ms | 175 ms | 14 |
| `or` | 967,126,225 | 10,314 (10,110) ms | 1,378 (1,401) ms | 166 ms | 59 |
| `not` | 967,126,225 | 9,001 (9,085) ms | 1,313 (1,333) ms | 86.4 ms | 293 |
| `and-not` | 967,126,225 | 10,052 (10,260) ms | 1,364 (1,373) ms | 178 ms | 223 |
| `anchor-exact` | 967,126,225 | 8,222 (8,422) ms | 1,199 (1,194) ms | 88.4 ms | 28 |
| `regex` | 967,126,225 | 10,293 (10,480) ms | 1,510 (1,520) ms | 424 ms | 3 |
| `path-term` | 967,126,225 | 14,221 (15,285) ms | 2,201 (2,167) ms | 297 ms | 13 |
| `search-diff-month` | 967,126,225 | 9,318 (9,592) ms | 1,313 (1,314) ms | 83.8 ms | 44 |
| `owner-one` | 967,126,225 | 4,234 (4,228) ms | 626 (616) ms | 0.0 ms | 30 |
| `owned` | 967,126,225 | 4,300 (4,310) ms | 645 (630) ms | 0.0 ms | 212 |
| `unowned` | 967,126,225 | 5,145 (5,155) ms | 783 (784) ms | 0.0 ms | 255 |
| `all-but-two` | 967,126,225 | 5,191 (5,231) ms | 796 (785) ms | 0.0 ms | 288 |
| `owned-search` | 967,126,225 | 9,071 (9,329) ms | 1,236 (1,231) ms | 86.7 ms | 16 |
| `class-one` | 967,126,225 | 3,991 (3,983) ms | 625 (618) ms | 0.0 ms | 216 |
| `class-owner` | 967,126,225 | 4,217 (4,230) ms | 620 (626) ms | 0.0 ms | 16 |
| `staged` | 967,126,225 | 3,730 (3,745) ms | 553 (558) ms | 4.0 ms | 27 |
| `staged-diff-month` | 967,126,225 | 4,644 (4,659) ms | 712 (713) ms | 4.1 ms | 30 |

By kind (the planner's first tag; "folder" and "diff" include their `@dirs` twins):

| Store | Threads | Kind | Queries | Warm median (p50 / max) | Cold (max) | Rows |
|---|---:|---|---:|---:|---:|---:|
| gcs | 1 | class | 5 | 4,217 / 9,158 ms | 9,461 ms | 967,126,225 |
| gcs | 1 | diff | 26 | 54.6 / 3,618 ms | 3,617 ms | 4,056,743 |
| gcs | 1 | folder | 28 | 40.2 / 4,045 ms | 4,066 ms | 4,056,743 |
| gcs | 1 | owner | 8 | 4,722 / 9,071 ms | 9,329 ms | 967,126,225 |
| gcs | 1 | search | 25 | 8,382 / 14,221 ms | 15,285 ms | 967,126,225 |
| gcs | 1 | staged | 4 | 4,188 / 9,127 ms | 9,324 ms | 967,126,225 |
| gcs | 16 | class | 5 | 625 / 1,243 ms | 1,263 ms | 967,126,225 |
| gcs | 16 | diff | 26 | 16.1 / 555 ms | 541 ms | 4,056,743 |
| gcs | 16 | folder | 28 | 12.5 / 593 ms | 597 ms | 4,056,743 |
| gcs | 16 | owner | 8 | 707 / 1,236 ms | 1,231 ms | 967,126,225 |
| gcs | 16 | search | 25 | 1,192 / 2,201 ms | 2,167 ms | 967,126,225 |
| gcs | 16 | staged | 4 | 634 / 1,231 ms | 1,232 ms | 967,126,225 |

What it shows:
- **A plain view of the whole fleet reads ~1B rows in under half a second on 16 threads**: the root at the newest scan 3.2 s on one thread (300M rows/s/thread, ~4 GB/s of the 14 bytes/row it reads), 0.45 s on 16 (7× on 8 cores). Any scan costs the same (a month back: 0.45 s), and a diff ~20% more (0.55 s): a version's liveness is two u16 compares whatever the scan.
- **Filters cost per row**: an owner, class or staged scope 0.55–0.8 s at the root on 16 threads. A search ~1.2 s: a dictionary pass (~90 ms on 16 threads over 10 GB of names) and a bitset lookup per path, which misses cache (172M names: a 21 MB bitset per atom); two atoms ~1.35 s, a regex 1.5 s (its dictionary pass 0.42 s), a path term 2.2 s (three bitsets and an automaton step per path). The first run's regex took 3.7 s: one regex shared across threads serialized its dictionary pass (a clone per piece now).
- **Folders scale with their rows**: a bucket 10–180 ms, a depth-3 folder 146 ms, a depth-6 one 11 ms on 16 threads. The cost is the folder's rows, not the answer's nodes.
- **From directory rows, unfiltered views take milliseconds**: the root 1.5 ms on 16 threads (it visits the 292K paths of the depth window, not 967M rows), a one-day diff 2.7 ms, a bucket ≤ 50 ms, and every answer equals the object sums. The cost is the window's paths: six levels of the fleet (`root-depth6@dirs`, 254M paths) take 470 ms, as much as summing the objects. (The first run's serial walk took 14 ms, 1.1 s and 10 s for the root, `bucket3`'s 31M-object flat directory and `root-depth6`; it now runs in chunks that skip subtrees below the window through the checkpointed ancestor chains, and emits only nodes over the threshold.)
- **Against the spec's targets on the gcs root** (p50 ≤ 0.5 s, max ≤ 2 s), 16 threads: plain views and diffs meet them (0.45 / 0.55 s summing objects; 1.5 / 2.7 ms from directory rows). Over the whole mix p50 is 0.21 s and p90 1.24 s, but scoped views (0.55–0.8 s) miss the p50 target at the root and the path term (2.2 s) misses the max; searches are 1.2–1.5 s.
- **Cold ≈ warm**: everything is in RAM after the load, so a first query costs what a repeat does (the cold column).

### Synthetic scaling

`fsb synth` (`synth.rs`) generates a fleet's history over 30 daily scans (2026-10-01 → 10-30) in the folded path versions' shape; `oracle/synth_store.py` cuts it with the repo's own code: the base's `pvl` range files into the served `path` sort by `interval_store.cut_gen` (dyadic segments, 8K-row groups, `.groups.parquet`), the last two scans as runs by `write_served`, a manifest per run. Shape: six buckets of checkpoints (runs → steps → zarr-v3 tensor trees: `zarr.json` beside `c/i/j/k` chunk paths, so many single-object dirs), tokenized datasets (flat dirs of `.npy` shards), raw ingest (a new day dir per scan), users' dirs (notebooks, `node_modules`, mixed-case names), logs rewritten while active, scratch churn; 8 owners by run / user; a nearline lifecycle rule (a new version per object at its transition), an archive bucket; step retention ("keep the last 3"), a sweep scan. Churn is ~2–3% of objects per scan. Each scale's mix is 98 queries (71 + 27 `@dirs`).

| Store | Versions read | Rows (objects / dirs) | Resident | B / object version | Load | Root rows/s/thread | Root 1 thread | Root 16 threads | All queries, 16 threads: p50 / p90 / max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| x450 | 46,847,025 | 44,940,531 (28,247,985 / 16,692,546) | 1.46 GB | 51.6 | 13 s | 227M | 198 ms | 23.2 ms | 5.5 / 55.5 / 91.4 ms |
| x900 | 93,385,359 | 89,490,923 (56,218,084 / 33,272,839) | 2.91 GB | 51.7 | 26 s | 229M | 390 ms | 45.8 ms | 9.9 / 113 / 178 ms |
| x1800 | 187,102,692 | 179,223,945 (112,837,981 / 66,385,964) | 5.82 GB | 51.6 | 53 s | 230M | 781 ms | 89.2 ms | 18.4 / 219 / 348 ms |
| gcs | 1,354,236,524 | 967,126,225 (717,920,013 / 249,206,212) | 48.29 GB | 67.3 | 360 s | 300M | 3,220 ms | 453 ms | 211 / 1,243 / 2,201 ms |

- **Linear in rows:** root time, load and resident size each double with the store (x450 → x900 → x1800: 23 → 46 → 89 ms on 16 threads; 13 → 26 → 53 s to load). Rows/s/thread is flat at ~230M; 16 threads are ~8.8× one (8 cores).
- **Synthetic vs gcs:** synthetic stores pack at 32 B/row against gcs's 50: the synthetic fleets hold 0.4–1.8M distinct names, gcs 172M (12.5 GB of dictionary), and gcs keeps 630M directory versions aside (12.6 GB). gcs's rows scan faster (300M rows/s/thread against 230M): it has fewer directory rows per object (26% of rows against 37%), and a directory row costs a stack push where an object row costs an add.
- **Extrapolation, checked:** from the synthetic ladder alone (230M rows/s/thread, 8.8× on 16 threads), gcs's 967M rows predict 4.2 s on one thread and 0.48 s on 16; measured: 3.2 s and 0.45 s. The brief's 1.16e9 versions, at gcs's 0.71 packed rows per version read, are ~830M rows: **an extrapolation**, ~0.39 s for the root on 16 threads.

## Slim index, cold start, small RAM (2026-11-13)

Three measurements on top of the first run (Ryan's go): the packed form as a file and how fast a process serves from it, a slimmed index, and serving from the page cache under a memory cap. Code on `w/full-scan-slim`; measured on `disky-full-scan-bench-nvme`, a spot twin of the bench VM (n2-highmem-16, us-east1-c, 200 GB pd-balanced boot disk) with 4 local NVMe SSDs in RAID0 and a 1 TB pd-ssd, so the bench VM stayed with the other runs. Same generation (`2026-10-09b.g3.c001` + the `2026-11-12T0702` manifest's two runs), fetched in region (24 GB, 32 s). Raw lines: `bench/full-scan/results/2026-11-13-slim/`; tables from `bench/full-scan/slim_summary.py`; driver `bench/full-scan/run-slim`. Cold starts ran at `f3aa3f540`, the thread sweep and the caps at `57298d754` (the pass's branchless scopes); the plain view the cold starts time is the same code in both.

### Pack files (`fsb pack`, `fsb slim`; `src/pack.rs`)

The packed in-memory form written once as one file and mapped back without parsing (in production, a Batch step after the store's append writes it; a server maps it):
- bytes 0–8 the magic `FSBPACK\0`, 8–16 the JSON header's length, then the header (`{"version": 1, "kind": "full" | "slim", "meta": {scans, owners, stats}, "sections": [{name, elem, offset, len}]}`);
- every column a section of raw little-endian `u8`/`u16`/`u32`/`u64` at a 4 KiB-aligned offset, so it maps as a typed slice (`Col<T>`: owned by the loader, or a slice of the map). A reader refuses another magic or version, a section of another element type, or one past the file's end.
- Opening is `mmap` plus the header (≈ 1 ms). Three ways to have the bytes resident: fault them in up front (`bench -p N`: `MADV_POPULATE_READ` in 64 MiB pieces on N threads), let queries fault what they read (lazy), or read the file into anonymous memory (`bench -m N`: parallel `pread`s into a `MADV_HUGEPAGE` buffer).

**Writing them.** From the store: the load (450 s on this VM, 359 s on the bench VM; peak RSS 110.7 GB), then the full pack written to pd-balanced in 237 s (48.3 GB at 203 MB/s; process peak 116 GB). The slim pack from the full pack (mapped): 29 s to build, 152 s to write 30.3 GB (peak RSS 58 GB, 28 GB of it anonymous). In production both are a Batch step's output, so a serving box never pays the load's peak.

**Process start → first answer** (the gcs root's plain view, 16 threads; seconds since `exec`, from `/proc/self/stat`). "Prefault" maps the file and faults all of it in before the query; "lazy" maps it and lets the query fault what it reads; "into RAM" reads the file into anonymous huge-page memory first. Caches dropped (`sync; echo 3 > drop_caches`) before each, except the page-cache row:

| Storage (read MiB/s measured) | Full pack (48.3 GB): prefault | lazy | into RAM | Slim pack (30.3 GB): prefault | lazy | into RAM |
|---|---:|---:|---:|---:|---:|---:|
| page cache (already resident) | 0.7–0.9 | – | – | 0.2–0.35 | – | – |
| pd-balanced, 200 GB (196) | 235.6 | 57.2 | 235.6 | 147.9 | 43.2 | 147.8 |
| pd-ssd, 1 TB (720) | 64.6 | 16.0 | 64.6 | 40.4 | 12.1 | 40.3 |
| local NVMe, 4 × 375 GB RAID0 (2,811) | 17.1 | 4.9 | 17.1 | 10.5 | 3.7 | 10.5 |
| the store (copy 30 s + load), the first run's way | 480 | – | – | – | – | – |

The page-cache row is from the pd-ssd and NVMe runs: in `cold-pdb-full-cache.jsonl` other files' pages had evicted 9.8 GB of the pack first, so it isn't one.

- **Every way reads at the device's rate**: 196 MiB/s on pd-balanced (its 0.28 MiB/s per GB + 140 baseline for 200 GB), 720 on the 1 TB pd-ssd (0.48 per GB + 240), 2.8 GiB/s on the four NVMe disks. A device's time is the pack's size over that rate; mapping costs ≈ 1 ms, the header nothing.
- **Lazy answers sooner and pays later**: the root's plain view faults only the columns it reads (full: 11.7 GB of 48.3; slim: 8.9 GB of 30.3, its `vf`/`vt`/`size` + directories; ~20% more on NVMe, whose readahead reads further), so the first answer comes in 3.7 s (slim, NVMe) to 57 s (full, pd-balanced); the next query of another kind (a search: names + dictionary) faults its own columns then.
- **The 110–120 GB load peak goes away.** Mapped, the process holds no anonymous memory (≤ 2 MB) and its RSS is the file's pages: 45 GB (full) / 28.3 GB (slim) once all faulted, 10.8 / 8.2 GB after a lazy root query. Read into RAM, anonymous = the file's size and nothing more. A 64 GB box holds the full pack; the slim pack's queries touch 24 GB, so a 32 GB box holds them all (§ small-RAM below).
- **Huge pages don't change query speed**: the same packs read into `MADV_HUGEPAGE` memory answer within noise of the mapped page cache (root 0.58 vs 0.57 s full, 0.12 s slim). The full index answers the root slower on this VM than on the bench VM (0.56–0.66 s vs 0.45 s, same binary and data), a VM difference, so this section compares the two indexes on this VM only.

### The slim index (`src/slim.rs`, `src/slim_scan.rs`)

Object versions only; the tree kept as structure; each column group its own section, so a query reads only what it needs:
- **Per object version** (717,920,013 rows, depth-first path order, a path's versions together): `vf`/`vt` (u16 scan ordinals) and `size` (u64), read for every row a pass sums; `name` (u32), read only by a search (and to hydrate emitted object nodes); `owner` (u16) and `cls` (u8: class bits, complex flag), read only under an owner or class scope; `cont`, one bit per row ("another version of the previous row's path"), read only where object paths are nodes (inside the window).
- **Per directory** holding at least one object version (244,035,402; the first run's 249,206,212 less 5,170,810 that never held one): `ddepth` (u8), `dend` (u32, its subtree's last directory), `dlo`/`dhi` (u32, its subtree's object rows), `dname` (u32, search and hydration). Checkpoints every 4,096 directories hold the ancestor chain (chunk seeds, paths). A path that is a directory in some scans and an object in others is one directory whose own versions lead its rows (`dself`; gcs has none).
- **Dropped:** directory rows and their rolled-up versions (`dir_*`, 12.6 GB), `end`/`depth`/`flags` per row, the per-row directory structure. Every total is a sum over object versions, so the `@dirs` view (a walk of directory totals) isn't answerable from it; its 27 queries are out of this comparison.
- **Hydration:** names are looked up only for the ≤ M emitted nodes, at assembly. The first run's answers carry only paths, bytes and file counts, so no other output column (mtime, last read) exists to hydrate yet; a color-by lens would read `owner`/`cls` only for the rows it sums (not in the query mix, not measured).

**The pass.** A chunk of object rows walks the directories whose first object falls in it, merged with the rows: the objects between two directory starts are the deepest open directory's direct children. A directory at the window's deepest level is one flat sum over `vf`/`vt`/`size` of its whole subtree (branchless, vectorized), never visiting the directories under it, so a plain view reads 12 bytes per object version and visits only the window's directories. Object paths inside the window are nodes (`cont` groups their versions). A search walks every directory below the window in one loop with per-depth bounds (no frames), each path's name atoms checked once; a directory whose matched atoms decide the expression for every descendant (a superset table over the atom masks) is summed flat (true) or skipped (false) whole. Chunk seeds, partial nodes, the threshold's lower bound and the assembly are the first run's. The name bitsets gained a summary bit per 64-id word (336 KB for 172M names, read first), which both indexes use.

**Verified:** every one of the 69 object-sum queries' answers equals the first run's (`bench -A`, at every thread count below; the first run's answers are the oracle-checked ones). `check-slim`: on all nine interval-store fixtures, the full pack's and the slim pack's answers equal the store's (1 and 4 threads, and with 1-row chunks so chunk edges fall everywhere): 7,920 slim answers per chunking, 0 different.

**Bytes per column group** (`fsb slim`'s output against the first run's full index; per object version = over 717,920,013):

| Column group | Read by | Full index GB | Slim GB | Slim B / object version |
|---|---|---:|---:|---:|
| validity (`vf`, `vt`) | every summed row | 3.87 | 2.87 | 4.00 |
| size | every summed row | 7.74 | 5.74 | 8.00 |
| structure | full: every row (`depth`, `end`, `flags`); slim: window directories, `cont` in the window | 5.80 | 3.26 | 4.55 |
| names (`name`, `dname`) | search; hydration of emitted nodes | 3.87 | 3.85 | 5.36 |
| dictionary: search buffer | search | 12.46 (together) | 7.08 | 9.86 |
| dictionary: offsets + original spellings | hydration, `find` | | 5.38 | 7.49 |
| owner | owner scope | 1.93 | 1.44 | 2.00 |
| class | class scope | (in `flags`) | 0.72 | 1.00 |
| directory versions | `@dirs` view | 12.61 | – | – |
| **total** | | **48.29** | **30.34** | **42.26** |

- A plain view, filter-free, reads 12 B per object version plus the window's directories: **8.6 GB at the root**, against the full index's 14 B per row over 967M rows (13.5 GB). The plain-view core (validity, size, directory structure) is 11.8 GB; everything else is touched only by the query kinds that need it.
- **The dictionary is now 41% of the slim index** (12.5 GB for 172M names): 7.1 GB lowercase buffer that every search scans, and 5.4 GB of offsets (1.4 GB) and original spellings of names whose lowercase differs (~4 GB). The spellings could be a case mask over the lowercase bytes; the search buffer is design B's territory (a suffix structure instead of a scan).
- `size` is two thirds of the plain-view rows' bytes. Not tried: a u32 size with an overflow table, or sizes delta-coded per path.

### Latency by thread count (stand-ins for n2-highmem-2/4/8/16)

Each thread count is its own process pinned with `taskset` to that many vCPUs as whole cores (on this VM vCPU k and k+8 are one core's two hyperthreads: 2 threads = `0,8`, 4 = `0-1,8-9`, 8 = `0-3,8-11`), as an n2 of that size has them; the slim pack mapped and prefaulted from the page cache; the 69 object-sum queries, each cold once then warm ×5 (median); every answer at every thread count equal to the first run's. The full pack ran on all 16 vCPUs on the same VM for a like-for-like column. Caveat: a pinned process still has this host's whole L3 and memory bandwidth, so the small sizes are, if anything, optimistic.

| Query (warm median, ms) | slim, 2 vCPU | slim, 4 | slim, 8 | slim, 16 | full, 16 (this VM) | full, 16 (first run, bench VM) | Slim reads GB |
|---|---:|---:|---:|---:|---:|---:|---:|
| root | 653 | 336 | 176 | 107 | 556 | 452 | 8.62 |
| root-depth6 | 2,936 | 1,490 | 782 | 404 | 770 | 579 | 8.78 |
| root-month | 647 | 336 | 175 | 104 | 566 | 451 | 8.62 |
| bucket0 | 320 | 166 | 90.5 | 55.7 | 218 | 173 | 4.05 |
| bucket3 | 388 | 202 | 109 | 60.4 | 140 | 83.1 | 0.99 |
| d3-0 | 132 | 69.5 | 37.5 | 23.1 | 174 | 140 | 1.71 |
| d6-0 | 49.1 | 27.4 | 36.0 | 48.6 | 17.6 | 10.5 | 0.05 |
| diff-day-root | 832 | 420 | 213 | 127 | 658 | 548 | 8.62 |
| diff-month-root | 834 | 420 | 214 | 116 | 658 | 544 | 8.62 |
| rare0 | 7,454 | 3,800 | 1,908 | 1,018 | 1,272 | 1,193 | 14.10 |
| common-zarr.json | 7,763 | 3,979 | 1,999 | 1,081 | 1,313 | 1,225 | 14.95 |
| common-ckpt | 7,445 | 3,795 | 1,920 | 1,014 | 1,257 | 1,194 | 14.10 |
| and | 8,688 | 4,414 | 2,295 | 1,245 | 1,471 | 1,380 | 21.18 |
| anchor-exact | 7,759 | 3,941 | 2,011 | 1,059 | 1,270 | 1,218 | 14.95 |
| regex | 10,571 | 5,385 | 2,723 | 1,374 | 1,627 | 3,712 | 14.10 |
| path-term | 12,520 | 6,456 | 3,377 | 1,771 | 2,129 | 2,206 | 28.25 |
| owner-one | 1,996 | 1,000 | 502 | 251 | 788 | 623 | 10.05 |
| unowned | 2,000 | 999 | 502 | 252 | 995 | 787 | 10.05 |
| class-one | 1,226 | 614 | 309 | 158 | 721 | 628 | 9.33 |
| class-owner | 2,841 | 1,420 | 711 | 356 | 780 | 637 | 10.77 |
| staged | 7.7 | 5.0 | 4.0 | 3.5 | 675 | 555 | 0.00 |
| staged-diff-month | 7.5 | 4.9 | 3.7 | 3.1 | 927 | 721 | 0.00 |

By kind (median over the kind's queries, max in parentheses):

| Kind | Queries | slim, 2 vCPU | slim, 4 | slim, 8 | slim, 16 | full, 16 (this VM) | full, 16 (first run) | Slim reads (GB / query, median) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| folder/root | 2 | 1,794 (2,936) | 913 (1,490) | 479 (782) | 256 (404) | 663 (770) | 516 (579) | 8.70 |
| folder/bucket | 6 | 123 (388) | 63.4 (202) | 35.0 (109) | 22.1 (60.4) | 101 (218) | 66.1 (173) | 0.96 |
| folder/d3 | 2 | 66.0 (132) | 35.0 (69.5) | 19.0 (37.5) | 11.8 (23.1) | 87.3 (174) | 70.2 (140) | 0.85 |
| folder/d6 | 2 | 24.6 (49.1) | 13.7 (27.4) | 18.1 (36.0) | 24.3 (48.6) | 8.9 (17.6) | 5.3 (10.5) | 0.03 |
| folder/old | 2 | 390 (647) | 203 (336) | 107 (175) | 63.6 (104) | 368 (566) | 295 (451) | 5.16 |
| diff/day | 3 | 411 (832) | 210 (420) | 110 (213) | 63.8 (127) | 249 (658) | 210 (548) | 4.05 |
| diff/month | 3 | 411 (834) | 212 (420) | 110 (214) | 63.8 (116) | 250 (658) | 207 (544) | 4.05 |
| search/rare | 4 | 7,448 (7,454) | 3,786 (3,800) | 1,920 (1,941) | 1,016 (1,057) | 1,268 (1,273) | 1,195 (1,200) | 14.10 |
| search/common | 6 | 5,527 (7,763) | 2,803 (3,979) | 1,423 (1,999) | 755 (1,081) | 884 (1,313) | 825 (1,225) | 12.66 |
| search/bool | 4 | 8,945 (9,219) | 4,533 (4,648) | 2,362 (2,386) | 1,259 (1,269) | 1,470 (1,501) | 1,389 (1,399) | 22.28 |
| search/anchor | 3 | 7,759 (9,057) | 3,941 (4,646) | 2,011 (2,338) | 1,059 (1,237) | 1,270 (1,455) | 1,240 (1,297) | 14.95 |
| search/regex | 2 | 8,252 (10,571) | 4,185 (5,385) | 2,125 (2,723) | 1,070 (1,374) | 1,274 (1,627) | 3,251 (3,712) | 11.56 |
| search/path | 2 | 11,461 (12,520) | 5,960 (6,456) | 3,071 (3,377) | 1,597 (1,771) | 1,927 (2,129) | 2,099 (2,206) | 24.72 |
| search/diff | 1 | 8,087 (8,087) | 4,152 (4,152) | 2,108 (2,108) | 1,111 (1,111) | 1,342 (1,342) | 1,309 (1,309) | 14.95 |
| search/old | 1 | 7,555 (7,555) | 3,827 (3,827) | 1,969 (1,969) | 1,034 (1,034) | 1,284 (1,284) | 1,207 (1,207) | 14.35 |
| owner | 6 | 1,997 (2,000) | 999 (1,000) | 501 (502) | 252 (252) | 796 (1,009) | 630 (795) | 10.05 |
| owner/diff | 1 | 2,551 (2,551) | 1,273 (1,273) | 639 (639) | 320 (320) | 1,045 (1,045) | 791 (791) | 10.05 |
| owner/search | 1 | 9,142 (9,142) | 4,673 (4,673) | 2,367 (2,367) | 1,227 (1,227) | 1,362 (1,362) | 1,245 (1,245) | 14.41 |
| class | 3 | 1,226 (1,226) | 613 (614) | 308 (309) | 157 (158) | 721 (922) | 628 (726) | 9.33 |
| class/search | 1 | 9,518 (9,518) | 4,830 (4,830) | 2,464 (2,464) | 1,286 (1,286) | 1,390 (1,390) | 1,252 (1,252) | 15.02 |
| class/owner | 1 | 2,841 (2,841) | 1,420 (1,420) | 711 (711) | 356 (356) | 780 (780) | 637 (637) | 10.77 |
| staged | 1 | 7.7 (7.7) | 5.0 (5.0) | 4.0 (4.0) | 3.5 (3.5) | 675 (675) | 555 (555) | 0.00 |
| staged/owner | 1 | 7.0 (7.0) | 4.5 (4.5) | 3.5 (3.5) | 2.9 (2.9) | 675 (675) | 555 (555) | 0.00 |
| staged/diff | 1 | 7.5 (7.5) | 4.9 (4.9) | 3.7 (3.7) | 3.1 (3.1) | 927 (927) | 721 (721) | 0.00 |
| staged/search | 1 | 9,420 (9,420) | 4,776 (4,776) | 2,421 (2,421) | 1,261 (1,261) | 1,385 (1,385) | 1,243 (1,243) | 14.95 |
| search/d6 | 2 | 550 (568) | 290 (297) | 167 (170) | 101 (106) | 117 (119) | 96.2 (97.9) | 7.11 |
| diff/d6 | 2 | 26.4 (52.6) | 14.8 (29.4) | 19.3 (38.4) | 24.2 (48.2) | 8.2 (16.3) | 6.1 (12.1) | 0.03 |
| diff/bucket | 5 | 104 (435) | 58.5 (230) | 28.4 (121) | 19.5 (66.7) | 71.4 (232) | 62.0 (187) | 0.92 |

- **Plain views scale with cores and fit small boxes**: the fleet root 0.65 s on 2 vCPU (one core), 0.34 s on 4, 0.18 s on 8, 0.11 s on 16. That is 5× the full index on the same 16 vCPU (0.56 s; 0.45 s on the bench VM). It reads 8.6 GB, 12 B per object version. A month back or a diff costs the same (0.10–0.13 s on 16 vCPU). On an n2-highmem-4 the root's plain view meets p50 ≤ 0.5 s; the full index needed 16 vCPU.
- **Scopes are a column more**: an owner scope 1.0 s on 4 vCPU, 0.50 on 8, 0.25 on 16 (+2 B/row); a class scope 0.6 / 0.31 / 0.16 (+1 B/row); both together 0.36 s on 16. A staged scope reads only the staged prefixes' rows, so 3–8 ms at any size.
- **Search doesn't fit a small box**: a rare term at the root takes 7.5 s on 2 vCPU, 3.8 on 4, 1.9 on 8 and 1.0 on 16 (full index on this VM 1.27 s, bench VM 1.19 s). Two atoms take 1.25 s, a path term 1.8 s, a regex 1.4 s on 16. It reads every row's `name` and every directory's (`vf`/`vt`/`size` only for matching paths) and scans the 7.1 GB dictionary: ~14 GB per one-atom search at the root.
- **Drill-downs are cheap everywhere**: a bucket takes up to 390 ms on 2 vCPU and up to 60 ms on 16; the larger depth-3 folder takes 132 ms on 2 vCPU and 23 ms on 16. Depth-6 folders cost 30–50 ms at every size, more than the full index's 18 ms: that folder is a flat directory of 4.2M objects, every one a node of the window, and the assembly that keeps the ones over the threshold is serial (not optimized).
- **Hyperthreads**: an n2's vCPUs are hyperthreads, two per core, so cores set the latency. 8 threads on 8 whole cores (`taskset -c 0-7`) almost match all 16 vCPUs on scans that compute per row: a rare search 1.08 s (16 vCPU 1.02, 4 cores × 2 1.91), an owner scope 262 ms (251; 502). The memory-bound plain views gain more from the second hyperthread: root 149 → 107 ms. So an n2-highmem-8 (4 cores) serves at about the 8-thread column, not half of the 16.

### Page-cache serving on a small-RAM budget

The slim pack served straight from the page cache with the process's memory capped, the cap covering the page cache it faults in (cgroup v2: `sudo systemd-run --scope -p MemoryMax=… -p MemorySwapMax=0`, `run-slim cap`). Caches dropped first, the pack on the 200 GB pd-balanced disk (196 MiB/s), mapped lazily, 16 threads. The queries run in drill-down order: depth-6 and depth-3 folders, the six buckets, the root, the root six deep, a month diff, an owner, a class and a staged scope, then three searches. Each runs once cold (first touch, in that order), then three times warm (back to back); the GB columns are what the process read from the disk (`/proc/self/io`). 32 GB and 16 GB run the whole list; 8 GB runs it without the searches.

| Query | 32 GB: cold ms (GB read) | warm | 16 GB: cold | warm | 8 GB: cold | warm |
|---|---:|---:|---:|---:|---:|---:|
| d6-0 | 444 (0.09) | 44.6 (0.00) | 445 (0.09) | 45.9 (0.02) | 441 (0.09) | 44.2 (0.01) |
| d6-1 | 127 (0.02) | 0.13 (0.00) | 131 (0.02) | 0.26 (0.00) | 124 (0.02) | 0.34 (0.00) |
| d3-0 | 10,058 (2.08) | 31.1 (0.00) | 10,028 (2.07) | 32.0 (0.60) | 10,039 (2.08) | 38.0 (0.28) |
| d3-1 | 847 (0.14) | 0.72 (0.00) | 854 (0.14) | 1.3 (0.05) | 871 (0.14) | 1.4 (0.04) |
| d3-0-month | 32.2 (0.00) | 29.8 (0.00) | 34.8 (0.00) | 28.2 (0.00) | 35.4 (0.00) | 31.7 (0.00) |
| bucket1 | 2,644 (0.56) | 36.0 (0.00) | 2,647 (0.56) | 36.6 (0.15) | 2,648 (0.56) | 45.5 (0.05) |
| bucket4 | 1,552 (0.32) | 6.0 (0.00) | 1,547 (0.32) | 8.8 (0.10) | 1,545 (0.32) | 6.4 (0.01) |
| bucket5 | 1,237 (0.25) | 6.2 (0.00) | 1,242 (0.25) | 6.0 (0.08) | 1,261 (0.25) | 6.1 (0.02) |
| bucket2 | 4,866 (1.00) | 18.8 (0.00) | 4,956 (1.00) | 25.7 (0.31) | 4,920 (1.00) | 21.5 (0.07) |
| bucket3 | 4,895 (1.00) | 60.8 (0.00) | 4,889 (1.01) | 63.5 (0.32) | 4,910 (1.00) | 71.6 (0.28) |
| bucket0 | 20,620 (4.24) | 64.9 (0.00) | 20,704 (4.24) | 77.7 (1.38) | 20,705 (4.25) | 170 (0.69) |
| root | 132 (0.02) | 138 (0.00) | 166 (0.02) | 166 (0.25) | 6,393 (1.32) | 4,640 (1.07) |
| root-depth6 | 1,897 (0.39) | 443 (0.00) | 1,855 (0.39) | 438 (0.16) | 9,875 (2.02) | 8,713 (1.49) |
| diff-month-root | 163 (0.01) | 146 (0.00) | 171 (0.01) | 142 (0.00) | 8,108 (1.67) | 5,640 (1.22) |
| owner-one | 7,012 (1.45) | 262 (0.00) | 6,943 (1.45) | 398 (0.52) | 20,560 (4.23) | 21,776 (4.35) |
| class-one | 3,441 (0.71) | 171 (0.00) | 3,439 (0.71) | 196 (0.27) | 19,860 (4.08) | 15,624 (3.68) |
| staged | 539 (0.09) | 4.4 (0.00) | 550 (0.09) | 5.0 (0.04) | 1,138 (0.20) | 5.2 (0.07) |
| rare0 | 58,501 (12.04) | 1,049 (0.00) | 60,771 (12.51) | 9,602 (4.61) | – | – |
| common-ckpt | 1,444 (0.10) | 1,064 (0.00) | 1,534 (0.14) | 1,083 (0.04) | – | – |
| regex | 6,818 (1.22) | 1,443 (0.00) | 6,853 (1.23) | 1,460 (0.52) | – | – |

Every answer under every cap equals the first run's (20, 20 and 17 checked).

- **32 GB holds everything these queries touch** (peak RSS 23.9 GB: the plain-view core, owner and class columns, names, dictionary). Each region's first query reads its rows at the disk's rate (a depth-3 folder 2.1 GB in 10 s, the largest bucket 4.2 GB in 21 s), and once the six buckets have been read the root is warm (0.02 GB). A search's first run reads its 12 GB (dictionary, names, directories) in 58 s. Warm, everything runs at the in-RAM speed of the thread table (root 0.14 s, a search 1.05 s, an owner scope 0.26 s). An n2-highmem-4 (32 GB, 4 vCPU) is this case: plain views 0.34 s, scopes 0.6–1 s, search 3.8 s.
- **16 GB holds either side, not both**: plain views and scopes (core 11.8 GB + `owner`/`cls` 2.2) stay resident and run at in-RAM speed, and so does a search's 14 GB, but switching between them re-reads. After the three cold searches, the first warm runs of the largest bucket (20 s, 4 GB), the root (3.6 s) and an owner scope (7.3 s) re-read, then they are back to 72 ms, 0.15 s and 0.26 s. The rare search's three warm runs took 58 s, 9.6 s, 1.5 s. An n2-highmem-2 is this case.
- **8 GB serves drill-downs, not the fleet**: folders whose rows fit are served warm (the largest bucket settles at 72 ms after two runs), but the root's 8.6 GB of rows don't fit: a plain view of the root takes 4.6 s (median of three warm runs; 2.9–8.7 s across the root views), re-reading 1–1.5 GB each run. An owner or class scope at the root (10 GB) re-reads everything each time (16–22 s). Searches weren't run at 8 GB.
- **Region order matters cold**: drilling down first warms what the root needs, so the root's first answer costs only what its children hadn't read. A server could warm the plain-view core (11.8 GB) at startup at the disk's rate (60 s on pd-balanced, 16 s on the 1 TB pd-ssd, 4 s on local NVMe) and fault the search columns in on first use.

## Cost model (filled from the bench)

- **Resident bytes:** for gcs alone, and projected for OA's fleets together (gcs, cw, hf, r2) from their stores' version counts.
- **The smallest machine** whose RAM holds that, and the thread count that meets each of these on the gcs root:
  - p50 ≤ 0.5 s;
  - max ≤ 2 s.
- Its monthly cost at list price, on-demand, committed and spot.
- **Spot needs a fallback** while preempted: the Worker's current reads.
- **Lambda fan-out:** shards of 250 MB in path order; the root's shard count; cold load time measured from S3 on one shard; warm scan time per shard; cost per root query.

Filled (2026-11-12), from gcs's measured load:
- **Resident bytes, gcs:** 48.3 GB for 1.35B version rows (967M packed rows). Of it, 23.2 GB is what a pass reads (24 B/row: `name` 4, `depth` 1, `end` 4, `flags` 1, `size` 8, `vf`/`vt` 4, `owner` 2), 12.5 GB the name dictionary (172M names), 12.6 GB the directory versions only the directory-total view reads. Without that view: 35.7 GB. Per object version, 67 B all in (50 B without directory versions).
- **OA's fleets together:** not measured; cw's, hf's and r2's stores' version counts are needed (the packed size is ~36 B per version row read, ~50 B per packed row with the directory view).
- **Machine:** the index fits a 64 GB box, but the loader as built peaks at 110 GB, so today a box that loads in place needs ~128 GB (n2-highmem-16, the bench VM); a 64 GB server would load a packed file written elsewhere (not built). Threads: the gcs root's plain view meets p50 ≤ 0.5 s at 8 cores / 16 threads (0.45 s); scaling was ~7× from 1 to 16 threads, so 4 cores would be ~0.9 s. From directory rows, plain views are milliseconds on any of these. Search and scoped views need more (1.2–2.2 s at the root on 16 threads).
- **Monthly cost:** not looked up here. For orientation only: n2-highmem-16 lists at roughly $0.95/h on demand (≈ $690/month), spot a fraction of that; check current list, committed-use and spot prices before deciding.
- **Spot fallback:** unchanged (the Worker's current reads); the restart cost is the copy (30 s) plus the load (6 min).
- **Lambda fan-out:** not measured.
- **Since (2026-11-13, § Slim index, cold start, small RAM):** the packed file is built (a mapped pack has no load peak: first answer = pack size ÷ disk rate, 10.5 s for the slim pack from local NVMe, 40 s from a 1 TB pd-ssd, 148 s from 200 GB pd-balanced). The slim index holds gcs in 30.3 GB, of which its queries touch 24 GB. So an **n2-highmem-4** (32 GB, 2 cores) serves plain views in 0.34 s and owner/class scopes in 0.6–1.0 s at the root, but searches take 3.8 s. An n2-highmem-16 serves searches in 1.0–1.8 s.

## Not in scope

- Serving it to the site. The box, the Worker hand-off and the edge cache are a later decision if the numbers are good.
- Daily load. A first cut reloads the whole store; incremental appends come later if A is chosen.
- Designs B and C. They get their own spec once A's numbers exist.

## Open

- #18 (`open-questions.md`): settled for this bench by reading gcs's store on a GCP VM in its region.
- The loader's 110 GB peak: built away (§ Slim index, cold start, small RAM): `fsb pack` / `fsb slim` write the packed columns once and a server maps them, with no load peak. Not built: the Batch step that writes the packs after the store's append (`fsb pack` + `fsb slim`, or `slim` straight from the loader without the full pack). Neither is incremental yet.
- Search cost: a bitset lookup per path misses cache; renumbering names so a folder's names sit together, or a per-folder name filter, would cut it. Not tried.
- **Search on a small box** (§ Slim index): a search is a whole pass over names plus a dictionary scan, ~4 s at the gcs root on 4 vCPU and ~8 s on 2; only plain views, scopes and drill-downs meet the targets there. That is designs B/C's question.
- **The dictionary is 41% of the slim index** (12.5 GB): the original spellings (~4 GB) could be a case mask over the lowercase buffer; the 7.1 GB search buffer is what B would replace.
- **Output columns beyond paths and sums** (mtime, last read) aren't in the index; when added, they are hydrated per emitted node (≤ M), never read by the pass.
- The recorded Batch run (`batch/submit`) is written, not submitted: it waits for a go, and for the output bucket the job may write to.
- Lambda fan-out and OA's fleets together: not measured.
