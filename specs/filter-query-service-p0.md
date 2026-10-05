# Filter query service, phase 0: measurements

Status: measured 2026-10-02 on gcs's 2026-10-01 generation (`listing/2026-10-01/index/20261002T113031Z/`, 778M `path` rows, 95,017 row groups; v1 search sidecars: 127.3M names, 5.23B trigram postings). This is phase 0 of [`filter-query-service.md`]. Measurement only: nothing in the product or infra changed.

Absolute byte totals stay out of this doc (the repo is public). Byte coverage is given as percentages; the raw numbers are in the [job outputs].

## TL;DR

- **Names-first is the answer for indexed generations.** On a warm 16-vCPU VM: vocabulary filter 0.25–0.34 s (precomputed lowercase), mapping matched names to rows via v1 `rgs` 0.2–1.8 s, match roots 0.01–0.44 s. Root sets are identical to a full `path` scan for all four queries (same count, same md5 of the sorted list).
- **The `path` scan is CPU-bound, and slow.** The `path` column is 3.6 GiB compressed but 95 GiB decoded. A warm local scan takes 34–35 s on 16 threads, 40 s on 8 and 77 s on 4. Cold, it takes 219 s through gcsfuse and 288 s through DuckDB `httpfs`. The spec's 10–30 s estimate for the fallback doesn't hold at Cloud Run sizes.
- **Cold start means "copy, then load locally".** Parallel ranged GETs pull the names file (1.56 GB) in 2.7–4.7 s and `path` (8.3 GB) in 11–13 s (550–810 MB/s). Loading the vocabulary from local disk then takes 3.6 s. Loading it in place takes 86 s through gcsfuse and 146 s through `httpfs`.
- **Memory:** the vocabulary `(id, name)` takes 9.5–11.4 GB in DuckDB (6.6 GB as Arrow). Adding a precomputed `lower(name)` brings it to 19.3 GB. Pinning only `(id, lower(name))` is enough for case-insensitive filtering.
- **Match volume is smaller than the spec assumed.** `safetensors` matches 89,122 rows (not millions) in 55,426 parent dirs. Roots plus the ancestor rollup take under 1 s. Grouping by parent saves nothing: the median parent holds 1 match, and only 50 parents match completely.
- **The trigram candidates were mostly true matches.** The Worker's candidate counts (spec §1) equal the exact matched-name counts for `grug` (8,308 vs 8,307), `safetensors` (1,327 vs 1,327) and `ckpt -eval` (166 vs 166). `tomat` is the exception: 52,586 candidates against 21,740 matches. So the Worker's limit is **verification and match volume, not candidate precision**. `tomat` really has **21,739 match roots**: the 4 `tomat` dirs hold 99.6% of the bytes, and the rest are mostly podcast mp3s ("Rotten Tomatoes") and names like `Automat…`.
- **Most of the index is numbered names.** Names with step, shard or long numbers (`part-00042-of-05047…`, `1234567_warc_examples.parquet`) are 74.6% of names and 76.8% of postings. Hex hashes add 21.4% of names and 20.5% of postings. **Templating** (digit runs → `D`, hex runs of 8 or more → `H`, UUIDs → `U`) collapses 127.3M names into **2.16M templates**, and 5.23B postings into **70.6M** (74× fewer).
- **Size/depth skew:** indexing only names whose largest occurrence is ≥ 1 GiB, or that occur at depth ≤ 3, keeps 0.72M names and 23.8M postings (0.46%). It still finds ≥ 99.58% of the matched bytes for all four queries, but few of the roots for scattered file-name terms (`tomat`: 5 of 21,739).

## Setup

- **Jobs** (GCP Batch, `us-east1`, n2-highmem-16 with 128 GB RAM and 2×375 GB local SSD, image `gcs-usage-snapshot:latest` with an entrypoint override):

  | job | what | run time |
  |---|---|---:|
  | `p0-a-20261002-152434r` | A: cold vs warm `path` scan, names-first, `safetensors` grouping | 1,331 s |
  | `p0-b-20261002-152437r` | B: name classes, templating, skew | 672 s |
  | `p0-c-20261002-154432` | C: direct-`httpfs` cold reads, scan floors, thread scaling | 933 s (the last, optional Arrow step failed; see Caveats) |

  Total: **2,936 s ≈ 49 min** of n2-highmem-16. Two earlier submissions (`p0-{a,b}-20261002-152214`) were rejected at scheduling and never got a VM: n2-highmem-16 needs 0/2/4/8… local SSDs, not 1. One more (`p0-c-20261002-154403`) had the wrong part argument and was deleted while still queued (0 s).
