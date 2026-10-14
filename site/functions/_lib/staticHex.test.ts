import { beforeAll, describe, expect, it } from 'vitest'
import { parseName } from '../../src/nameModel.js'
import { matchRoots } from './filter.js'
import { type HexRule } from './hexRuns.js'
import { answerKey, staticSummary, type Store } from './nameSummaryStatic.js'
import { compileQuery } from './pathQuery.js'
import type { QueryAst } from './queryAst.js'
import { type Found, hexNote, hexQuery, injectedStores, type StaticFilterStore } from './staticFilter.js'
import { type Blobs, scanAt } from './staticNames.js'
import { AnchoredSource, termKey } from './staticAnchors.js'
import { Drill } from './staticDrill.js'
import { tiers } from './staticRuns.js'
import { fixture } from './testStore.js'

// The static name index under the hex-run rule (specs/static-hex-runs.md) over `fixtures/static-hex/` (`gen.py`): suffix
// rows only where the rule keeps them, a catalog by `occurs`, `hex_runs` recorded in `catalog/meta.json`. The reader,
// the catalog, the `hexRuns` note and the path-store fallback's predicate, against a brute-force oracle under the rule.

const R: HexRule = { min: 16, tail: 8 }
const DATES = ['2026-08-01', '2026-09-01', '2026-10-01']
const BUCKETS = ['bkt-a', 'bkt-b', 'bkt-c']
const ENV = { STORE_BUCKETS: BUCKETS.join(','), STORE: 'cw', STATIC_GEN: '2026-10-10cw' }
/** `gen.py`'s `TERMS`. */
const TERMS = ['cafe', '1234', '5678', '7c1d0e4f', '3f9a', 'data', '.json', 'data.json', 'obj_3f9a', 'bkt', 'txt', 'c', 'ca', '1', 'q0']
/** `gen.py`'s versions `(path, usr, vf, vt, size, n)` (the fallback's view of the scans: every live path). */
const OPEN = '2106-01-01'
const VERSIONS: [string, string, string, string, number, number][] = [
  ['bkt-a', '', '2026-08-01', OPEN, 10_000, 100],
  ['bkt-a/objs', '', '2026-08-01', OPEN, 5_000, 50],
  ['bkt-a/objs/3f9a2b7c1d0e4f5a6b7c8d9e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c', '', '2026-08-01', OPEN, 64, 1],
  ['bkt-a/objs/0123cafe56789abcdef0123456789abc.bin', '', '2026-08-01', OPEN, 32, 1],
  ['bkt-a/objs/0123456789abcdefcafe.bin', '', '2026-09-01', OPEN, 20, 1],
  ['bkt-a/cafe-notes.txt', '', '2026-08-01', '2026-10-01', 7, 1],
  ['bkt-a/logs/run-1234.log', '', '2026-08-01', OPEN, 9, 1],
  ['bkt-b', '', '2026-08-01', OPEN, 20_000, 200],
  ['bkt-b/x/20261009123456789.json', '', '2026-08-01', OPEN, 17, 1],
  ['bkt-b/x/0123456789ABCDEF1234.parquet', '', '2026-09-01', OPEN, 21, 1],
  ['bkt-b/x/obj_3f9a2b7c1d0e4f5a6b7c3f0data.json', '', '2026-08-01', OPEN, 33, 1],
  ['bkt-b/x/data.json', 'alice', '2026-08-01', '2026-09-01', 5, 1],
  ['bkt-b/y/q0123456789abcdeq.txt', '', '2026-08-01', OPEN, 15, 1],
  ['bkt-b/y/q0123456789abcdefq.txt', '', '2026-08-01', OPEN, 16, 1],
  ['bkt-b/y/q0123456789abcdef0q.txt', '', '2026-09-01', OPEN, 18, 1],
  ['bkt-c', '', '2026-08-01', OPEN, 30_000, 300],
  ['bkt-c/deadbeef00cafe0011223344556677', '', '2026-08-01', OPEN, 400, 4],
  ['bkt-c/deadbeef00cafe0011223344556677/cafe.txt', '', '2026-08-01', OPEN, 40, 1],
  ['bkt-c/deadbeef00cafe0011223344556677/1234.bin', 'bob', '2026-08-01', OPEN, 41, 1],
]
const KEYS = ['shards.json', 'scans.json', 'expected.json', 'expected-full.json', 'members.json', 'sx/s0000.parquet', 'sx/s0001.parquet', 'catalog/cells.parquet', 'catalog/index.parquet', 'catalog/meta.json']
type Answers = Record<string, Record<string, Record<string, [number, number]>>>
const held = new Map<string, ArrayBuffer>()
let expected: Answers, full: Answers
beforeAll(async () => {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(path: string): Uint8Array }
  for (const k of KEYS) { const b = fs.readFileSync(fixture(`static-hex/${k}`)); held.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
  const text = (k: string) => JSON.parse(new TextDecoder().decode(held.get(k)))
  expected = text('expected.json')
  full = text('expected-full.json')
})

