import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { NamePage, NamePlanStatus, catalogDomain, nameScanParams, nameUrlParams, scanList, staticDomain } from './NamePage'
import { nameDiff, nameFixture } from './nameTestFixtures'
import { NAME_DIRS_ONLY, type NameResult, nameRequest, parseName, parseNameRegistry } from './nameModel'
import { fmtScan } from './scan'
import { DEFAULT_STORE } from './stores'
import { scanMatches, selOf } from './scanSlug'
import { SUB_DAILY_SCANS, dailyNameFixture, datedNameRegistry, fiveBucketFixture, fiveBucketRegistry, legacyNameRegistry, mixedDatedNameDiff } from './datedNameTestFixtures'

vi.mock('./SiteKbd', () => ({ SiteKbd: () => null }))
const { roots } = vi.hoisted(() => ({ roots: [] as { b: number }[] }))
vi.mock('@rdub/treemap', async original => ({ ...await original<typeof import('@rdub/treemap')>(), Treemap: ({ root }: { root: { b: number } }) => { roots.push(root); return createElement('div') } }))
/** The store's scan list (`useScans`, newest first): by default the registry's own scans — a name index
 *  that has caught up with the store. */
const SCANS_KEY = ['scans', DEFAULT_STORE.key]
function render(client: QueryClient, path = '/names?date=2026-10-05&name=datakit', seedRegistry = true) {
  if (seedRegistry && !client.getQueryState(['name-summary-registry'])) client.setQueryData(['name-summary-registry'], parseNameRegistry(legacyNameRegistry()))
  const reg = client.getQueryData<{ dates: { date: string }[] }>(['name-summary-registry'])
  if (reg && !client.getQueryState(SCANS_KEY)) client.setQueryData(SCANS_KEY, reg.dates.map(r => r.date).sort().reverse())
  return renderToStaticMarkup(createElement(QueryClientProvider, { client }, createElement(MemoryRouter, { initialEntries: [path] }, createElement(NamePage))))
}
/** The "no scan matches" state: its text, and its links. */
const misses = (html: string) => [...html.matchAll(/<p class="no-scan loading" role="alert">(.*?)<\/p>/g)]
  .map(([, inner]) => [inner.replace(/<[^>]+>/g, ''), [...inner.matchAll(/<a href="([^"]+)">(.*?)<\/a>/g)].map(([, href, label]) => [href.replace(/&amp;/g, '&'), label])])
const links = (html: string) => [...html.matchAll(/<a href="([^"]+)">(.*?)<\/a>/g)].map(([, href, label]) => [href, label])
const planParagraphs = (html: string) => /<div aria-label="Name-summary execution plan">(.*?)<\/div>/.exec(html)?.[1]
const tableRows = (html: string) => [...(html.match(/<tbody>(.*?)<\/tbody>/)?.[1] ?? '').matchAll(/<tr[^>]*>(.*?)<\/tr>/g)]
  .map(([, row]) => [...row.matchAll(/<t[hd][^>]*>(.*?)<\/t[hd]>/g)].map(([, text]) => text.replace(/<[^>]+>/g, '')))
beforeEach(() => { roots.length = 0 })
it('defaults the existing controlled form to datakit and no baseline', () => {
  const client = new QueryClient()
  try {
    const html = render(client, '/names')
    expect(html.match(/<input[^>]+>/)?.[0]).toBe('<input required="" aria-describedby="hot-availability" name="name" value="datakit"/>')
    expect(html.match(/<select name="from">(.*?)<\/select>/)?.[1]).toBe('<option value="" selected="">No baseline</option><option value="2026-10-04">2026-10-04</option>')
    expect([...html.matchAll(/<p role="status">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual(['Loading exact name summary…'])
  } finally { client.clear() }
})
it('waits for availability without inventing scan controls or fetching a summary', () => {
  const client = new QueryClient()
  try {
    const html = render(client, '/names?date=2026-10-06&name=datakit', false)
    expect([...html.matchAll(/<p role="status">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual(['Loading available name-summary scans…'])
    expect([...html.matchAll(/<select\b/g)]).toEqual([])
    expect([...html.matchAll(/<table\b/g)]).toEqual([])
    expect(roots).toEqual([])
    expect(client.getQueryState(['name-summary', undefined, undefined, undefined])?.fetchStatus).toBe('idle')
  } finally { client.clear() }
})
it('offers only metadata dates and retains the mixed-date URL baseline without fake detail links', () => {
  const client = new QueryClient(), body = mixedDatedNameDiff()
  client.setQueryData(['name-summary-registry'], parseNameRegistry(datedNameRegistry()))
  client.setQueryData(['name-summary', body.date, body.pattern, body.from], parseName(body, { date: body.date, name: body.pattern, from: body.from }))
  try {
    const html = render(client, '/names?date=2026-10-06&name=datakit&from=2026-10-05')
    expect(html.match(/<select name="date">(.*?)<\/select>/)?.[1]).toBe('<option>2026-10-04</option><option>2026-10-05</option><option selected="">2026-10-06</option>')
    expect(html.match(/<select name="from">(.*?)<\/select>/)?.[1]).toBe('<option value="">No baseline</option><option>2026-10-04</option><option selected="">2026-10-05</option>')
    expect(html.match(/<p id="hot-availability" class="hot-note">(.*?)<\/p>/)?.[1]).toBe('2026-10-06: catalog literals only, using membership qualified on 2026-10-04, 2026-10-05—not a current-scan frequency claim. Other literals are unavailable for those scans, not zero matches; no on-demand fallback.')
    expect(links(html)).toEqual([['/', 'Map'], ['/', 'storage map']])
    expect(roots.map(row => row.b)).toEqual([8, 10])
    expect(tableRows(html)).toEqual([
      ['All buckets', '8', '5', '10', '6', '+2', '+1'], ['bucket-a', '8', '3', '10', '4', '+2', '+1'], ['bucket-b', '0', '2', '0', '2', '0', '0'],
      ['bucket-c', '0', '0', '0', '0', '0', '0'], ['bucket-d', '0', '0', '0', '0', '0', '0'], ['bucket-e', '0', '0', '0', '0', '0', '0'], ['bucket-f', '0', '0', '0', '0', '0', '0'],
    ])
    expect([...html.matchAll(/<p role="alert">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual([])
  } finally { client.clear() }
})
it('a daily scan with its own name index is not "catalog literals only": other literals are answered on demand', () => {
  const client = new QueryClient(), registry = datedNameRegistry(), body = dailyNameFixture()
  registry.dates[registry.dates.length - 1].plans = ['catalog', 'bounded-name-postings']
  client.setQueryData(['name-summary-registry'], parseNameRegistry(registry))
  client.setQueryData(['name-summary', body.date, body.pattern, undefined], parseName(body, { date: body.date, name: body.pattern }))
  try {
    const html = render(client, '/names?date=2026-10-06&name=datakit')
    expect(html.match(/<p id="hot-availability" class="hot-note">(.*?)<\/p>/)?.[1]).toBe('Catalog literals use prepared summaries. Other literals use bounded name postings on demand; requests exceeding the work budget fail explicitly, not as zero matches. No unbounded fleet scan.')
    expect([...html.matchAll(/<p class="hot-note">(.*?)<\/p>/g)].map(([, text]) => text).at(-1)).toBe('These dated root summaries have exact bucket totals only; no prepared bucket detail or deeper drill-down.')
  } finally { client.clear() }
})
it('single dated scan reload has no baseline and preserves zero-byte object counts in the exact table', () => {
  const client = new QueryClient(), body = dailyNameFixture(); body.root = { b: 0, o: 2 }; body.buckets.forEach(row => { row.b = 0; row.o = row.path === 'bucket-b' ? 2 : 0 })
  client.setQueryData(['name-summary-registry'], parseNameRegistry(datedNameRegistry()))
  client.setQueryData(['name-summary', body.date, body.pattern, undefined], parseName(body, { date: body.date, name: body.pattern }))
  try {
    const html = render(client, '/names?date=2026-10-06&name=datakit')
    expect(html.match(/<select name="from">(.*?)<\/select>/)?.[1]).toBe('<option value="" selected="">No baseline</option><option>2026-10-04</option><option>2026-10-05</option>')
    expect(tableRows(html)).toEqual([['All buckets', '0', '2'], ['bucket-a', '0', '0'], ['bucket-b', '0', '2'], ['bucket-c', '0', '0'], ['bucket-d', '0', '0'], ['bucket-e', '0', '0'], ['bucket-f', '0', '0']])
    expect(links(html)).toEqual([['/', 'Map'], ['/', 'storage map']])
    expect([...html.matchAll(/<p role="alert">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual([])
  } finally { client.clear() }
})
it.each(['gcs_other', 'gcs'])('refuses a cached response with unrelated store/source (%s) before display', store => {
  const client = new QueryClient(), body = dailyNameFixture(); body.logical_store = store
  if (store === 'gcs') body.source_identity.artifact_sha256 = 'e'.repeat(64)
  client.setQueryData(['name-summary-registry'], parseNameRegistry(datedNameRegistry()))
  client.setQueryData(['name-summary', body.date, body.pattern, undefined], parseName(body, { date: body.date, name: body.pattern }))
  try {
    const html = render(client, '/names?date=2026-10-06&name=datakit')
    expect([...html.matchAll(/<p role="alert">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual([store === 'gcs_other'
      ? 'Name summary returned a different logical store or bucket set from the available-scan registry.' : 'Name summary returned a different pinned per-scan source from the available-scan registry.'])
    expect(tableRows(html)).toEqual([])
    expect(roots).toEqual([])
  } finally { client.clear() }
})
it('unknown dates remain explicit unavailable selections rather than silently changing the URL', () => {
  const client = new QueryClient()
  client.setQueryData(['name-summary-registry'], parseNameRegistry(datedNameRegistry()))
  try {
    const html = render(client, '/names?date=2026-10-07&name=datakit')
    expect(html.match(/<select name="date">(.*?)<\/select>/)?.[1]).toBe('<option value="2026-10-07" selected="">2026-10-07 (unavailable)</option><option>2026-10-04</option><option>2026-10-05</option><option>2026-10-06</option>')
    expect(misses(html)).toEqual([['No scan matches 261007. Nearest: ← 10/6', [['/names?name=datakit&d=2610060000', '← 10/6']]]])
    expect([...html.matchAll(/<p role="alert">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual([])
    expect(roots).toEqual([])
  } finally { client.clear() }
})
it('failed metadata shows no guessed dates, cached result, maps or totals', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retryOnMount: false } } }), message = 'Available name-summary scans could not be loaded. Try again; no dates or zero results have been inferred.'
  client.getQueryCache().build(client, { queryKey: ['name-summary-registry'] }, { data: undefined, dataUpdatedAt: 0, dataUpdateCount: 0,
    error: new Error(message), errorUpdatedAt: 1, errorUpdateCount: 1, fetchFailureCount: 1, fetchFailureReason: null, fetchMeta: null, isInvalidated: false, status: 'error', fetchStatus: 'idle' })
  client.setQueryData(['name-summary', '2026-10-05', 'datakit', undefined], parseName(nameFixture(), { date: '2026-10-05', name: 'datakit' }))
  try {
    const html = render(client, '/names', false)
    expect([...html.matchAll(/<p role="alert">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual([message])
    expect([...html.matchAll(/<select\b|<table\b/g)]).toEqual([])
    expect(roots).toEqual([])
  } finally { client.clear() }
})
it.each(['catalog', 'bounded-name-postings'] as const)('renders truthful %s status and no bucket detail links', plan => {
  const client = new QueryClient(), body = nameFixture(plan)
  client.setQueryData(['name-summary', body.date, body.pattern, undefined], parseName(body, { date: body.date, name: body.pattern }))
  try {
    const html = render(client)
    expect(planParagraphs(html)).toBe(`<p class="hot-note">2026-10-05: ${plan === 'catalog' ? 'Catalog (precomputed)' : 'Bounded name postings (on demand)'}. ${body.source} — ${body.validation.description}</p>`)
    expect(links(html)).toEqual([['/', 'Map'], ['/', 'storage map']])
    expect([...html.matchAll(/<span>Hover a bucket(.*?)<\/span>/g)].map(([, text]) => text)).toEqual([' or press Enter on it for exact totals. No deeper drill-down.'])
    expect(roots.map(row => row.b)).toEqual([8])
  } finally { client.clear() }
})
it('mixed comparison shows both source plans and no prepared bucket detail', () => {
  const client = new QueryClient(), body = nameDiff('catalog', 'bounded-name-postings')
  client.setQueryData(['name-summary', '2026-10-05', 'datakit', '2026-10-04'], parseName(body, { date: '2026-10-05', name: 'datakit', from: '2026-10-04' }))
  try {
    const html = render(client, '/names?date=2026-10-05&name=datakit&from=2026-10-04')
    expect(planParagraphs(html)).toBe('<p class="hot-note">2026-10-04: Catalog (precomputed). registered precomputed batch artifact — pinned catalog validation</p><p class="hot-note">2026-10-05: Bounded name postings (on demand). bounded dated name postings; directory rollups are atomic — bounded exact first-hit coverage; no per-request source oracle</p>')
    expect(links(html)).toEqual([['/', 'Map'], ['/', 'storage map']])
    expect(roots.map(row => row.b)).toEqual([14, 8])
  } finally { client.clear() }
})
it('failed request shows a nonzero-result error with no maps or table', () => {
  const client = new QueryClient({ defaultOptions: { queries: { retryOnMount: false } } })
  try {
    client.getQueryCache().build(client, { queryKey: ['name-summary', '2026-10-05', 'datakit', undefined] }, { data: undefined, dataUpdatedAt: 0, dataUpdateCount: 0,
      error: new Error('Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.'), errorUpdatedAt: 1, errorUpdateCount: 1,
      fetchFailureCount: 1, fetchFailureReason: null, fetchMeta: null, isInvalidated: false, status: 'error', fetchStatus: 'idle' })
    const html = render(client)
    expect([...html.matchAll(/<p role="alert">(.*?)<\/p>/g)].map(([, text]) => text)).toEqual(['Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.'])
    expect([...html.matchAll(/<table\b/g)]).toEqual([]); expect(roots).toEqual([])
  } finally { client.clear() }
})
it('lists few scans and summarizes many', () => {
  expect([scanList(['2026-10-04', '2026-10-05', '2026-10-06']), scanList(Array.from({ length: 68 }, (_, i) => new Date(Date.UTC(2026, 6, 30 + i)).toISOString().slice(0, 10)))])
    .toEqual(['2026-10-04, 2026-10-05, 2026-10-06', '68 scans, 2026-07-30 to 2026-10-05'])
})

it('describes a cost-weighted registry by the on-demand rows it bounds', () => {
  const registry = { qualification_dates: ['2026-10-06'], target: 'catalog', patterns: 3, selection_contract: 'membership on declared qualification dates; no current-scan frequency claim',
    threshold_rows: 100000, name_rows: 256, max_chars: null, short_chars: 2 }
  expect(catalogDomain(registry)).toBe('≥100,000 index rows to answer on demand (256 per name), or at most 2 characters')
  expect(catalogDomain({ ...registry, threshold_rows: undefined, name_rows: undefined, threshold_paths: 100000 })).toBe('≥100,000 matching paths, or at most 2 characters')
})

it('describes the static name index\'s dispatch by its bound', () => {
  expect(staticDomain({ generation: '2026-10-08c', max_rows: 100000 })).toBe('Every scan answers from the static name index (generation 2026-10-08c) with no query server: literals of one or two characters, and literals with more than 100,000 index rows, from its precomputed catalog; every other literal from its suffix postings, reading at most 100,000 rows.')
})

describe('two scans on one day: ?d= addresses each, a day picks the later', () => {
  // distinct totals per scan, so the table shows which one answered
  const seeded = () => {
    const client = new QueryClient()
    client.setQueryData(['name-summary-registry'], parseNameRegistry(fiveBucketRegistry()))
    for (const [date, b] of [['2026-10-09T0601', 6], ['2026-10-09T1802', 18]] as const)
      client.setQueryData(['name-summary', date, 'datakit', undefined], parseName(fiveBucketFixture('catalog', date, b), { date, name: 'datakit' }))
    return client
  }
  const view = (html: string) => [html.match(/<select name="date">(.*?)<\/select>/)?.[1], tableRows(html)[0], [...html.matchAll(/<p role="alert">(.*?)<\/p>/g)].map(([, text]) => text)]
  const opts = (sel: string) => SUB_DAILY_SCANS.map(day => day === sel ? `<option selected="">${day}</option>` : `<option>${day}</option>`).join('')
  it.each([
    ['/names?d=261009&name=datakit', '2026-10-09T1802', 18],
    ['/names?d=261009-18&name=datakit', '2026-10-09T1802', 18],
    ['/names?d=261009-0601&name=datakit', '2026-10-09T0601', 6],
    ['/names?d=261009-06&name=datakit', '2026-10-09T0601', 6],
    ['/names?name=datakit', '2026-10-09T1802', 18],
    // legacy links: the old key and ISO spellings
    ['/names?date=2026-10-09&name=datakit', '2026-10-09T1802', 18],
    ['/names?date=2026-10-09T0601&name=datakit', '2026-10-09T0601', 6],
    ['/names?d=2026-10-09T0601&name=datakit', '2026-10-09T0601', 6],
  ] as const)('%s → %s', (path, scan, b) => {
    const client = seeded()
    try {
      expect(view(render(client, path))).toEqual([opts(scan), ['All buckets', String(b), '6'], []])
    } finally { client.clear() }
  })
  it.each([
    // an hour with no scan, between the 10/9 scans (UTC; display is viewer-local)
    ['/names?d=26100903&name=datakit', 'No scan matches 26100903. Nearest: ← $A · $B →', [['/names?d=2610080000&name=datakit', '← $A'], ['/names?d=2610090601&name=datakit', '$B →']]],
    // a day after every scan: only an earlier neighbour
    ['/names?d=261010&name=datakit', 'No scan matches 261010. Nearest: ← $C', [['/names?d=2610091802&name=datakit', '← $C']]],
    // unparseable: nothing to offer
    ['/names?d=junk&name=datakit', 'No scan matches junk. No scan to offer instead.', []],
    // a baseline naming no earlier scan
    ['/names?d=2610091802-261007&name=datakit', 'No baseline scan matches 261007. Nearest: $A →', [['/names?d=2610091802-2610080000&name=datakit', '$A →']]],
  ])('%s is a miss with links to the nearest scans, and nothing answered', (path, text, hrefs) => {
    const client = seeded()
    const lbl = { $A: fmtScan('2026-10-08'), $B: fmtScan('2026-10-09T0601'), $C: fmtScan('2026-10-09T1802') }
    const fill = (t: string) => t.replace(/\$[ABC]/g, k => lbl[k as keyof typeof lbl])
    try {
      const html = render(client, path)
      expect(misses(html)).toEqual([[fill(text), hrefs.map(([h, l]) => [h, fill(l)])]])
      expect(tableRows(html)).toEqual([])
      expect(roots).toEqual([])
    } finally { client.clear() }
  })
})

describe('the store has a scan the name index doesn\'t hold yet: ?d= means the map\'s scan, shown as not indexed', () => {
  // The store's newest scan (`2026-10-10T1236`) is past the registry's (`SUB_DAILY_SCANS`).
  const STORE_SCANS = ['2026-10-10T1236', ...[...SUB_DAILY_SCANS].reverse()]
  const seeded = () => {
    const client = new QueryClient()
    client.setQueryData(['name-summary-registry'], parseNameRegistry(fiveBucketRegistry()))
    client.setQueryData(['scans', DEFAULT_STORE.key], STORE_SCANS)
    client.setQueryData(['name-summary', '2026-10-09T1802', 'datakit', undefined], parseName(fiveBucketFixture('catalog', '2026-10-09T1802', 18), { date: '2026-10-09T1802', name: 'datakit' }))
    return client
  }
  const notIndexed = (html: string) => [...html.matchAll(/<p class="no-scan loading" role="alert" aria-label="Scan not indexed">(.*?)<\/p>/g)]
    .map(([, inner]) => [inner.replace(/<[^>]+>/g, ''), [...inner.matchAll(/<a href="([^"]+)">(.*?)<\/a>/g)].map(([, href, label]) => [href.replace(/&amp;/g, '&'), label])])
  it.each([
    ['/names?d=261010&name=datakit', '2026-10-10T1236', '/names?d=2610091802&name=datakit'],
    ['/names?name=datakit', '2026-10-10T1236', '/names?name=datakit&d=2610091802'],
  ])('%s resolves as the map does (%s) and says it isn\'t indexed: no answer for another scan', (path, scan, href) => {
    const client = seeded()
    try {
      const html = render(client, path)
      // The map's resolution of the same `?d=` (`useScan`: the newest store scan the slug names).
      const d = selOf(new URLSearchParams(path.split('?')[1]))?.d
      expect(d ? scanMatches(d, STORE_SCANS)[0] : STORE_SCANS[0]).toBe(scan)
      const latest = fmtScan('2026-10-09T1802')
      expect(notIndexed(html)).toEqual([[
        `Scan ${fmtScan(scan)} is not in the name index yet (it is indexed after each scan lands). This is not a zero-match result. Latest indexed: ${latest}`,
        [[href, latest]],
      ]])
      expect(misses(html)).toEqual([])
      expect(tableRows(html)).toEqual([])
      expect(roots).toEqual([])
    } finally { client.clear() }
  })
  it('a day the index holds answers for that day\'s latest store scan, the one the map shows', () => {
    const client = seeded()
    try {
      const html = render(client, '/names?d=261009&name=datakit')
      expect([notIndexed(html), tableRows(html)[0]]).toEqual([[], ['All buckets', '18', '6']])
    } finally { client.clear() }
  })
  it('a slug naming no store scan is the miss (404 state), not "not indexed"', () => {
    const client = seeded()
    try {
      const html = render(client, '/names?d=261012&name=datakit')
      expect([notIndexed(html), misses(html)]).toEqual([[], [[`No scan matches 261012. Nearest: ← ${fmtScan('2026-10-10T1236')}`, [['/names?d=2610101236&name=datakit', `← ${fmtScan('2026-10-10T1236')}`]]]]])
    } finally { client.clear() }
  })
})

// "now" for the slugs /names writes: 10/9's hours 06 and 12 have ended, the day hasn't
const SLUG_NOW = new Date(Date.UTC(2026, 9, 9, 20))

it('a /names search writes the canonical ?d= (latest floats, as on the map)', () => {
  const write = (qs: string) => nameUrlParams(new URLSearchParams(qs), SUB_DAILY_SCANS, SLUG_NOW).toString()
  expect([
    write('name=gof&date=2026-10-09T1802'),
    write('name=gof&date=2026-10-09T0601'),
    write('name=gof&date=2026-10-09T1802&from=2026-10-09T0601'),
    write('name=gof&date=2026-10-08&from='),
  ]).toEqual(['name=gof', 'd=26100906&name=gof', 'd=-26100906&name=gof', 'd=261008&name=gof'])
})

it('every registry scan, date-only or timed on the same day, round-trips through a /names search URL to itself', () => {
  const DAY2 = ['2026-10-08', '2026-10-09', '2026-10-09T1236']
  const pick = (date: string, from = '') => {
    const url = nameUrlParams(new URLSearchParams({ name: 'gof', date, from }), DAY2, SLUG_NOW)
    return [url.toString(), nameRequest(nameScanParams(url, selOf(url), DAY2), DAY2)]
  }
  expect([pick('2026-10-08'), pick('2026-10-09'), pick('2026-10-09T1236'), pick('2026-10-09T1236', '2026-10-09')]).toEqual([
    ['d=261008&name=gof', { date: '2026-10-08', name: 'gof' }],
    ['d=2610090000&name=gof', { date: '2026-10-09', name: 'gof' }],
    ['name=gof', { date: '2026-10-09T1236', name: 'gof' }],
    ['d=-2610090000&name=gof', { date: '2026-10-09T1236', name: 'gof', from: '2026-10-09' }],
  ])
})

describe('NamePlanStatus: a dir-only (v1) scan', () => {
  it('flags that side\'s totals as folder-name matches only, in words; an unflagged side says nothing more', () => {
    const view = (date: string) => ({ target: 'static_names', date, pattern: 'bkt', root: { b: 1, o: 1 }, buckets: [] })
    const execution = (dirs: boolean) => ({ plan: 'bounded-name-postings' as const, source: 'S', validation: { description: 'D' }, source_identity: { kind: 'static-names-v1' as const }, ...(dirs ? { dirs_only: true as const } : {}) })
    const result = { before: view('2026-08-01'), after: view('2026-10-01'), delta: { b: 0, o: 0 }, execution: { before: execution(true), after: execution(false) } } as NameResult
    const html = renderToStaticMarkup(createElement(NamePlanStatus, { result }))
    expect([...html.matchAll(/<p class="hot-note">(.*?)<\/p>/g)].map(([, p]) => p.replace(/<[^>]+>/g, ''))).toEqual([
      `2026-08-01: Bounded name postings (on demand). S — D ${NAME_DIRS_ONLY}`,
      '2026-10-01: Bounded name postings (on demand). S — D',
    ])
  })
})
