import { beforeAll, describe, expect, it } from 'vitest'
import { answerKey, staticSummary, type Store } from './nameSummaryStatic.js'
import { catalogAnswer } from './staticCatalog.js'
import { liveTotal, SuffixHits } from './staticFilter.js'
import type { Blobs } from './staticNames.js'
import { latestManifest, tiers } from './staticRuns.js'
import { fixture } from './testStore.js'

// The daily runs' reader (`staticRuns.ts`) over `fixtures/static-runs/` (`gen.py`): a base through 2026-09-01 and
// a run per scan — 2026-10-01, 2026-10-02 (date ids), then two scans on one day, 2026-10-03T0600 and 2026-10-03T1800
// (scan ids, specs/scan-ids-not-dates.md) — against a brute-force oracle over every version on every scan.

const RUNS = ['2026-10-01', '2026-10-02', '2026-10-03T0600', '2026-10-03T1800']
const TIERS = ['base', ...RUNS.map(r => `deltas/${r}`)]
const FILES = ['shards.json', 'sx/s0000.parquet', 'sx/s0001.parquet', 'catalog/cells.parquet', 'catalog/index.parquet', 'catalog/meta.json']
const DATES = ['2026-08-01', '2026-09-01', ...RUNS]
const MANIFESTS = RUNS.map(r => `manifests/${r}.json`)
/** A deferred carry (`static_merge`): the first two runs merged, published by a revision of 2026-10-02's manifest. */
const MERGED = 'deltas/2026-10-01_2026-10-02'
const REV = 'manifests/2026-10-02.m001.json'
const held = new Map<string, ArrayBuffer>()
let expected: Record<string, Record<string, Record<string, [number, number]>>>
let catalog: Record<string, [string, number, number, number][] | null>
beforeAll(async () => {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readFileSync(path: string): Uint8Array }
  const keys = [...[...TIERS, MERGED].flatMap(t => FILES.map(f => `${t}/${f}`)), 'base/scans.json', ...MANIFESTS, REV, 'expected.json', 'catalog-expected.json']
  for (const k of keys) { const b = fs.readFileSync(fixture(`static-runs/${k}`)); held.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
  const text = (k: string) => JSON.parse(new TextDecoder().decode(held.get(k)))
  expected = text('expected.json')
  catalog = text('catalog-expected.json')
})

/** A broken publish: keys `missing` (as R2 says it), keys `corrupt` (garbage bytes). */
interface Damage { missing?: Set<string>; corrupt?: Set<string> }
const GARBAGE = new Uint8Array(64).fill(0xab).buffer

/** The fixture as one generation's `Blobs` (the base at the root, runs under `deltas/`), listing only the
 *  manifests in `visible`, recording each read. */
function files(visible: string[] = MANIFESTS, log: string[] = [], damage: Damage = {}): Blobs {
  const path = (key: string) => key.startsWith('deltas/') || key.startsWith('manifests/') ? key : `base/${key}`
  const bytes = (key: string) => {
    if (damage.missing?.has(key)) throw new Error(`static names: static-names/fixture/${key} is missing`)
    if (damage.corrupt?.has(key)) return GARBAGE
    const b = held.get(path(key)); if (!b) throw new Error(`no fixture ${key}`); return b
  }
  return {
    async range(key, offset, length) { const b = bytes(key); const end = length == null ? b.byteLength : offset + length; log.push(`${key}@${offset}+${end - offset}`); return b.slice(offset, end) },
    async suffix(key, n) { const b = bytes(key), size = b.byteLength; log.push(`${key}@-${n}`); return { buf: b.slice(Math.max(0, size - n)), size } },
    async json(key) { log.push(key); return JSON.parse(new TextDecoder().decode(bytes(key))) },
    async list(prefix) { log.push(`list ${prefix}`); return visible.filter(k => k.startsWith(prefix)) },
  }
}
const num = (answers: Record<string, Record<string, [bigint, bigint]>>) =>
  Object.fromEntries(Object.entries(answers).map(([d, t]) => [d, Object.fromEntries(Object.entries(t).filter(([, [b, o]]) => b !== 0n || o !== 0n).map(([b, [x, o]]) => [b, [Number(x), Number(o)]]))]))