/** The fixture as a base generation's `Blobs`; `meta` overrides `catalog/meta.json`'s fields. */
function files(meta: Record<string, unknown> = {}): Blobs {
  const bytes = (key: string) => { const b = held.get(key); if (!b) throw new Error(`no fixture ${key}`); return b }
  return {
    async range(key, offset, length) { const b = bytes(key); return b.slice(offset, length == null ? b.byteLength : offset + length) },
    async suffix(key, n) { const b = bytes(key); return { buf: b.slice(Math.max(0, b.byteLength - n)), size: b.byteLength } },
    async json(key) { const v = JSON.parse(new TextDecoder().decode(bytes(key))); return key === 'catalog/meta.json' ? { ...v, ...meta } : v },
  }
}
const store = (blobs: Blobs = files()): Store => {
  const t = tiers(blobs)
  return { names: t.names, catalog: t.catalog, scans: t.scans, clock: async () => Date.now(), hexRuns: () => t.names.hexRuns() }
}
const num = (answers: Record<string, Record<string, [bigint, bigint]>>) =>
  Object.fromEntries(Object.entries(answers).map(([d, t]) => [d, Object.fromEntries(Object.entries(t).filter(([, [b, o]]) => b !== 0n || o !== 0n).map(([b, [x, o]]) => [b, [Number(x), Number(o)]]))]))
const sub = (text: string): QueryAst => ({ alts: [[{ kind: 'sub', text }]], neg: [] })

/** The path-store fallback's answer: `pred` over every path live on `date` (as `view.ts` filters a read), its match
 *  roots (`filter.ts`), each root's live slices summed per bucket. */
function fallback(pred: ReturnType<typeof compileQuery>, date: string): Record<string, [number, number]> {
  const live = VERSIONS.filter(([, , vf, vt]) => vf <= date && date < vt)
  const roots = new Set(matchRoots(new Set(live.map(v => v[0])), pred, ''))
  const out: Record<string, [number, number]> = {}
  for (const [path, , , , size, n] of live) {
    if (!roots.has(path)) continue
    const b = path.split('/')[0], t = (out[b] ??= [0, 0])
    t[0] += size; t[1] += n
  }
  return Object.fromEntries(Object.entries(out).sort(([x], [y]) => x < y ? -1 : 1))
}

describe('the static index under the hex-run rule', () => {
  it('records the rule, and the rule changes exactly the hex-affected terms', async () => {
    expect(await store().hexRuns!()).toEqual(R)
    expect(TERMS.filter(t => JSON.stringify(expected[t]) !== JSON.stringify(full[t]))).toEqual(['cafe', '1234', '5678', '7c1d0e4f', 'c', 'ca', '1'])
    // The spec's examples: a glued word, a hash prefix and the hash's own name still match; a hash interior doesn't.
    expect([expected.data['2026-10-01'], expected['3f9a']['2026-10-01'], expected['7c1d0e4f']['2026-10-01'], full['7c1d0e4f']['2026-10-01']])
      .toEqual([{ 'bkt-b': [33, 1] }, { 'bkt-a': [64, 1], 'bkt-b': [33, 1] }, {}, { 'bkt-a': [64, 1], 'bkt-b': [33, 1] }])
    // A directory whose hash holds `cafe` is no hit, so its child `cafe.txt` is one.
    expect(expected.cafe['2026-10-01']).toEqual({ 'bkt-c': [40, 1] })
  })

  it.each(TERMS)('answers %s (suffix rows or catalog) exactly like the oracle under the rule, on every date', async term => {
    const got = await answerKey(store(), term, DATES)
    expect(num(got.answers)).toEqual(expected[term])
  })

  it('carries `hexRuns` exactly for hex-affected literals, never refusing', async () => {
    const ask = async (name: string, from?: string) => {
      const r = await staticSummary(ENV, new URLSearchParams({ date: '2026-10-01', name, ...(from ? { from } : {}) }), store())
      const body = await r.json() as { hexRuns?: HexRule }
      return [r.status, body.hexRuns ?? null]
    }
    expect(await Promise.all(['cafe', '1234', '7c1d0e4f', 'data', '.json', 'bkt', 'c'].map(n => ask(n))))
      .toEqual([[200, R], [200, R], [200, R], [200, null], [200, null], [200, null], [200, R]])
    expect(await ask('cafe', '2026-08-01')).toEqual([200, R])
    // The page's contract keeps it.
    const r = await staticSummary(ENV, new URLSearchParams({ date: '2026-10-01', name: 'cafe' }), store())
    expect(parseName(await r.json(), { date: '2026-10-01', name: 'cafe' }).hexRuns).toEqual(R)
  })

  it('a generation without the rule answers without `hexRuns`', async () => {
    const plain = store(files({ hex_runs: undefined }))
    expect(await plain.hexRuns!()).toBeNull()
    const r = await staticSummary(ENV, new URLSearchParams({ date: '2026-10-01', name: 'cafe' }), plain)
    expect([r.status, (await r.json() as { hexRuns?: unknown }).hexRuns ?? null]).toEqual([200, null])
  })

  it('refuses a bad recorded rule (the tier is unusable, never read under a guess)', async () => {
    const r = await staticSummary(ENV, new URLSearchParams({ date: '2026-10-01', name: 'cafe' }), store(files({ hex_runs: { min: 16 } })))
    expect(r.status).toBe(503)
  })

  it.each(TERMS)('the path-store fallback matches %s as the static index does', term => {
    const pred = compileQuery(sub(term), { hexRuns: R })
    expect(Object.fromEntries(DATES.map(d => [d, fallback(pred, d)]))).toEqual(expected[term])
    // …and without the rule, as the full index.
    expect(Object.fromEntries(DATES.map(d => [d, fallback(compileQuery(sub(term)), d)]))).toEqual(full[term])
  })
})

