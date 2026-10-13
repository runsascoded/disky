import { beforeAll, describe, expect, it, vi } from 'vitest'
import { scanTime } from '../../src/scanSlug'
import { AnchoredHits, AnchoredSource, parseKey, segmentMatches, START_MAX_ROWS, termInPath, termKey } from './staticAnchors'
import { DRILL_RULES, rollupAt } from './staticDrill'
import { type Found, SuffixHits, staticLiteral } from './staticFilter'
import { type Blobs, type Hit } from './staticNames'
import { tiers } from './staticRuns'
import { parseAst } from './querySyntax'
import { fixture, readJson } from './testStore'

// Anchored search (`staticAnchors.ts`) over `fixtures/static-anchors/` (`gen.py`): a base generation of four scans and
// two runs, built by `dt_cloud.static_anchors` (R = 3, K = 2; 4-row name groups, 5-row suffix groups, 4-row rollup
// groups, 3-entry index groups). Every key's view at every path on every scan of each stack (the base alone, the
// first run, both) — light, scoped roots and rollups, at three whole-range bounds — equals brute force from the
// versions (`expected.json`).

type Sums = Record<string, [number, number]>
type Expected = { dates: string[]; base: string[]; paths: string[]; keys: string[]; R: number; K: number; views: Record<string, Record<string, Record<string, Sums>>>; start_keys: string[]; start_root: Record<string, Record<string, Sums>> }
let E: Expected
const files = new Map<string, ArrayBuffer>()
const LAST = 'manifests/2026-10-01.json', FIRST = 'manifests/2026-09-15.json'

function blobsOf(opts: { hide?: string[] } = {}): Blobs {
  const has = (k: string) => files.has(k) && !opts.hide?.includes(k)
  const bytes = (k: string) => { if (!has(k)) throw new Error(`no fixture ${k}`); return files.get(k)! }
  return {
    async range(key, offset, length) { const b = bytes(key); return b.slice(offset, length == null ? b.byteLength : offset + length) },
    async suffix(key, n) { const b = bytes(key); return { buf: b.slice(Math.max(0, b.byteLength - n)), size: b.byteLength } },
    async json(key) { return JSON.parse(new TextDecoder().decode(bytes(key))) },
    async list(prefix) { return [...files.keys()].filter(k => k.startsWith(prefix) && has(k)).sort() },
  }
}

async function readTree(dir: string, rel = ''): Promise<void> {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readdirSync(p: string, o: { withFileTypes: true }): { name: string; isDirectory(): boolean }[]; readFileSync(p: string): Uint8Array }
  for (const e of fs.readdirSync(fixture(dir), { withFileTypes: true })) {
    const k = rel ? `${rel}/${e.name}` : e.name
    if (e.isDirectory()) await readTree(`${dir}/${e.name}`, k)
    else if (k !== 'expected.json' && k !== 'gen.py') { const b = fs.readFileSync(fixture(`${dir}/${e.name}`)); files.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
  }
}

beforeAll(async () => {
  await readTree('static-anchors')
  E = await readJson<Expected>('static-anchors/expected.json')
})

const STACKS = {
  base: { hide: [FIRST, LAST], scans: () => E.base },
  run1: { hide: [LAST], scans: () => E.dates.slice(0, E.base.length + 1) },
  run2: { hide: [], scans: () => E.dates },
} as const

function source(stack: keyof typeof STACKS, maxRows?: number, opts: { hide?: string[]; startMaxRows?: number; startMaxHits?: number } = {}): AnchoredSource {
  const blobs = blobsOf({ hide: [...STACKS[stack].hide, ...opts.hide ?? []] })
  return new AnchoredSource(tiers(blobs).tiers, blobs, { maxRows, startMaxRows: opts.startMaxRows ?? maxRows, startMaxHits: opts.startMaxHits })
}

const n = (x: bigint) => Number(x)
function childSums(hits: Hit[], P: string, date: string): Sums {
  const D = scanTime(date), acc = new Map<string, [number, number]>()
  for (const h of hits) {
    if (!(h.vf <= D && D < h.vt)) continue
    const c = (P === '' ? h.path : h.path.slice(P.length + 1)).split('/')[0]
    const e = acc.get(c) ?? [0, 0]
    e[0] += n(h.size); e[1] += n(h.n)
    acc.set(c, e)
  }
  return Object.fromEntries([...acc].filter(([, v]) => v[0] || v[1]).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0))
}