const terms = () => Object.keys(expected)
const long = () => terms().filter(t => [...t].length >= 3)
const store = (blobs: Blobs): Store => { const t = tiers(blobs); return { names: t.names, catalog: t.catalog, scans: t.scans, clock: async () => Date.now() } }
const until = (date: string) => Object.fromEntries(DATES.filter(d => d <= date).map(d => [d, null]))

describe('static runs', () => {
  it('takes the newest manifest; without one (or without `list`) the base alone', async () => {
    expect((await latestManifest(files()))?.runs.map(r => r.key)).toEqual(TIERS.slice(1))
    expect((await latestManifest(files([MANIFESTS[0]])))?.runs.map(r => r.key)).toEqual(['deltas/2026-10-01'])
    expect(await latestManifest(files([]))).toBeNull()
    const { list: _, ...listless } = files()
    expect(await latestManifest(listless)).toBeNull()
    expect(await tiers(files()).scans()).toEqual(DATES)
    expect(await tiers(files([MANIFESTS[0]])).scans()).toEqual(DATES.slice(0, 3))
    expect(await tiers(files([])).scans()).toEqual(DATES.slice(0, 2))
  })

  it('names the base\'s dir-only (v1) scans, with runs or without; an unversioned scans.json has none', async () => {
    const versioned = (b: Blobs): Blobs => ({ ...b, json: async <T,>(key: string) => (key === 'scans.json' ? { scans: [{ id: '2026-08-01', version: 1 }, { id: '2026-09-01', version: 2 }] } : await b.json(key)) as T })
    expect(await Promise.all([tiers(files()).dirOnly(), tiers(versioned(files())).dirOnly(), tiers(versioned(files([]))).dirOnly()]))
      .toEqual([[], ['2026-08-01'], ['2026-08-01']])
  })

  it('exercises close records, opens, and a literal that became a member in the last run', () => {
    expect(catalog.qqq?.length).toBeGreaterThan(1)
    expect(expected.foo['2026-10-02']).not.toEqual(expected.foo['2026-10-01'])
    expect(expected['late']['2026-10-01']).toEqual({ 'bkt-b': [43, 2] })
    expect(expected['late']['2026-10-02']).toEqual({ 'bkt-b': [21, 1] })
  })

  it.each([['2026-10-03T1800', MANIFESTS], ['2026-10-03T0600', MANIFESTS.slice(0, 3)], ['2026-10-02', MANIFESTS.slice(0, 2)],
    ['2026-10-01', [MANIFESTS[0]]], ['2026-09-01', []]] as const)(
    'answers every literal of 3+ characters like the oracle on every date through %s', async (last, visible) => {
      const t = tiers(files([...visible]))
      const dates = Object.keys(until(last))
      for (const term of long()) {
        const { answer } = await t.names.answer(term, dates)
        expect([term, num(answer!.answers)]).toEqual([term, Object.fromEntries(dates.map(d => [d, expected[term][d]]))])
      }
    })

  it('looks up every literal in the merged catalog: the newest header, every tier\'s cells', async () => {
    const t = tiers(files())
    for (const term of terms()) {
      const { member } = await t.catalog.lookup(term)
      const want = catalog[term]
      if (!want) { expect([term, member]).toEqual([term, null]); continue }
      expect([term, member!.rows, member!.cells.map(c => [c.bucket, Number(c.vf), Number(c.b), Number(c.o)])])
        .toEqual([term, want[0][2], want.slice(1)])
      const ans = catalogAnswer(member!, DATES)
      expect([term, num(ans)]).toEqual([term, expected[term]])
    }
  })

  it('answers every literal through the summary dispatch (catalog or bounded postings) like the oracle', async () => {
    const s = store(files())
    for (const term of terms()) {
      const { answers } = await answerKey(s, term, DATES)
      expect([term, num(answers)]).toEqual([term, expected[term]])
    }
  })

  it('folds the map filter\'s hits over the tiers: per bucket root, the live totals equal the oracle\'s', async () => {
    const t = tiers(files())
    const src = new SuffixHits(t.names)
    for (const term of long()) {
      const got = await src.all(term)
      for (const d of DATES) {
        const per = Object.fromEntries(['bkt-a', 'bkt-b'].map(b => {
          const { b: x, o } = liveTotal(got!.hits.filter(h => h.path === b || h.path.startsWith(`${b}/`)), d)
          return [b, [x, o]] as const
        }).filter(([, [x, o]]) => x !== 0 || o !== 0))
        expect([term, d, per]).toEqual([term, d, expected[term][d]])
      }
    }
  })

  it('keys runs by scan id: two scans on one day are two runs, two manifests, two answers', async () => {
    expect((await latestManifest(files()))?.date).toEqual('2026-10-03T1800')
    expect((await latestManifest(files(MANIFESTS.slice(0, 3))))?.date).toEqual('2026-10-03T0600')
    expect(await tiers(files(MANIFESTS.slice(0, 3))).scans()).toEqual(DATES.slice(0, 5))
    const am = '2026-10-03T0600', pm = '2026-10-03T1800'
    const { answer } = await tiers(files()).names.answer('sub-', ['2026-10-02', am, pm])
    expect(num(answer!.answers)).toEqual({ '2026-10-02': {}, [am]: { 'bkt-a': [7, 1] }, [pm]: { 'bkt-b': [9, 1] } })
    const hits = (await new SuffixHits(tiers(files()).names).all('sub-'))!.hits
    expect([am, pm].map(d => liveTotal(hits, d))).toEqual([{ b: 7, o: 1, roots: 1 }, { b: 9, o: 1, roots: 1 }])
    expect(expected['sub-']).toEqual({ ...Object.fromEntries(DATES.slice(0, 4).map(d => [d, {}])), [am]: { 'bkt-a': [7, 1] }, [pm]: { 'bkt-b': [9, 1] } })
  })

  it('picks up a new manifest after the TTL: the hit lists are held per version', async () => {
    const visible = [MANIFESTS[0]]
    let now = 0
    const blobs: Blobs = { ...files(visible), list: async p => visible.filter(k => k.startsWith(p)) }
    const t = tiers(blobs, { ttlMs: 1000, now: () => now })
    const src = new SuffixHits(t.names)
    const total = async () => liveTotal((await src.all('qqq'))!.hits, '2026-10-02')
    expect(await t.scans()).toEqual(DATES.slice(0, 3))
    expect(await total()).toEqual({ b: 0, o: 0, roots: 0 })
    visible.push(MANIFESTS[1])
    expect(await total()).toEqual({ b: 0, o: 0, roots: 0 }) // within the TTL: the held state
    now = 1000
    expect(await t.scans()).toEqual(DATES.slice(0, 4))
    expect(await total()).toEqual({ b: 30 + 31 + 32 + 33 + 34, o: 5, roots: 5 })
  })
})

