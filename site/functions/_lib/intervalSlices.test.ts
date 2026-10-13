import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { IV_JOIN, openIndex, slices } from './index'
import { type Req, reqOf, withReq } from './shared'
import type { OwnerScope } from './scope'
import { LEDGER_KIND, sqliteD1 } from './testD1'
import { type D1Variant, fixture, GETS, readJson, seedGeneration } from './testStore'
import { buildDiff, buildView, readRootAgg } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// Owner-scoped reads from the interval store's slice sorts (specs/interval-store.md §2.1): `fixtures/iv/
// interval-store/g2` (`fixtures/iv/gen.py g2`) is one generation over three scans of the per-scan store
// fixtures — `v2-slices` (multi-owner slices), `v2-lens`, `v2-slices` again. Every lens view, owner pool,
// root total and lens diff it serves must equal the per-scan reader's over the same per-scan files, and
// must read nothing per-scan.

const [S1, S2, S3] = ['2026-09-30T0007', '2026-09-30T0008', '2026-09-30T0009']
const base = { w: 1280, h: 800, minArea: 12, atten: 1 }
let perScan: Env
let iv: Env
const r2Reads: string[] = []

/** `INDEX_R2` over `fixtures/iv/`. */
function r2(): R2Bucket {
  const get = async (key: string, o?: { range?: { offset: number; length: number } | { suffix: number } }) => {
    const mod = 'node:fs'
    const fs = (await import(/* @vite-ignore */ mod)) as { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array }
    const file = fixture(`iv/${key}`)
    if (!fs.existsSync(file)) return null
    r2Reads.push(key)
    const buf = fs.readFileSync(file)
    const r = o?.range
    const { offset = 0, length = buf.byteLength - offset } = r && 'suffix' in r ? { offset: Math.max(0, buf.byteLength - r.suffix) } : r ?? {}
    const part = buf.slice(offset, offset + length)
    return { size: buf.byteLength, arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength), json: async () => JSON.parse(new TextDecoder().decode(part)) }
  }
  // `list` / `head` over the fixture dir: the store's runs (`g5`) are found by their manifests.
  const list = async ({ prefix, cursor }: { prefix: string; cursor?: string }) => {
    const mod = 'node:fs'
    const fs = (await import(/* @vite-ignore */ mod)) as { existsSync(p: string): boolean; readdirSync(p: string, o: { recursive: true }): string[]; statSync(p: string): { isFile(): boolean } }
    const dir = prefix.slice(0, prefix.lastIndexOf('/') + 1)
    const root = fixture(`iv/${dir}`)
    const keys = fs.existsSync(root) ? fs.readdirSync(root, { recursive: true }).map(f => `${dir}${f}`).filter(k => k.startsWith(prefix) && fs.statSync(fixture(`iv/${k}`)).isFile()).sort() : []
    return { objects: keys.map(key => ({ key })), truncated: false, cursor }
  }
  const head = async (key: string) => {
    const mod = 'node:fs'
    const fs = (await import(/* @vite-ignore */ mod)) as { existsSync(p: string): boolean }
    return fs.existsSync(fixture(`iv/${key}`)) ? { key } : null
  }
  return { get, list, head } as unknown as R2Bucket
}

const files = (dir: string, sorts: Record<string, string>) =>
  Object.fromEntries(Object.entries(sorts).map(([v, stem]) => [v, { parquet: `${dir}/${stem}.parquet`, groups: `${dir}/${stem}.groups.json` }]))

/** The per-scan stores and the interval store over the same scans, on one D1 with the gcs ledger's tables
 *  and `ledger` (SQL) in them. */
