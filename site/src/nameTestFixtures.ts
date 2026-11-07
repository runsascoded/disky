import { HOT_SCOPE } from './hotModel'
import type { NamePlan } from './nameModel'

/** Synthetic, shared model/proxy fixtures; no fleet data or source artifact paths. */
export function nameFixture(plan: NamePlan = 'bounded-name-postings', date = '2026-10-05', pattern = 'datakit') {
  return { schema: 'name-summary-v1', target: 'fixture', date, pattern, path: '', exact: true, incremental: false, levels: 1, scope: HOT_SCOPE,
    plan, source: plan === 'catalog' ? 'registered precomputed batch artifact' : 'bounded dated name postings; directory rollups are atomic',
    validation: { source_prefix_proofs_checked: true, independent_query_source_oracle: false,
      description: plan === 'catalog' ? 'pinned catalog validation' : 'bounded exact first-hit coverage; no per-request source oracle',
      ...(plan === 'catalog' ? { catalog: { description: 'registered fixture proof', independently_scanned_entire_catalog: false, references: [] } } : {}) },
    source_identity: { generation: 'fixture-generation', snapshot_db: date === '2026-10-05' ? 'snapshot_05' : 'snapshot_04', history_manifest_sha256: 'a'.repeat(64) },
    root: { b: 8, o: 5 }, buckets: Array.from('abcdef', (letter, i) => ({ path: `bucket-${letter}`, pre: 1 + 4 * i, post: 4 + 4 * i, b: i === 0 ? 8 : 0, o: i === 0 ? 3 : i === 1 ? 2 : 0 })) }
}
export function nameDiff(beforePlan: NamePlan = 'bounded-name-postings', afterPlan: NamePlan = 'bounded-name-postings') {
  const before = nameFixture(beforePlan, '2026-10-04'), after = nameFixture(afterPlan)
  before.root = { b: 14, o: 7 }; before.buckets[0].b = 14; before.buckets[1].o = 4
  return { schema: 'name-summary-diff-v1', target: after.target, pattern: after.pattern, path: '', exact: true, incremental: false, levels: 1, scope: HOT_SCOPE,
    before, after, delta: { b: -6, o: -2 }, buckets: after.buckets.map((row, i) => ({ pre: row.pre, post: row.post, path: row.path,
      before: { b: before.buckets[i].b, o: before.buckets[i].o }, after: { b: row.b, o: row.o }, delta: { b: row.b - before.buckets[i].b, o: row.o - before.buckets[i].o } })) }
}
