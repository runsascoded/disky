# Descriptive field names: store columns, D1 and the site API

**Status:** draft, 2026-11-12. Owner: the root `disky` session (`cloud`). Implementation: a subagent on `w/field-names` in `wt/field-names`.

## Why

Short names (`us`, `wb`, `wts`, `vf`/`vt`, `c2..c4`, and the map API's `k b o d a cb f`) date from an early design that golfed JSON bytes on the wire. Today they cost readability everywhere and save almost nothing:

- **Parquet** stores a column name once, in the footer. A short column name saves nothing.
- **D1** column names are schema, not per-row bytes.
- **The site's JSON** repeats keys per node, but responses are compressed (brotli/gzip at Cloudflare's edge), which removes most of the repetition. At 10⁴–10⁵ nodes (a full-window treemap at a 16 px² minimum cell), the remaining cost is browser parse time over the uncompressed bytes. The fix for that is a **columnar** body (one array per field), not short keys.

One rename is already done: gcs's `c2`/`c3`/`c4` became `nearline_bytes`/`coldline_bytes`/`archive_bytes` in the generation backfill rebuild (2026-10-18). hf names its own classes (`private_bytes`, `git_bytes`).

## Phase 1: inventory (commit it into this spec first)

Every short or opaque name, per surface, with: where it's defined, every reader and writer (site functions, `site/src`, `fleet/`, `packages/*`, `dt-cloud` Python incl. probe/warm-cache/digest/diff-bench/layout-sim, job scripts, tests, fixtures), its meaning, and a proposed descriptive name. Surfaces:

1. **Served-store columns:** interval store sorts (`path`, `bysize`, `slices`, `level`, `attribution`, …), static-names files, per-scan path-index sorts, sidecars (`.groups.json` keys like `b_min`/`b_max`/`p_min`), `scans.json`/`meta.json`/manifest keys.
2. **D1 tables and columns** (`site/schema.sql`, and each deployment branch's schema where it differs).
3. **Site API JSON:** map/subtree, diff, series, search/filter, level, staged, scans, admin, fleet. Node keys (`k b o d a cb f us`, …) and top-level keys.
4. **URL query params** are out of scope (they're user-visible URLs; renaming breaks links). List any that look worth it separately, no changes.

Names that are already clear (`depth`, `path`, `size`, `mtime`, `n_files`, …) stay. A name stays short only if the inventory gives a reason (e.g. a key a third party reads).

## Phase 2: site API (ships on its own)

- Rename node and response keys to their descriptive names, server and client in the same commit(s), with a `RESPONSE_V`/`CACHE_V` bump for every cached body whose shape changes (see the "Bump RESPONSE_V on shape change" rule).
- **Columnar bodies** for the large node lists (map, diff, level): `{ "path": [...], "bytes": [...], "owner": [...], ... }` plus a small decoder on the client. Measure before/after on real responses (gcs root, a depth-6 window, a search): compressed bytes, uncompressed bytes, client parse + decode time. Keep columnar only if it doesn't lose on those.
- Python consumers of the API (probe, warm-cache, diff-bench, digest, …) change in the same commit.
- `functions/gates.test.ts` and every API test assert the new shapes exactly.

## Phase 3: store and D1 renames (ride a rebuild; never their own rebuild)

- **Store columns** change only in a generation that is being rebuilt anyway (the next compaction or backfill, as `c2..c4` did), with the reader accepting both spellings for exactly the generations still served, and that dual-read removed once no served generation has the old names. Each deployment's rebuild is its owner's.
- **D1 column renames** are recipes (`site/migrations/<slug>.sql` + `schema.sql` regenerated, `ALTER TABLE … RENAME COLUMN`), applied by each deployment's owner with Ryan's go.
- **The index rethink** (`specs/full-scan-serving-bench.md`, `wt/index-research`'s `fleet-index-research.md`) may replace the served layout. Don't rename columns of a layout the rethink is about to retire; the inventory marks which.

## Constraints

- Commits are `[cloud]`, generic (no deployment defaults). Send each sha to the root session; root lands it. Don't push `cloud`, `dev` or any deployment branch.
- No tests or builds on the laptop: CI (push `rw/<branch>` to `r` and watch it with `ghws`) or node `mgu` (`scripts/remote-test`).
- No D1 writes, no store writes, no deploys.

## Open

- Whether `vf`/`vt` (valid from / valid to, scan ordinals) should become `valid_from`/`valid_to` or `from_scan`/`to_scan`.
