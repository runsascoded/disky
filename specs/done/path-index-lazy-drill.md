# Path index + pixel-budget subtree serving (kill the floor for real)

The floors in `tree.json` (parent-relative + abs + top-K, 2026-08-26/27) are a
bridge: the artifact doubles as the database, so it must fit a browser tab.
This spec is the destination — a complete, floor-free index served through a
pixel-budget query, so any subpath renders exactly as much detail as its canvas
can physically show, at any depth.

## Layer: the path index (complete, no floor)

Per scan date, materialize `listing/<date>/path-index.parquet`:

- One row per **path** (every ancestor of every dir), descendant-inclusive:
  `path, depth, pb (bytes), po (objects), wts/wb, c2..c4, tm/us` (attribution
  additive fields, so nodes can be assembled without re-aggregation).
- Sorted `(depth, path)` (the disk-tree engine's canonical order), modest row
  groups → row-group pruning on both range-by-prefix and depth predicates.
- Derived from `dir-cache/dir-stats.parquet` + `dir_attr` in the daily job
  (the rollup is the per-bucket explode we ran on mgu 8/27 — minutes on the
  Batch node). Fleet scale: ~800K paths ≥1 GB; total O(100M+) rows / ~10-20 GB
  — fine as a bucket artifact.

## Query contract (Ryan's formulation, 8/27)

**Area in a treemap is proportional to bytes** — a node's on-screen area under
query root `P` on a `w×h` canvas is `(b / P.pb) · w · h`. So "everything this
canvas can draw" is one predicate:

```
GET /api/subtree?date=<scan>&path=P&w=W&h=H[&minArea=A]
→ all paths under P with pb ≥ P.pb · A / (W·H), assembled into a nested tree
```

- `A` = smallest legible cell, default ~9-16 px² (≈3×3; cells need not be
  square). Conservative to absorb nesting chrome (borders/title bars); a
  per-level discount is a refinement, not a requirement.
- Depth needs no separate parameter: descendants keep passing the same
  threshold until their share goes sub-pixel. Response node count is bounded
  by `W·H / A` by construction (≈160K worst case at 1600×900/9; in practice
  far fewer — area concentrates. Hard-cap ~50K + `truncated` flag anyway).
- Sub-threshold residual per parent = the familiar `(other)` node (subtraction,
  as in `tree_build`), now *expandable*: clicking it re-queries with that
  parent as `P` — where it gets the whole canvas and a proportionally lower
  absolute threshold. **The floor disappears as a concept**: it's just "what
  this screen can show", recomputed per drill.

## Serving + caching

- CFN (`functions/api/subtree.ts`) reading the parquet via HTTP range requests
  through the existing HMAC store (pure-JS parquet reader; no DuckDB in
  Workers). Client-side reads (scan-browser-style) are the fallback locus if
  CFN CPU limits bite.
- **Quantize `w,h` to 128px steps** → cache key `(date, path, w₁₂₈, h₁₂₈)`.
  Scans are immutable → Cache API / KV entries never expire for correctness.
  First paint may estimate dims from vw; refetch on resize (debounced) usually
  hits cache.
- `tree.json` demotes to a bootstrap: the initial `(root, default-canvas)`
  answer, precomputed. (Long-term it can *be* that cache entry.)

## Client

- Core Treemap `loadChildren` → `/api/subtree`; `(other)` cells get the expand
  affordance (drill = re-query). Makeup stripes (`CellStyle.segments`) keep
  working on residuals.
- The filter/lens re-aggregation over lazy-loaded subtrees is follow-up work
  (today it operates on the loaded tree only; note it in the UI).

## Tests / harnesses (BE is arithmetic — assert the contract)

- **Size**: node count ≤ `W·H/A`; every child `pb ≥ threshold`; response bytes
  vs budget curve.
- **Correctness**: Σ(kept children)+other = parent (bytes, objects, tm/us);
  agreement with `tree.json` where both cover a path.
- **Perf**: scripted sweep over (path × dims × hot/cold cache) against the
  deployed CFN; p50/p95 budgets (cold ≤ ~1.5s, warm ≤ ~150ms as starting SLOs).

## Order

1. Materialize `path-index.parquet` in the daily job (+ mgu backfill for 8/26-27).
2. CFN + contract tests.
3. Client `loadChildren` + expandable `(other)`.
4. Retire pipeline floors to generous safety caps only.
