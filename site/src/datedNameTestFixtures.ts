import { HOT_DATES, HOT_SCOPE } from './hotModel'
import { nameFixture } from './nameTestFixtures'

/** Synthetic identities/weights only: independent numbering deliberately differs. */
export const datedCapabilities = { bucket_drill: false, child_drill: false, fallback: false }
const selection = 'membership on declared qualification dates; no current-scan frequency claim'
export function legacyNameRegistry() {
  return { schema: 'name-summary-registry-v1', target: 'fixture', dates: [...HOT_DATES], levels: 1, scope: HOT_SCOPE,
    catalog_patterns: { '2026-10-04': 3, '2026-10-05': 3 }, source_prefix_proofs_checked: true, cold_slots: 1, catalog_slots: 2,
    compute_seconds: 5, work_bounds: { max_names: 200000, max_postings: 100000, max_roots: 100000 }, cold_quarantined: false }
}
export function dailyNameFixture(date = '2026-10-06') {
  return { schema: 'dated-name-summary-v1', logical_store: 'gcs', target: 'daily_fixture', date, pattern: 'datakit', path: '',
    exact: true, incremental: false, levels: 1, scope: HOT_SCOPE, plan: 'catalog', source: 'published dated precomputed batch artifact',
    source_identity: { target: 'daily_fixture', snapshot_db: 'daily_fixture', generation: 'b'.repeat(32), artifact_sha256: 'c'.repeat(64),
      artifact_bytes: 1000, source_manifest_sha256: 'd'.repeat(64), source_prefix_proofs_checked: true, kind: 'daily-scalar-source-v1' },
    registry: { qualification_dates: [...HOT_DATES], target: 'fixture', patterns: 3, threshold_paths: 100000, max_chars: 16 as number | null, selection_contract: selection },
    validation: { description: 'bound audited scalar source; not an independent full-catalog oracle', source_prefix_proofs_checked: true, independent_full_catalog_source_oracle: false },
    capabilities: { ...datedCapabilities }, root: { b: 10, o: 6 },
    // Reverse path order in DFS, and different interval widths from the frozen fixture.
    buckets: Array.from('fedcba', (letter, i) => ({ path: `bucket-${letter}`, pre: 1 + 3 * i, post: 3 + 3 * i, b: letter === 'a' ? 10 : 0, o: letter === 'a' ? 4 : letter === 'b' ? 2 : 0 })) }
}
export function datedNameRegistry() {
  const daily = dailyNameFixture(), { kind: _kind, generation, ...source } = daily.source_identity
  return { schema: 'dated-name-summary-registry-v1', logical_store: 'gcs', bucket_paths: Array.from('abcdef', letter => `bucket-${letter}`),
    dates: [...HOT_DATES.map(date => ({ date, plans: ['catalog', 'bounded-name-postings'], kind: 'frozen-history',
      registry: { qualification_dates: [...HOT_DATES], target: 'fixture', patterns: 3, selection_contract: selection } })),
    { date: daily.date, plans: ['catalog'], kind: 'daily-scalar-source-v1', registry: daily.registry, source, generation }],
    levels: 1, scope: HOT_SCOPE, daily_catalog_slots: 2, legacy: legacyNameRegistry(), capabilities: { ...datedCapabilities } }
}
export const consolidatedSource = 'bounded name postings over the consolidated store; directory rollups are atomic'
export function consolidatedNameFixture(date = '2026-09-15') {
  const { registry: _registry, ...daily } = dailyNameFixture(date)
  return { ...daily, target: 'default', plan: 'bounded-name-postings', source: consolidatedSource,
    source_identity: { kind: 'consolidated-store-v1', target: 'default', postings: 'm', through: '2026-10-06', geometry: 'preorder' },
    validation: { description: "bounded exact first-hit coverage over the consolidated store's name index; no per-request source oracle", source_prefix_proofs_checked: true, independent_full_catalog_source_oracle: false } }
}
/** The dated registry with one older scan only the consolidated store holds, and the daily scan's cold plan over it. */
export function consolidatedNameRegistry() {
  const body = datedNameRegistry()
  body.dates[2].plans = ['catalog', 'bounded-name-postings']
  return { ...body, dates: [{ date: '2026-09-15', plans: ['bounded-name-postings'], kind: 'consolidated-store-v1', source: { target: 'default', postings: 'm', through: '2026-10-06', geometry: 'preorder' } }, ...body.dates] }
}
export const consolidatedCatalogSource = "the consolidated catalog: every scan's registered literals precomputed in the store"
export function consolidatedCatalogRegistry(date = '2026-10-01') {
  return { qualification_dates: [date], target: 'catalog', patterns: 70_001, selection_contract: 'membership on declared qualification dates; no current-scan frequency claim',
    threshold_paths: 100_000, max_chars: null, short_chars: 2 }
}
/** A consolidated scan's answer with the store's catalog bound: registered literals from it, the rest on demand. */
export function consolidatedCatalogFixture(plan: 'catalog' | 'bounded-name-postings', date = '2026-10-01') {
  const body = consolidatedNameFixture(date)
  return { ...body, plan, source: plan === 'catalog' ? consolidatedCatalogSource : consolidatedSource,
    source_identity: { ...body.source_identity, catalog: 'catalog' }, registry: consolidatedCatalogRegistry(date),
    ...(plan === 'catalog' ? { validation: { description: "exact first-hit totals appended from each scan's changes; checked against single-scan builds offline, no per-request source oracle", source_prefix_proofs_checked: true, independent_full_catalog_source_oracle: false } } : {}) }
}
/** The dated registry with a consolidated scan the store's catalog covers. */
export function consolidatedCatalogNameRegistry() {
  const body = consolidatedNameRegistry()
  return { ...body, dates: [body.dates[0], { date: '2026-10-01', plans: ['catalog', 'bounded-name-postings'], kind: 'consolidated-store-v1',
    source: { target: 'default', postings: 'm', through: '2026-10-06', geometry: 'preorder', catalog: 'catalog' }, registry: consolidatedCatalogRegistry() }, ...body.dates.slice(1)] }
}
export function mixedDatedNameDiff() {
  const old = nameFixture('catalog'), before = { ...old, schema: 'dated-name-summary-v1', logical_store: 'gcs',
    source_identity: { ...old.source_identity, generation: 'a'.repeat(32), kind: 'frozen-history' }, capabilities: { ...datedCapabilities } }, after = dailyNameFixture()
  return { schema: 'dated-name-summary-diff-v1', logical_store: 'gcs', from: before.date, date: after.date, pattern: 'datakit', path: '',
    exact: true, incremental: false, levels: 1, scope: HOT_SCOPE, capabilities: { ...datedCapabilities }, before, after, delta: { b: 2, o: 1 },
    buckets: before.buckets.map(row => {
      const next = after.buckets.find(next => next.path === row.path)!
      const side = ({ pre, post, b, o }: typeof row) => ({ pre, post, b, o })
      return { path: row.path, before: side(row), after: side(next), delta: { b: next.b - row.b, o: next.o - row.o } }
    }) }
}
/** The static name index's registry (`functions/_lib/nameSummaryStatic.ts`): every scan of a generation, one bound V. */
export function staticNameRegistry(dates = ['2026-10-04', '2026-10-05', '2026-10-06']) {
  return { schema: 'static-name-registry-v1', logical_store: 'gcs', generation: '2026-10-08c', max_rows: 100000, bucket_paths: Array.from('abcdef', letter => `bucket-${letter}`),
    dates, levels: 1, scope: 'case-insensitive substring within names; directory hits cover descendants; bytes/objects only', capabilities: datedCapabilities }
}
export const staticSource = 'static suffix postings on R2, one ranged read by the Worker; directory rollups are atomic'
export const staticCatalogSource = 'the static catalog on R2: per-bucket running totals precomputed for every scan of the generation, one ranged read by the Worker'
export function staticNameFixture(plan: 'catalog' | 'bounded-name-postings', date = '2026-10-05') {
  const body = dailyNameFixture(date)
  return { schema: body.schema, logical_store: 'gcs', target: 'static_names', date, pattern: body.pattern, path: '', exact: true, incremental: false, levels: 1, scope: body.scope,
    plan, source: plan === 'catalog' ? staticCatalogSource : staticSource, source_identity: { kind: 'static-names-v1', generation: '2026-10-08c', max_rows: 100000 },
    validation: { description: 'exact first-hit totals from the static name index', source_prefix_proofs_checked: true, independent_full_catalog_source_oracle: false },
    capabilities: datedCapabilities, root: body.root, buckets: body.buckets }
}
/** A sub-daily store (cw-shaped): two scans on 10/9 plus a date-only 10/8, five buckets, its own generation. */
export const SUB_DAILY_SCANS = ['2026-10-08', '2026-10-09T0601', '2026-10-09T1802']
export function fiveBucketRegistry() {
  return { ...staticNameRegistry(SUB_DAILY_SCANS), logical_store: 'cw', generation: '2026-10-09cw', bucket_paths: Array.from('abcde', letter => `bucket-${letter}`) }
}
/** `staticNameFixture` on the five-bucket store: bucket-f (preorder 1–3, empty) dropped, the rest renumbered from 1. */
export function fiveBucketFixture(plan: 'catalog' | 'bounded-name-postings', date: string, b = 10) {
  const body = staticNameFixture(plan, date)
  const buckets = body.buckets.filter(row => row.path !== 'bucket-f').map(row => ({ ...row, pre: row.pre - 3, post: row.post - 3, b: row.path === 'bucket-a' ? b : row.b }))
  return { ...body, logical_store: 'cw', source_identity: { ...body.source_identity, generation: '2026-10-09cw' }, root: { b, o: body.root.o }, buckets }
}
