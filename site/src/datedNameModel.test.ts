import { afterEach, describe, expect, it, vi } from 'vitest'
import { consolidatedCatalogFixture, consolidatedCatalogNameRegistry, consolidatedCatalogRegistry, consolidatedCatalogSource, consolidatedNameFixture, consolidatedNameRegistry, consolidatedSource, dailyNameFixture, datedCapabilities, datedNameRegistry, legacyNameRegistry, mixedDatedNameDiff, staticNameFixture, staticNameRegistry } from './datedNameTestFixtures'
import type { NameQualification } from './nameModel'
import { SUB_DAILY_SCANS, fiveBucketFixture, fiveBucketRegistry } from './datedNameTestFixtures'
import { datedNameRequest, loadName, loadNameRegistry, nameRequest, nameResultForRegistry, parseName, parseNameRegistry } from './nameModel'

const request = { date: '2026-10-06', name: 'datakit', from: '2026-10-05' }
const scans = ['2026-10-04', '2026-10-05', '2026-10-06']
const view = (body: ReturnType<typeof dailyNameFixture> | ReturnType<typeof mixedDatedNameDiff>['before'] | ReturnType<typeof consolidatedNameFixture>) => ({ target: body.target, date: body.date, pattern: body.pattern, root: body.root, buckets: [...body.buckets].sort((a, b) => a.path < b.path ? -1 : 1) })
const execution = (body: ReturnType<typeof dailyNameFixture> | ReturnType<typeof mixedDatedNameDiff>['before'] | ReturnType<typeof consolidatedNameFixture>) => ({ plan: body.plan, source: body.source, validation: body.validation, source_identity: body.source_identity, ...('registry' in body ? { registry: body.registry } : {}) })
afterEach(() => vi.unstubAllGlobals())

