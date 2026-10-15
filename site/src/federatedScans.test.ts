import { describe, expect, it } from 'vitest'
import {
  federatedDeltas, federatedEvents, federatedViewKey, latestObserved, parseFederatedPin, parseSourceCatalog,
  pinFederation, resolveFederation, restoreFederation, utcInstant,
  type SourceCatalog, type SourceScan,
} from './federatedScans'

const t = (hour: number) => `2026-10-13T${String(hour).padStart(2, '0')}:00:00.000Z`
const digest = (n: number) => String(n).padStart(64, '0')
function scan(hour: number, bytes: number, extra: Partial<SourceScan> = {}): SourceScan {
  return {
    id: `scan-${hour}`, revision: '1', digest: digest(hour), complete: true,
    observed_from: t(hour - 1), observed_through: t(hour), published_at: t(hour + 1),
    total_bytes: bytes, total_objects: bytes / 10,
    artifacts: { format: 'path-store-v2', path: `scans/${hour}/path.parquet` },
    ...extra,
  }
}
function catalog(source: string, scans: SourceScan[]): SourceCatalog {
  return parseSourceCatalog({
    version: 1, source, label: source, mount: source,
    scope: { store: `r2:rac/${source}`, prefix: '' }, scans,
  })
}
const refs = (view: ReturnType<typeof resolveFederation>) => view.sources.map(s => ({
  source: s.source, state: s.state, scan: s.reference,
}))

describe('project scan catalog', () => {
  it('normalizes capture timestamps and preserves exact artifacts and totals', () => {
    const input = {
      version: 1, source: 'awair', label: 'Awair', mount: 'awair',
      scope: { store: 'r2:rac/awair', prefix: 'pyramid/' },
      scans: [scan(8, 100, { observed_through: '2026-10-13T08:00:00Z',
        artifacts: { format: 'path-store-v2', path: 'scans/8/path.parquet', bysize: 'scans/8/bysize.parquet', mtime: 'scans/8/mtime.json' } })],
    }
    expect(parseSourceCatalog(input)).toEqual({
      ...input, scans: [{ ...input.scans[0], observed_through: t(8) }],
    })
  })
  it.each([
    ['2026-10-13', 'expected a UTC ISO timestamp'],
    ['2026-10-13T08:00:00-04:00', 'expected a UTC ISO timestamp'],
    ['2026-02-30T08:00:00Z', 'invalid UTC timestamp'],
    ['2026-10-13T24:00:00Z', 'invalid UTC timestamp'],
  ])('rejects ambiguous or nonexistent instants: %s', (value, error) => {
    expect(() => utcInstant(value)).toThrow(error)
  })
  it.each([
    [{ complete: false }, 'catalog advertises an incomplete scan'],
    [{ observed_from: t(10) }, 'invalid capture/publication ordering'],
    [{ published_at: t(7) }, 'invalid capture/publication ordering'],
    [{ total_bytes: -1 }, 'expected a nonnegative safe integer'],
    [{ total_objects: 0.5 }, 'expected a nonnegative safe integer'],
    [{ digest: 'not-a-hash' }, 'expected a SHA-256 content digest'],
    [{ artifacts: { format: 'path-store-v2', path: '../private.parquet' } }, 'expected a relative artifact key or scope prefix'],
    [{ artifacts: { format: 'path-store-v2', path: 'https://other.example/key' } }, 'expected a relative artifact key or scope prefix'],
    [{ artifacts: { format: 'path-store-v2', path: 'scans/%2e%2e/key' } }, 'expected a relative artifact key or scope prefix'],
  ])('rejects malformed/pending publications: %j', (extra, error) => {
    const c = catalog('awair', [scan(8, 100)])
    expect(() => parseSourceCatalog({ ...c, scans: [{ ...c.scans[0], ...extra }] })).toThrow(error)
  })
  it('an optional icon: a URL or a site path, kept as given; anything else refused', () => {
    const base = { version: 1, source: 'awair', label: 'Awair', mount: 'awair', scope: { store: 'r2:rac/awair', prefix: '' }, scans: [] }
    const icon = (v: unknown) => { try { return parseSourceCatalog({ ...base, icon: v }).icon } catch (e) { return (e as Error).message } }
    expect([icon('/icons/awair.svg'), icon('https://example.com/a.png'), icon('icons/a.svg'), icon('javascript:x'), icon('//x.test/a.png'), parseSourceCatalog(base).icon]).toEqual([
      '/icons/awair.svg', 'https://example.com/a.png',
      'expected an icon URL or site path', 'expected an icon URL or site path', 'expected an icon URL or site path',
      undefined,
    ])
  })
  it('rejects duplicate scan revisions rather than depending on catalog order', () => {
    expect(() => catalog('awair', [scan(8, 100), scan(8, 200)])).toThrow('duplicate scan revision')
  })
  it('requires scope directory boundaries so foo/ cannot overlap silently with foo-bar/', () => {
    const c = catalog('awair', [])
    expect(() => parseSourceCatalog({ ...c, scope: { ...c.scope, prefix: 'foo' } }))
      .toThrow('nonempty scope prefix must end with /')
    const foo = { ...c, scope: { ...c.scope, prefix: 'foo/' } }
    const other = { ...catalog('other', []), scope: { ...c.scope, prefix: 'foo-bar/' } }
    expect(refs(resolveFederation([foo, other], t(10)))).toEqual([
      { source: 'awair', state: 'unknown', scan: null },
      { source: 'other', state: 'unknown', scan: null },
    ])
  })
})