- **Scripts and outputs:** `gs://oa-gcs-usage-dvx/scratch/p0/{scripts,out}/` ([job outputs]). Each job's `*.json` holds every timed step, with `s`, `peak_rss` (VmHWM after a `clear_refs` reset) and step-specific counts.
- **DuckDB** 1.5.6, `threads=16`, `memory_limit=100GB`, spill to local SSD. **pyarrow** 22.0.0.
- **"Cold"** means first read from GCS, either through a Batch gcsfuse volume (`--implicit-dirs`) or through DuckDB `httpfs` with a bearer-token HTTP secret against `storage.googleapis.com`. **"Warm"** means a local-SSD copy made with `transfer_manager.download_chunks_concurrently` (64 MiB chunks, 32 workers). After the first pass it is also OS-page-cached, since 128 GB RAM holds the 8.3 GB file.
- **Query semantics:** case-insensitive substring match on the last path segment (`regexp_extract(path, '[^/]*$')`, as `search.py` does). `ckpt -eval` means `contains(l, 'ckpt') AND NOT contains(l, 'eval')`, per name. Dir rows carry one row per owner slice, so slices are summed per path first.
- **Match roots:** matched paths with no matched proper ancestor, found by exploding each matched path's ancestors and anti-joining. Totals come from the root rows' own aggregates (`Σ size`, `Σ n_files` over slices). For every dir root, they were **verified against a descendant-file sum** and matched exactly for all four queries.

## A. Query strategies

### Results (identical across strategies)

| query | matched names | matched paths | roots (dir / file) | v1 `rgs` groups | names with NULL `rgs` | rows read via `rgs` | same root set in all 5 strategy/source runs |
|---|---:|---:|---:|---:|---:|---:|:---:|
| `tomat` | 21,740 | 21,748 | 21,739 (9 / 21,730) | 211 | 0 | 1.73M | yes (`5fbd9c42…`) |
| `grug` | 8,307 | 8,459 | 7,724 (7,251 / 473) | 59 | 0 | 0.48M | yes (`bff8ed35…`) |
| `safetensors` | 1,327 | 89,122 | 89,122 (2 / 89,120) | 288 | 0 | 2.36M | yes (`4dc3a871…`) |
| `ckpt -eval` | 166 | 176 | 170 (161 / 9) | 28 | 0 | 0.23M | yes (`feb97026…`) |

The five runs were: cold scan (gcsfuse), warm scan ×2, names-first cold and names-first warm. Job B's independent scan and job C's prefiltered scans (local and `httpfs`) reproduce the same md5s. No matched name had a NULL `rgs` (more than 512 row groups), so the full-scan fallback for those was never needed.

### 1. Cold `path` scan