async function seed(ledger = ''): Promise<{ perScan: Env; iv: Env }> {
  const { db, raw } = await sqliteD1('cw')
  const slicesD1 = await readJson<Record<string, D1Variant>>('v2-slices/d1.json')
  const lensD1 = await readJson<Record<string, D1Variant>>('v2-lens/d1.json')
  for (const date of [S1, S3]) seedGeneration(raw, { date, gen: 'g', dir: `listing/${date}/index/g`, variants: slicesD1, files: files('v2-slices', { path: 'path-index', bysize: 'path-index-bysize' }) })
  raw.exec(`
    CREATE TABLE actions (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, ts INTEGER NOT NULL, scan TEXT NOT NULL, pattern TEXT NOT NULL, set_owner INTEGER NOT NULL DEFAULT 0, owner TEXT);
    CREATE TABLE owner_prefixes (action_id INTEGER NOT NULL REFERENCES actions (id), prefix TEXT NOT NULL, owner TEXT, ts INTEGER NOT NULL, tombstoned TEXT, PRIMARY KEY (prefix, action_id));
    ${LEDGER_KIND}
    CREATE TABLE owner_totals (scan TEXT NOT NULL, head INTEGER NOT NULL, body TEXT NOT NULL, claims TEXT NOT NULL, computed_ts INTEGER, ms INTEGER, PRIMARY KEY (scan, head));
    ${ledger}
  `)
  seedGeneration(raw, { date: S2, gen: 'g', dir: `listing/${S2}/index/g`, variants: lensD1, files: files('v2-lens', { path: 'path-index', bysize: 'path-index-bysize', 'bysize-user': 'path-index-bysize-by-user' }) })
  const perScan = { DB: db, ROOT_LABEL: 'root', BASE_SCOPE: 'gcs', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' } as Env
  return { perScan, iv: { ...perScan, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g2', INDEX_R2: r2() } as Env }
}

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  // The ledger's tables, empty: owners are the scans' own attribution.
  ;({ perScan, iv } = await seed())
})

/** What a read did per-scan: the store's `listing/` keys it fetched. */
async function perScanGets<T>(f: () => Promise<T>): Promise<[T, string[]]> {
  const n = GETS.length
  const v = await f()
  return [v, [...new Set(GETS.slice(n).map(g => g.key))]]
}

const PATHS = ['', 'bk', 'bk/m', 'bk/m/deep', 'bk/nest', 'bk/flat']

/** The store's kind for a path that is both an object and a prefix (`bk/m/twin`: alice's 50 KiB object
 *  and 600 KiB dir; the fixture's only node named `twin`) is `dir`, its rows folded into one slice; the
 *  per-scan reader takes the kind of whichever row it merged last (`file` here). Every other field of
 *  the tile is the same. */
function storeKinds<T>(v: T): T {
  type N = { n?: string; k?: string; c?: N[] }
  const fix = (n: N): N => ({ ...n, ...(n.n === 'twin' ? { k: 'dir' } : {}), ...(n.c ? { c: n.c.map(fix) } : {}) })
  return (v && typeof v === 'object' && 'n' in (v as object) ? fix(v as N) : v) as T
}
const SCOPES: { lens?: { key: string }; owner?: OwnerScope }[] = [
  { lens: { key: 'alice' } }, { lens: { key: 'bob' } }, { lens: { key: 'carol' } },
  { owner: 'owned' }, { owner: 'unowned' }, { owner: { not: ['alice'] } },
]

// Each suite runs over `g2` (one generation of the three scans) and `g5` (a base of two and a per-scan run of the third,
// `interval_append`): the same answers.
for (const GEN of ['g2', 'g5'] as const) {
  const ivg = (): Env => ({ ...iv, INTERVAL_STORE_GEN: GEN }) as Env
  describe(`interval store ${GEN}: owner slices`, () => {
    it('opens the slice sorts for a sliced read of a held date, and refuses its v1 `user` sort', async () => {
      const h = await openIndex(slices(ivg()), S1, 'bysize-user')
      expect([h.mode, h.variant, h.version, h.asOf, h.gen, h.columns?.includes('usr')]).toEqual(['pq', 'bysize-user', 3, 1790726820, `iv:${GEN}:s`, true])
      await expect(openIndex(slices(ivg()), S1, 'user')).rejects.toThrow(`index variant 'user' not synced for ${S1}`)
    })

    it('reads a user\'s view from the user-first size sort, as the per-scan reader does', async () => {
      // The path-first slice sort (`slices-bytotal`) mixes users in every group: a big user's root there
      // selects most groups over the threshold (gcs: refused as too wide on 2 of 5 v1 scans). The store
      // plans a big subtree on its size sort alone (`ivPlanRows`, lowered here to reach that read).
      const o = { ...base, date: S2, path: 'bk/nest', lens: { key: 'alice' }, ivPlanRows: 0 }
      const [got, want] = [await buildView(ivg(), o), await buildView(perScan, o)]
      expect([got.index, got.tier, got.tree]).toEqual([`iv:${GEN}`, 'bysize-user', storeKinds(want.tree)])
    })

    it('serves every lens view and owner pool of every scan as the per-scan reader draws it, reading nothing per-scan', async () => {
      let drawn = 0
      for (const date of [S1, S2, S3]) {
        for (const path of PATHS) {
          for (const scope of SCOPES) {
            const o = { ...base, date, path, ...scope }
            const want = await buildView(perScan, o).catch(e => String(e))
            const [got, gets] = await perScanGets(() => buildView(ivg(), o).catch(e => String(e)))
            const tree = (v: typeof want) => (typeof v === 'string' ? v : v.tree)
            expect({ o, tree: tree(got), gets }).toEqual({ o, tree: storeKinds(tree(want)), gets: [] })
            if (typeof got !== 'string' && typeof want !== 'string') {
              // `none`: nothing in scope (an owner with no bytes there), drawn without a read.
              expect([got.index, want.index === 'none']).toEqual([want.index === 'none' ? 'none' : `iv:${GEN}`, want.index === 'none'])
              drawn += got.index === 'none' ? 0 : 1
            }
          }
        }
      }
      expect(drawn).toBe(49)
    }, 60_000)  // every scan × scope against the per-scan reader: seconds alone, past the 5 s default under the parallel suite

    it('totals a root by owner as the per-scan reader does', async () => {
      for (const date of [S1, S2, S3]) {
        for (const path of ['', 'bk', 'bk/m', 'bk/nest']) {
          for (const scope of SCOPES) {
            const o = { date, path, ...scope }
            expect({ o, agg: await readRootAgg(ivg(), o) }).toEqual({ o, agg: await readRootAgg(perScan, o) })
          }
        }
      }
    })

    it('diffs a lens and a pool across scans as the per-scan reader does', async () => {
      for (const [from, to] of [[S1, S2], [S2, S3], [S1, S3]]) {
        for (const scope of SCOPES) {
          const o = { ...base, from, to, path: '', top: 500, ...scope }
          // A lens with nothing on either side is a 404 from both.
          const shape = (d: Awaited<ReturnType<typeof buildDiff>> | string) => typeof d === 'string' ? d
            : { rows: d.rows.map(r => r.p.split('/').pop() === 'twin' ? { ...r, k: 'dir' } : r), totals: [d.total_a, d.total_b, d.objects_a, d.objects_b] }
          // The store's read first: a per-scan diff that throws on one side leaves the other side's reads in flight.
          const [got, gets] = await perScanGets(() => buildDiff(ivg(), o).catch(e => `${(e as Error).name}: ${(e as Error).message}`))
          const want = await buildDiff(perScan, o).catch(e => `${(e as Error).name}: ${(e as Error).message}`)
          expect({ o, got: shape(got), gets }).toEqual({ o, got: shape(want), gets: [] })
        }
      }
    })

    it('reads the slice sorts, never the folded ones, for owner-scoped reads', () => {
      const sorts = ['slices-bysize-user', 'slices-bytotal', 'slices'].flatMap(x => [`${x}.groups.parquet`, `${x}.parquet`])
      const dirs = [`interval-store/${GEN}/served/`, ...(GEN === 'g5' ? [`interval-store/${GEN}/deltas/${S3}/served/`] : [])]
      expect([...new Set(r2Reads.filter(k => k.startsWith(`interval-store/${GEN}/`) && k.includes('/served/')))].sort()).toEqual(dirs.flatMap(d => sorts.map(x => d + x)).sort())
    })
  })

  // A user lens's assigned regions (specs/interval-store.md §2.6): on S2 (`v2-lens`, where bob holds all of
  // `bk/flat`'s 8,000 files) carol is assigned 26 of them — 24 of 32 KiB, and `f00000` and `f00016` of one
  // byte. A view reads the largest `REGION_READS` (24) regions and values the rest from the assignments'
  // totals: two unread regions with no row of their own. The interval store reads the regions from its
  // folded `bysize` (one exact row per path: a path-first read of the slice sorts selected a run per depth
  // per dyadic segment, and a big user's root regions were refused as too wide).
  describe(`interval store ${GEN}: a lens's assigned regions`, () => {
    const FILES: [string, number][] = [
      ...Array.from({ length: 24 }, (_, i): [string, number] => [`bk/flat/f${String(16 * i + 15).padStart(5, '0')}`, 32768]),
      ['bk/flat/f00000', 1],
      ['bk/flat/f00016', 1],
    ]
    const claims = FILES.map(([p, bytes], i) => ({ prefix: `gs://${p}`, kind: 'object', owner: 'carol', ts: 1, action_id: i + 1, bytes, objects: 1, us: { bob: bytes }, uo: { bob: 1 } }))
    const ledger = [
      ...claims.map(c => `INSERT INTO actions (id, actor, ts, scan, pattern, set_owner, owner, kind) VALUES (${c.action_id}, 'ann', 1, '${S2}', '${c.prefix}', 1, 'carol', 'object');`),
      ...claims.map(c => `INSERT INTO owner_prefixes (action_id, prefix, owner, ts, kind) VALUES (${c.action_id}, '${c.prefix}', 'carol', 1, 'object');`),
      `INSERT INTO owner_totals (scan, head, body, claims) VALUES ('${S2}', ${claims.length}, '{}', '${JSON.stringify(claims)}');`,
    ].join('\n')
    let envs: { perScan: Env; iv: Env }
    // The store's reads from here on (the groups each test decodes are cached for the next).
    let n0 = 0
    beforeAll(async () => { envs = await seed(ledger); n0 = r2Reads.length })
    const lens = { key: 'carol' }
    type N = { n: string; k: string; b: number; o: number; c?: N[] }
    const shape = (n: N): unknown => [n.n, n.k, n.b, n.o, ...(n.c ? [n.c.map(shape)] : [])]
    const byName = (n: N): N => ({ ...n, ...(n.c ? { c: [...n.c].sort((x, y) => (x.n < y.n ? -1 : 1)).map(byName) } : {}) })
    const tiles = FILES.map(([p, b]) => [p.split('/').pop(), 'file', b, 1]).sort((x, y) => (x[0]! < y[0]! ? -1 : 1))

    it('values an unread region once: its objects from the assignment, never added again to it or its ancestors', async () => {
      for (const env of [envs.perScan, { ...envs.iv, INTERVAL_STORE_GEN: GEN } as Env]) {
        const flat = await buildView(env, { ...base, date: S2, path: 'bk/flat', lens, threshold: 1 })
        expect(shape(byName(flat.tree as N))).toEqual(['flat', 'dir', 24 * 32768 + 2, 26, tiles])
        const bk = await buildView(env, { ...base, date: S2, path: 'bk', lens, threshold: 1 })
        expect(shape(byName(bk.tree as N))).toEqual(['bk', 'dir', 24 * 32768 + 2, 26, [['flat', 'dir', 24 * 32768 + 2, 26, tiles]]])
      }
    })

    it('draws them from the folded `bysize` on the interval store, as the per-scan reader does', async () => {
      for (const path of ['', 'bk', 'bk/flat']) {
        const o = { ...base, date: S2, path, lens }
        const want = await buildView(envs.perScan, o)
        const [got, gets] = await perScanGets(() => buildView({ ...envs.iv, INTERVAL_STORE_GEN: GEN } as Env, o))
        expect({ path, tree: got.tree, index: got.index, gets }).toEqual({ path, tree: storeKinds(want.tree), index: `iv:${GEN}`, gets: [] })
      }
      // carol's own rows come from the slice sort, whose footer says she has none: its groups go unread. On g5 the
      // run (S3's, begun after S2) is not read either: a view reads the runs begun by its scan (`asOfScans`).
      expect([...new Set(r2Reads.slice(n0).filter(k => k.startsWith(`interval-store/${GEN}/`) && k.endsWith('.parquet') && !k.endsWith('.groups.parquet')))].sort()).toEqual([
        `interval-store/${GEN}/served/bysize.parquet`,
      ])
    })
  })

  // Unscoped diffs between the store's scans (specs/interval-store.md §2.6): a name one side lacks is looked
  // up on the other within the version window the drawn side's row implies (`Ask.vtLe` / `vfGe`), or is
  // that very row when its version is live at the other scan. The answers are the per-scan reader's. (Here
  // S1/S3 and S2 share only `bk`: every lookup finds the name absent; `intervalStore.test.ts` checks
  // bounded lookups that find rows.)
  describe(`interval store ${GEN}: unscoped diffs`, () => {
    it('diffs pairs of scans as the per-scan reader does, lookups and all', async () => {
      for (const [from, to] of [[S1, S2], [S2, S3], [S1, S3], [S2, S1]]) {
        for (const path of ['', 'bk', 'bk/m', 'bk/nest']) {
          for (const minArea of [12, 400]) {
            const o = { ...base, minArea, from, to, path, top: 500 }
            const shape = (d: Awaited<ReturnType<typeof buildDiff>> | string) => typeof d === 'string' ? d
              : { rows: d.rows.map(r => r.p.split('/').pop() === 'twin' ? { ...r, k: 'dir' } : r), totals: [d.total_a, d.total_b, d.objects_a, d.objects_b], lookups: d.lookups }
            const [got, gets] = await perScanGets(() => buildDiff(ivg(), o).catch(e => `${(e as Error).name}: ${(e as Error).message}`))
            const want = await buildDiff(perScan, o).catch(e => `${(e as Error).name}: ${(e as Error).message}`)
            expect({ o, got: shape(got), gets }).toEqual({ o, got: shape(want), gets: [] })
          }
        }
      }
    }, 60_000)  // every scan × scope against the per-scan reader: seconds alone, past the 5 s default under the parallel suite
  })
}

// A request's interval-store reads are shared with the isolate's other requests while in flight: the store's state,
// handles, footers, footer groups and row groups. Workers freeze a cancelled request's promises, so a viewer leaving
// a slow lens view left them pending for good, and every later lens read in the isolate waited on them with no
// response (gcs, 2026-10-10: ~125 s, no headers). Here a first request's R2 reads at one stage never return (its
// request cancelled, frozen); the same read again must answer exactly as the per-scan reader does, once it has
// waited `IV_JOIN` on each frozen read and read for itself.
describe('interval store g5: a lens read joining a cancelled request\'s frozen reads', () => {
  type Range = { offset: number; length: number } | { suffix: number }
  const STAGES: [string, (key: string, range?: Range) => boolean][] = [
    ['state', k => k.endsWith('/scans.json')],
    ['footer', (k, r) => k.endsWith('.groups.parquet') && !!r && 'suffix' in r],
    ['footer group', (k, r) => k.endsWith('.groups.parquet') && !!r && 'offset' in r],
    ['row group', k => /\/slices[^/]*\.parquet$/.test(k) && !k.endsWith('.groups.parquet')],
  ]
  const lens = { key: 'alice' }
  type Answer = Awaited<ReturnType<typeof buildView>> | Awaited<ReturnType<typeof buildDiff>>
  const READS: [string, (e: Env) => Promise<Answer>, (a: Answer) => unknown][] = [
    ['view', e => buildView(e, { ...base, date: S3, path: '', lens }), a => storeKinds((a as Awaited<ReturnType<typeof buildView>>).tree)],
    ['diff', e => buildDiff(e, { ...base, from: S1, to: S3, path: '', top: 500, lens }), a => {
      const d = a as Awaited<ReturnType<typeof buildDiff>>
      return { rows: d.rows.map(r => r.p.split('/').pop() === 'twin' ? { ...r, k: 'dir' } : r), totals: [d.total_a, d.total_b, d.objects_a, d.objects_b] }
    }],
  ]
  const join0 = { ...IV_JOIN }
  beforeAll(() => { Object.assign(IV_JOIN, { min: 100, max: 100, cold: 100 }) })
  afterAll(() => { Object.assign(IV_JOIN, join0) })
  const after = <T>(ms: number, v: T) => new Promise<T>(r => setTimeout(() => r(v), ms))

  for (const [stage, freeze] of STAGES) {
    for (const [kind, read, shape] of READS) {
      it(`${stage}: a lens ${kind} answers, exactly`, async () => {
        const want = shape(await read(perScan))
        let frozen = true
        let reach!: () => void
        const reached = new Promise<true>(r => { reach = () => r(true) })
        const bucket = r2()
        const get0 = bucket.get.bind(bucket) as (k: string, o?: { range?: Range }) => Promise<unknown>
        const e = {
          ...iv,
          INTERVAL_STORE_GEN: 'g5',
          INTERVAL_STORE_REV: `frozen-${stage}-${kind}`, // this test's own isolate caches
          INDEX_R2: { ...bucket, get: (k: string, o?: { range?: Range }) => (frozen && freeze(k, o?.range) ? (reach(), new Promise(() => {})) : get0(k, o)) },
        } as Env
        void read(e) // the cancelled request: never settles
        expect(await Promise.race([reached, after(2000, false)])).toBe(true)
        await after(20, null) // its other reads, started beside the frozen one, register too
        frozen = false
        const got = await Promise.race([read(e), after(5000, 'no answer' as const)])
        expect(got === 'no answer' ? got : shape(got)).toEqual(want)
      }, 10_000)
    }
  }
})

// The same, after a cancelled request froze at several stages at once — the state, a footer, footer groups and row
// groups each its own memo: prod (2026-10-10) waited a fixed 10 s on each in turn, ~60 s, and later requests found
// the ones it hadn't touched still there. Now the first dead entry a request joins convicts its owner (every entry
// it owns is evicted) once that entry is `IV_JOIN`'s bound old, so the next request waits at most one bound, and a
// third waits on nothing; a cancel (the request's abort signal) evicts them all at once, and nothing waits at all.
describe('interval store g5: the requests after a cancelled read frozen at several stages', () => {
  type Range = { offset: number; length: number } | { suffix: number }
  const BOUND = 1000
  const join0 = { ...IV_JOIN }
  beforeAll(() => { Object.assign(IV_JOIN, { min: BOUND, max: BOUND, cold: BOUND, budget: 10 * BOUND }) })
  afterAll(() => { Object.assign(IV_JOIN, join0) })
  const after = <T>(ms: number, v: T) => new Promise<T>(r => setTimeout(() => r(v), ms))
  const lens = { key: 'alice' }
  const view = (e: Env) => buildView(e, { ...base, date: S3, path: '', lens }).then(a => storeKinds(a.tree))
  const diff = (e: Env) => buildDiff(e, { ...base, from: S1, to: S3, path: '', top: 500, lens }).then(d => ({
    rows: d.rows.map(r => r.p.split('/').pop() === 'twin' ? { ...r, k: 'dir' } : r), totals: [d.total_a, d.total_b, d.objects_a, d.objects_b],
  }))
  /** A store whose first request freezes at two stages at once: its prefetch of the sibling sort's footers (the
   *  base's and the run's, `slices-bytotal`), and its read of the run's row group (`deltas/…/slices.parquet`). */
  function freezing(rev: string) {
    const stage = (k: string, r?: Range): string | null =>
      /\/slices-bytotal\.groups\.parquet$/.test(k) && r && 'suffix' in r ? 'footer'
        : /\/deltas\/[^/]+\/served\/slices\.parquet$/.test(k) ? 'row group' : null
    const froze = new Set<string>()
    let frozen = true
    const bucket = r2()
    const get0 = bucket.get.bind(bucket) as (k: string, o?: { range?: Range }) => Promise<unknown>
    const env = {
      ...iv,
      INTERVAL_STORE_GEN: 'g5',
      INTERVAL_STORE_REV: rev, // this test's own isolate caches
      INDEX_R2: {
        ...bucket,
        get: (k: string, o?: { range?: Range }) => {
          const s = stage(k, o?.range)
          if (frozen && s) {
            froze.add(s)
            return new Promise(() => {})
          }
          return get0(k, o)
        },
      },
    } as Env
    return { env, froze, thaw: () => { frozen = false } }
  }
  const timed = async <T>(p: Promise<T>): Promise<[T, number]> => {
    const t0 = performance.now()
    const got = await p
    return [got, performance.now() - t0]
  }
  const evicts = (r: Req, t: [string, string?][]) => { r.trace = (name, _ms, desc) => { if (name === 'join-evict') t.push([name, desc]) } }

  it('the next request waits out one bound, the one after nothing; both answer exactly', async () => {
    const [wantView, wantDiff] = [await view(perScan), await diff(perScan)]
    const { env, froze, thaw } = freezing('chain')
    void view(withReq(env)) // the cancelled request: never settles
    for (let i = 0; i < 100 && froze.size < 2; i++) await after(20, null)
    await after(50, null) // its other reads, started beside the frozen ones, register too
    expect([...froze].sort()).toEqual(['footer', 'row group'])
    thaw()
    const b = withReq(env), c = withReq(env)
    const tb: [string, string?][] = [], tc: [string, string?][] = []
    evicts(reqOf(b)!, tb)
    evicts(reqOf(c)!, tc)
    const [gotB, msB] = await timed(view(b))
    const [gotC, msC] = await timed(diff(c))
    expect([gotB, gotC, tb.length, tc]).toEqual([wantView, wantDiff, 1, []])
    expect([reqOf(b)!.waited <= BOUND, msB < 2 * BOUND, reqOf(c)!.waited, msC < BOUND]).toEqual([true, true, 0, true])
  }, 30_000)

  it('a cancel evicts its frozen reads at once: the next requests wait on nothing, and answer exactly', async () => {
    const [wantView, wantDiff] = [await view(perScan), await diff(perScan)]
    const { env, froze, thaw } = freezing('abort')
    const ctl = new AbortController()
    void view(withReq(env, ctl.signal))
    for (let i = 0; i < 100 && froze.size < 2; i++) await after(20, null)
    await after(50, null)
    expect([...froze].sort()).toEqual(['footer', 'row group'])
    ctl.abort()
    thaw()
    const b = withReq(env), c = withReq(env)
    const [gotB, msB] = await timed(view(b))
    const [gotC, msC] = await timed(diff(c))
    expect([gotB, gotC, reqOf(b)!.waited, reqOf(c)!.waited, msB < BOUND, msC < BOUND]).toEqual([wantView, wantDiff, 0, 0, true, true])
  }, 30_000)
})
