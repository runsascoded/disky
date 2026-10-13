# Re-encoding fleet parquet with a better codec: opportunity and pilot

Status: research done 2026-10-10 (read-only; nothing in any fleet bucket was written, deleted or re-classed). Decision and pilot need the data owners.

## The proposal

In a private Marin ops channel on 2026-08-24, Russell Power suggested that besides deleting old datakit data, the GCS fleet's parquet could be recompressed with zstd. That had been done on CoreWeave and saved about 30% against the S3 copies. He also noted it probably isn't worth it for data already in a cheap storage class.

Since then, Russell's zephyr change [marin#8401] (2026-09-11) has made every zephyr parquet write zstd level 3, with page indexes and at most 256 rows per data page. New outputs are covered. The open question is the parquet written before 2026-09-11.

## Method

- **Sizing.** I streamed the head of the 2026-10-10 gcs path store's `bysize` sort (largest rows first) down to 32 MiB. That is about 1,650 of its 8K-row groups, read as range reads, from `listing/2026-10-10/index/20261010T070115Z/path-index-bysize.parquet`. From it I kept the `.parquet` object rows with `size`, `created`, owner (`usr`) and the per-object Autoclass class pivots (`sum_storage_class_id_{2,3,4}`). The CW store (`cw-l2/2026-10-11T0001`) got the same treatment.
  - **Gap:** parquet objects under 32 MiB are not counted, so every share below is for objects of at least 32 MiB.
  - The full layer-1 listing query (about 10.7 GB of listing for all six buckets) needs a cloud VM. Creating one was refused in this session.
- **Codec census.** I read footers only: 177 GCS objects from 24 prefixes, plus 80 CW objects from the top 20 CW prefixes.
  - On GCS the census was limited to objects in **STANDARD** class. Every fleet bucket runs **Autoclass**, and under Autoclass a media read of a NEARLINE, COLDLINE or ARCHIVE object promotes it back to STANDARD. Even a footer range read would have changed Marin's storage bill.
  - The codec of the cold bulk is therefore *inferred*, in two ways (next section).
- **Re-encode experiment.** I took 5 SNAPPY files and 1 zstd file from 5 lineages. For each I range-read up to 2 row groups (~60–100 MB compressed) and rewrote them locally with pyarrow 23.0.1, keeping the same row groups, schema and key-value metadata.
  - Rows were verified identical with a NaN-aware comparison (see Risks: a plain `Table.equals` reports NaN columns as different).
  - Output was timed for encode and for decode with 1 and 4 threads.
  - Scripts and raw results are in `tmp/reencode/` (untracked).

## Findings

### 1. Where the parquet is (objects of at least 32 MiB; shares of bytes)

