import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { type D1Variant, fixture, GETS, readJson, seedGeneration } from '../_lib/testStore'
import { onRequestGet as largest } from './largest'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// `/api/largest` under `PATH_STORE=intervals` reads the interval store's `bysize` sort as of the scan, and answers
// what the scan's own size sort does. `fixtures/iv/interval-store/g7` is one generation over three scans of the
// per-scan fixtures (`v2`, `v2-search`, `v2`), `g2` over `v2-slices`, `v2-lens`, `v2-slices`, and `g5` the latter as a
// base and per-scan runs (their close records end the base's versions); the per-scan stores are seeded beside them.
// One file per pair (the per-scan handles are memoized per isolate by date, and the dates are the same): this one
// holds `g7`, whose scans hold no object that is also a folder — so its answers are the per-scan ones exactly.

const [S1, S2, S3] = ['2026-09-30T0007', '2026-09-30T0008', '2026-09-30T0009']
const PATHS = ['', 'bk', 'bk/flat', 'bk/nest', 'bk/nest/a', 'bk/nest/a/b', 'bk/small', 'nope']
const NS = [1, 3, 10, 499]

/** `INDEX_R2` over `fixtures/iv/`, recording the keys read. */
function r2(reads: string[]): R2Bucket {
  type Fs = { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array; readdirSync(p: string, o: { recursive: true }): string[]; statSync(p: string): { isFile(): boolean } }
  const fs = async () => (await import(/* @vite-ignore */ 'node:fs' as string)) as Fs
  const get = async (key: string, o?: { range?: { offset: number; length: number } | { suffix: number } }) => {
    const f = await fs()
    const file = fixture(`iv/${key}`)
    if (!f.existsSync(file)) return null
    reads.push(key)
    const buf = f.readFileSync(file)
    const r = o?.range
    const { offset = 0, length = buf.byteLength - offset } = r && 'suffix' in r ? { offset: Math.max(0, buf.byteLength - r.suffix) } : r ?? {}
    const part = buf.slice(offset, offset + length)
    return { size: buf.byteLength, arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength), json: async () => JSON.parse(new TextDecoder().decode(part)) }
  }
  const list = async ({ prefix, cursor }: { prefix: string; cursor?: string }) => {
    const f = await fs()
    const dir = prefix.slice(0, prefix.lastIndexOf('/') + 1)
    const root = fixture(`iv/${dir}`)
    const keys = f.existsSync(root) ? f.readdirSync(root, { recursive: true }).map(x => `${dir}${x}`).filter(k => k.startsWith(prefix) && f.statSync(fixture(`iv/${k}`)).isFile()).sort() : []
    return { objects: keys.map(key => ({ key })), truncated: false, cursor }
  }
  const head = async (key: string) => ((await fs()).existsSync(fixture(`iv/${key}`)) ? { key } : null)
  return { get, list, head } as unknown as R2Bucket
}

const files = (dir: string, sorts: Record<string, string>) =>
  Object.fromEntries(Object.entries(sorts).map(([v, stem]) => [v, { parquet: `${dir}/${stem}.parquet`, groups: `${dir}/${stem}.groups.json` }]))

let perScan: Env
const ivReads: string[] = []
const ivEnv = (gen: string): Env => ({ ...perScan, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: gen, INDEX_R2: r2(ivReads) } as Env)

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db, raw } = await sqliteD1()
  const v2 = await readJson<Record<string, D1Variant>>('v2/d1.json')
  const search = await readJson<Record<string, D1Variant>>('v2-search/d1.json')
  for (const date of [S1, S3]) seedGeneration(raw, { date, gen: 'g', dir: `listing/${date}/index/g`, variants: v2, files: files('v2', { path: 'path-index', bysize: 'path-index-bysize' }) })
  seedGeneration(raw, { date: S2, gen: 'g', dir: `listing/${S2}/index/g`, variants: search, files: files('v2-search', { path: 'path-index', bysize: 'path-index-bysize' }) })
  perScan = { DB: db, ROOT_LABEL: 'root', BASE_SCOPE: 'gcs', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data' } as Env
})