| step | `tomat` | `grug` | `safetensors` | `ckpt -eval` |
|---|---:|---:|---:|---:|
| scan, gcsfuse, first read of the file | **218.9 s** | — | — | — |
| scan, gcsfuse, file already page-cached | — | 34.3 s | 34.6 s | 34.7 s |
| scan, `httpfs` cold (job C) | **287.6 s** | 35.1 s (DuckDB's cache warm) | — | — |
| scan, local SSD, run 1 / run 2 | 35.0 / 34.1 s | 34.3 / 34.4 s | 34.0 / 34.0 s | 34.3 / 34.3 s |
| roots (anti-join) | 0.08 s | 0.03 s | 0.43 s | 0.01 s |
| peak RSS during scan | 2.1–3.1 GB | | | |

What the scan costs (job C, local, `tomat`, 16 threads):

| variant | time |
|---|---:|
| `count(*) WHERE contains(path, 'tomat')` (case-sensitive, whole path): the decode floor | 13.1 s |
| `count(*) WHERE regexp_extract(name) ILIKE '%tomat%'` | 49.2 s |
| `count(*) WHERE path ILIKE '%tomat%'` | 108.8 s |
| full matched-rows query (regexp name + `lower` + `contains`) | 34–35 s |
| same, 8 threads / 4 threads | 40.0 s / 76.7 s |

- The `path` column is 3.59 GiB compressed but 95 GiB decoded (26:1), so a scan is decompression plus string work, not I/O. Even the floor (13 s at 16 threads) is out of request range.
- DuckDB's `ILIKE` is 3–8× slower than `contains(lower(…))`. The AST compiler should never emit `ILIKE`.
- Copying first is about 6× faster than reading through gcsfuse. The local copy took 11.4–13.0 s for 8.3 GB, then the scan took 34 s, about 46 s in total, against 219 s for the cold gcsfuse scan and 288 s for `httpfs`.

### 2. Names-first

| step | gcsfuse (cold) | `httpfs` (cold) | local SSD |
|---|---:|---:|---:|
| copy names file (1.56 GB) | — | — | 2.7–4.7 s |
| load vocab `(id, name)` into DuckDB | 86.0 s | 146.0 s | 3.6 s |
| load vocab `(id, name, lower(name))` | | | 4.9 s |
| load vocab `(id, name)` with `pq.read_table` | | | 5.6 s |

| vocabulary in memory | size |
|---|---:|
| `(id, name)`, DuckDB (`duckdb_memory()`) | 9.5–11.4 GB (process RSS ≈ 10.4 GB) |
| `(id, name)`, Arrow `nbytes` | 6.6 GB |
| `(id, name, l)`, DuckDB | 19.3 GB |
| `name` column decoded, from the parquet footer | 5.67 GiB |

Per query, warm (cold-mount numbers in parentheses where they differ):

| step | `tomat` | `grug` | `safetensors` | `ckpt -eval` |
|---|---:|---:|---:|---:|
| filter, precomputed `l` | 0.29 s | 0.25 s | 0.29 s | 0.34 s |
| filter, `contains(lower(name), …)` | 1.31 s | 1.28 s | 1.32 s | 1.37 s |
| filter, `name ILIKE '%…%'` | 6.31 s | 6.26 s | 6.42 s | 6.33 s |
| `rgs` lookup (re-reading the names parquet by `id`) | 3.8 s (4.9) | 3.9 s | 3.6 s | 3.4 s |
| read the `rgs` row groups (pyarrow), filter by name | 1.80 s (2.96) | 0.36 s | 1.72 s (1.70) | 0.20 s |
| roots | 0.08 s | 0.03 s | 0.43 s | 0.01 s |
| **total with pinned `l` + pinned `rgs`** (excluding the `rgs` lookup) | **≈ 2.2 s** | **≈ 0.6 s** | **≈ 2.4 s** | **≈ 0.6 s** |

- The `rgs` lookup costs 3.4–4.9 s because names are in impact order, so the ids a query needs are scattered across the file's row groups. Pin `rgs` alongside the vocabulary (1.03 GiB decoded) or use the v2 name-major rows file. Don't re-read it per query.
- The row mapping reads whole `path` row groups: 2.36M rows to get `safetensors`' 89k, and 1.73M for `tomat`'s 21.7k. It was a single pyarrow `read_row_groups` call, not tuned. v2's name-major rows file is not automatically better. It reads at least one 1024-row group per matched name, because ids are in impact order and so scattered. For `safetensors` that is about 1,327–1,414 groups (≈ 1.4M rows, a little better than v1). For `tomat` it is up to 21,740 groups (≈ 22M rows, much worse than v1's 1.73M). So the row-map layout should be measured on a v2 generation before choosing between them.

### 3. `safetensors` match volume

| measure | value |
|---|---:|
| matched rows = roots | 89,122 (89,120 files, 2 dirs) |
| distinct parent dirs | 55,426 |
| roots per parent: median / p90 / p99 / max | 1 / 3 / 8 / 186 |
| parents whose direct children all match (vs `max(n_children)` over slices) | 50 |
| group by parent | 0.27 s |
| ancestor rollup (every ancestor of every root, with bytes) | 0.36 s → 122,065 ancestors |
| ancestors with bytes ≥ 1e-3 / 1e-4 / 1e-5 of the largest | 1,298 / 13,510 / 83,235 |

Per the spec, where a whole directory matches, its dir aggregate is used instead of its files. That doesn't happen here: almost no parent matches completely. The ancestor rollup is already cheap at this volume, and the threshold cut (the drawn tree) keeps 1–14k nodes. The "millions of rows" case would come from terms like `.json` or a bare common token, which weren't measured. Extrapolating at the same cost per row, 1M matched rows should still roll up in a few seconds.

## B. Name shapes

Classes are first-match-wins in this order:

