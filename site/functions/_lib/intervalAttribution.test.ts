import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { asOfScans, ivAttributes, ivAttribution, type IvAttribution } from './index'
import { extrasFor, hasExtras, viewOfRows, type AttrRow } from './extras'
import { FILES, fixture, readJson } from './testStore'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// The interval store's attribution table (specs/attr-interval-table.md), read as of a scan: `fixtures/iv-attr/` holds
// two generations over eight scans (`gen.py`): `g`, whose base has the table and whose four runs are unmerged (the
// uncovered scan's run holds no attribution sort), and `o`, whose table opens at its first run (merged by a carry).
// `expected.json` is each scan's own map (null: uncovered). Every answer must be the scan's map, the per-scan sidecar's
// provenance must agree with the table's, and a generation without the table never reads it.

type Expected = { scans: string[]; maps: Record<string, AttrRow[] | null> }

/** `INDEX_R2` over a fixture dir, logging every key read. */
function r2(dir: string, reads: string[]): R2Bucket {
  type Fs = { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array; readdirSync(p: string, o: { recursive: true }): string[]; statSync(p: string): { isFile(): boolean } }
  const nodeFs = async () => {
    const mod = 'node:fs'
    return (await import(/* @vite-ignore */ mod)) as Fs
  }
  const list = async ({ prefix }: { prefix: string }) => {
    const fs = await nodeFs()
    const d = prefix.slice(0, prefix.lastIndexOf('/') + 1)
    const root = fixture(`${dir}/${d}`)
    const keys = fs.existsSync(root) ? fs.readdirSync(root, { recursive: true }).map(f => `${d}${f}`).filter(k => k.startsWith(prefix) && fs.statSync(fixture(`${dir}/${k}`)).isFile()).sort() : []
    return { objects: keys.map(key => ({ key })), truncated: false }
  }
  const get = async (key: string, o?: { range?: { offset?: number; length?: number; suffix?: number } }) => {
    const fs = await nodeFs()
    const file = fixture(`${dir}/${key}`)
    if (!fs.existsSync(file)) return null
    const buf = fs.readFileSync(file)
    const r = o?.range
    const offset = r?.suffix != null ? Math.max(0, buf.byteLength - r.suffix) : r?.offset ?? 0
    const length = r?.suffix != null ? buf.byteLength - offset : r?.length ?? buf.byteLength - offset
    reads.push(key)
    const part = buf.slice(offset, offset + length)
    return { size: buf.byteLength, arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength), json: async () => JSON.parse(new TextDecoder().decode(part)) }
  }
  const head = async (key: string) => ((await nodeFs()).existsSync(fixture(`${dir}/${key}`)) ? { key } : null)
  return { get, list, head } as unknown as R2Bucket
}

const env = (gen: string, reads: string[] = [], dir = 'iv-attr'): Env => ({ ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: gen, INDEX_R2: r2(dir, reads), GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'b' }) as unknown as Env
/** Reading per-scan stores only: the sidecars. */
const perScan = (): Env => ({ ROOT_LABEL: 'all buckets', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'b' }) as unknown as Env

const PATHS = ['', 'b1', 'b1/gof', 'b1/gof/x', 'b1/d', 'b2', 'b2/e', 'b2/f/y', 'b3']
/** What a view at `p` reads: its subtree's prefixes (`[p, p + '0')`) and its ancestors. */
const scope = (rows: AttrRow[], p: string): AttrRow[] => {
  const hi = p === '' ? '￿' : `${p}0`
  const anc = new Set<string>()
  for (let q = p; q.includes('/');) anc.add(q = q.slice(0, q.lastIndexOf('/')))
  return rows.filter(([k]) => (k >= p && k < hi) || anc.has(k))
}
const asRows = (rows: IvAttribution[]): AttrRow[] => rows.map(r => [r.path, r.user, r.source, r.evidence])
const USERS = ['alice', 'bob', 'carol', 'dave', 'erin', 'mallory']

let want: Expected
beforeEach(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  want ??= await readJson<Expected>('iv-attr/expected.json')
  FILES.clear()
  for (const d of want.scans) {
    if (!want.maps[d]) continue
    for (const n of ['attr.tsv', 'attr.tsv.idx.json']) FILES.set(`listing/${d}/index/g1/${n}`, fixture(`iv-attr/listing/${d}/index/g1/${n}`))
  }
})