/** Every (key, P, date) view of a stack against brute force: `[got, want]` lists and the sources seen. */
async function sweep(s: AnchoredSource, dates: string[], stackScans: string[]) {
  const got: unknown[] = [], want: unknown[] = []
  const seen: Record<string, number> = {}
  for (const key of E.keys) for (const P of E.paths) {
    const f: Found | null = termInPath(key, P) ? null : await s.hits(key, P)
    const kind = termInPath(key, P) ? 'plain' : !f ? 'declined' : f.rollup ? (f.rollup.bucketsOnly ? 'catalog' : 'rollup') : String(f.io.from === 'anchors' ? f.io.source : 'light')
    seen[kind] = (seen[kind] ?? 0) + 1
    if (kind === 'plain') continue
    if (!f) { expect(parseKey(key).mode).toBe('start'); expect(s.why(key)).toBe('term-too-common'); continue }
    expect(f.scans).toEqual(stackScans)
    for (const d of dates) {
      const b = E.views[key]?.[P]?.[d] ?? {}
      if (f.rollup) {
        const { kids, rest } = rollupAt(f.rollup, d)
        const kept = Object.fromEntries(kids.map(([c, kb, ko]) => [c, [n(kb), n(ko)]]))
        const other = Object.entries(b).filter(([c]) => !(c in kept)).reduce((a, [, v]) => [a[0] + v[0], a[1] + v[1]], [0, 0])
        got.push([key, P, d, kept, [n(rest[0]), n(rest[1])]])
        want.push([key, P, d, Object.fromEntries(Object.keys(kept).map(c => [c, b[c] ?? [0, 0]])), other])
      } else {
        got.push([key, P, d, childSums(f.hits!, P, d)])
        want.push([key, P, d, b])
      }
    }
  }
  return { got, want, seen }
}

describe('anchored views equal brute force', () => {
  for (const stack of ['base', 'run1', 'run2'] as const) {
    for (const maxRows of [undefined, 8, 0]) {
      it(`${stack}, whole-range bound ${maxRows ?? 'MAX_ROWS'}`, async () => {
        const scans = STACKS[stack].scans()
        const { got, want, seen } = await sweep(source(stack, maxRows), scans, scans)
        expect(got).toEqual(want)
        if (maxRows === undefined) expect(Object.keys(seen).sort()).toEqual(['light', 'plain'])
        else expect(Math.min(seen.roots ?? 0, seen.rollup ?? 0, seen.catalog ?? 0, seen.declined ?? 0)).toBeGreaterThan(0)
        if (maxRows === 8) expect(seen.scoped).toBeGreaterThan(0)
      })
    }
  }
})

/** Every `^q` prefix's fleet root from the starts-with catalog (bounds 0) against brute force: `[got, want]`, and the
 *  keys it answered (the catalog's members). */
async function startRoots(s: AnchoredSource, dates: string[]) {
  const got: unknown[] = [], want: unknown[] = [], members: string[] = []
  for (const key of E.start_keys) {
    const f = await s.hits(key, '')
    if (!f) { expect(s.why(key)).toBe('term-too-common'); continue }
    // A key with no rows is light at any bound (nothing to read).
    if (!f.rollup) { expect([f.io.from, f.hits, E.start_root[key]]).toEqual(['names', [], undefined]); continue }
    members.push(key)
    expect([f.rollup?.bucketsOnly, f.rollup?.scopedBelow, f.io.source]).toEqual([true, true, 'catalog'])
    for (const d of dates) {
      const b = E.start_root[key]?.[d] ?? {}
      const { kids, rest } = rollupAt(f.rollup!, d)
      const kept = Object.fromEntries(kids.map(([c, kb, ko]) => [c, [n(kb), n(ko)]]))
      const other = Object.entries(b).filter(([c]) => !(c in kept)).reduce((a, [, v]) => [a[0] + v[0], a[1] + v[1]], [0, 0])
      got.push([key, d, kept, [n(rest[0]), n(rest[1])]])
      want.push([key, d, Object.fromEntries(Object.keys(kept).map(c => [c, b[c] ?? [0, 0]])), other])
    }
  }
  return { got, want, members }
}

