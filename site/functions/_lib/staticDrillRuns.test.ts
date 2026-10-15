import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { scanTime } from '../../src/scanSlug'
import { Drill, type DrillAnswer, type DrillRules, DRILL_RULES, DrillSource, rollupAt, rollupTotal } from './staticDrill'
import { covers, drillSource, fromDrill } from './staticFilter'
import { type Blobs, type Hit } from './staticNames'
import { tiers } from './staticRuns'
import { fixture, readJson } from './testStore'
import { abandoned, BOUND, hangFirst } from './testHang'
import { JOIN, Req } from './shared'

// The drilldown's runs (`staticDrill.ts` over base ⊕ runs) on `fixtures/static-drill-runs/` (`gen.py`): a base
// generation's `drill/` over three scans and two runs' `deltas/<scan>/drill/` on one day (`2026-08-04T0600`,
// `2026-08-04T1800`), built by `dt_cloud.static_drill` (R = 3, K = 2; 4-row base and 2-row run groups), the runs
// covering a class split, kept-set entries and a leaver (full headers), newly heavy directories, new members,
// closes and resurrection. Every view at every directory on every scan is checked against brute force from the
// versions, and the dispatch and rollup headers against the Python reader (`TieredDrill`).

type Sums = Record<string, [number, number]>
type PyView = [string] | [string, number] | [string, number, [number, number, number]]
type Expected = {
  dates: string[]
  paths: string[]
  long: string[]
  short: string[]
  members1: string[]
  R: number
  K: number
  /** Non-empty brute-force views only: `views[t][P][d]`. */
  views: Record<string, Record<string, Record<string, Sums>>>
  py: Record<'run1' | 'run2', Record<string, Record<string, PyView>>>
}
let E: Expected
const files = new Map<string, ArrayBuffer>()
const NONMEMBERS = ['nomatch', 'qq']
const RUN1 = 'deltas/2026-08-04T0600'
const RUN2 = 'deltas/2026-08-04T1800'

/** `Blobs` over the fixture's keys, with `list`; `hide` keys are absent, `corrupt` keys garbage, `reads` collects
 *  every key read. */
function blobsOf(opts: { hide?: string[]; corrupt?: string[]; reads?: string[] } = {}): Blobs {
  const has = (k: string) => files.has(k) && !opts.hide?.includes(k)
  const bytes = (k: string) => {
    opts.reads?.push(k)
    if (!has(k)) throw new Error(`no fixture ${k}`)
    return opts.corrupt?.includes(k) ? new Uint8Array(64).fill(0xab).buffer : files.get(k)!
  }
  return {
    async range(key, offset, length) { const b = bytes(key); return b.slice(offset, length == null ? b.byteLength : offset + length) },
    async suffix(key, n) { const b = bytes(key); return { buf: b.slice(Math.max(0, b.byteLength - n)), size: b.byteLength } },
    async json(key) { return JSON.parse(new TextDecoder().decode(bytes(key))) },
    async list(prefix) { return [...files.keys()].filter(k => k.startsWith(prefix) && has(k)).sort() },
  }
}

/** The drill as the site wires it: the runs and the fleet root's catalog from the light index's tiers. `now`: the
 *  drill's and the tiers' clock (a broken tier's cut lasts `brokenTtlMs` of it). */
function wired(opts: { hide?: string[]; corrupt?: string[]; reads?: string[]; rules?: DrillRules; now?: () => number } = {}): Drill {
  const blobs = blobsOf(opts)
  const t = tiers(blobs, { now: opts.now })
  const runs = async () => (await t.tiers.state()).tiers.flatMap(x => x.dir ? [x.dir] : [])
  if (!opts.rules && !opts.now) return (drillSource(blobs, undefined, { runs, catalog: t.catalog }) as unknown as { drill: Drill }).drill
  return new Drill(blobs, t.catalog, { runs, rules: opts.rules, now: opts.now })
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
  await readTree('static-drill-runs')
  E = await readJson<Expected>('static-drill-runs/expected.json')
})

