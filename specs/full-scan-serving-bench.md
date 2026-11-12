# Full-scan serving: benchmark design A at fleet scale

Status: spec, 2026-11-12. Ryan's go ("go", after the index-rethink discussion). Prototype on a side branch (`w/full-scan-bench`), measured on node `mgu`, never on the laptop. Nothing here touches a deployment.

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

**gcs's interval store** (R2 `oa-gcs-usage-index`, `CURRENT.json`'s generation, base + runs), the v2 scans only (2026-09-30 on). Before 09-30, scans are dirs-only.

At 10-09 it held:
- 1,156,324,516 folded versions over 71 scans;
- `path` sort 16.79 GB;
- 609M open versions at the newest scan.

Owner slices come from its `slices` sort.

Reading it needs an R2 read credential for that bucket; see open question #18. Until then, develop against the r2 demo's store (`disk-tree-demo`), which is small and is ours on `cloud`.

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

## Oracle

DuckDB over the same store's rows, on mgu, for every bench query: per-node sums by the same semantics, written as plain SQL with no shared code. Each answer must equal the oracle's exactly: same nodes, bytes and counts. A disagreement fails the run.

## Bench

Run on `mgu` (m6g.4xlarge: 16 vCPU, 64 GB). If the packed size leaves no room, ask before resizing.

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