describe('one cutoff selects several independent captures', () => {
  const sources = [
    catalog('ctbk', [scan(12, 120), scan(6, 60)]),
    catalog('awair', [scan(8, 80)]),
    catalog('crashes', [scan(11, 110)]),
    catalog('jc-taxes', [scan(5, 0)]),
  ]
  it('selects latest captures <= cutoff, never a future scan; known zero is ready', () => {
    expect(refs(resolveFederation(sources, t(10)))).toEqual([
      { source: 'awair', state: 'ready', scan: { id: 'scan-8', revision: '1', digest: digest(8) } },
      { source: 'crashes', state: 'unknown', scan: null },
      { source: 'ctbk', state: 'ready', scan: { id: 'scan-6', revision: '1', digest: digest(6) } },
      { source: 'jc-taxes', state: 'ready', scan: { id: 'scan-5', revision: '1', digest: digest(5) } },
    ])
    expect(resolveFederation(sources, t(10)).sources.map(s => s.scan?.total_bytes ?? null))
      .toEqual([80, null, 60, 0])
  })
  it('uses capture end rather than a misleading scan ID or start', () => {
    const c = catalog('awair', [scan(8, 80, { id: '2099-01-01', observed_from: t(2) })])
    expect(refs(resolveFederation([c], t(7)))).toEqual([{ source: 'awair', state: 'unknown', scan: null }])
    expect(refs(resolveFederation([c], t(8)))).toEqual([
      { source: 'awair', state: 'ready', scan: { id: '2099-01-01', revision: '1', digest: digest(8) } },
    ])
  })
  it('accepts later-published backfills and selects same-time corrections deterministically', () => {
    const old = scan(8, 80)
    const correction = scan(8, 90, { revision: '2', digest: digest(9), published_at: t(14) })
    const a = catalog('awair', [old, correction]), b = catalog('awair', [correction, old])
    expect([refs(resolveFederation([a], t(10))), refs(resolveFederation([b], t(10)))]).toEqual([
      [{ source: 'awair', state: 'ready', scan: { id: 'scan-8', revision: '2', digest: digest(9) } }],
      [{ source: 'awair', state: 'ready', scan: { id: 'scan-8', revision: '2', digest: digest(9) } }],
    ])
  })
  it('deduplicates simultaneous events and source corrections for picker/history', () => {
    const c = catalog('awair', [scan(8, 80), scan(8, 90, { revision: '2', published_at: t(14) })])
    expect(federatedEvents([...sources.filter(s => s.source !== 'awair'), c, catalog('other', [scan(8, 80)])])).toEqual([
      { at: t(5), sources: ['jc-taxes'] },
      { at: t(6), sources: ['ctbk'] },
      { at: t(8), sources: ['awair', 'other'] },
      { at: t(11), sources: ['crashes'] },
      { at: t(12), sources: ['ctbk'] },
    ])
  })
  it('rejects duplicate mounts and overlapping object scopes before summing', () => {
    expect(() => resolveFederation([sources[0], sources[0]], t(10))).toThrow('duplicate source or mount')
    const awair = catalog('awair', [scan(8, 80)])
    const overlap = { ...catalog('other', [scan(8, 80)]), scope: { store: awair.scope.store, prefix: 'pyramid/' } }
    expect(() => resolveFederation([awair, overlap], t(10))).toThrow('overlapping source scopes')
    expect(() => resolveFederation([awair, { ...overlap, mount: 'awair' }], t(10))).toThrow('duplicate source or mount')
  })
})

describe('latestObserved: the selection rule over bare observations', () => {
  const obs = (id: string, hour: number, published = hour, revision = '') => ({ id, revision, observed_through: t(hour), published_at: t(published) })
  it('the greatest observation through the cutoff; null before the first, never a later one', () => {
    const scans = [obs('b', 8), obs('a', 6), obs('c', 12)]
    expect([5, 6, 7, 8, 11, 12, 13].map(h => latestObserved(scans, t(h))?.id ?? null)).toEqual([null, 'a', 'a', 'b', 'b', 'c', 'c'])
  })
  it('ties on capture end break by publication, then id, then revision — independent of input order', () => {
    const scans = [obs('x', 8, 9), obs('y', 8, 9), obs('x', 8, 10, '1'), obs('x', 8, 10, '2')]
    expect([latestObserved(scans, t(8)), latestObserved([...scans].reverse(), t(8))]).toEqual([obs('x', 8, 10, '2'), obs('x', 8, 10, '2')])
    expect(latestObserved([obs('x', 8, 9), obs('y', 8, 9)], t(8))).toEqual(obs('y', 8, 9))
  })
})