const n = (x: bigint) => Number(x)
/** Live roots summed per child of `P` on `date` (zeros dropped, by name). */
function childSums(hits: Hit[], P: string, date: string): Sums {
  const D = scanTime(date), acc = new Map<string, [number, number]>()
  for (const h of hits) {
    if (!(h.vf <= D && D < h.vt)) continue
    const c = (P === '' ? h.path : h.path.slice(P.length + 1)).split('/')[0]
    const e = acc.get(c) ?? [0, 0]
    acc.set(c, [e[0] + n(h.size), e[1] + n(h.n)])
  }
  return Object.fromEntries([...acc].filter(([, [b, o]]) => b || o).sort(([x], [y]) => (x < y ? -1 : 1)))
}
const total = (s: Sums): [number, number] => Object.values(s).reduce<[number, number]>((t, [b, o]) => [t[0] + b, t[1] + o], [0, 0])
const brute = (t: string, P: string, d: string): Sums => E.views[t]?.[P]?.[d] ?? {}
const terms = () => [...E.long, ...E.short, ...NONMEMBERS]

/** One view on one scan, as compared with brute force: `[source, kept children's sums, remainder]` (roots: every
 *  child, no remainder). */
function asRead(a: DrillAnswer, P: string, d: string): unknown {
  if (a.source === 'plain' || a.source === 'none') return [a.source]
  if (a.source === 'roots') return ['roots', childSums(a.hits, P, d)]
  const { kids, rest } = rollupAt(a.rollup, d)
  return [a.source, Object.fromEntries(kids.map(([c, b, o]) => [c, [n(b), n(o)]])), [n(rest[0]), n(rest[1])]]
}
/** What brute force says the same view is, given which children the answer keeps by name. */
function asBrute(a: DrillAnswer, t: string, P: string, d: string, members: string[]): unknown {
  if (a.source === 'plain') return [P.toLowerCase().includes(t) ? 'plain' : 'not plain']
  if (a.source === 'none') return [members.includes(t) || [...t].length <= 2 ? 'a member' : 'none']
  const exp = brute(t, P, d)
  if (a.source === 'roots') return ['roots', exp]
  const kept = new Set(a.rollup.cells.filter(c => c.kind === 1).map(c => c.child))
  const keptExp = Object.fromEntries(Object.entries(exp).filter(([c]) => kept.has(c)))
  const all = total(exp), k = total(keptExp)
  return [a.source, keptExp, [all[0] - k[0], all[1] - k[1]]]
}

/** Every term × path × scan of `dates` read through `drill`, against brute force; per source, the views read. */
async function check(drill: Drill, dates: string[], members: string[]) {
  const got: unknown[] = [], want: unknown[] = []
  const sources: Record<string, number> = {}
  for (const t of terms()) for (const P of E.paths) {
    const a = await drill.view(t, P)
    sources[a.source] = (sources[a.source] ?? 0) + 1
    for (const d of dates) { got.push([t, P, d, asRead(a, P, d)]); want.push([t, P, d, asBrute(a, t, P, d, members)]) }
  }
  return { got, want, sources }
}

