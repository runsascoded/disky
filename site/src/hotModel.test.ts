import { describe, expect, it } from 'vitest'
import { HOT_SCOPE, exactInteger, hotRequest, parseRootSummary, type HotRequest } from './hotModel'

const SCHEMAS = [{ view: 'fixture-v1', diff: 'fixture-diff-v1' }, { view: 'fixture-batch-v1', diff: 'fixture-batch-diff-v1' }]
const parseHot = (value: unknown, request: HotRequest) => parseRootSummary(value, request, SCHEMAS)

const request = { date: '2026-10-05', name: '.json' }
function fixture(date = request.date, batch = true) {
  return { schema: batch ? 'fixture-batch-v1' : 'fixture-v1', target: 'fixture_union', date, pattern: '.json', path: '',
    exact: true, incremental: false, levels: 1, scope: HOT_SCOPE,
    root: { b: 12, o: 9 }, buckets: [
      { path: 'bucket-a', pre: 1, post: 4, b: 7, o: 2 },
      { path: 'bucket-b', pre: 5, post: 8, b: 5, o: 3 },
      { path: 'bucket-c', pre: 9, post: 12, b: 0, o: 4 },
      { path: 'bucket-d', pre: 13, post: 16, b: 0, o: 0 },
      { path: 'bucket-e', pre: 17, post: 20, b: 0, o: 0 },
      { path: 'bucket-f', pre: 21, post: 24, b: 0, o: 0 },
    ] }
}
function diff(batch = true) {
  const before = fixture('2026-10-04', batch), after = fixture('2026-10-05', batch)
  after.buckets[0].b = 3
  after.buckets[0].o = 3
  after.buckets[1].b = 9
  after.buckets[2].o = 6
  after.root.o = 12
  return { schema: batch ? 'fixture-batch-diff-v1' : 'fixture-diff-v1', target: 'fixture_union', pattern: '.json', path: '',
    exact: true, incremental: false, levels: 1, scope: HOT_SCOPE, before, after, delta: { b: 0, o: 3 },
    buckets: before.buckets.map((row, i) => ({ path: row.path, pre: row.pre, post: row.post,
      before: { b: row.b, o: row.o }, after: { b: after.buckets[i].b, o: after.buckets[i].o },
      delta: { b: after.buckets[i].b - row.b, o: after.buckets[i].o - row.o } })) }
}