describe('the hex-run note and the fallback predicate', () => {
  it('notes exactly the hex-affected substrings, on a generation with the rule', () => {
    expect(['cafe', '1234', 'deadbeef0x', 'data', '.json', 'deadbeefx'].map(t => hexNote(R, sub(t)).hexRuns ?? null))
      .toEqual([R, R, R, null, null, null])
    expect(hexNote(null, sub('cafe'))).toEqual({})
    // Any substring of the query: an alternative or an exclusion.
    expect(hexNote(R, { alts: [[{ kind: 'sub', text: 'data' }], [{ kind: 'sub', text: 'bad' }]], neg: [] })).toEqual({ hexRuns: R })
    expect(hexNote(R, { alts: [[{ kind: 'sub', text: 'data' }]], neg: [{ kind: 'sub', text: 'cafe' }] })).toEqual({ hexRuns: R })
    expect(hexNote(R, { alts: [[{ kind: 'glob', pieces: ['ca', 'fe'] }]], neg: [] })).toEqual({})
  })

  it('recompiles a query under the deployment store\'s rule (and leaves it alone without one)', async () => {
    const path = 'bkt-a/objs/0123cafe56789abcdef0123456789abc.bin'
    const env = {}, plainEnv = {}
    injectedStores.set(env, { gen: 'g', scans: async () => DATES, source: { hits: async () => null }, hexRuns: async () => R } as StaticFilterStore)
    injectedStores.set(plainEnv, { gen: 'g', scans: async () => DATES, source: { hits: async () => null } } as StaticFilterStore)
    const q = compileQuery(sub('cafe'))
    const [ruled, plain] = await Promise.all([hexQuery(env, q), hexQuery(plainEnv, q)])
    expect([ruled.hexRuns, ruled.query!(path), ruled.query!('bkt-a/cafe-notes.txt'), plain.hexRuns, plain.query!(path)]).toEqual([R, false, true, null, true])
    expect(plain.query).toBe(q)
  })
})

describe('the drilldown\'s plain view under the rule', () => {
  const nothing: Blobs = {
    range: async () => { throw new Error('no drill files') }, suffix: async () => { throw new Error('no drill files') },
    json: async () => { throw new Error('no drill files') },
  }
  const drill = (rule: HexRule | null) => new Drill(nothing, { lookup: async () => ({ member: null }), info: async () => ({ gen: 'g', bytes: 1, cells_rows: 0, row_groups: 0, membership: { max_rows: 4 }, ...(rule ? { hex_runs: rule } : {}) }) })
  const HASHED = 'bkt-c/deadbeef00cafe0011223344556677'
  it('a view path holding the literal only inside a hash is no plain view (its matches are read); outside one it is', async () => {
    expect(await drill(R).view('cafe', 'bkt-a/cafe-notes.txt')).toEqual({ source: 'plain' })
    // Not plain: it goes on to the tiers' alias maps (this tier has none to give).
    const st = { version: 'base', scans: [], tiers: [{ dir: null, aliases: async () => { throw new Error('no drill files') } }] } as never
    await expect(drill(R).view('cafe', HASHED, st)).rejects.toThrow('no drill files')
    expect(await drill(null).view('cafe', HASHED)).toEqual({ source: 'plain' })
  })
})