- `uuid`: an 8-4-4-4-12 hex pattern.
- `number`: all digits.
- `hex`: a run of 8 or more hex characters containing both a digit and a letter.
- `step/shard/long-num`: `(step|shard|ckpt|checkpoint|epoch|iter|part|chunk|batch|rank|seed|split|worker|block)[-_=.]?\d+`, or `\d+-of-\d+`, or any run of 5 or more digits.
- `digits+ext`: has a digit and a `.ext` suffix.
- `other w/ digits`, then `other (no digits)`.

Postings per name are counted exactly from the trigram file (`GROUP BY id`), so they sum to the 5,227,164,581 postings. "% file bytes" is the share of Σ file size over files with names in the class.

| class | names | % names | postings | % postings | % file bytes | rows |
|---|---:|---:|---:|---:|---:|---:|
| step/shard/long-num | 95.0M | 74.6% | 4,015.7M | 76.8% | 38.4% | 300.9M |
| hex | 27.2M | 21.4% | 1,074.1M | 20.5% | 31.2% | 33.0M |
| digits+ext | 3.2M | 2.5% | 66.3M | 1.3% | 2.6% | 31.4M |
| uuid | 1.5M | 1.1% | 57.3M | 1.1% | 0.4% | 3.4M |
| other w/ digits | 0.43M | 0.3% | 12.3M | 0.2% | 3.3% | 36.8M |
| other (no digits) | 0.06M | 0.05% | 1.5M | 0.03% | 2.0% | 295.3M |
| number | 0.03M | 0.02% | 0.08M | 0.00% | 22.0% | 77.5M |

The shape is extreme: the 0.09M names with no long digit run and no hash (`other (no digits)` + `number`) cover 24% of file bytes and 48% of rows. The 122M numbered or hashed names cover the rest with one name per few rows. Most numbered names are unique per shard: `finemath-3plus_2815734.jsonl.gz`, `part-00228-of-05047.tmp.<32 hex>`, `<n>_warc_examples.parquet`.

### Templating

Each lowercase name has UUIDs replaced by `U`, then runs of 8 or more `[0-9a-f]` by `H`, then remaining digit runs by `D`. Postings are the distinct trigrams per template, computed as in `search.py`.

| | names | postings |
|---|---:|---:|
| current vocabulary | 127.3M | 5,227M |
| templates | **2.16M** (59× fewer) | **70.6M** (74× fewer) |
| singleton templates (1 name) | 1.19M | |
| names under the top 1,000 templates | 95.6M (75%) | |

| top templates | names | postings of their names today |
|---|---:|---:|
| `part-D-of-D.tmp.H` | 13.6M | 734M |
| `finemath-Dplus_D.jsonl.gz.success` | 8.8M | 322M |
| `finemath-Dplus_D.jsonl.gz` | 8.7M | 250M |
| `local-shard_D_of_D_D.jsonl.gz.success` | 6.2M | 258M |
| `local-shard_D_of_D_D.jsonl.gz` | 6.2M | 207M |
| `D_warc_examples.success` | 5.2M | 140M |
| `D_links.jsonl.gz` / `.success` | 5.2M each | 103M / 145M |
| `D_warc_examples.parquet` | 5.2M | 140M |
| `H` (a bare hash) | 3.5M | 108M |

Two caveats on templating:

- `[0-9a-f]{8,}` also swallows pure-digit runs of 8 or more (into `H`, not `D`) and letter-only hex words, such as `deadbeef`. Both are negligible for sizing.
- The `D`/`H` choice changes the template count only marginally.

### Size/depth skew

Each subset is defined per name: a name is indexed if any occurrence qualifies, and indexing a name brings all of its rows, as v1 `rgs` and v2 rows do. "Largest occurrence" is the path's size summed over owner slices. Depth counts the bucket as depth 1. Each query cell reads "roots found by the index alone / all roots (share of the roots' bytes found)".