/** Rows each `^q` key's range holds over a stack's name indexes (row exact; the catalog's membership count). */
async function startRows(stack: keyof typeof STACKS): Promise<Map<string, number>> {
  const { parquetReadObjects } = await import('hyparquet')
  const { compressors } = await import('./zstd')
  const dirs = ['', ...(stack === 'base' ? [] : ['deltas/2026-09-15/']), ...(stack === 'run2' ? ['deltas/2026-10-01/'] : [])]
  const out = new Map<string, number>()
  for (const d of dirs) for (const [k, b] of files) {
    if (!k.startsWith(`${d}names/sx/`)) continue
    for (const r of await parquetReadObjects({ file: b, columns: ['s'], compressors }) as { s: string }[]) {
      const cs = [...r.s]
      for (let L = 3; L <= cs.length; L++) { const p = cs.slice(0, L).join(''); out.set(p, (out.get(p) ?? 0) + 1) }
    }
  }
  return out
}

describe('`^q`: the starts-with catalog and scoped reads', () => {
  for (const stack of ['base', 'run1', 'run2'] as const) {
    it(`${stack}: every prefix's fleet root from the catalog equals brute force; its members are the prefixes past R`, async () => {
      const scans = STACKS[stack].scans()
      const { got, want, members } = await startRoots(source(stack, 0), scans)
      expect(got).toEqual(want)
      const rows = await startRows(stack)
      expect(members).toEqual(E.start_keys.filter(k => (rows.get(k) ?? 0) > E.R))
      expect(members.length).toBeGreaterThan(10)
    })
  }
  it('below the root: exact at a bound equal to the groups meeting the path, `term-too-common` one row under', async () => {
    const probe = source('run2')
    const st = await probe.state()
    let seen = 0
    for (const key of E.keys.filter(k => parseKey(k).mode === 'start')) {
      const k = key
      const count = async (under?: string) => (await Promise.all(st.tiers.map(t => t.names.select(k, { shard: null, groups: 0, bytes: 0, rows_read: 0, rows_matching: 0, index: 'none', ms: {} }, { under })))).reduce((x, s) => x + (s?.rows ?? 0), 0)
      const whole = await count()
      for (const P of E.paths) {
        if (P === '' || termInPath(key, P)) continue
        const upper = await count(P)
        if (!(upper > 0 && upper < whole)) continue
        const at = await source('run2', 0, { startMaxRows: upper }).hits(key, P)
        expect([at?.io.source, at?.io.upper, at?.scans]).toEqual(['scoped', upper, E.dates])
        expect(E.dates.map(d => childSums(at!.hits!, P, d))).toEqual(E.dates.map(d => E.views[key]?.[P]?.[d] ?? {}))
        const under = source('run2', 0, { startMaxRows: upper - 1 })
        expect([await under.hits(key, P), under.why(key)]).toEqual([null, 'term-too-common'])
        seen++
      }
    }
    expect(seen).toBeGreaterThan(5)
  })
  it('reads up to 400K rows (+ two groups) whole by default', async () => {
    expect(START_MAX_ROWS).toBe(400_000 + 2 * 8192)
    const blobs = blobsOf()
    const s = new AnchoredSource(tiers(blobs).tiers, blobs, { maxRows: 0 })
    // `q$` / `^q$` are past their bound (0), `^q` is light under its own.
    expect([(await s.hits('/tr', ''))?.io.from, (await s.hits('/config.json/', ''))?.io.source]).toEqual(['names', 'rollup'])
  })
  it('a run without `anchors/start/meta.json` cuts the catalog\'s stack there (its scans out); scoped reads keep every tier', async () => {
    const s = source('run2', 0, { hide: ['deltas/2026-10-01/anchors/start/meta.json'] })
    const st = await s.state()
    expect([st.tiers.length, st.start.tiers.length, st.start.scans]).toEqual([3, 2, E.dates.slice(0, -1)])
    const { got, want } = await startRoots(s, E.dates.slice(0, -1))
    expect(got).toEqual(want)
    const none = source('run2', 0, { hide: ['anchors/start/meta.json'] })
    expect([await none.hits('/tr', ''), none.why('/tr'), (await none.state()).start]).toEqual([null, 'term-too-common', { tiers: [], scans: [] }])
  })
  it('past `startMaxHits` first hits a whole read is heavy (the root: the catalog) and a scoped one declines', async () => {
    const last = E.dates[E.dates.length - 1]
    const hitsOf = (f: Found | null) => f && (f.rollup ? ['catalog', rollupAt(f.rollup, last).kids.map(([c, b, o]) => [c, n(b), n(o)])] : [f.io.source ?? f.io.from, childSums(f.hits!, '', last)])
    const want = E.start_root['/tr'][last]
    const capped = source('run2', undefined, { startMaxHits: 1 })
    expect(hitsOf(await capped.hits('/tr', ''))).toEqual(['catalog', Object.entries(want).map(([c, [b, o]]) => [c, b, o])])
    expect([await capped.hits('/tr', 'b2'), capped.why('/tr')]).toEqual([null, 'term-too-common'])
    const roomy = source('run2', undefined, { startMaxHits: 1000 })
    expect(hitsOf(await roomy.hits('/tr', ''))).toEqual(['names', want])
    expect(childSums((await roomy.hits('/tr', 'b2'))!.hits!, 'b2', last)).toEqual(E.views['/tr'].b2[last])
  })
  it('a first paint of the fleet root is the catalog until the range is read, then the hits', async () => {
    const s = source('run2')
    const first = await s.hits('/tr', '', { firstPaint: true })
    expect([first?.io.source, first?.io.firstPaint, first?.rollup?.bucketsOnly]).toEqual(['catalog', true, true])
    const full = await s.hits('/tr', '')
    expect([full?.io.from, full?.rollup]).toEqual(['names', undefined])
    const again = await s.hits('/tr', '', { firstPaint: true })
    expect([again?.io.from, again?.hits?.length]).toEqual(['names', full!.hits!.length])
    // A key with rows but no catalog entry (at most R of them): a first paint reads the range as the full one does.
    const rows = await startRows('run2')
    const light = E.start_keys.find(k => E.start_root[k] && (rows.get(k) ?? 0) <= E.R)!
    expect((await source('run2').hits(light, '', { firstPaint: true }))?.io.from).toBe('names')
  })
  it('a catalog stack that ignores the runs\' cells is caught', async () => {
    const real = DRILL_RULES.stack
    const spy = vi.spyOn(DRILL_RULES, 'stack').mockImplementation(parts => real([parts[0], ...parts.slice(1).map(() => [])]))
    try {
      const { got, want } = await startRoots(source('run2', 0), E.dates)
      expect(got).not.toEqual(want)
    } finally { spy.mockRestore() }
  })
})