describe('pins and comparison coverage', () => {
  const a = catalog('awair', [scan(8, 80)])
  it('pins exact content and pinned unknowns despite later backfills/corrections', () => {
    const before = resolveFederation([a, catalog('crashes', [])], t(10))
    const pin = pinFederation(before)
    const changed = catalog('awair', [...a.scans, scan(8, 90, { revision: '2', digest: digest(9), published_at: t(14) })])
    expect(restoreFederation(pin, [changed, catalog('crashes', [scan(7, 70)])])).toEqual(before)
    expect(pin).toEqual({
      version: 1, at: t(10), sources: [
        { source: 'awair', mount: 'awair', scope: { store: 'r2:rac/awair', prefix: '' }, reference: { id: 'scan-8', revision: '1', digest: digest(8) } },
        { source: 'crashes', mount: 'crashes', scope: { store: 'r2:rac/crashes', prefix: '' }, reference: null },
      ],
    })
  })
  it('reports withdrawn/missing pinned scans unavailable, not another capture', () => {
    const pin = pinFederation(resolveFederation([a], t(10)))
    const newer = catalog('awair', [scan(9, 90)])
    expect([refs(restoreFederation(pin, [newer])), refs(restoreFederation(pin, []))]).toEqual([
      [{ source: 'awair', state: 'unavailable', scan: { id: 'scan-8', revision: '1', digest: digest(8) } }],
      [{ source: 'awair', state: 'unavailable', scan: { id: 'scan-8', revision: '1', digest: digest(8) } }],
    ])
  })
  it('validates persisted pins and rejects duplicate sources before restoring', () => {
    const pin = pinFederation(resolveFederation([a], t(10)))
    expect(parseFederatedPin(JSON.parse(JSON.stringify(pin)))).toEqual(pin)
    expect(() => restoreFederation({ ...pin, sources: [...pin.sources, ...pin.sources] }, [a]))
      .toThrow('duplicate source or mount')
    expect(() => parseFederatedPin({ ...pin, sources: [{ ...pin.sources[0], reference: { id: 'x', revision: '1', digest: 'bad' } }] }))
      .toThrow('expected a SHA-256 content digest')
  })
  it('keys source order stably and changes keys when the cutoff or content changes', () => {
    const c = catalog('ctbk', [scan(6, 60)])
    const view = resolveFederation([a, c], t(10))
    expect(federatedViewKey(view)).toBe(JSON.stringify({
      version: 1, at: t(10), sources: [
        { source: 'awair', mount: 'awair', scope: { store: 'r2:rac/awair', prefix: '' }, reference: { id: 'scan-8', revision: '1', digest: digest(8) } },
        { source: 'ctbk', mount: 'ctbk', scope: { store: 'r2:rac/ctbk', prefix: '' }, reference: { id: 'scan-6', revision: '1', digest: digest(6) } },
      ],
    }))
    expect(federatedViewKey(resolveFederation([c, a], t(10)))).toBe(federatedViewKey(view))
    expect(federatedViewKey(resolveFederation([a, c], t(11))) === federatedViewKey(view)).toBe(false)
  })
  it('separates observed deltas from new/missing source coverage', () => {
    const sources = [a, catalog('ctbk', [scan(6, 60), scan(12, 120)]), catalog('crashes', [scan(11, 110)])]
    const before = resolveFederation(sources, t(10))
    const after = resolveFederation([...sources, catalog('jc-taxes', [scan(5, 0)])], t(13))
    expect(federatedDeltas(before, after)).toEqual([
      { source: 'awair', kind: 'unchanged', bytes: 0, objects: 0 },
      { source: 'crashes', kind: 'coverage', bytes: null, objects: null },
      { source: 'ctbk', kind: 'changed', bytes: 60, objects: 6 },
      { source: 'jc-taxes', kind: 'coverage', bytes: null, objects: null },
    ])
  })
  it('scope changes are coverage changes, not storage growth', () => {
    const before = resolveFederation([a], t(10))
    const after = resolveFederation([{ ...a, scope: { ...a.scope, prefix: 'pyramid/' } }], t(10))
    expect(federatedDeltas(before, after)).toEqual([{ source: 'awair', kind: 'coverage', bytes: null, objects: null }])
  })
  it('unreadable content cannot masquerade as a comparable complete scan', () => {
    const before = resolveFederation([a], t(10))
    const after = { ...before, sources: before.sources.map(s => ({ ...s, state: 'unavailable' as const })) }
    expect(federatedDeltas(before, after)).toEqual([{ source: 'awair', kind: 'coverage', bytes: null, objects: null }])
  })
})