| slice | share | notes |
|---|---|---|
| `raw/`, `finetranslations-*/` (upstream downloads) | 42% | byte copies of HF and similar sources; leave as is (provenance, checksums) |
| written on or after 2026-09-11 (post-[marin#8401]) | 3% | already zstd-3, all STANDARD |
| **candidates**: pipeline outputs written before 2026-09-11 | **55%** | `normalized/` 48%, `datakit/` 29%, `processed/` 6%, `data/` 6%, rest small |

By Autoclass class, the candidates are **72% COLDLINE, 28% NEARLINE and 0.4% STANDARD**. Nobody is reading them, which is exactly Russell's caveat. Autoclass's terminal class is ARCHIVE on five buckets and NEARLINE on `marin-us-west4`.

- By bucket, the candidates sit mostly in eu-west4, us-central1 and us-central2 (26–29% each), then us-east5 (12%), with us-west4 at 3%.
- Many datasets are stored **in 3–5 regions at once**, e.g. `normalized/nemotron_cc_v2` (5 buckets) and `datakit/tokenize` (5). Dropping replicas is a larger lever than any codec change.

The top candidate prefixes, in order: `normalized/nemotron_cc_v2` (×4 buckets), `datakit/tokenize` (×5), `normalized/nemotron_cc_v2_1`, `data/datakit`, `datakit/dedup*`, `normalized/finetranslations_*`, `processed/finetranslations_*`, `datakit/minhash`, `normalized/cp`, `normalized/finepdfs*`.

### 2. Codec census

**GCS, STANDARD sample (177 files):**
- Writers: parquet-cpp-arrow 23.0.1 (160 files), parquet-rs 58.3.0 (8), Polars (8).
- By compressed bytes: 84% ZSTD (uncompressed/compressed = 3.45×) and 16% SNAPPY (1.91×). Format 2.6 except Polars and parquet-rs (1.0).
- The sample is biased toward recent writes: STANDARD means recently written or read.

| prefix (STANDARD copies) | codec | uncomp/comp | row groups/file | dominant column |
|---|---|---|---|---|
| `documents/fast_curation` (6 buckets) | ZSTD | 2.8–3.4× | 1 | `text` 97% |
| `documents/baseline_*_deduped` | SNAPPY | 1.8× | 3 | `text` 100% |
| `normalized/stack-v3_32b6fa6f` | 87% SNAPPY / 13% ZSTD | 2.8× | 6 | `text` 98% |
| `normalized/nemotron_cc_v2` (us-west4) | ZSTD | 2.7× | 6 | `text` 97% |
| `normalized/nemotron_cc_v2`, `normalized/cp` (us-east5) | SNAPPY | 1.6–2.0× | 11–14 | `text` |
| `datakit/tokenize` | ZSTD | 1.25–3.0× | 1–29 | `input_ids` 100% |
| `datakit/embed` | ZSTD | 1.4× | 2 | `embedding` 98% |
| `normalized/sft`, `rendered/sft`, `*-chat/*` | ZSTD | 3.1–8.6× | 11–389 | text/messages |

**The cold bulk is SNAPPY, inferred two ways:**
1. **Code history.** Before [marin#8401], zephyr's `write_parquet` called `pq.ParquetWriter` with no codec, which defaults to SNAPPY.
2. **GCS ↔ CW pairing.** About 237K files exist on both GCS and CW (`marin-us-east-02a/marin/…`) under identical relative paths; CW's copies are zstd. Across all of them, **CW bytes / GCS bytes = 0.643**, with p10–p90 inside 0.60–0.69 for every large dataset.
   - That matches the experiment's SNAPPY → zstd-3 ratio of 0.61–0.64.
   - It is the "about 30%" Russell saw; measured, it is 36%.
   - Paired GCS copies in COLDLINE or NEARLINE are effectively all SNAPPY-like (ratio < 0.9). The only paired copies already at ratio 1.0 are STANDARD ones written by newer code.

**CW** (80 footers, top 20 prefixes): 94% ZSTD by bytes (3.3×). The only SNAPPY prefix in the top 20 is `marin/normalized/code-alchemy`. Russell's CW recompression is visible: CW is done, apart from stragglers.

### 3. Re-encode experiment

Size relative to the original row groups. Every variant kept its row groups and verified row-identical.

| lineage (orig codec) | snappy rewrite | zstd-1 | zstd-3 | zstd-3 as zephyr writes it | zstd-9 | zstd-19 |
|---|---|---|---|---|---|---|
| web text, `baseline_lpv11_*_deduped` (SNAPPY) | 1.00 | 0.71 | 0.64 | 0.64 | 0.58 | 0.53 |
| web text, `baseline_llm_pipeline_*_deduped` (SNAPPY) | 1.00 | 0.72 | 0.64 | 0.64 | 0.58 | 0.53 |
| papers, `normalized/cp` peS2o (SNAPPY) | 1.00 | 0.69 | 0.62 | 0.62 | 0.55 | 0.48 |
| synthetic, `normalized/nemotron_cc_v2` (SNAPPY) | 1.00 | 0.69 | 0.61 | 0.62 | 0.56 | 0.51 |
| code, `normalized/stack-v3` (SNAPPY) | 1.00 | 0.67 | 0.61 | 0.61 | 0.51 | – |
| `documents/fast_curation` (already ZSTD) | 1.78 | 1.26 | 1.12 | 1.13 | 1.02 | 0.93 |

- **SNAPPY → zstd-3 saves 36–39%.** zstd-9 saves 42–49% and zstd-19 saves 47–52%.
- **Already-zstd files have little headroom.** `fast_curation`'s original compresses like zstd-9, and even zstd-19 saves only 7%. Going from zstd-3 to zstd-9 on data that is already zstd-3 (CW, post-#8401 GCS) saves 9–16% (the low figure is text, the high one code).
- **Encode cost.** Taking zstd-3 as 1×, zstd-9 costs about 4× and zstd-19 about 70–130×; zstd-19 runs at roughly 1 MB/s per core, i.e. hundreds of core-hours per TB. zstd-19 is impractical at fleet scale; zstd-3 is I/O-bound.
- **Decode** speed of zstd-1/3/9 matched snappy within noise, 1-thread and 4-thread, on these text-heavy files. There is no read-performance regression for downstream consumers; zephyr itself now writes zstd-3.

### 4. Economics: Autoclass dominates

A rewrite creates a **new object**. Under Autoclass it starts at STANDARD, demotes to NEARLINE after 30 days, COLDLINE after 90 and ARCHIVE after 365. Rewriting therefore re-warms cold data for months.

The overwritten original stays in **soft delete for 7 days**, billed after the read has promoted it to STANDARD. Autoclass charges no early-deletion or retrieval fees.

The model below assumes list regional prices (STANDARD : NEARLINE : COLDLINE : ARCHIVE ≈ 20 : 10 : 4 : 1.2 per GB-month), zstd-3 at 0.62, and that nothing reads the data afterwards. Results are in months of the original object's *current-class* storage cost.

| starting class | 12-month net | 36-month net | verdict |
|---|---|---|---|
| STANDARD (read regularly) | saves about 36% of storage from week one | same | **do it** (pays back in days) |
| NEARLINE, idle about 45 days | gains about 0.5 month | gains about 1.5 months | marginal |
| NEARLINE on `marin-us-west4` (terminal NEARLINE) | gains about 28% of year-1 storage | about 37% saved for good | **positive** |
| COLDLINE, idle about 150 days | loses about 4 months | loses about 1.5 months | **net loss** |
| COLDLINE, idle about 300 days | loses about 7 months | loses about 5 months | **net loss** |

Higher zstd levels barely move the COLDLINE rows: zstd-19 still loses over 12 and 24 months.

- **Fleet-wide**, about 72% of candidate bytes are COLDLINE, so a blanket re-encode loses money in year one. The steady-state ceiling is roughly 37% of the candidates' current monthly storage, about 20% of all parquet at 32 MiB or more, and it shrinks further as everything reaches ARCHIVE.
- **Where it does win:**
  - data that is or will be read again, since a re-read promotes it to STANDARD anyway;
  - `marin-us-west4`, which can't go colder than NEARLINE;
  - CW, which has flat pricing and no classes but a binding quota. There, every byte saved is quota relief, with no reset penalty.
- **One-time costs** are small next to storage:
  - compute about 1% of one month's NEARLINE storage per TB at zstd-3 (5× that at zstd-9);
  - Class A writes at a few thousand objects per TB;
  - no egress if the job runs in the bucket's region. Batch has no `us-central2` location, so that bucket would need a VM in the region; cross-continent reads for `marin-eu-west4` would cost real egress.

## Recommendation

1. **Do not run a fleet-wide re-encode of the cold bulk.** It is net-negative under Autoclass for COLDLINE data, which is most of it. Russell's caveat holds, quantitatively.
2. **Re-encode on reuse.** When a pre-September dataset is picked up again (training mix, re-tokenization), re-materialize it, or re-encode it in place first. It will be STANDARD for the reuse anyway, so the class reset costs nothing and the 36% saving applies while it is hot.
3. **Pilot on us-west4's NEARLINE candidates** (about 3% of candidate bytes, all NEARLINE, terminal class NEARLINE). This is the one place a cold re-encode pays.
4. **CW:** re-encode the remaining SNAPPY stragglers (`marin/normalized/code-alchemy` and any others a full census finds). Separately, measure zstd-9 on CW as quota relief (9–16% of the zstd-3 bytes), which needs a CW-side compute node.
5. **Bigger lever, out of scope here:** cross-region replicas of the same `normalized/`, `datakit/` and `raw/` datasets in 3–5 buckets. Each dropped replica frees 100% of its bytes, not 36%.

## Pilot

- **Prefix:** `gs://marin-us-west4/datakit/sample_10pct_91269634/` (16,257 objects, 100% NEARLINE, written 2026-08-04, SNAPPY-like). Its CW twin `marin/datakit/sample_10pct_91269634` pairs file-for-file at 0.634.
- **Owner:** datakit pipeline, Rafal Wojdyla (`rafal-wojdyla`), with William Held (`Helw150`); zephyr writer: Russell Power (`rjpower`). The ownership ledger has no explicit owner on this prefix.
  - Ask first whether the sample is still needed at all. Its downstream `datakit/tokenize/` in the same bucket was cut the same day, and deleting the sample beats re-encoding it.
- **Readers:** check with the owners that nothing pins object size, CRC32C/MD5, generation or `timeCreated` for these files (executor step metadata, tokenize/cache ledgers, dvx/DVC pins). A rewrite changes all four.

### Tooling: what `pqtk` needs

`~/c/pqtk` `re-rg` has `-c/--compression` and in-place `-i`/`-k` with a `gcp-batch` runner. A re-encode op needs:

- `-l/--compression-level`. Arrow's zstd default is not 3, and the op should match zephyr's level-3 output.
- **Keep the source's row-group boundaries.** `re-rg` today re-chunks to a uniform `--rows`/`--bytes`.
- Optionally match zephyr's writer: `write_page_index=True`, `max_rows_per_page=256` (pyarrow ≥ 23). Statistics and bloom filters are preserved only if the source had them; check `sorting_columns`.
- **Verification before the swap:** equal row count and row-group count, schema and key-value metadata, and a NaN-aware per-column digest. A plain `Table.equals` fails on NaN floats (seen on `fast_curation`'s `modernbert_prob`).
- **Generation-guarded swap.** Upload with `if_generation_match=<generation from the layer-1 listing>` (the listing carries `generation`). Never `fs.mv`'s copy-then-delete: a concurrently replaced object must fail, not be clobbered.
- **Skip rules:** objects not SNAPPY or uncompressed, objects below a size floor, and objects whose class makes the rewrite net-negative (COLDLINE/ARCHIVE unless `--allow-cold`).
- **A manifest** of `(path, old generation, new generation, old bytes, new bytes, digest)` as the record and the undo index.
- **Undo:** buckets have 7-day soft delete and no versioning. Restore an original with `gcloud storage restore gs://…#<old generation>` within 7 days.

### Pilot steps (each one gated on owner agreement)

1. **Copy mode, no fleet writes.** Take 100 files (about 1% of the prefix) to a scratch destination in a us-west4 bucket we own. Verify the digests, record ratio and timings, and have the owners point a reader at the copies.
2. **In place, 100 files,** with the generation guard and manifest, run in us-west4 (Batch). Re-read every footer from GCS afterwards and diff against the manifest. Keep the 7-day soft-delete window as the undo path.
3. **The rest of the prefix,** then the other us-west4 NEARLINE candidates, if steps 1–2 hold and the owners agree.

Proposed command, for the extended `pqtk` (not yet runnable):

```bash
pqtk re-rg -c zstd -l 3 --keep-row-groups --page-index --max-rows-per-page 256 \
  --verify --if-generation-match --manifest gs://<scratch>/reencode-pilot/manifest.parquet \
  -R gcp-batch --region us-west4 --sa <job SA with objectAdmin on the pilot prefix only> \
  -i 'gs://marin-us-west4/datakit/sample_10pct_91269634/**.parquet'
```

**Verification:**
- the manifest's 100% verify pass;
- post-swap footer re-read (codec ZSTD, same row-group count);
- the next daily scan shows the prefix about 36% smaller in `/` and `sum_storage_class_id_2` back to 0 for 30 days;
- owners' reader smoke test.

[marin#8401]: https://github.com/marin-community/marin/pull/8401
