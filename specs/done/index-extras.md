# Index extras: one sidecar for provenance and checkpoint shape

Two asks land on the path index at once — inferred-ownership provenance (`assignment-provenance.md` Phase 2) and an ahead-of-time "checkpoint-shaped" flag per directory (`children-table-selection.md` §3). Both are small, per-path facts the daily index build knows and the site cannot cheaply recompute. Rather than two new parquet columns (a schema bump, `index_schema` pointer churn, every reader touched), they ride the mechanism `index-blob` already established: a JSON sidecar **beside** the tiers under `listing/<date>/index/<gen>/`, written by the daily job for new scans and backfilled for old ones by a CLI, opened by the site on demand and cached per isolate.

## The files

Beside the tiers under `listing/<date>/index/<gen>/`. First measurement (2026-09-08 scan): **486k** checkpoint-shaped dirs, **165k** attributing prefixes (122k of them per-run `wandb-config` prefixes) — 87 MB + 25 MB as JSON, far past what a worker isolate should parse per request. So both are **sorted text with a block index**, and a view fetches only the byte range covering its subtree:

```
ck.txt            one index path per line, sorted            (checkpoint-shaped dirs)
ck.txt.idx.json   { v, n, size, keys: [first key per ~64 KB block], offsets: [byte offset per block] }
attr.tsv          key \t user \t source \t evidence, sorted by key   (every attributing prefix)
attr.tsv.idx.json (same shape)
```

Keys are index paths (bucket-relative, no `gs://`, no trailing slash) — the same key space as the tiers, so every key under a view root `P` is contiguous (`[P, P + '0')`, `'0'` sorting just past `/`). The reader (`functions/_lib/extras.ts`) memoizes the two small indexes per (scan, generation), binary-searches the block range for `P`, and range-GETs it — plus, for provenance, one block per ancestor of `P` (inherited attribution). A range past `MAX_RANGE` (3 MB — a bucket root over a huge sidecar) is skipped for that view rather than parsed whole; drilled views, where the extras matter, are small ranges. A row's provenance is then the deepest `attr` key that is an ancestor-or-self of the row's path with the row's `usr` (the walk `deepest_lookup` does at build time). A node's `ck` is exact: the flag was computed over the dir's **full** child list at listing time, not the pixel-budgeted subtree the client sees.

`ck` rule (`gcs_usage.extras.ckpt_dirs`): a direct child named exactly `checkpoints`/`ckpts` (a run dir), or ≥ 2 direct children matching the step/checkpoint-number pattern (`sweep.ts`'s `CKPT_SEG_RE`, mirrored). A dir whose **own** last segment matches `(^|[-_.])(ckpts?|checkpoints?)([-_.]|$)` is checkpoint-shaped too, but that's decidable from the name, so the reader applies it and the sidecar leaves those out. The child match is the whole segment on purpose: the first run's substring match flagged 250k eval-output dirs named after checkpoint paths (`…/AIME24_0shot/gs__…__checkpoints__…__step-600`). The client's `looksCkpt` keeps the same rule as the fallback for scans without extras. (A bucket root with a top-level `checkpoints/` dir is flagged too; nothing offers a mark at bucket level, so it's inert.)

## Writers

- **New scans**: `webdata` writes the four files next to `path-index.parquet` (`gcs_usage.extras.write_extras` / `write_blocked`) from data it already holds — `by_prefix` (user, source; evidence once the attribution rows carry it) and `dir_stats` (the parent/child relation for `ck`). Cost: one pass over the dir list, seconds.
- **Backfill**: `gcs-usage index-extras -P <path-index.parquet> [-a <attribution.parquet>…] [-i <identities.yaml>] [-o <dir>] <date>` rebuilds the same files for an archived generation from the attribution parquets and the floor-free path index (children = rows at depth+1 under the path; the floor-free index has every dir). Runs on the `mgu` node, not the laptop.

## Reader (site)

`functions/_lib/extras.ts`: `extras(env, date)` → the parsed files for the scan's current generation (`index_schema.dir`), memoized per isolate with the same TTL/`shared()` pattern as the owner-lens claims; `null` when the file is absent (older gens until backfilled) — every consumer degrades to today's behaviour.

- `buildView` stamps `k: 1` on nodes whose path is in `ck`, and `pv: [source, evidence?, prefix]` for the node's top owner (deepest attributing ancestor with that user). Both ride the subtree payload as optional `TreeNode` fields (`k`, `pv`).
- `/api/resolve` gains `owner.inferred` later; the client reads `pv` off the node it already has.
- `?by=<signal>` (Phase 2's "filter slices by source") becomes: keep the rows whose deepest attributing ancestor has that source — the same probe, no parquet column.

## Client

- `<OwnerFactChip>`: the assignee's chip plus a provenance mark — the assigner's avatar for a person, a source glyph for a signal — and one tooltip (`assignment-provenance.md` § Surfaces). Used by the mark panel's owner row, the children table's owner cell, and the treemap tooltip.
- `looksCkpt(node)` returns `node.k` when defined; the shape heuristic only otherwise.

## Not in this round

- Structured W&B evidence (`entity/project/run_id`) needs the attribution rows to carry it; the next `wandb-mine` writes it, and `extras.json`'s third slot is null until then.
- `/api/assignments` signal rows (Phase 2's `by_source` rollup) — once `extras` is on every scan, the rollup is a fold over `attr` × the tiers; separate change.

## Status (2026-09-09)

Landed (`dfc4734`, `e8ede57`, `f0af501`, `4aa30bde`) and deployed. Sidecars backfilled for scans 2026-09-02 … 2026-09-09 (the five pre-generation scans at `listing/<date>/`, the rest under their generation dir); new scans get them from `webdata`. Older scans: run `tmp/extras/backfill-extras.sh <date>` on the `mgu` node when wanted.