describe('frozen root summary model', () => {
  it.each([true, false])('accepts supported catalog family batch=%s and preserves zero-byte counts', batch => {
    const body = fixture(request.date, batch)
    expect(parseHot(body, request)).toEqual({ after: {
      target: 'fixture_union', date: '2026-10-05', pattern: '.json', root: { b: 12, o: 9 }, buckets: [
        { path: 'bucket-a', pre: 1, post: 4, b: 7, o: 2 }, { path: 'bucket-b', pre: 5, post: 8, b: 5, o: 3 },
        { path: 'bucket-c', pre: 9, post: 12, b: 0, o: 4 }, { path: 'bucket-d', pre: 13, post: 16, b: 0, o: 0 },
        { path: 'bucket-e', pre: 17, post: 20, b: 0, o: 0 }, { path: 'bucket-f', pre: 21, post: 24, b: 0, o: 0 },
      ],
    } })
  })
  it.each([true, false])('keeps offsetting and zero-byte object changes in batch=%s diffs', batch => {
    const result = parseHot(diff(batch), { ...request, from: '2026-10-04' })
    expect({ before: result.before?.root, after: result.after.root, delta: result.delta,
      buckets: result.after.buckets.map((row, i) => [row.path, row.b - result.before!.buckets[i].b, row.o - result.before!.buckets[i].o]) })
      .toEqual({ before: { b: 12, o: 9 }, after: { b: 12, o: 12 }, delta: { b: 0, o: 3 }, buckets: [
        ['bucket-a', -4, 1], ['bucket-b', 4, 0], ['bucket-c', 0, 2], ['bucket-d', 0, 0], ['bucket-e', 0, 0], ['bucket-f', 0, 0],
      ] })
  })
  it('represents a registered zero result rather than an unavailable query', () => {
    const body = fixture()
    body.root = { b: 0, o: 0 }
    body.buckets.forEach(row => { row.b = 0; row.o = 0 })
    const result = parseHot(body, request)
    expect([result.after.root, result.after.buckets.map(row => [row.b, row.o])])
      .toEqual([{ b: 0, o: 0 }, [[0, 0], [0, 0], [0, 0], [0, 0], [0, 0], [0, 0]]])
  })
  it.each([
    ['schema', 'unknown-v1'], ['exact', false], ['incremental', true], ['levels', 2], ['path', 'bucket-a'],
    ['scope', 'full path'], ['target', 'invalid-target'],
  ])('refuses an unsupported %s', (key, value) => {
    expect(() => parseHot({ ...fixture(), [key]: value }, request)).toThrowError(new Error(
      key === 'schema' ? 'Preview returned an unsupported response schema.' : 'Preview returned an unsupported root-summary contract.',
    ))
  })
  it.each([2 ** 53, -1, .5, NaN, Infinity, '12', null, true])('refuses unsupported scalar %s', value => {
    expect(() => parseHot({ ...fixture(), root: { b: value, o: 9 } }, request))
      .toThrowError(new Error('Preview returned an unsupported integer total.'))
  })
  it('refuses unsafe intermediate sums even when individual scalars are safe', () => {
    const body = fixture()
    body.root.b = Number.MAX_SAFE_INTEGER
    body.buckets[0].b = Number.MAX_SAFE_INTEGER
    body.buckets[1].b = 1
    expect(() => parseHot(body, request)).toThrowError(new Error('Preview returned an unsupported integer total.'))
  })
  it.each(['b', 'o'] as const)('checks exact %s conservation', field => {
    const body = fixture()
    body.buckets[0][field] += 1
    expect(() => parseHot(body, request)).toThrowError(new Error('Preview bucket totals do not equal the fleet total.'))
  })
  it('requires all six buckets', () => {
    const body = fixture()
    body.buckets.pop()
    expect(() => parseHot(body, request)).toThrowError(new Error('Preview must return all six buckets.'))
  })
  it.each(['duplicate', 'gap', 'overlap', 'inverted', 'missing-root'])('rejects %s bucket geometry', kind => {
    const body = fixture()
    if (kind === 'duplicate') body.buckets[1].path = 'bucket-a'
    if (kind === 'gap') body.buckets[1].pre = 6
    if (kind === 'overlap') body.buckets[1].pre = 4
    if (kind === 'inverted') body.buckets[1].post = 3
    if (kind === 'missing-root') body.buckets[0].pre = 0
    expect(() => parseHot(body, request)).toThrowError(new Error('Preview buckets do not form a complete disjoint partition.'))
  })
  it.each([{ date: '2026-10-04' }, { pattern: '.npy' }])('checks request echo %s', override => {
    expect(() => parseHot({ ...fixture(), ...override }, request)).toThrowError(new Error('Preview does not match the requested scan and literal.'))
  })
  it.each(['top', 'before', 'after'])('checks comparison %s scope', side => {
    const body = diff()
    if (side === 'top') body.pattern = '.npy'
    else body[side as 'before' | 'after'].pattern = '.npy'
    expect(() => parseHot(body, { ...request, from: '2026-10-04' })).toThrowError(new Error('Preview comparison does not match the requested scope.'))
  })
  it('does not accept mixed catalog schema families', () => {
    const body = diff()
    body.before.schema = 'fixture-v1'
    expect(() => parseHot(body, { ...request, from: '2026-10-04' })).toThrowError(new Error('Preview comparison does not match the requested scope.'))
  })
  it('checks the signed root change', () => {
    const body = diff()
    body.delta.b = 1
    expect(() => parseHot(body, { ...request, from: '2026-10-04' })).toThrowError(new Error('Preview comparison totals are inconsistent.'))
  })
  it.each(['identity', 'before', 'after', 'delta'])('checks comparison bucket %s', change => {
    const body = diff()
    if (change === 'identity') body.buckets[0].pre = 2
    else body.buckets[0][change as 'before' | 'after' | 'delta'].b += 1
    expect(() => parseHot(body, { ...request, from: '2026-10-04' })).toThrowError(new Error('Preview comparison bucket identities or totals are inconsistent.'))
  })
  it('formats exact locale integers with explicit signed changes', () => {
    expect([exactInteger(1_234_567), exactInteger(4, true), exactInteger(-4, true), exactInteger(0, true)])
      .toEqual([(1_234_567).toLocaleString(), '+4', '-4', '0'])
  })
})

describe('frozen query parameters', () => {
  it('defaults to the latest frozen scan and .json', () => {
    expect(hotRequest(new URLSearchParams())).toEqual({ date: '2026-10-05', name: '.json' })
  })
  it('normalizes literals without interpreting wildcard or Boolean punctuation', () => {
    expect(hotRequest(new URLSearchParams({ date: '2026-10-05', name: 'Ab%_* AND', from: '2026-10-04' })))
      .toEqual({ date: '2026-10-05', name: 'ab%_* and', from: '2026-10-04' })
  })
  it.each(['date=2026-10-06', 'from=', 'from=2026-10-03'])('refuses unavailable dates %s', query => {
    expect(() => hotRequest(new URLSearchParams(query))).toThrowError(new Error('Only the frozen 2026-10-04 and 2026-10-05 scans are available.'))
  })
  it.each(['date=2026-10-04&from=2026-10-04', 'date=2026-10-04&from=2026-10-05', 'from=2026-10-05'])('requires a strictly earlier comparison %s', query => {
    expect(() => hotRequest(new URLSearchParams(query))).toThrowError(new Error('Baseline scan must precede the selected scan.'))
  })
  it.each(['name=', 'name=a%2Fb', 'name=a%00b', `name=${'x'.repeat(513)}`])('refuses invalid literals', query => {
    expect(() => hotRequest(new URLSearchParams(query))).toThrowError(new Error('Enter one nonempty name literal without slashes (at most 512 characters).'))
  })
  it.each(['name=a&name=b', 'date=2026-10-04&date=2026-10-05', 'path=', 'owner=a', 'store=meta', 'q=foo'])('does not silently ignore unsupported parameters %s', query => {
    expect(() => hotRequest(new URLSearchParams(query))).toThrowError(new Error('Use only one date, name and optional from parameter.'))
  })
})