describe('a broken tier: the stack is cut at it, the scans before it answer exactly, never a failure', () => {
  const RUN3 = 'deltas/2026-10-03T0600'
  /** The base and runs 1–2: the scans a cut at run 3 still answers. */
  const BEFORE = DATES.slice(0, 4)
  const oracle = (term: string, dates: string[]) => Object.fromEntries(dates.map(d => [d, expected[term][d]]))
  const missing = (key: string) => `static names: static-names/fixture/${key} is missing`
  const brokeMsg = (dir: string, error: string) => `static tiers: ${dir} is broken; its scans and every later tier's are not indexed until it loads: ${error}`
  const storeOf = (t: ReturnType<typeof tiers>): Store => ({ names: t.names, catalog: t.catalog, scans: t.scans, clock: async () => Date.now() })
  /** Every literal through the summary dispatch on `dates`: its answers and the scans they are exact on. */
  const dispatch = async (s: Store, dates: string[]) => {
    const out: [string, unknown, string[] | undefined][] = []
    for (const term of terms()) {
      const a = await answerKey(s, term, dates)
      // Only the scans the answer is exact on (past a cut, a caller's `scan-not-indexed`).
      out.push([term, Object.fromEntries(Object.entries(num(a.answers)).filter(([d]) => !a.scans || a.scans.includes(d))), a.scans])
    }
    return out
  }
  const want = (dates: string[], scans: string[]) => terms().map(term => [term, oracle(term, dates), scans])
  /** Every long literal's map hits, per bucket root on each of `dates`, and the scans they are exact on. */
  const mapHits = async (src: SuffixHits, dates: string[]) => {
    const out: unknown[] = []
    for (const term of long()) {
      const got = (await src.all(term))!
      out.push([term, got.scans, Object.fromEntries(dates.map(d => [d, Object.fromEntries(['bkt-a', 'bkt-b'].map(b => {
        const { b: x, o } = liveTotal(got.hits.filter(h => h.path === b || h.path.startsWith(`${b}/`)), d)
        return [b, [x, o]] as const
      }).filter(([, [x, o]]) => x !== 0 || o !== 0))]))])
    }
    return out
  }
  const wantHits = (dates: string[], scans: string[]) => long().map(term => [term, scans, oracle(term, dates)])

  it('a run recording another hex-run rule than the base (specs/static-hex-runs.md) is cut at, never mixed in', async () => {
    const logged: string[] = []
    const base = files(MANIFESTS)
    const json = async (key: string): Promise<unknown> => key === `${RUN3}/catalog/meta.json` ? { ...await base.json<object>(key), hex_runs: { min: 16, tail: 8 } } : base.json(key)
    const ruled = { ...base, json } as Blobs
    const t = tiers(ruled, { log: m => logged.push(m) })
    const st = await t.tiers.state()
    expect([st.version, st.scans, st.tiers.map(x => x.dir), st.broken?.dir, st.hexRuns]).toEqual(['2026-10-02', BEFORE, [null, 'deltas/2026-10-01', 'deltas/2026-10-02'], RUN3, null])
    expect(await dispatch(storeOf(t), BEFORE)).toEqual(want(BEFORE, BEFORE))
    expect(logged).toEqual([brokeMsg(RUN3, `static tiers: ${RUN3}/catalog/meta.json hex_runs {"min":16,"tail":8} disagrees with the base's null`)])
  })

  it('a run listed before its `catalog/meta.json` is written (the gcs incident): cut at it, logged once with the key', async () => {
    const logged: string[] = []
    const t = tiers(files(MANIFESTS, [], { missing: new Set([`${RUN3}/catalog/meta.json`]) }), { log: m => logged.push(m) })
    const st = await t.tiers.state()
    // The stack the manifest that ended at run 2 listed, versioned as that manifest was.
    expect([st.version, st.scans, st.tiers.map(x => x.dir), st.broken?.dir]).toEqual(['2026-10-02', BEFORE, [null, 'deltas/2026-10-01', 'deltas/2026-10-02'], RUN3])
    expect(await dispatch(storeOf(t), BEFORE)).toEqual(want(BEFORE, BEFORE))
    expect(await mapHits(new SuffixHits(t.names), BEFORE)).toEqual(wantHits(BEFORE, BEFORE))
    expect(logged).toEqual([brokeMsg(RUN3, missing(`${RUN3}/catalog/meta.json`))])
  })

  it('heals once the file lands: a cut state lives `brokenTtlMs`, not `ttlMs`, and a cut hit list is never held', async () => {
    let now = 0
    const gone = new Set([`${RUN3}/catalog/meta.json`])
    const t = tiers(files(MANIFESTS, [], { missing: gone }), { now: () => now, ttlMs: 300_000, brokenTtlMs: 30_000, log: () => {} })
    const src = new SuffixHits(t.names)
    expect([await t.scans(), (await src.all('qqq'))!.scans]).toEqual([BEFORE, BEFORE])
    gone.clear()
    now = 29_999
    expect(await t.scans()).toEqual(BEFORE)
    now = 30_000
    expect(await t.scans()).toEqual(DATES)
    expect(await dispatch(storeOf(t), DATES)).toEqual(want(DATES, DATES))
    expect(await mapHits(src, DATES)).toEqual(wantHits(DATES, DATES))
  })

  it.each([
    ['corrupt suffix shards', [`${RUN3}/sx/s0000.parquet`, `${RUN3}/sx/s0001.parquet`], 'Start offset -2880154483 is outside the bounds of the buffer'],
    ['a corrupt catalog index', [`${RUN3}/catalog/index.parquet`], 'parquet file invalid (footer != PAR1)'],
    ['a corrupt `shards.json`', [`${RUN3}/shards.json`], `Unexpected token '\ufffd', "${'\ufffd'.repeat(10)}"... is not valid JSON`],
  ])('%s, found by a read: the read is re-run over the cut stack, exact on the scans before it', async (_, keys, error) => {
    const logged: string[] = []
    const t = tiers(files(MANIFESTS, [], { corrupt: new Set(keys) }), { log: m => logged.push(m) })
    // The probe (`catalog/meta.json`) passes: the stack is whole until a read fails.
    expect(await t.scans()).toEqual(DATES)
    const got = await dispatch(storeOf(t), DATES)
    // The literals read before the failure answer every scan; from it on, the scans before run 3.
    const cutAt = got.findIndex(([, , scans]) => scans!.length < DATES.length)
    expect(cutAt).toBeGreaterThanOrEqual(0)
    expect(got).toEqual(terms().map((term, i) => i < cutAt ? [term, oracle(term, DATES), DATES] : [term, oracle(term, BEFORE), BEFORE]))
    expect([await t.scans(), logged]).toEqual([BEFORE, [brokeMsg(RUN3, error)]])
  })

  it('`/api/name-summary`: a tier found broken mid-read is `scan-not-indexed` for its scans, the scans before it answer', async () => {
    const env = { NAME_SUMMARY_STATIC: '1', STATIC_GEN: 'fixture', STORE_BUCKETS: 'bkt-a,bkt-b' }
    const t = tiers(files(MANIFESTS, [], { corrupt: new Set([`${RUN3}/catalog/index.parquet`, `${RUN3}/sx/s0000.parquet`, `${RUN3}/sx/s0001.parquet`]) }), { log: () => {} })
    const ask = async (date: string) => {
      const r = await staticSummary(env, new URLSearchParams({ date, name: 'foo' }), storeOf(t))
      const j = await r.json() as { code?: string; buckets?: { path: string; b: number; o: number }[] }
      return [r.status, j.code ?? Object.fromEntries(j.buckets!.filter(x => x.b || x.o).map(x => [x.path, [x.b, x.o]]))]
    }
    // The registry still lists 10/3 6pm when the request starts; the catalog read finds run 3 broken.
    expect(await t.scans()).toEqual(DATES)
    expect([await ask('2026-10-03T1800'), await ask('2026-10-02'), await t.scans()]).toEqual([[400, 'scan-not-indexed'], [200, expected.foo['2026-10-02']], BEFORE])
  })

  it('a broken base: no scan is indexed (none answered from runs without it)', async () => {
    const logged: string[] = []
    const t = tiers(files(MANIFESTS, [], { missing: new Set(['catalog/meta.json']) }), { log: m => logged.push(m) })
    const st = await t.tiers.state()
    expect([st.version, st.scans, st.tiers, st.broken?.dir]).toEqual(['none', [], [], ''])
    const { io, answer, scans } = await t.names.answer('foo', [])
    expect([io.tiers, io.rows_read, answer!.answers, scans]).toEqual([0, 0, {}, []])
    expect(logged).toEqual([brokeMsg('the base', missing('catalog/meta.json'))])
  })

  it('a cut read is never cached under the whole stack\'s version', async () => {
    const puts: string[] = []
    const cache = { get: async () => null, put: async (k: string) => { puts.push(k) } }
    const t = tiers(files(MANIFESTS, [], { corrupt: new Set([`${RUN3}/sx/s0000.parquet`, `${RUN3}/sx/s0001.parquet`]) }), { log: () => {} })
    const src = new SuffixHits(t.names, { cache })
    const first = await src.all('foo'), second = await src.all('foo')
    expect([first!.scans, first!.io.version, second!.scans, second!.io.version, puts]).toEqual([BEFORE, '2026-10-02', BEFORE, '2026-10-02', ['foo@2026-10-02']])
  })

  it('mutation: answering past a broken run from the runs around it (skipping it) is wrong — hence the cut', async () => {
    // Run 2 broken, and a reader that skips it: the base, run 1 and runs 3–4, a partial stack.
    const RUN2 = 'deltas/2026-10-02'
    const whole = files()
    const skip: Blobs = { ...whole, json: async key => {
      const j = await whole.json<{ runs: { key: string }[]; scans: string[] }>(key)
      return (key === MANIFESTS[3] ? { ...j, runs: j.runs.filter(r => r.key !== RUN2), scans: j.scans.filter(d => d !== '2026-10-02') } : j) as never
    } }
    const pm = '2026-10-03T1800'
    const wrong: string[] = []
    for (const term of long()) {
      const { answer } = await tiers(skip).names.answer(term, [pm])
      if (JSON.stringify(num(answer!.answers)[pm]) !== JSON.stringify(expected[term][pm])) wrong.push(term)
    }
    expect(wrong).toEqual(['foo', 'foo.', 'qqq', 'qqq-', 'late', 'e-foo'])
  })
})