describe('anchored search on a generation with the rule', () => {
  /** `q$` literals: proper hex suffixes of trailing runs (dropped), a whole run (kept), and plain endings. */
  const ENDS = ['2b3c', 'a2b3c', '6677', 'e0011223344556677', 'deadbeef00cafe0011223344556677', '.json', '.txt', 'cafe.txt', '1234.bin', 'abc.bin']
  const endAst = (text: string, start = false): QueryAst => ({ alts: [[{ kind: 'sub', text, end: true, ...(start ? { start: true } : {}) }]], neg: [] })
  /** The base generation's fixture as `Blobs`, listing what it holds (no `anchors/meta.json` unless `anchors` is given). */
  const listed = (anchors?: Record<string, unknown>): Blobs => {
    const b = files()
    return {
      ...b,
      async json<T>(key: string): Promise<T> { if (key === 'anchors/meta.json' && anchors) return anchors as T; return b.json<T>(key) },
      async list(prefix) { return [...KEYS, ...(anchors ? ['anchors/meta.json'] : [])].filter(k => k.startsWith(prefix)) },
    }
  }
  const sums = (found: Found | null, date: string): Record<string, [number, number]> => {
    const D = scanAt(date), out: Record<string, [number, number]> = {}
    for (const h of found!.hits!) {
      if (!(h.vf <= D && D < h.vt)) continue
      const t = (out[h.path.split('/')[0]] ??= [0, 0])
      t[0] += Number(h.size); t[1] += Number(h.n)
    }
    return Object.fromEntries(Object.entries(out).sort(([x], [y]) => x < y ? -1 : 1))
  }

  it('`q$` reads the light index under the rule: equal to the rule-aware fallback (brute force over the versions), on every date', async () => {
    const t = tiers(listed())
    const src = new AnchoredSource(t.tiers, listed())
    const got: unknown[] = [], want: unknown[] = []
    for (const text of ENDS) {
      const f = await src.hits(termKey({ text, start: false, end: true }), '')
      for (const d of DATES) {
        got.push([text, d, sums(f, d)])
        want.push([text, d, fallback(compileQuery(endAst(text), { hexRuns: R }), d)])
      }
    }
    expect(got).toEqual(want)
    // The rule changes exactly the proper hex suffixes of a 16+ digit trailing run (the hash, the hashed directory).
    const differs = ENDS.filter(text => DATES.some(d => JSON.stringify(fallback(compileQuery(endAst(text), { hexRuns: R }), d)) !== JSON.stringify(fallback(compileQuery(endAst(text)), d))))
    expect(differs).toEqual(['2b3c', 'a2b3c', '6677', 'e0011223344556677'])
  })

  it('`^q`, `^q$` and a heavy `q$` decline cleanly without an anchors build (cw), and with one built under another rule', async () => {
    const meta = { R: 3, K: 2, rg: 5, idx_rg: 3 }
    for (const anchors of [undefined, meta, { ...meta, hex_runs: { min: 16, tail: 0 } }]) {
      const t = tiers(listed(anchors))
      const src = new AnchoredSource(t.tiers, listed(anchors), { maxRows: 0 })
      const keys = [termKey({ text: 'cafe', start: true, end: false }), termKey({ text: 'cafe.txt', start: true, end: true }), termKey({ text: '.txt', start: false, end: true })]
      expect(await Promise.all(keys.map((k, i) => src.hits(k, ['', 'bkt-c', 'bkt-b'][i])))).toEqual([null, null, null])
      // …named: the term's form isn't indexed here (`anchor-not-indexed`), not the scan.
      expect(keys.map(k => src.why(k))).toEqual(['anchor-not-indexed', 'anchor-not-indexed', 'anchor-not-indexed'])
      // A light `q$` still answers from the light index, and has no reason.
      const light = new AnchoredSource(t.tiers, listed(anchors))
      expect([(await light.hits(keys[2], ''))?.hits?.length, light.why(keys[2])]).toEqual([(await new AnchoredSource(tiers(listed()).tiers, listed()).hits(keys[2], ''))!.hits!.length, undefined])
    }
  })

  it('the fallback reads `^q` / `^q$` as without the rule (a segment\'s start is never inside a run), `q$` by `segmentOccurs`', () => {
    const live = (ast: QueryAst, rule: HexRule | null) => DATES.map(d => fallback(compileQuery(ast, { hexRuns: rule }), d))
    for (const text of ['dead', 'deadbeef00cafe0011223344556677', '0123', 'cafe']) {
      expect(live(endAst(text, true), R)).toEqual(live(endAst(text, true), null))
      expect(live({ alts: [[{ kind: 'sub', text, start: true }]], neg: [] }, R)).toEqual(live({ alts: [[{ kind: 'sub', text, start: true }]], neg: [] }, null))
    }
    // `^q` never notes the rule; `q$` does when hex-affected.
    expect([hexNote(R, { alts: [[{ kind: 'sub', text: 'cafe', start: true }]], neg: [] }), hexNote(R, endAst('cafe')), hexNote(R, endAst('.txt'))])
      .toEqual([{}, { hexRuns: R }, {}])
  })
})