describe('Drill over base ⊕ runs: every view = brute force', () => {
  it('both runs live: every term at every directory on every scan (two runs on one day), roots child for child, rollups kept + remainder', async () => {
    const drill = wired()
    const st = await drill.state()
    expect([st.version, st.tiers.map(x => x.dir), st.scans]).toEqual([RUN2, [null, RUN1, RUN2], E.dates])
    const { got, want, sources } = await check(drill, E.dates, E.long)
    expect(got).toEqual(want)
    // 409 terms × 20 paths: the fleet root's heavy members read the tiers' catalog; `nomatch` is no member.
    expect(sources).toEqual({ roots: 7826, rollup: 77, catalog: 77, plain: 180, none: 20 })
  }, 60_000)

  it('the dispatch and every rollup header = the Python `TieredDrill`', async () => {
    const drill = wired()
    const got: unknown[] = [], want: unknown[] = []
    for (const t of terms()) for (const P of E.paths.slice(1)) {
      const a = await drill.view(t, P)
      // A long non-member: the light reader's (the Python reader answers its empty roots).
      if (a.source === 'none') { got.push([t, P, 'none']); want.push([t, P, NONMEMBERS.includes(t) ? 'none' : 'a member']); continue }
      got.push([t, P, a.source === 'plain' ? ['plain'] : [a.source, a.upper, ...(a.source === 'rollup' ? [[a.rollup.kept, a.rollup.rows, a.rollup.children]] : [])]])
      want.push([t, P, E.py.run2[t][P]])
    }
    expect(got).toEqual(want)
  }, 60_000)

  it('run 2\'s drill not there yet: the base and run 1, their scans exact and the dispatch = Python\'s over the same tiers', async () => {
    const drill = wired({ hide: [`${RUN2}/drill/meta.json`] })
    const st = await drill.state()
    const dates = E.dates.slice(0, 4)
    expect([st.version, st.tiers.map(x => x.dir), st.scans]).toEqual([RUN1, [null, RUN1], dates])
    const { got, want } = await check(drill, dates, E.members1)
    expect(got).toEqual(want)
    const dispatch: unknown[] = [], py: unknown[] = []
    for (const t of [...E.members1, ...E.short]) for (const P of E.paths.slice(1)) {
      const a = await drill.view(t, P)
      dispatch.push([t, P, a.source === 'plain' ? ['plain'] : a.source === 'none' ? ['none'] : [a.source, a.upper, ...(a.source === 'rollup' ? [[a.rollup.kept, a.rollup.rows, a.rollup.children]] : [])]])
      py.push([t, P, E.py.run1[t][P]])
    }
    expect(dispatch).toEqual(py)
  }, 60_000)

  it('a run without its drill stops the tiers there, even with a later run\'s live; no runs = the base alone', async () => {
    const at = async (hide: string[], runs = true) => {
      const blobs = blobsOf({ hide })
      const t = tiers(blobs)
      const st = await new Drill(blobs, t.catalog, runs ? { runs: async () => (await t.tiers.state()).tiers.flatMap(x => x.dir ? [x.dir] : []) } : {}).state()
      return [st.version, st.tiers.map(x => x.dir), st.scans]
    }
    expect([await at([`${RUN1}/drill/meta.json`]), await at([], false)]).toEqual([
      ['base', [null], E.dates.slice(0, 3)],
      ['base', [null], E.dates.slice(0, 3)],
    ])
  })
})

describe('DrillSource over the runs', () => {
  it('answers carry the live tiers\' scans: `covers` takes run 1\'s scan and declines run 2\'s until its drill lands', async () => {
    const [a, b] = await Promise.all([wired(), wired({ hide: [`${RUN2}/drill/meta.json`] })].map(d => new DrillSource(d).hits('ckpt', 'b1/runs')))
    expect([a!.rollup ? 'rollup' : 'hits', a!.scans, covers(a!, [E.dates[4]]), b!.scans, covers(b!, [E.dates[3]]), covers(b!, [E.dates[4]])])
      .toEqual([E.py.run2.ckpt['b1/runs'][0] === 'rollup' ? 'rollup' : 'hits', E.dates, true, E.dates.slice(0, 4), true, false])
  })

  it('a middle run without its drill (cw 10-10: a merged run built before drill was on), a later run\'s live: the drill covers the base\'s scans only', async () => {
    const blobs = blobsOf({ hide: [`${RUN1}/drill/meta.json`] })
    const t = tiers(blobs)
    const runs = async () => (await t.tiers.state()).tiers.flatMap(x => x.dir ? [x.dir] : [])
    const src = drillSource(blobs, undefined, { runs, catalog: t.catalog })
    const [below, root] = [(await src.hits('ckpt', 'b1/runs'))!, (await src.hits('ckpt', ''))!]
    expect([below.scans, root.scans, covers(below, E.dates.slice(3, 4)), covers(below, E.dates.slice(4)), covers(root, E.dates.slice(4)), fromDrill(below)])
      .toEqual([E.dates.slice(0, 3), E.dates.slice(0, 3), false, false, false, true])
  })

  it('roots answers in the colo cache are keyed by the newest tier (the base\'s keep their bare key)', async () => {
    const held = new Map<string, string>()
    const colo = {
      async match(url: string) { const b = held.get(url); return b == null ? undefined : new Response(b) },
      async put(url: string, r: Response) { held.set(url, await r.text()) },
    } as unknown as Cache
    const blobs = blobsOf()
    const t = tiers(blobs)
    const runs = async () => (await t.tiers.state()).tiers.flatMap(x => x.dir ? [x.dir] : [])
    await drillSource(blobs, { cache: colo, prefix: 'g' }, { runs, catalog: t.catalog }).hits('zebra', 'b2')
    await drillSource(blobs, { cache: colo, prefix: 'g' }).hits('ckpt', 'b2/mix')
    expect([...held.keys()].filter(u => u.includes('/roots-v1/')).map(u => decodeURIComponent(u.split('/roots-v1/')[1])))
      .toEqual([`zebra\0b2@${RUN2}.json`, 'ckpt\0b2/mix.json'])
  })
})

