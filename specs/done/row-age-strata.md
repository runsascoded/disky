# Row age strata: replace the per-row "mean created" with a distribution

**Status:** implemented (2026-10-01) on `m3`, live on dev.disk.rbw.sh; `[base]` for `cloud` to pick.

## Why

The children table's `created` column (and the treemap's age colour) is one number per directory: the bytes-weighted mean write time. It is misleading wherever timestamps are fake or mixed. `~/.cargo` reads **Jun '13** because Cargo stamps packaged crate sources with a fixed 2006 time (30,853 files), mixed with ~3,000 real 2025–26 files. A mean of a bimodal distribution describes neither mode.

The age pyramid (`/api/age-pyramid`, `AgeChart`) already shows a distribution, but for one path at a time; a table page would need one request per row. On the laptop store it isn't built at all: that ingest is `path-index` from listings, and the pyramid comes from `index-write` over layer-2.

## Proposal: a fixed age-strata column on every store row

`dt-cloud path-index` already computes `age_days` (bytes and objects per `(day, dir)`, the dir's own objects) to write `age.json`. Roll it up descendant-inclusive, the same way `b`/`o` roll up, into a small fixed vector per row:

- Seven `BIGINT` columns `age_b0`…`age_b6` (`dt_cloud.index.AGE_COLS`; separate columns, not a list, so the D1 footer and hyparquet decode stay simple): bytes by **age at scan time**, in log buckets `<1d, <1w, <1mo, <3mo, <1y, <3y, ≥3y` (Σ = `b` where every object has a created time). On the wire, `TreeNode.ag`.
- Age, not calendar year, because the question is "how stale is this", and it stays comparable across scans.

The subtree API already returns every child row, so the table gets the strata for free: no extra request, and none per row. It renders as a 7-cell stacked micro-bar (the existing viridis ramp) in place of the dot, month and year. Hover shows the bucket bytes. `.cargo` would read as mostly `≥3y` with a sliver of `<1mo`.

The `created` dot and the treemap's age colour can keep using `d` (the mean) until someone wants them changed; this adds a column, it removes nothing.

## Cost

- **Rows**: one 7-element list per row. On gcs (the largest store) that's about 56 bytes uncompressed per row, before parquet's list encoding and compression. Measure on one gcs scan before committing.
- **Compute**: the roll-up is a group-by over `age_days` joined to its ancestors, like the existing `b` roll-up. Measure on the laptop (`/`, 8.6M rows, 4.3 GiB peak today).
- **Wire**: `/api/subtree` rows grow by 7 numbers. Gate behind a `cv` bump.

## Open questions

- Bucket edges: the 7 above, or the age pyramid's own ladder (`1h…8d`, then months)?
- Whether `index-write` (cw's layer-2 path) emits the same column, so cw rows carry it too.

## As built (2026-10-01)

- **Opt-in** (`dt-cloud path-index -g/--age-strata`, `write_path_index(age_strata=True)`): off by default, so a store that hasn't asked for them keeps its schema, `dir-stats` cache and ingest cost (gcs's 777M-row sorts in particular). m3's `aws/ingest.sh` passes `-g`.
- **Index** (`dt_cloud.viz.write_path_index`): `dir_stats` sums each dir's own objects into the 7 buckets (`index.age_bucket_sql`: age = scan day − created day; a future stamp lands in `<1d`), the `ptu` roll-up carries them to every ancestor like `b`, and object rows put their size in their one bucket. The columns join the store only where a source's shape lists them (`store_columns`), so `index-write` stores (cw, gcs) keep their schema until they opt in. Laptop `/` ingest: peak RSS 4.2 → 5.0 GiB, wall unchanged, 1,050 → 1,052 row groups.
- **Edge**: `Row.ages` (decoded when the generation has the columns, else null), summed / subtracted / scaled through `view.ts`'s `Agg`, emitted as `ag` when any bucket is non-zero. Additive, so no `cv` bump.
- **Table**: when the page's rows carry `ag`, the `created` column becomes **age**: a 64 px stacked bar per row, newest (yellow) → oldest (purple) on the date ramp; the tip lists each bucket's bytes and share plus the mean written date. Without `ag` the swatch · month · year columns stay. Sorting still keys on the mean.
- Measured on `~/.cargo` (1.2 GB): 77% `≥3y` (the 2006 crate stamps), 12% `<1mo`, 6% `<1d`, where the mean said "Jun '13".

Not done: the treemap's age colour and the column's sort still use the mean; a cw/gcs `index-write` path to the same columns.