describe('deferred carries: a merge\'s revision manifest (`static_merge`)', () => {
  it('takes a merge\'s revision over its scan\'s manifest, and the next scan\'s over the revision (deferred carries)', async () => {
    const upTo = [...MANIFESTS.slice(0, 2), REV]
    expect((await latestManifest(files(upTo)))?.runs.map(r => [r.key, r.level])).toEqual([[MERGED, 1]])
    expect((await latestManifest(files([...MANIFESTS, REV])))?.date).toEqual('2026-10-03T1800')
    const revised = tiers(files(upTo)), before = tiers(files(MANIFESTS.slice(0, 2)))
    const dates = DATES.slice(0, 4)
    expect(await revised.scans()).toEqual(dates)
    expect((await revised.tiers.state()).tiers.map(t => t.dir)).toEqual([null, MERGED])
    expect((await revised.tiers.state()).version).toEqual('2026-10-02')
    for (const term of long()) {
      const got = await revised.names.answer(term, dates)
      expect([term, got.io.tiers, num(got.answer!.answers)]).toEqual([term, 2, Object.fromEntries(dates.map(d => [d, expected[term][d]]))])
      expect([term, (await before.names.read(term)).fold!.hits.length]).toEqual([term, (await revised.names.read(term)).fold!.hits.length])
    }
    const s = store(files(upTo))
    for (const term of terms()) {
      const { answers } = await answerKey(s, term, dates)
      expect([term, num(answers)]).toEqual([term, Object.fromEntries(dates.map(d => [d, expected[term][d]]))])
    }
  })

  it('orders revisions numerically (fixed width) and ignores what isn\'t a manifest', async () => {
    const docs: Record<string, object> = Object.fromEntries(['2026-10-02.json', '2026-10-02.m009.json', '2026-10-02.m010.json']
      .map(n => [`manifests/${n}`, { gen: 'fixture', date: '2026-10-02', scans: [], runs: [{ key: n }] }]))
    const blobs = { ...files(), list: async () => [...Object.keys(docs), 'manifests/notes/x.json', 'manifests/2026-10-02.json.tmp'], json: async <T,>(k: string) => docs[k] as T }
    expect((await latestManifest(blobs))?.runs.map(r => r.key)).toEqual(['2026-10-02.m010.json'])
  })
})
