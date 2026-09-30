/** The age pyramid's tier set + planner glue (specs/age-index.md, Phase B).
 *
 * Unlike a stock pyrmts pyramid (finer tiers cover only the live tail via a
 * shard ladder, stitched with coarser ones), our tiers are **complete**: one
 * path-major parquet per bin (`age-pyramid-<bin>`), each covering all history.
 * So we use pyrmts purely for **tier selection** — `planQuery` picks the finest
 * tier whose bin count fits the budget (and, later, `targetBin` ragged
 * decomposition) — then read that single tier directly through our D1-footer
 * reader. No `stitch`/monoid-state-columns: the metric is a plain additive
 * `(b, o)` total (the "DIY combine" fork in the spec).
 */
import { planQuery, type Pyramid, type QueryPlan, type StorageBackend, type Tier } from 'pyrmts'

/** Finest → coarsest; must match `AGE_PYRAMID_BINS` in `cloud/src/dt_cloud/index.py`.
 * A dense, all-fixed-width ladder (powers-of-2 in days, ~2× steps) so tier
 * selection lands close to any requested bin. `shards` are unused here (we store
 * a complete file per bin) but the type requires an ascending ladder ≥ `bin`. */
export const AGE_TIERS: Tier[] = [
  { name: '1h', bin: '1h', shards: ['8d'] },
  { name: '3h', bin: '3h', shards: ['8d'] },
  { name: '6h', bin: '6h', shards: ['8d'] },
  { name: '12h', bin: '12h', shards: ['8d'] },
  { name: '1d', bin: '1d', shards: ['8d'] },
  { name: '2d', bin: '2d', shards: ['8d'] },
  { name: '4d', bin: '4d', shards: ['8d'] },
  { name: '8d', bin: '8d', shards: ['8d'] },
]

// planQuery reads only tiers/limits/metrics; storage is never touched for a
// plan, so a stub satisfies the type.
const stubStorage: StorageBackend = { name: 'stub', fetchSegment: async () => [] }

export const AGE_PYRAMID: Pyramid = {
  storage: stubStorage,
  keyTemplate: 'age-pyramid-{tier}',
  axis: 'time',
  binCol: 'binstart',
  dims: [{ name: 'path', type: 'string' }],
  metrics: [{ name: 'b', monoid: 'sum' }, { name: 'o', monoid: 'sum' }],
  tiers: AGE_TIERS,
}

/** The tier + output bin pyrmts picks for a (range, bin-budget). */
export function planAge(from: Date, to: Date, binBudget: number): QueryPlan {
  return planQuery(AGE_PYRAMID, { range: { from, to }, binBudget })
}

/** The variant key for the tier a plan selected. */
export function variantForPlan(plan: QueryPlan): string {
  return `age-pyramid-${plan.outputTier?.bin ?? plan.outputBin}`
}