describe('the attribution table as of a scan', () => {
  it('answers every covered scan with its own map, at every view root; the uncovered not at all', async () => {
    for (const gen of ['g', 'o']) {
      const e = env(gen)
      const baseNot = gen === 'o' ? new Set(want.scans.slice(0, 4)) : new Set<string>()
      const got: Record<string, Record<string, AttrRow[] | null | undefined>> = {}
      const exp: typeof got = {}
      for (const d of want.scans) {
        got[d] = {}
        exp[d] = {}
        const map = baseNot.has(d) ? null : want.maps[d]
        for (const p of PATHS) {
          const a = await ivAttribution(e, d, p)
          got[d][p] = a == null ? a : asRows(a)
          exp[d][p] = map == null ? undefined : scope(map, p)
        }
      }
      expect([gen, got]).toEqual([gen, exp])
    }
  })

  it('a covered scan with an empty map reads empty (nothing attributed); an uncovered one is unknown', async () => {
    const e = env('g')
    const empty = want.scans.find(d => want.maps[d]?.length === 0)!
    const uncovered = want.scans.filter(d => want.maps[d] === null)
    expect([await ivAttribution(e, empty, ''), await ivAttributes(e, empty), await hasExtras(e, empty)]).toEqual([[], true, true])
    const ex = await extrasFor(e, empty, 'b1')
    expect(USERS.map(u => ex!.provenance('b1/gof', u))).toEqual(USERS.map(() => null))
    for (const d of uncovered) expect([d, await ivAttribution(e, d, ''), await ivAttributes(e, d), await hasExtras(e, d), await extrasFor(e, d, '')]).toEqual([d, undefined, false, false, null])
  })

  it('the table and the per-scan sidecar give the same provenance', async () => {
    const probes = ['b1', 'b1/gof', 'b1/gof/run/x', 'b1/d/z', 'b2/e/q', 'b2/f', 'b3/w']
    for (const d of want.scans.filter(x => want.maps[x])) {
      for (const p of ['', 'b1', 'b2']) {
        const [table, side] = [await extrasFor(env('g'), d, p), await extrasFor(perScan(), d, p)]
        const under = probes.filter(q => p === '' || q === p || q.startsWith(`${p}/`))
        const prov = (v: typeof table) => under.flatMap(q => USERS.map(u => v!.provenance(q, u)))
        expect([d, p, prov(table)]).toEqual([d, p, prov(side)])
        expect([d, p, prov(table)]).toEqual([d, p, prov(viewOfRows(scope(want.maps[d]!, p)))])
      }
    }
  })

  it('a request naming its scans reads the same, without the later runs', async () => {
    const reads: string[] = []
    const d = want.scans[5]
    const e = asOfScans(env('g', reads), [d])
    expect(asRows((await ivAttribution(e, d, ''))!)).toEqual(want.maps[d])
    const later = want.scans.slice(6).map(x => `deltas/${x}/`)
    expect(reads.filter(k => later.some(l => k.includes(l)))).toEqual([])
  })

  it('never reads an uncovered run’s (absent) sort, and a read past the cap is too wide', async () => {
    const reads: string[] = []
    const d = want.scans[want.scans.length - 1]
    expect(asRows((await ivAttribution(env('g', reads), d, ''))!)).toEqual(want.maps[d])
    const uncoveredRun = want.scans.filter((x, i) => i >= 4 && want.maps[x] === null).map(x => `interval-store/g/deltas/${x}/`)
    expect(reads.filter(k => uncoveredRun.some(u => k.startsWith(u)))).toEqual([])
    expect(await ivAttribution(env('g'), d, '', 1)).toBeNull()
  })

  it('a generation without the table never opens it', async () => {
    const reads: string[] = []
    const e = env('g1', reads, 'iv')
    expect([await ivAttribution(e, '2026-08-05', ''), await ivAttributes(e, '2026-08-05')]).toEqual([undefined, false])
    expect(reads.filter(k => k.includes('attribution'))).toEqual([])
  })
})
