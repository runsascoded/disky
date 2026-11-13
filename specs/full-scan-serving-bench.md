# Full-scan serving: benchmark design A at fleet scale

Status: prototype built and measured, 2026-11-12 (§ Results). Ryan's go ("go", after the index-rethink discussion). Prototype on a side branch (`w/full-scan-bench`), measured on the GCP VM `disky-full-scan-bench` (n2-highmem-16, us-east1-c, beside gcs's store), never on the laptop. Nothing here touches a deployment.

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

## Not in scope

- Serving it to the site. The box, the Worker hand-off and the edge cache are a later decision if the numbers are good.
- Daily load. A first cut reloads the whole store; incremental appends come later if A is chosen.
- Designs B and C. They get their own spec once A's numbers exist.

## Open

- #18 (`open-questions.md`): settled for this bench by reading gcs's store on a GCP VM in its region.
- The loader's 110 GB peak: a load that writes the packed columns to a file once (and a server that maps it) would serve from a 64 GB box; not built.
- Search cost: a bitset lookup per path misses cache; renumbering names so a folder's names sit together, or a per-folder name filter, would cut it. Not tried.
- The recorded Batch run (`batch/submit`) is written, not submitted: it waits for a go, and for the output bucket the job may write to.
- Lambda fan-out and OA's fleets together: not measured.