| index subset | names | postings | % postings | `tomat` | `grug` | `safetensors` | `ckpt -eval` |
|---|---:|---:|---:|---:|---:|---:|---:|
| all (today) | 127.3M | 5,227M | 100% | 21,739/21,739 (100%) | 7,724/7,724 (100%) | 89,122/89,122 (100%) | 170/170 (100%) |
| largest ≥ 1 GiB | 0.50M | 14.7M | 0.28% | 5 (99.58%) | 906 (100.00%) | 76,139 (99.97%) | 18 (99.74%) |
| largest ≥ 10 GiB | 0.02M | 0.53M | 0.01% | 4 (99.56%) | 66 (98.45%) | 6,050 (12.28%) | 13 (94.83%) |
| min depth ≤ 3 | 0.23M | 9.4M | 0.18% | 5 (99.58%) | 6,809 (96.62%) | 12,473 (0.00%) | 128 (57.84%) |
| min depth ≤ 5 | 63.4M | 3,048M | 58.3% | 21,731 (100%) | 7,723 (100%) | 86,383 (95.38%) | 158 (99.74%) |
| dirs only (has a dir occurrence) | 15.6M | 785M | 15.0% | 9 (99.58%) | 7,251 (100.00%) | 2 (0.00%) | 161 (99.75%) |
| dirs ∧ depth ≤ 5 | 1.37M | 40.1M | 0.77% | 5 (99.58%) | 7,250 (100.00%) | 0 (0%) | 155 (99.74%) |
| dirs ∨ ≥ 1 GiB | 16.0M | 797M | 15.3% | 9 (99.58%) | 7,251 (100.00%) | 76,141 (99.97%) | 161 (99.75%) |
| **≥ 1 GiB ∨ depth ≤ 3** | **0.72M** | **23.8M** | **0.46%** | 5 (99.58%) | 6,905 (100.00%) | 88,612 (99.97%) | 139 (99.74%) |
| ≥ 1 GiB ∧ dirs | 0.08M | 2.3M | 0.04% | 5 (99.58%) | 906 (100.00%) | 0 (0%) | 18 (99.74%) |
| dirs ∧ (≥ 1 GiB ∨ depth ≤ 5) | 1.41M | 41.2M | 0.79% | 5 (99.58%) | 7,250 (100.00%) | 0 (0%) | 155 (99.74%) |
| no uuid/hex/number | 98.6M | 4,096M | 78.4% | 21,739 (100%) | 7,709 (100.00%) | 89,122 (100%) | 169 (100.00%) |
| no uuid/hex/number/step | 3.66M | 80.0M | 1.53% | 45 (99.61%) | 6,389 (99.78%) | 55,582 (26.95%) | 47 (41.80%) |
| dirs ∨ (no hash ∧ ≥ 1 GiB) | 15.7M | 785M | 15.0% | 9 (99.58%) | 7,251 (100.00%) | 76,141 (99.97%) | 161 (99.75%) |

Reading the table:

- **Bytes are concentrated, roots are not.** Every size-skewed subset finds ≥ 99.5% of the matched bytes for the dir-shaped terms, at 0.04–0.5% of today's postings. None finds the long tail of small scattered matches: `tomat`'s 21.7k mp3s, or `grug`'s small run dirs under the 1 GiB cut.
- **Depth alone doesn't separate the vocabulary.** At depth ≤ 3, too little survives (a 0.00% byte share for `safetensors`, whose files sit deeper). At depth ≤ 5, 58% of postings survive, because the numbered shards already sit at depth 4–5.
- **Dropping the hash classes alone buys little** (−22% postings). The numbered class is the bulk, and dropping it as well breaks `safetensors` (`model-00001-of-00004.safetensors` is "numbered").

## Recommendation

### §4 evaluation strategy

1. **Names-first only, for any generation the service answers.** Pin `(id, lower(name))` plus each name's `path` row groups (v1 `rgs`) or its v2 row range. Filter with `contains(l, …)`, never `ILIKE`, and map to rows by range reads. Measured warm on 16 vCPU, end to end: ≈ 0.6 s for `grug` and `ckpt -eval`, ≈ 2.2–2.4 s for `tomat` and `safetensors`. The two slow ones spend about 1.8 s decoding whole `path` row groups. That is the lever for small instances: parallel row-group decode, reading only the needed columns (done here), or a row map tuned for many-name terms. As measured in §A.2, v2's one-group-per-name layout helps `safetensors` a little and hurts `tomat` a lot. Measure v2 before picking it for the service.
2. **Drop the live `path`-scan fallback** from the interactive design. It is 34 s at best (16 vCPU, warm, local), 77 s at 4 vCPU, plus 12–13 s to copy, or 219–288 s read in place. For an unindexed generation, either backfill the sidecars (a Batch job in the time of one scan) or return the Worker's `partial` with a "not indexed" flag. If a fallback is kept, make it an async job, not a request, and copy the file locally before scanning.
3. **Cold start: copy, don't mount.** Fetch the names file with parallel ranged GETs to local disk or tmpfs (3–5 s for 1.56 GB), then load (3.6 s, or 4.9 s with `lower`). That gives ≈ 8–10 s from container start to a hot generation, against 86 s (gcsfuse) or 146 s (DuckDB `httpfs`). The warm-up ping after each scan publishes (spec §5) is still worth it.
4. **Match volume needs no special path at these sizes.** Roots by ancestor anti-join, and the drawn tree as a `GROUP BY` over root ancestors cut at the view threshold, together take under 1 s for 89k matched rows. Drop "group by parent first": only 50 of 55k parents match completely.