describe('a broken drill tier: cut there, the scans before it answer exactly, never a failure', () => {
  let logged: string[] = []
  beforeAll(() => { vi.spyOn(console, 'error').mockImplementation((m: unknown) => { logged.push(String(m)) }) })
  afterEach(() => { logged = [] })
  afterAll(() => { vi.restoreAllMocks() })
  const THROUGH_RUN1 = () => E.dates.slice(0, 4)
  const drillMsg = (dir: string, error: string) => `static drill: ${dir} is broken; its scans and every later tier's are not indexed until it loads: ${error}`
  const tops = (dir: string) => ['long', 'short'].flatMap(k => ['roots', 'rollups'].map(x => `${dir}/drill/${k}-${x}-index.top.parquet`))

  it.each([
    ['its index files missing (its `meta.json` there)', { hide: tops(RUN2) }, `no fixture ${RUN2}/drill/long-roots-index.top.parquet`],
    ['its index files corrupt', { corrupt: tops(RUN2) }, 'parquet file invalid (footer != PAR1)'],
  ])('run 2\'s drill with %s: the first view that reads it cuts the tiers at it', async (_, damage, error) => {
    // The drill's own clock: the views below read the cut state however long they take in real time (a loaded
    // machine took past its 30 s `brokenTtlMs`, and the direct `view`s, which don't retry, met run 2 again).
    const drill = wired({ ...damage, now: () => 0 })
    const src = new DrillSource(drill)
    expect((await drill.state()).tiers.map(x => x.dir)).toEqual([null, RUN1, RUN2])
    const found = await src.hits('ckpt', 'b1/runs')
    const st = await drill.state()
    expect([found!.scans, st.version, st.tiers.map(x => x.dir), st.scans, logged]).toEqual([THROUGH_RUN1(), RUN1, [null, RUN1], THROUGH_RUN1(), [drillMsg(RUN2, error)]])
    // Every view on the scans through run 1 = brute force (as if run 2's drill weren't there yet), with the wall
    // clock past the cut's life by then.
    const t1 = Date.now() + 31_000
    const slow = vi.spyOn(Date, 'now').mockImplementation(() => t1)
    try {
      const { got, want } = await check(drill, THROUGH_RUN1(), E.members1)
      expect(got).toEqual(want)
    } finally { slow.mockRestore() }
  }, 60_000)

  it('run 2\'s drill index files missing, the cut\'s life over: the next answer re-checks run 2, cuts it again (logged again), and answers through run 1', async () => {
    let clock = 0
    const drill = wired({ hide: tops(RUN2), now: () => clock })
    const src = new DrillSource(drill)
    const first = await src.hits('ckpt', 'b1/runs')
    clock = 30_000
    const relisted = (await drill.state()).tiers.map(x => x.dir)
    const again = await src.hits('ckpt', 'b1/runs')
    const st = await drill.state()
    const msg = drillMsg(RUN2, `no fixture ${RUN2}/drill/long-roots-index.top.parquet`)
    expect([first!.scans, relisted, again!.scans, st.tiers.map(x => x.dir), logged]).toEqual([THROUGH_RUN1(), [null, RUN1, RUN2], THROUGH_RUN1(), [null, RUN1], [msg, msg]])
  })

  it('run 1\'s drill `meta.json` corrupt: the base alone, logged', async () => {
    const drill = wired({ corrupt: [`${RUN1}/drill/meta.json`] })
    const st = await drill.state()
    const found = await new DrillSource(drill).hits('ckpt', 'b1/runs')
    expect([st.version, st.tiers.map(x => x.dir), st.scans, found!.scans, logged.map(m => m.slice(0, drillMsg(RUN1, '').length))])
      .toEqual(['base', [null], E.dates.slice(0, 3), E.dates.slice(0, 3), [drillMsg(RUN1, '')]])
  })

  it('the base\'s drill `meta.json` missing: no tier, every heavy view declines (null)', async () => {
    const drill = wired({ hide: ['drill/meta.json'] })
    const st = await drill.state()
    expect([st.version, st.tiers, st.scans, await new DrillSource(drill).hits('ckpt', 'b1/runs'), logged]).toEqual(['none', [], [], null, [drillMsg('the base', 'no fixture drill/meta.json')]])
  })

  it('run 2\'s light catalog broken: the drill follows the light tiers\' cut at once (its runs changed)', async () => {
    const drill = wired({ hide: [`${RUN2}/catalog/meta.json`] })
    const st = await drill.state()
    expect([st.version, st.tiers.map(x => x.dir), st.scans]).toEqual([RUN1, [null, RUN1], THROUGH_RUN1()])
    const { got, want } = await check(drill, THROUGH_RUN1(), E.members1)
    expect(got).toEqual(want)
  }, 60_000)

  it('run 2\'s catalog index corrupt, found at the fleet root: that answer covers the scans through run 1 only, and is not held', async () => {
    const drill = wired({ corrupt: [`${RUN2}/catalog/index.parquet`] })
    const src = new DrillSource(drill)
    // The drill's own tiers are whole: the catalog's cut narrows the fleet root's answer.
    expect((await drill.state()).tiers.map(x => x.dir)).toEqual([null, RUN1, RUN2])
    const healthy = wired()
    const t = (await Promise.all(E.long.map(async t => [t, (await healthy.view(t, '')).source] as const))).find(([, s]) => s === 'catalog')![0]
    const found = await src.hits(t, '')
    const rows = THROUGH_RUN1().map(d => { const r = rollupTotal(found!.rollup!, d); return [d, [r.b, r.o]] })
    expect([found!.io.source, found!.scans, rows]).toEqual(['catalog', THROUGH_RUN1(), THROUGH_RUN1().map(d => [d, total(brute(t, '', d))])])
    expect(logged.map(m => m.split(' is broken')[0])).toEqual([`static tiers: ${RUN2}`])
  })
})

