# Layer-2 dir-cache: attribution-independent rollups per scan date

Re-attribution runs (REPROC, future ledger→attribution refreshes) were paying
the full ~30-60 min webdata cost — mostly 595M-row object scans — to change only
the *attribution* of unchanged bytes. But every object-derived input webdata
needs is attribution-independent:

- **`dir-stats.parquet`** — per-(bucket, dir): bytes, objects, weighted-mtime
  sums, per-class bytes (~34M rows). Feeds `dir_agg` (attr is a join over it),
  the dirs list for `dir_attr`, the storage-class mix, and — via `listing_dirs`
  — bucket enumeration + path-glob `prefix_owners` expansion.
- **`age-days.parquet`** — per-(day, bucket, dir): bytes, objects. Feeds both
  age branches (attr joins `dir_attr`).

`write_webdata(dir_cache=…)` / `gcs-usage webdata -c <dir>` caches both
write-through; `job/run.sh` points at `listing/<date>/dir-cache/` (colocated
with the listing — immutable per date, same lifecycle). Warm runs do **zero**
object scans (tested: a corrupted listing on a warm run changes nothing); cold
runs also improve, four object scans → two. This is the first concrete payoff
of the disk-tree "canonical layer-2 parquet" idea.

## Expected timings (n2-highmem-16)

- cold daily: roughly unchanged (~2 scans instead of 4, plus one parquet write)
- warm re-attribution: **~3-6 min** (dominated by `dir_attr` + tree emit)

## Manual-run sizing (lever 1)

For one-off heavy runs, `batch-submit.sh` env: `MACHINE=n2-highmem-32
MEMORY_MIB=250000 DUCKDB_MEM=200GB DUCKDB_THREADS=32 LOCAL_SSD_GB=1500`
(n2-32 requires ≥4 local SSDs). ~2× the DuckDB phases for ~$2/hr extra.

## Follow-up: `dir_attr` iterative resolution

The remaining memory hog is the deepest-prefix join: dirs × maxd exploded into
one un-spillable `arg_max` aggregate — this OOM'd the 8/26 REPROC twice at
100GB (the fresh wandb parquet's rows survive the ancestor join at much higher
rates than the old one's). Replace the single explode with per-depth passes:
for k = maxd…1, equi-join still-unresolved dirs against depth-k prefixes and
remove hits. Peak memory becomes one depth-slice instead of the whole
candidate set, and each pass shrinks the working set. (The depth cap —
`GCS_USAGE_ATTR_MAX_DEPTH`, default 12 — stays as a guard either way.)