### Sizing

- **One generation pinned:** about 10–12 GB in DuckDB for `(id, l)` plus `rgs` (pin `l` instead of `name`; display names come from the rows). Arrow holds `(id, name)` in 6.6 GB, so an Arrow-backed vocabulary with DuckDB scanning it zero-copy would be tighter. That Arrow variant is unmeasured (see Caveats).
- **Diff (two generations):** 20–24 GB of vocabulary. That fits **8 vCPU / 32 GiB**, the Cloud Run gen2 maximum, but not 4 vCPU / 16 GiB. Start at 8 vCPU / 32 GiB. Threads matter for the filter and the row decode: the scan's 16 → 8 → 4 thread timings were 35 → 40 → 77 s.
- An option worth a follow-up: one **shared vocabulary across consecutive generations**, since names change little day to day. A diff then pins one vocabulary plus two row maps. Unmeasured.

### Shrinking the index (for the Worker and for service memory)

| option | names / postings | what it finds | trade-off |
|---|---|---|---|
| **Templated trigrams** (digits → `D`, hex runs → `H`, UUIDs → `U`) | 2.16M / 70.6M (74× smaller) | All four test queries exactly, since none has a digit or a pure-hex term | A term with digits (`step-1000`, `00001-of`) or that falls inside a hex run (`cafe`) can't be decided from the template alone: it needs member-name verification, and big templates hold millions of names (`part-D-of-D.tmp.H`: 13.6M). Matching templates must also expand to their names to reach rows, so it needs a template → name-range map; ordering ids by template would make that a range. |
| **Size/depth head** (≥ 1 GiB ∨ depth ≤ 3) | 0.72M / 23.8M (0.46%) | ≥ 99.58% of matched bytes for all four; roots only for big or shallow names | A byte-complete first paint, but missing the long tail of small matches, so it must be flagged `partial` and handed off |
| Dirs only | 15.6M / 785M (15%) | Dir-shaped terms | Misses file-name terms entirely (`safetensors`: 0%) |
| Drop hash-like names | 98.6M / 4,096M (78%) | Nearly everything | Saves little; the numbered class dominates |

**Suggested combination:**

- **Worker:** a small head index (≥ 1 GiB ∨ depth ≤ 3, about 24M postings) for an instant, mostly-byte-complete first paint, plus templated trigrams for exact answers to digit-free terms where the matching templates are small.
- **Service:** the exact full vocabulary for everything else, including digit-bearing terms. Its memory could later shrink by holding names grouped by template too (2.16M templates, with a per-template digit/hex payload), but that is an encoding project, not a phase-1 need.

## Caveats

- Times are from n2-highmem-16 with 16 threads. Cloud Run's 4–8 vCPU will be slower, roughly proportionally for the filter, decode and scan steps (the scan measured 40 s at 8 threads and 77 s at 4).
- The fourth gcsfuse scan onward and the `httpfs` second scan were served from caches (OS page cache, DuckDB's external file cache). Only the first read of each file is truly cold, and those are marked above.
- Job C's last step (filtering the vocabulary with Arrow `match_substring`) failed: `combine_chunks` overflowed 32-bit string offsets on 127M names and needs `large_string`. It wasn't rerun, to stay within the three-job budget, so the Arrow-backed vocabulary's filter time is unmeasured. Its load time (5.6 s) and size (6.6 GB) are measured.
- "Fully matched parents" compares against `max(n_children)` over owner slices, so it is approximate.
- The skew analysis is per name: indexing a name brings all of its rows, including small or deep occurrences. A per-row cut would find fewer roots than the table shows.

[`filter-query-service.md`]: filter-query-service.md
[job outputs]: https://console.cloud.google.com/storage/browser/oa-gcs-usage-dvx/scratch/p0
