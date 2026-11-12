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

<!-- RESULTS:VERIFIED -->

### gcs

<!-- RESULTS:GCS -->

### Synthetic scaling

<!-- RESULTS:SYNTH -->

## Cost model (filled from the bench)

- **Resident bytes:** for gcs alone, and projected for OA's fleets together (gcs, cw, hf, r2) from their stores' version counts.
- **The smallest machine** whose RAM holds that, and the thread count that meets each of these on the gcs root:
  - p50 ≤ 0.5 s;
  - max ≤ 2 s.
- Its monthly cost at list price, on-demand, committed and spot.
- **Spot needs a fallback** while preempted: the Worker's current reads.
- **Lambda fan-out:** shards of 250 MB in path order; the root's shard count; cold load time measured from S3 on one shard; warm scan time per shard; cost per root query.

## Not in scope

- Serving it to the site. The box, the Worker hand-off and the edge cache are a later decision if the numbers are good.
- Daily load. A first cut reloads the whole store; incremental appends come later if A is chosen.
- Designs B and C. They get their own spec once A's numbers exist.

## Open

- #18 (`open-questions.md`): the R2 read credential for gcs's store, or the store copied to S3 by gcs's session.