describe('independently numbered dated root summaries', () => {
  it('accepts mixed sources, path-aligns all buckets and preserves each side’s own identity and intervals', () => {
    const body = mixedDatedNameDiff(), result = parseName(body, request)
    expect(result).toEqual({ before: view(body.before), after: view(body.after), delta: { b: 2, o: 1 },
      execution: { before: execution(body.before), after: execution(body.after) }, logical_store: 'gcs', capabilities: datedCapabilities })
    expect(result.after.buckets.map(row => [row.path, row.pre, row.post])).toEqual([
      ['bucket-a', 16, 18], ['bucket-b', 13, 15], ['bucket-c', 10, 12], ['bucket-d', 7, 9], ['bucket-e', 4, 6], ['bucket-f', 1, 3],
    ])
  })
  it('accepts zero matches without a fake frozen history identity or drill capability', () => {
    const body = dailyNameFixture(); body.root = { b: 0, o: 0 }; body.buckets.forEach(row => { row.b = 0; row.o = 0 })
    expect(parseName(body, { date: request.date, name: request.name })).toEqual({ after: view(body), execution: { after: execution(body) }, logical_store: 'gcs', capabilities: datedCapabilities })
  })
  it('accepts two independently pinned daily catalog dates, without enabling detail', () => {
    const body = mixedDatedNameDiff(), before = dailyNameFixture('2026-10-06')
    body.after.date = '2026-10-07'; body.date = '2026-10-07'; body.from = '2026-10-06'
    Object.assign(before.source_identity, { target: 'earlier_daily', snapshot_db: 'earlier_daily', generation: 'e'.repeat(32) }); before.target = 'earlier_daily'
    before.buckets.forEach((row, i) => { row.pre = 1 + 5 * i; row.post = 5 + 5 * i })
    Object.assign(body, { before, delta: { b: 0, o: 0 }, buckets: body.after.buckets.map(row => {
      const a = before.buckets.find(a => a.path === row.path)!
      return { path: row.path, before: { pre: a.pre, post: a.post, b: a.b, o: a.o }, after: { pre: row.pre, post: row.post, b: row.b, o: row.o }, delta: { b: 0, o: 0 } }
    }) })
    const result = parseName(body, { date: '2026-10-07', name: 'datakit', from: '2026-10-06' })
    expect(result).toEqual({ before: view(before), after: view(body.after), delta: { b: 0, o: 0 }, execution: { before: execution(before), after: execution(body.after) }, logical_store: 'gcs', capabilities: datedCapabilities })
  })
  it('accepts a complete-length registry (`max_chars: null`: a miss of any length is below the threshold)', () => {
    const body = dailyNameFixture(); body.registry.max_chars = null
    expect(parseName(body, { date: request.date, name: request.name })).toEqual({ after: view(body), execution: { after: execution(body) }, logical_store: 'gcs', capabilities: datedCapabilities })
    expect(execution(body).registry).toEqual({ qualification_dates: ['2026-10-04', '2026-10-05'], target: 'fixture', patterns: 3, threshold_paths: 100000, max_chars: null, selection_contract: 'membership on declared qualification dates; no current-scan frequency claim' })
  })
  it('accepts a short-literal domain (`short_chars`: every literal that short is registered whatever its frequency) and refuses a zero one', () => {
    const body = dailyNameFixture(); Object.assign(body.registry, { max_chars: null, short_chars: 2 })
    const parsed = parseName(body, { date: request.date, name: request.name }) as { execution: { after: { registry?: unknown } } }
    expect(parsed.execution.after.registry).toEqual({ qualification_dates: ['2026-10-04', '2026-10-05'], target: 'fixture', patterns: 3, threshold_paths: 100000, max_chars: null, short_chars: 2, selection_contract: 'membership on declared qualification dates; no current-scan frequency claim' })
    const zero = dailyNameFixture(); Object.assign(zero.registry, { short_chars: 0 })
    expect(() => parseName(zero, { date: request.date, name: request.name })).toThrow()
  })
  it('accepts a cost-weighted registry (`threshold_rows` with `name_rows`: on-demand postings rows) in place of `threshold_paths`, and refuses a mix or a missing per-name charge', () => {
    const body = dailyNameFixture(); const { threshold_paths: _, ...rest } = body.registry
    body.registry = { ...rest, max_chars: null, short_chars: 2, threshold_rows: 100000, name_rows: 256 } as unknown as typeof body.registry
    const parsed = parseName(body, { date: request.date, name: request.name }) as { execution: { after: { registry?: NameQualification } } }
    expect(parsed.execution.after.registry).toEqual({ qualification_dates: ['2026-10-04', '2026-10-05'], target: 'fixture', patterns: 3, threshold_rows: 100000, name_rows: 256, max_chars: null, short_chars: 2, selection_contract: 'membership on declared qualification dates; no current-scan frequency claim' })
    const mixed = dailyNameFixture(); Object.assign(mixed.registry, { threshold_rows: 100000, name_rows: 256 })
    expect(() => parseName(mixed, { date: request.date, name: request.name })).toThrow()
    const bare = dailyNameFixture(); const { threshold_paths: __, ...others } = bare.registry
    bare.registry = { ...others, threshold_rows: 100000 } as unknown as typeof bare.registry
    expect(() => parseName(bare, { date: request.date, name: request.name })).toThrow()
  })
  it.each(['wrong root', 'missing bucket', 'duplicate path', 'geometry gap', 'unsafe scalar', 'wrong snapshot', 'wrong date', 'wrong literal', 'fake history', 'false source proof', 'oracle claim', 'drill claim', 'missing registry', 'zero threshold', 'zero length', 'extra top field'])('refuses %s, not a partial or zero result', issue => {
    const body = dailyNameFixture()
    if (issue === 'wrong root') body.root.b++
    if (issue === 'missing bucket') body.buckets.pop()
    if (issue === 'duplicate path') body.buckets[0].path = body.buckets[1].path
    if (issue === 'geometry gap') body.buckets[0].post++
    if (issue === 'unsafe scalar') body.buckets[0].o = Number.MAX_SAFE_INTEGER + 1
    if (issue === 'wrong snapshot') body.source_identity.snapshot_db = 'different'
    if (issue === 'wrong date') body.date = '2026-10-07'
    if (issue === 'wrong literal') body.pattern = 'other'
    if (issue === 'fake history') Object.assign(body.source_identity, { history_manifest_sha256: 'f'.repeat(64) })
    if (issue === 'false source proof') body.source_identity.source_prefix_proofs_checked = false
    if (issue === 'oracle claim') body.validation.independent_full_catalog_source_oracle = true
    if (issue === 'drill claim') body.capabilities.bucket_drill = true
    if (issue === 'missing registry') Object.assign(body, { registry: undefined })
    if (issue === 'zero threshold') body.registry.threshold_paths = 0
    if (issue === 'zero length') body.registry.max_chars = 0
    if (issue === 'extra top field') Object.assign(body, { fallback: true })
    expect(() => parseName(body, { date: request.date, name: request.name })).toThrow(Error)
  })
  it('accepts a daily scan\'s bounded answer over its own name index, bound to the same pinned scan and registry', () => {
    const body = { ...dailyNameFixture(), plan: 'bounded-name-postings', source: "bounded dated name postings over the scan's own name index; directory rollups are atomic" }
    const result = parseName(body, { date: request.date, name: request.name })
    expect(result).toEqual({ after: view(body), execution: { after: execution(body) }, logical_store: 'gcs', capabilities: datedCapabilities })
    const registry = datedNameRegistry(); registry.dates[2].plans = ['catalog', 'bounded-name-postings']
    expect(nameResultForRegistry(result, parseNameRegistry(registry))).toBe(result)
    expect(() => nameResultForRegistry(result, parseNameRegistry(datedNameRegistry()))).toThrow('Name summary returned a different scan or execution plan from the available-scan registry.')
  })
  it.each(['catalog source', 'frozen source', 'unknown plan'])('refuses a daily bounded answer with a mismatched %s', issue => {
    const body = { ...dailyNameFixture(), plan: 'bounded-name-postings', source: "bounded dated name postings over the scan's own name index; directory rollups are atomic" }
    if (issue === 'catalog source') body.source = 'published dated precomputed batch artifact'
    if (issue === 'frozen source') body.source = 'bounded dated name postings; directory rollups are atomic'
    if (issue === 'unknown plan') body.plan = 'scan'
    expect(() => parseName(body, { date: request.date, name: request.name })).toThrow('Name summary returned an invalid dated contract.')
  })
  it('accepts a daily scan\'s bounded answer over the consolidated store, bound to the same pinned scan and registry', () => {
    const body = { ...dailyNameFixture(), plan: 'bounded-name-postings', source: consolidatedSource }
    const result = parseName(body, { date: request.date, name: request.name })
    expect(result).toEqual({ after: view(body), execution: { after: execution(body) }, logical_store: 'gcs', capabilities: datedCapabilities })
    expect(nameResultForRegistry(result, parseNameRegistry(consolidatedNameRegistry()))).toBe(result)
  })
  it('accepts a scan only the consolidated store holds: on demand, no registry qualification, pinned to the store\'s postings', () => {
    const body = consolidatedNameFixture(), result = parseName(body, { date: '2026-09-15', name: 'datakit' })
    expect(result).toEqual({ after: view(body), execution: { after: { plan: 'bounded-name-postings', source: consolidatedSource, validation: body.validation, source_identity: body.source_identity } }, logical_store: 'gcs', capabilities: datedCapabilities })
    const registry = parseNameRegistry(consolidatedNameRegistry())
    expect(registry.dates[0]).toEqual({ date: '2026-09-15', plans: ['bounded-name-postings'], kind: 'consolidated-store-v1', source_identity: body.source_identity })
    expect(nameResultForRegistry(result, registry)).toBe(result)
    const moved = parseName({ ...body, source_identity: { ...body.source_identity, postings: 'other' } }, { date: '2026-09-15', name: 'datakit' })
    expect(() => nameResultForRegistry(moved, registry)).toThrow('Name summary returned a different pinned per-scan source from the available-scan registry.')
  })
  it.each(['catalog plan', 'own-index source', 'registry', 'unknown postings', 'through date', 'geometry kind'])('refuses a consolidated answer with a mismatched %s', issue => {
    const body: Record<string, unknown> & ReturnType<typeof consolidatedNameFixture> = consolidatedNameFixture()
    if (issue === 'catalog plan') body.plan = 'catalog'
    if (issue === 'own-index source') body.source = "bounded dated name postings over the scan's own name index; directory rollups are atomic"
    if (issue === 'registry') body.registry = dailyNameFixture().registry
    if (issue === 'unknown postings') Object.assign(body.source_identity, { postings: 'Bad-Name' })
    if (issue === 'through date') Object.assign(body.source_identity, { through: '2026-13-01' })
    if (issue === 'geometry kind') Object.assign(body.source_identity, { geometry: 'approximate' })
    expect(() => parseName(body, { date: '2026-09-15', name: 'datakit' })).toThrow('Name summary returned an invalid dated contract.')
  })
  it.each(['catalog plan', 'registry', 'through before date', 'legacy day'])('refuses consolidated metadata with a %s', issue => {
    const body = consolidatedNameRegistry(), row: Record<string, unknown> = body.dates[0]
    if (issue === 'catalog plan') row.plans = ['catalog', 'bounded-name-postings']
    if (issue === 'registry') row.registry = dailyNameFixture().registry
    if (issue === 'through before date') row.source = { target: 'default', postings: 'm', through: '2026-09-14', geometry: 'preorder' }
    if (issue === 'legacy day') row.date = '2026-10-04'
    expect(() => parseNameRegistry(body)).toThrow('Name summary returned an invalid dated contract.')
  })
  it.each(['catalog', 'bounded-name-postings'] as const)('accepts a consolidated scan the store\'s catalog covers, answered by its %s plan with the scan\'s own registry', plan => {
    const body = consolidatedCatalogFixture(plan), result = parseName(body, { date: '2026-10-01', name: 'datakit' })
    expect(result).toEqual({ after: view(body), execution: { after: { plan, source: plan === 'catalog' ? consolidatedCatalogSource : consolidatedSource, validation: body.validation,
      source_identity: body.source_identity, registry: consolidatedCatalogRegistry() } }, logical_store: 'gcs', capabilities: datedCapabilities })
    const registry = parseNameRegistry(consolidatedCatalogNameRegistry())
    expect(registry.dates[1]).toEqual({ date: '2026-10-01', plans: ['catalog', 'bounded-name-postings'], kind: 'consolidated-store-v1', qualification_dates: ['2026-10-01'],
      source_identity: body.source_identity, registry: consolidatedCatalogRegistry() })
    expect(nameResultForRegistry(result, registry)).toBe(result)
    expect(() => nameResultForRegistry(result, parseNameRegistry(consolidatedNameRegistry()))).toThrow('Name summary returned a different scan or execution plan from the available-scan registry.')
  })
  it.each(['qualification date', 'catalog target', 'missing registry', 'postings source on the catalog plan', 'catalog plan without a catalog'])('refuses a consolidated catalog answer with a mismatched %s', issue => {
    const body: Record<string, unknown> & ReturnType<typeof consolidatedCatalogFixture> = consolidatedCatalogFixture('catalog')
    if (issue === 'qualification date') body.registry = consolidatedCatalogRegistry('2026-10-02')
    if (issue === 'catalog target') body.registry = { ...consolidatedCatalogRegistry(), target: 'other' }
    if (issue === 'missing registry') delete (body as Record<string, unknown>).registry
    if (issue === 'postings source on the catalog plan') body.source = consolidatedSource
    if (issue === 'catalog plan without a catalog') body.source_identity = consolidatedNameFixture('2026-10-01').source_identity as typeof body.source_identity
    expect(() => parseName(body, { date: '2026-10-01', name: 'datakit' })).toThrow('Name summary returned an invalid dated contract.')
  })
  it.each(['qualification date', 'missing registry', 'postings-only plans'])('refuses catalog-covered consolidated metadata with a %s', issue => {
    const body = consolidatedCatalogNameRegistry(), row: Record<string, unknown> = body.dates[1]
    if (issue === 'qualification date') row.registry = consolidatedCatalogRegistry('2026-09-15')
    if (issue === 'missing registry') delete row.registry
    if (issue === 'postings-only plans') row.plans = ['bounded-name-postings']
    expect(() => parseNameRegistry(body)).toThrow('Name summary returned an invalid dated contract.')
  })
  it.each(['store', 'date', 'root delta', 'bucket delta', 'side bounds', 'side weights', 'missing row', 'different paths'])('refuses inconsistent paired %s', issue => {
    const body = mixedDatedNameDiff()
    if (issue === 'store') body.before.logical_store = 'other'
    if (issue === 'date') body.from = '2026-10-04'
    if (issue === 'root delta') body.delta.o++
    if (issue === 'bucket delta') body.buckets[0].delta.o++
    if (issue === 'side bounds') body.buckets[0].after.pre = body.buckets[0].before.pre
    if (issue === 'side weights') body.buckets[0].before.b++
    if (issue === 'missing row') body.buckets.pop()
    if (issue === 'different paths') body.after.buckets[0].path = 'bucket-unknown'
    expect(() => parseName(body, request)).toThrow('Name summary returned an invalid dated contract.')
  })
})

