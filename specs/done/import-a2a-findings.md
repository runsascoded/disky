# `disk-tree import --engine duckdb` a2a vs marin's production aggregation

2026-08-14, from the marin-gcs-usage session. First real-data validation of the out-of-core
import: `marin-us-west4`'s 2026-08-14 DIY listing (7,546,103 objects / 142.1 TB, 26 shards) →
`import -e duckdb` → layer-2, compared against marin's production `tree.json` (built from the
same listing by the Batch job's DuckDB aggregation).

## Verdict: bytes validated; two upstream items

**Bytes: EXACT match at root** (142,125,521,859,283) and at every depth-1 dir except one
8-byte case (below). The engine is fit for marin's Batch job to switch to.

## Item 1 — `n_desc` counts dirs; consumers need `n_files`

DT `n_desc` = all descendant rows (files + synthesized dirs): root 8,244,675 = 7,546,103
objects + 698,572 dirs. marin's `o` = objects only. Semantic mismatch for any
object-store consumer ("objects" is the number people expect). Fix: emit an **`n_files`**
column alongside `n_desc` in the aggregation (both engines; trivial extra SUM). Consumers
keep `n_desc` for tree mechanics, display `n_files`.

## Item 2 — double-slash object names mis-parented (8-byte discrepancy)

Two real objects have empty path components:
`tokenized/finemath_3_plus-a26b0f//.artifact.json` (4 B) and
`tokenized/starcoderdata-12f018//.artifact.json` (4 B). DT's import assigned them outside
the `tokenized` subtree (root total still exact — they landed under a different parent),
while prefix-based aggregation (marin's, and plain `LIKE 'tokenized/%'`) keeps them inside.
Decide + implement a policy: treat `a//b` as `a/b` (collapse empty components, probably
matching user intent) or as a literal child named `''` — either way, **deterministic and
subtree-preserving** (the current behavior moves bytes across top-level subtrees). Add a
listing fixture with `//` names to the import tests.

## Repro

Inputs: `gs://oa-gcs-usage-dvx/listing/2026-08-14/marin-us-west4/shard-*.parquet` (145 MB),
compare against `gs://oa-gcs-usage-dvx/snapshots/2026-08-14/tree.json` node `marin-us-west4`.
`DISK_TREE_ROOT=<tmp> disk-tree import -e duckdb -l '<shards-glob>' -t 2026-08-14T00:00:00Z`.

## Status

- [ ] `n_files` column (both engines) + test
- [ ] `//` path-component policy + fixture test