type Body = { date: string; path: string; n: number; files: { path: string; size: number; mtime: number | null }[]; more: boolean; capped?: true }
type DirBody = { date: string; path: string; n: number; kind: 'dir'; dirs: { path: string; size: number; mtime: number | null; n_files: number }[]; more: boolean; capped?: true }

/** The route's answer: status, and the body's answer fields (not its read cost, `groups` / `rows`: another sort). */
async function ask(env: Env, date: string, path: string, n: number): Promise<[number, unknown]> {
  const r = await largest({ request: new Request(`http://localhost/api/largest?date=${date}&path=${encodeURIComponent(path)}&n=${n}`), env })
  if (r.status !== 200) return [r.status, await r.text()]
  const j = await r.json() as Body
  return [200, { date: j.date, path: j.path, n: j.n, files: j.files, more: j.more, ...(j.capped ? { capped: true } : {}) }]
}

/** `ask`'s `kind=dir` twin: the largest folders. */
async function askDirs(env: Env, date: string, path: string, n: number): Promise<[number, unknown]> {
  const r = await largest({ request: new Request(`http://localhost/api/largest?date=${date}&path=${encodeURIComponent(path)}&n=${n}&kind=dir`), env })
  if (r.status !== 200) return [r.status, await r.text()]
  const j = await r.json() as DirBody
  return [200, { date: j.date, path: j.path, n: j.n, dirs: j.dirs, more: j.more, ...(j.capped ? { capped: true } : {}) }]
}

describe('`/api/largest` from the interval store = the scan\'s own size sort', () => {
  it('g7: every scan, path and n — the files exactly, `more` wherever more exist', async () => {
    const cases = [S1, S2, S3].flatMap(date => PATHS.flatMap(path => NS.map(n => [date, path, n] as const)))
    const want: [number, unknown][] = [], got: [number, unknown][] = [], beyond: boolean[] = []
    for (const [date, path, n] of cases) {
      const [status, b] = await ask(perScan, date, path, n + 1) as [number, Body]
      want.push([status, { date, path, n, files: b.files.slice(0, n) }])
      beyond.push(b.files.length > n)
    }
    const gets = GETS.length
    ivReads.length = 0
    for (const [date, path, n] of cases) got.push(await ask(ivEnv('g7'), date, path, n))
    const sansMore = ([status, b]: [number, unknown]) => { const { more: _, ...rest } = b as Body; return [status, rest] }
    expect(got.map(sansMore)).toEqual(want)
    expect(cases.filter((_, i) => beyond[i] && !(got[i][1] as Body).more)).toEqual([])
    expect([GETS.length - gets, ivReads.some(k => k.startsWith('interval-store/g7/'))]).toEqual([0, true])
    expect(want.filter(w => (w[1] as Body).files.length).length).toBeGreaterThan(60)
  })
})

describe('`/api/largest?kind=dir` from the interval store = the scan\'s own size sort', () => {
  for (const gen of ['g7']) {
    it(`${gen}: every scan, path and n — the folders exactly, \`more\` wherever more exist`, async () => {
      const cases = [S1, S2, S3].flatMap(date => PATHS.flatMap(path => NS.map(n => [date, path, n] as const)))
      const want: [number, unknown][] = [], got: [number, unknown][] = [], beyond: boolean[] = []
      for (const [date, path, n] of cases) {
        const [status, b] = await askDirs(perScan, date, path, n + 1) as [number, DirBody]
        want.push([status, { date, path, n, dirs: b.dirs.slice(0, n) }])
        beyond.push(b.dirs.length > n)
      }
      for (const [date, path, n] of cases) got.push(await askDirs(ivEnv(gen), date, path, n))
      const sansMore = ([status, b]: [number, unknown]) => { const { more: _, ...rest } = b as DirBody; return [status, rest] }
      expect(got.map(sansMore)).toEqual(want)
      expect(cases.filter((_, i) => beyond[i] && !(got[i][1] as DirBody).more)).toEqual([])
      // Not vacuous: answers with folders.
      expect(want.filter(w => (w[1] as DirBody).dirs.length).length).toBeGreaterThan(30)
    })
  }
})