describe('scan-specific availability', () => {
  const expectedOld = { dated: false, dates: scans.slice(0, 2).map(date => ({ date, plans: ['catalog', 'bounded-name-postings'], kind: 'frozen-history' })) }
  const expected = { dated: true, logical_store: 'gcs', bucket_paths: Array.from('abcdef', letter => `bucket-${letter}`),
    dates: [...expectedOld.dates.map(row => ({ ...row, qualification_dates: scans.slice(0, 2) })), { date: scans[2], plans: ['catalog'], kind: 'daily-scalar-source-v1', qualification_dates: scans.slice(0, 2), source_identity: dailyNameFixture().source_identity, registry: dailyNameFixture().registry }] }
  it('separates available scan dates from registry qualification dates and preserves old metadata', () => {
    expect(parseNameRegistry(legacyNameRegistry())).toEqual(expectedOld)
    expect(parseNameRegistry(datedNameRegistry())).toEqual(expected)
    expect(nameRequest(new URLSearchParams(), scans)).toEqual({ date: '2026-10-06', name: 'datakit' })
    expect(nameRequest(new URLSearchParams('date=2026-10-06&name=DATAKIT&from=2026-10-05'), scans)).toEqual(request)
    expect(datedNameRequest(new URLSearchParams('date=2026-10-08&name=A%26B'))).toEqual({ date: '2026-10-08', name: 'a&b' })
  })
  it.each(['cold plan without catalog', 'missing legacy day', 'invalid source hash', 'fake history source', 'invalid qualification date', 'false drill capability', 'extra field'])('refuses %s metadata', issue => {
    const body = datedNameRegistry()
    if (issue === 'cold plan without catalog') body.dates[2].plans = ['bounded-name-postings']
    if (issue === 'missing legacy day') body.dates.splice(0, 1)
    const daily = body.dates[2]
    if (issue === 'invalid source hash' && 'source' in daily) Object.assign(daily.source, { source_manifest_sha256: 'invalid' })
    if (issue === 'fake history source' && 'source' in daily) Object.assign(daily.source, { history_manifest_sha256: 'a'.repeat(64) })
    if (issue === 'invalid qualification date') Object.assign(daily.registry, { qualification_dates: ['2026-02-30'] })
    if (issue === 'false drill capability') body.capabilities.fallback = true
    if (issue === 'extra field') Object.assign(body, { fallback: true })
    expect(() => parseNameRegistry(body)).toThrow('Name summary returned an invalid dated contract.')
  })
  it.each(['logical store', 'bucket paths', 'scan plan', 'source generation', 'artifact hash', 'source hash', 'source bytes'])('binds cached results to metadata %s rather than displaying a valid but unrelated scope', issue => {
    const result = parseName(mixedDatedNameDiff(), request), registry = parseNameRegistry(datedNameRegistry())
    if (issue === 'logical store') result.logical_store = 'different'
    if (issue === 'bucket paths') result.after.buckets[0].path = 'other-bucket'
    if (issue === 'scan plan') registry.dates[2].plans = ['bounded-name-postings']
    if (issue === 'source generation') result.execution.after.source_identity.generation = 'e'.repeat(32)
    if (issue === 'artifact hash') result.execution.after.source_identity.artifact_sha256 = 'e'.repeat(64)
    if (issue === 'source hash') result.execution.after.source_identity.source_manifest_sha256 = 'e'.repeat(64)
    if (issue === 'source bytes') result.execution.after.source_identity.artifact_bytes = 2000
    expect(() => nameResultForRegistry(result, registry)).toThrow(issue === 'scan plan' ? 'Name summary returned a different scan or execution plan from the available-scan registry.' : issue === 'logical store' || issue === 'bucket paths' ? 'Name summary returned a different logical store or bucket set from the available-scan registry.' : 'Name summary returned a different pinned per-scan source from the available-scan registry.')
  })
  it('rejects oversized or recursive dated metadata rather than walking an unbounded availability graph', () => {
    const oversized = datedNameRegistry(); oversized.dates = Array.from({ length: 401 }, () => oversized.dates[2])
    const recursive = datedNameRegistry(); Object.assign(recursive, { legacy: datedNameRegistry() })
    expect(() => parseNameRegistry(oversized)).toThrow('Name summary returned an invalid dated contract.')
    expect(() => parseNameRegistry(recursive)).toThrow('Name summary returned an invalid dated contract.')
  })
  it.each(['qualification dates', 'qualification target', 'threshold', 'registered count'])('binds daily response %s to metadata rather than pretending current-scan hot qualification', issue => {
    const body = mixedDatedNameDiff()
    if (issue === 'qualification dates') body.after.registry.qualification_dates = ['2026-10-05']
    if (issue === 'qualification target') body.after.registry.target = 'other_registry'
    if (issue === 'threshold') body.after.registry.threshold_paths = 200000
    if (issue === 'registered count') body.after.registry.patterns = 4
    expect(() => nameResultForRegistry(parseName(body, request), parseNameRegistry(datedNameRegistry()))).toThrow('Name summary returned different registry qualification from the available-scan registry.')
  })
  it.each(['date=2026-02-30', 'date=0000-01-01', 'date=2026-10-06&from=2026-10-06', 'date=2026-10-06&from=2026-10-07', 'name=a%2Fb', 'name=', 'date=2026-10-06&date=2026-10-05', 'owner=other'])('refuses invalid dated request %s', params => {
    expect(() => datedNameRequest(new URLSearchParams(params))).toThrow(Error)
  })
  it('rejects an unavailable date before traffic, while legacy defaults remain frozen', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
    expect(() => nameRequest(new URLSearchParams('date=2026-10-06'))).toThrow('Only the frozen 2026-10-04 and 2026-10-05 scans are available.')
    await expect(loadName({ date: '2026-10-07', name: 'datakit' }, undefined, scans)).rejects.toEqual(new Error('This scan is unavailable in the name-summary registry; it is not a zero-match result.'))
    expect(fetcher.mock.calls).toEqual([])
  })
  it('fetches metadata and a selected dated comparison with exact parameter encoding', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(datedNameRegistry()))).mockResolvedValueOnce(new Response(JSON.stringify(mixedDatedNameDiff())))
    vi.stubGlobal('fetch', fetcher)
    expect(await loadNameRegistry()).toEqual(expected)
    expect(await loadName(request, undefined, scans)).toEqual(parseName(mixedDatedNameDiff(), request))
    expect(fetcher.mock.calls).toEqual([['/api/name-summary-registry', { signal: undefined }], ['/api/name-summary?date=2026-10-06&name=datakit&from=2026-10-05', { signal: undefined }]])
  })
  it('refuses unavailable daily literals explicitly, with no fallback request', async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response('private details', { status: 400 })); vi.stubGlobal('fetch', fetcher)
    await expect(loadName(request, undefined, scans)).rejects.toEqual(new Error('This literal or scan is unavailable for the selected name-summary plan; it is not a zero-match result.'))
    expect(fetcher.mock.calls).toEqual([['/api/name-summary?date=2026-10-06&name=datakit&from=2026-10-05', { signal: undefined }]])
  })
  it.each([[401, 'Sign in to view available name-summary scans.'], [503, 'Available name-summary scans could not be loaded. Try again; no dates or zero results have been inferred.']] as const)('metadata status %s does not invent dates', async (status, message) => {
    const fetcher = vi.fn().mockResolvedValue(new Response('private details', { status })); vi.stubGlobal('fetch', fetcher)
    await expect(loadNameRegistry()).rejects.toEqual(new Error(message))
    expect(fetcher.mock.calls).toEqual([['/api/name-summary-registry', { signal: undefined }]])
  })
})