describe('the anchored stack', () => {
  it('drops a run without `anchors/meta.json` and every later one (its scans out of the answers)', async () => {
    const blobs = blobsOf({ hide: ['deltas/2026-09-15/anchors/meta.json'] })
    const s = new AnchoredSource(tiers(blobs).tiers, blobs, { maxRows: 0 })
    const st = await s.state()
    expect([st.tiers.length, st.scans]).toEqual([1, E.base])
    // `q$` light reads stay on every scan (the suffix shards are every tier's); scoped ones cover the anchored tiers.
    const light = await new AnchoredSource(tiers(blobs).tiers, blobs).hits('.json/', '')
    expect(light!.scans).toEqual(E.dates)
    const scoped = await s.hits('.json/', '')
    expect(scoped!.scans).toEqual(E.base)
  })
  it('a generation without anchors declines `^q` and `^q$` (and `q$` past the light bound)', async () => {
    const blobs = blobsOf({ hide: ['anchors/meta.json'] })
    const s = new AnchoredSource(tiers(blobs).tiers, blobs, { maxRows: 0 })
    expect(await Promise.all(['/tomat', '/config.json/', '.json/'].map(k => s.hits(k, '')))).toEqual([null, null, null])
    expect(['/tomat', '/config.json/', '.json/'].map(k => s.why(k))).toEqual(['anchor-not-indexed', 'anchor-not-indexed', 'anchor-not-indexed'])
  })
  it('with anchors (gcs), the same keys answer and name no reason', async () => {
    const s = source('run2', 0)
    const got = await Promise.all(['/tomat', '/config.json/', '.json/'].map(k => s.hits(k, '')))
    expect([got.map(f => f !== null), ['/tomat', '/config.json/', '.json/'].map(k => s.why(k))]).toEqual([[true, true, true], [undefined, undefined, undefined]])
  })
  it('no light index at all is not `anchor-not-indexed` (the scan, not the term, is the reason)', async () => {
    const blobs = blobsOf({ hide: [...files.keys()] })
    const s = new AnchoredSource(tiers(blobs).tiers, blobs, { maxRows: 0 })
    expect([await s.hits('/tomat', ''), s.why('/tomat')]).toEqual([null, undefined])
  })
})