describe('a refresh abandoned by its request (its I/O frozen) is never awaited by the next one (`shared.ts`)', () => {
  it.each(['cancel', 'end'] as const)('request A\'s drill state hangs on `meta.json` and A %ss: request B builds its own', async how => {
    const fresh = await wired().state()
    const blobs = hangFirst(blobsOf(), (method, key) => method === 'json' && key === 'drill/meta.json')
    const t = tiers(blobs)
    const drill = new Drill(blobs, t.catalog, { runs: async req => (await t.tiers.state(req)).tiers.flatMap(x => x.dir ? [x.dir] : []) })
    const got = await abandoned(req => drill.state(req).then(s => [s.version, s.scans]), blobs.hung, how, how === 'cancel' ? BOUND / 2 : 5 * BOUND)
    expect(got).toEqual([fresh.version, fresh.scans])
    expect(fresh.version).toBe(RUN2)
  })
})

describe('requests sharing the isolate\'s memos while every read outlasts the join bound (`shared.ts`)', () => {
  it('concurrent requests, some cancelled mid-read, joining and convicting each other\'s reads: every view = the lone reader\'s', async () => {
    const lone = wired()
    const terms = [...E.long.slice(0, 12), ...E.short.slice(0, 4), ...NONMEMBERS]
    const want: unknown[] = []
    // `io.top` says where a group index came from (this isolate's memo or its file): under concurrency that is the race's
    // outcome, not the answer, so it is left out of the comparison.
    const answer = (x: unknown): unknown => Array.isArray(x) ? x.map(answer)
      : x && Object.getPrototypeOf(x) === Object.prototype ? Object.fromEntries(Object.entries(x).map(([k, v]) => [k, k === 'io' && v && typeof v === 'object' ? { ...v, top: undefined } : answer(v)]))
      : x
    for (const t of terms) for (const P of E.paths) { const a = answer(await lone.view(t, P)); want.push([t, P, a], [t, P, a], [t, P, a]) }
    // Each read settles after 1–4 ms (in a fixed order), past the 1 ms join bound: joiners convict owners at every turn.
    const b = blobsOf()
    let i = 0
    const later = <T>(go: () => Promise<T>) => new Promise<T>((ok, no) => setTimeout(() => go().then(ok, no), 1 + (i++ % 4)))
    const slow: Blobs = {
      range: (k, o, l) => later(() => b.range(k, o, l)), suffix: (k, n) => later(() => b.suffix(k, n)),
      json: <T>(k: string) => later(() => b.json<T>(k)), list: p => later(() => b.list!(p)),
    }
    const t = tiers(slow)
    const drill = new Drill(slow, t.catalog, { runs: async req => (await t.tiers.state(req)).tiers.flatMap(x => x.dir ? [x.dir] : []) })
    const src = new DrillSource(drill)
    const join0 = { ...JOIN }
    Object.assign(JOIN, { min: 1, max: 1, cold: 1, budget: 4 })
    try {
      const got: unknown[] = []
      let joins = 0, evicts = 0
      for (const term of terms) for (const P of E.paths) {
        // Four requests at once: two run to the end, one is cancelled mid-read, one carries no request.
        const ctl = new AbortController()
        const reqs = [new Req(), new Req(), new Req(ctl.signal), undefined]
        for (const r of reqs) if (r) r.trace = name => { if (name === 'join-evict') evicts++; else if (name.startsWith('join-')) joins++ }
        const views = reqs.map(async r => src.answer(term, P, await drill.state(r), undefined, r))
        setTimeout(() => ctl.abort(), 2)
        const [a, b2, , d] = (await Promise.allSettled(views)).map(x => x.status === 'fulfilled' ? x.value : x.reason)
        got.push([term, P, answer(a)], [term, P, answer(b2)], [term, P, answer(d)])
      }
      expect(got).toEqual(want)
      // The setup did what it says: requests joined others' reads, and convicted their owners.
      expect([joins > 0, evicts > 0]).toEqual([true, true])
    } finally {
      Object.assign(JOIN, join0)
    }
  }, 60_000)
})