describe('the static name index', () => {
  const identity = { kind: 'static-names-v1', generation: '2026-10-08c', max_rows: 100000 }
  it('binds every scan of the generation to one identity, and both plans to it', () => {
    const registry = parseNameRegistry(staticNameRegistry())
    expect(registry).toEqual({ dated: true, logical_store: 'gcs', bucket_paths: Array.from('abcdef', letter => `bucket-${letter}`), static: { generation: '2026-10-08c', max_rows: 100000 },
      dates: ['2026-10-04', '2026-10-05', '2026-10-06'].map(date => ({ date, plans: ['catalog', 'bounded-name-postings'], kind: 'static-names-v1', source_identity: identity })) })
    for (const plan of ['catalog', 'bounded-name-postings'] as const) {
      const body = staticNameFixture(plan), result = nameResultForRegistry(parseName(body, { date: '2026-10-05', name: 'datakit' }), registry)
      expect([result.after, result.execution.after]).toEqual([{ target: body.target, date: body.date, pattern: body.pattern, root: body.root, buckets: [...body.buckets].sort((a, b) => a.path < b.path ? -1 : 1) }, { plan, source: body.source, validation: body.validation, source_identity: identity }])
    }
  })
  it.each([
    ['unsorted dates', { dates: ['2026-10-05', '2026-10-04'] }],
    ['a bad generation', { generation: 'Latest/1' }],
    ['a zero bound', { max_rows: 0 }],
    ['duplicate buckets', { bucket_paths: ['bucket-a', 'bucket-b', 'bucket-a'] }],
    ['a bucket with a slash', { bucket_paths: ['bucket-a', 'bucket/b'] }],
    ['no buckets', { bucket_paths: [] }],
    ['an extra key', { legacy: legacyNameRegistry() }],
  ])('refuses a registry with %s', (_, override) => {
    expect(() => parseNameRegistry({ ...staticNameRegistry(), ...override })).toThrow('Name summary returned an invalid dated contract.')
  })
  it.each([
    ['a catalog answer with the postings source', { plan: 'catalog', source: staticNameFixture('bounded-name-postings').source }],
    ['another target', { target: 'default' }],
    ['a registry binding', { registry: dailyNameFixture().registry }],
  ])('refuses %s', (_, override) => {
    expect(() => parseName({ ...staticNameFixture('catalog'), ...override }, { date: '2026-10-05', name: 'datakit' })).toThrow('Name summary returned an invalid dated contract.')
  })
  it('refuses a static answer under a dated-store registry, and a dated-store answer under the static one', () => {
    const request = { date: '2026-10-06', name: 'datakit' }
    expect(() => nameResultForRegistry(parseName(staticNameFixture('catalog', '2026-10-06'), request), parseNameRegistry(datedNameRegistry()))).toThrow('Name summary returned a different scan or execution plan from the available-scan registry.')
    expect(() => nameResultForRegistry(parseName(dailyNameFixture(), request), parseNameRegistry(staticNameRegistry()))).toThrow('Name summary returned a different scan or execution plan from the available-scan registry.')
  })
})