describe('mutations are caught', () => {
  it('the contains literal\'s parent test in place of the per-segment one', async () => {
    const spy = vi.spyOn(AnchoredHits.prototype, 'add').mockImplementation(function (this: AnchoredHits, cols) {
      const k = this.rowKey
      this.rows_read += cols.path.length
      for (let i = 0; i < cols.path.length; i++) {
        if (this.mode === 'start' ? !cols.s[i].startsWith(k) : cols.s[i] !== k) continue
        const p = cols.path[i], slash = p.lastIndexOf('/')
        if (cols.depth[i] < 1 || (slash >= 0 && p.slice(0, slash).toLowerCase().includes(this.text))) continue
        this.hits.push({ path: p, depth: cols.depth[i], usr: cols.usr[i], vf: Number(cols.vf[i]), vt: Number(cols.vt[i]), size: cols.size[i], n: cols.n_files[i] })
      }
    })
    try {
      const { got, want } = await sweep(source('run2'), E.dates, E.dates)
      expect(got).not.toEqual(want)
    } finally { spy.mockRestore() }
  })
})

describe('keys and segments', () => {
  it('`termKey` marks anchors with `/`, `parseKey` reads them back', () => {
    const terms = [{ text: 'ab', start: true, end: false }, { text: '.gz', start: false, end: true }, { text: 'x', start: true, end: true }, { text: 'tomat', start: false, end: false }]
    expect(terms.map(termKey)).toEqual(['/ab', '.gz/', '/x/', 'tomat'])
    expect(terms.map(termKey).map(parseKey)).toEqual([{ text: 'ab', mode: 'start' }, { text: '.gz', mode: 'end' }, { text: 'x', mode: 'exact' }, { text: 'tomat', mode: null }])
  })
  it('`termInPath`: some segment matches (`big-tomato` is no `^tomat`)', () => {
    const cases: [string, string, boolean][] = [
      ['/tomat', 'b1/big-tomato', false], ['/tomat', 'b1/tomato', true], ['/b1', 'b1', true], ['/b1', '', false],
      ['.json/', 'b/sub.json', true], ['.json/', 'b/a.jsonx', false], ['/config.json/', 'b/config.json', true], ['/config.json/', 'b/config.json.bak', false],
      ['/config.json/', 'b/CONFIG.JSON', true], ['tomat', 'b1/big-tomato', true],
    ]
    expect(cases.map(([k, p]) => termInPath(k, p))).toEqual(cases.map(c => c[2]))
    expect([segmentMatches('abc', 'ab', 'start'), segmentMatches('abc', 'bc', 'end'), segmentMatches('abc', 'abc', 'exact'), segmentMatches('abc', 'b', null)]).toEqual([true, true, true, true])
  })
  it('`staticLiteral`: an anchored literal\'s key, null under its minimum length or with `/`', () => {
    const lit = (q: string) => staticLiteral(parseAst(q) ?? undefined)
    expect(['^tomat', '.json$', '^config.json$', '^ab', '^a', 'gz$', '.gz$', '^x$', '"^a"', 'tomat'].map(lit))
      .toEqual(['/tomat', '.json/', '/config.json/', '/ab', null, null, '.gz/', '/x/', '^a', 'tomat'])
  })
  it('`SuffixHits` hands anchored keys to the anchored source, and nobody else', async () => {
    const blobs = blobsOf()
    const t = tiers(blobs)
    const anchored = new AnchoredSource(t.tiers, blobs)
    const s = new SuffixHits(t.names, { anchored })
    const got = await s.hits('/config.json/', 'b2')
    expect(childSums(got!.hits!, 'b2', E.dates[E.dates.length - 1])).toEqual(E.views['/config.json/'].b2[E.dates[E.dates.length - 1]])
    expect(await new SuffixHits(t.names).hits('/config.json/', 'b2')).toBe(null)
  })
})
