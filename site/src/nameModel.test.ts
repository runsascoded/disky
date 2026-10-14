import { afterEach, describe, expect, it, vi } from 'vitest'
import { loadName, nameLiteral, namePageParams, nameRequest, parseName } from './nameModel'
import { nameDiff, nameFixture } from './nameTestFixtures'

const request = { date: '2026-10-05', name: 'datakit' }
const view = (body: ReturnType<typeof nameFixture>) => ({ target: 'fixture', date: body.date, pattern: 'datakit', root: body.root, buckets: body.buckets })
const execution = (body: ReturnType<typeof nameFixture>) => ({ plan: body.plan, source: body.source, validation: body.validation, source_identity: body.source_identity })
afterEach(() => vi.unstubAllGlobals())
describe('unified exact schemas and truthful plans', () => {
  it.each(['catalog', 'bounded-name-postings'] as const)('parses complete %s body without inventing a catalog schema or artifact identity', plan => {
    const body = nameFixture(plan)
    expect(parseName(body, request)).toEqual({ after: view(body), execution: { after: execution(body) } })
  })
  it.each([['catalog', 'catalog'], ['catalog', 'bounded-name-postings'], ['bounded-name-postings', 'catalog'], ['bounded-name-postings', 'bounded-name-postings']] as const)
    ('validates complete %s/%s comparisons', (before, after) => {
      const body = nameDiff(before, after), r = { ...request, from: '2026-10-04' }, result = parseName(body, r)
      expect(result).toEqual({ before: view(body.before), after: view(body.after), delta: { b: -6, o: -2 }, execution: { before: execution(body.before), after: execution(body.after) } })
    })
  it.each(['unknown plan', 'missing proof', 'false proof', 'source oracle claim', 'empty source', 'empty description', 'cold catalog metadata', 'invalid identity'])('refuses %s execution metadata', issue => {
    const body = nameFixture()
    if (issue === 'unknown plan') Object.assign(body, { plan: 'full-fleet-scan' })
    if (issue === 'missing proof') Object.assign(body, { validation: {} })
    if (issue === 'false proof') body.validation.source_prefix_proofs_checked = false
    if (issue === 'source oracle claim') body.validation.independent_query_source_oracle = true
    if (issue === 'empty source') body.source = ''
    if (issue === 'empty description') body.validation.description = ''
    if (issue === 'cold catalog metadata') Object.assign(body.validation, { catalog: {} })
    if (issue === 'invalid identity') body.source_identity.history_manifest_sha256 = 'bad'
    expect(() => parseName(body, request)).toThrow('Name summary returned invalid execution metadata.')
  })
  it('rejects comparisons across different source generations', () => {
    const body = nameDiff(); body.before.source_identity.generation = 'other'
    expect(() => parseName(body, { ...request, from: '2026-10-04' })).toThrow('Name summary comparison has inconsistent frozen source identities.')
  })
  it.each(['foreign schema', 'partial buckets', 'wrong root', 'wrong literal', 'rounded integer', 'wrong delta', 'wrong geometry'])('rejects %s rather than returning partial totals', issue => {
    const body = nameDiff()
    if (issue === 'foreign schema') body.schema = 'unknown-diff-v1'
    if (issue === 'partial buckets') body.after.buckets.pop()
    if (issue === 'wrong root') body.after.root.o++
    if (issue === 'wrong literal') body.before.pattern = '.json'
    if (issue === 'rounded integer') body.after.root.b = 9007199254740992
    if (issue === 'wrong delta') body.buckets[0].delta.b = -5
    if (issue === 'wrong geometry') body.buckets[0].post++
    expect(() => parseName(body, { ...request, from: '2026-10-04' })).toThrow(Error)
  })
})
describe('requests and explicit failures', () => {
  it('defaults to datakit without rewriting explicit names or baseline controls', () => {
    expect(namePageParams(new URLSearchParams()).toString()).toBe('name=datakit')
    expect(nameRequest(new URLSearchParams())).toEqual(request)
    expect(nameRequest(new URLSearchParams('name=.JSON&date=2026-10-05&from=2026-10-04'))).toEqual({ date: request.date, name: '.json', from: '2026-10-04' })
  })
  it('fetches exactly the unified endpoint and preserves metadata', async () => {
    const body = nameFixture(), fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(body)))
    vi.stubGlobal('fetch', fetcher)
    expect(await loadName({ ...request, name: 'DATAKIT' })).toEqual({ after: view(body), execution: { after: execution(body) } })
    expect(fetcher.mock.calls).toEqual([['/api/name-summary?date=2026-10-05&name=datakit', { signal: undefined }]])
  })
  it('invalid dates fail before any request', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
    await expect(loadName({ ...request, date: '2026-10-06' })).rejects.toEqual(new Error('Only the frozen 2026-10-04 and 2026-10-05 scans are available.'))
    expect(fetcher.mock.calls).toEqual([])
  })
  it.each([[400, 'Invalid name-summary request. Check the literal and frozen scan dates; this is not a zero-match result.'], [503, 'Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.'], [401, 'Sign in to view name summaries.']] as const)('status %s never becomes an empty result or scan fallback', async (status, message) => {
    const fetcher = vi.fn().mockResolvedValue(new Response('private backend details', { status })); vi.stubGlobal('fetch', fetcher)
    await expect(loadName(request)).rejects.toEqual(new Error(message))
    expect(fetcher.mock.calls.length).toBe(1)
  })
})
describe('`nameLiteral`: an entry under the map term’s escape rules', () => {
  it.each([
    ['foo', { anchored: false, text: 'foo' }],
    ['foo\\$', { anchored: false, text: 'foo$' }],
    ['\\^foo', { anchored: false, text: '^foo' }],
    ['\\^foo\\$', { anchored: false, text: '^foo$' }],
    ['"^foo"', { anchored: false, text: '^foo' }],
    ['"foo$"', { anchored: false, text: 'foo$' }],
    ['"^foo', { anchored: false, text: '^foo' }],
    ['a\\\\b', { anchored: false, text: 'a\\b' }],
    ['a\\b', { anchored: false, text: 'a\\b' }],
    ['foo\\', { anchored: false, text: 'foo\\' }],
    ['a^b$c', { anchored: false, text: 'a^b$c' }],
    ['^', { anchored: false, text: '^' }],
    ['$', { anchored: false, text: '$' }],
    ['^$', { anchored: false, text: '^$' }],
    ['^foo', { anchored: true }],
    ['foo$', { anchored: true }],
    ['^foo$', { anchored: true }],
    ['^foo\\$', { anchored: true }],
    ['foo\\\\$', { anchored: true }],
  ])('%s', (raw, want) => {
    expect(nameLiteral(raw)).toEqual(want)
  })
})
describe('/names with an escaped `^`/`$`', () => {
  it('sends the entry as written; the answer’s pattern is the literal', async () => {
    const body = nameFixture('bounded-name-postings', '2026-10-05', 'foo$'), fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify(body)))
    vi.stubGlobal('fetch', fetcher)
    expect((await loadName({ ...request, name: 'FOO\\$' })).after.pattern).toBe('foo$')
    expect(fetcher.mock.calls).toEqual([['/api/name-summary?date=2026-10-05&name=foo%5C%24', { signal: undefined }]])
  })
  it('a bare anchor is refused before any request', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher)
    const msg = 'Starts-with (^) and ends-with ($) search isn’t indexed on this deployment yet; search for a plain substring instead.'
    await expect(loadName({ ...request, name: '^foo' })).rejects.toEqual(new Error(msg))
    await expect(loadName({ ...request, name: 'foo$' })).rejects.toEqual(new Error(msg))
    expect(fetcher.mock.calls).toEqual([])
  })
})