describe('mutations: each reader rule, broken, makes the views wrong', () => {
  /** Views (term × path × scan) a reader with `rules` gets wrong, or fails on. */
  async function wrong(rules: DrillRules): Promise<number> {
    const drill = wired({ rules })
    let bad = 0
    for (const t of terms()) for (const P of E.paths) {
      let a: DrillAnswer
      try { a = await drill.view(t, P) } catch { bad += E.dates.length; continue }
      for (const d of E.dates) if (JSON.stringify(asRead(a, P, d)) !== JSON.stringify(asBrute(a, t, P, d, E.long))) bad++
    }
    return bad
  }

  it('the smallest `vt`, the stop at a full header, each tier\'s own alias map, and the summed dispatch bound', async () => {
    const got = {
      rules: await wrong(DRILL_RULES),
      // A root's newest `vt` instead (a close record loses to the base's open row).
      maxVt: await wrong({ ...DRILL_RULES, combine: parts => { const m = new Map<string, Hit>(); for (const h of parts.flat()) { const k = `${h.path}\0${h.usr}\0${h.vf}`; const o = m.get(k); if (!o || h.vt > o.vt) m.set(k, h) } return [...m.values()] } }),
      // Every tier's cells, past a restatement.
      noStop: await wrong({ ...DRILL_RULES, stack: parts => { const ps = parts.filter(p => p.length); return ps.length ? { head: ps[ps.length - 1][0], cells: ps.flatMap(p => p.slice(1)) } : null } }),
      // The newest tier's canonical in every tier (a split class's base rows are its old canonical's).
      newestCanon: await wrong({ ...DRILL_RULES, canon: (maps, _i, t) => maps[maps.length - 1].get(t)?.c ?? t }),
      // The base's canonical in every tier.
      baseCanon: await wrong({ ...DRILL_RULES, canon: (maps, _i, t) => maps[0].get(t)?.c ?? t }),
      // The base's own bound (`dispatch_rows`), not the tiers' sum: views over it with no rollup.
      baseBound: await wrong({ ...DRILL_RULES, bound: metas => metas[0].dispatch_rows }),
    }
    expect(got).toEqual({ rules: 0, maxVt: 2075, noStop: 107, newestCanon: 139, baseCanon: 38, baseBound: 20 })
  }, 120_000)
})