describe('sub-daily scan ids and any fleet size (specs/scan-ids-not-dates.md)', () => {
  it('parses a five-bucket registry of sub-daily scans and its generation', () => {
    const registry = parseNameRegistry(fiveBucketRegistry())
    expect([registry.bucket_paths, registry.static, registry.dates.map(row => row.date)]).toEqual([
      ['bucket-a', 'bucket-b', 'bucket-c', 'bucket-d', 'bucket-e'],
      { generation: '2026-10-09cw', max_rows: 100000 },
      SUB_DAILY_SCANS,
    ])
  })
  it('parses a five-bucket answer at a sub-daily scan, bound to its registry', () => {
    const body = fiveBucketFixture('catalog', '2026-10-09T0601'), registry = parseNameRegistry(fiveBucketRegistry())
    const result = nameResultForRegistry(parseName(body, { date: '2026-10-09T0601', name: 'datakit' }), registry)
    expect([result.after.date, result.after.root, result.after.buckets.map(row => [row.path, row.pre, row.post, row.b, row.o])]).toEqual(['2026-10-09T0601', { b: 10, o: 6 }, [
      ['bucket-a', 13, 15, 10, 4], ['bucket-b', 10, 12, 0, 2], ['bucket-c', 7, 9, 0, 0], ['bucket-d', 4, 6, 0, 0], ['bucket-e', 1, 3, 0, 0],
    ]])
  })
  it('datedNameRequest accepts scan ids, not just dates', () => {
    expect(datedNameRequest(new URLSearchParams('date=2026-10-09T1802&from=2026-10-09T0601&name=gof'))).toEqual({ date: '2026-10-09T1802', name: 'gof', from: '2026-10-09T0601' })
    for (const params of ['date=2026-10-09T2460', 'date=2026-10-09T12', 'date=2026-10-09T1802&from=2026-10-09T1802']) expect(() => datedNameRequest(new URLSearchParams(params))).toThrow(Error)
  })
  it('nameRequest resolves slugs to the latest matching scan; a day is its later scan', () => {
    const req = (qs: string) => nameRequest(new URLSearchParams(qs), SUB_DAILY_SCANS)
    expect([
      req('name=gof'),
      req('date=2026-10-09&name=gof'),
      req('date=261009&name=gof'),
      req('date=261009-06&name=gof'),
      req('date=2026-10-09T0601&name=gof'),
      req('date=261009&from=261009&name=gof'),
      req('date=261009-0601&from=261008&name=gof'),
    ]).toEqual([
      { date: '2026-10-09T1802', name: 'gof' },
      { date: '2026-10-09T1802', name: 'gof' },
      { date: '2026-10-09T1802', name: 'gof' },
      { date: '2026-10-09T0601', name: 'gof' },
      { date: '2026-10-09T0601', name: 'gof' },
      { date: '2026-10-09T1802', name: 'gof', from: '2026-10-09T0601' },
      { date: '2026-10-09T0601', name: 'gof', from: '2026-10-08' },
    ])
    for (const qs of ['date=261010&name=gof', 'date=261008&from=261009&name=gof']) expect(() => req(qs)).toThrow('This scan is unavailable in the name-summary registry; it is not a zero-match result.')
  })
  it.each([
    ['a duplicate bucket', ['bucket-a', 'bucket-a']],
    ['a slash', ['bucket-a', 'bucket/b']],
  ])('refuses a dated registry with %s', (_, bucket_paths) => {
    expect(() => parseNameRegistry({ ...datedNameRegistry(), bucket_paths })).toThrow('Name summary returned an invalid dated contract.')
  })
})
