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
// One file per pair (the per-scan handles are memoized per isolate by date, and the dates are the same):
// `largestIntervals.v2.test.ts` holds `g7`.
//
// The one difference: an object whose key is also a folder's prefix (`bk/m/twin` beside `bk/m/twin/f0`, in
// `v2-slices` / `v2-lens`) is folded into the folder's row by the interval store (a path's rows are one version, `kind`
// `dir` if any is: `interval_store.py`), so it is never listed (specs/search-box-no-refusals.md, "Largest files").

const [S1, S2, S3] = ['2026-09-30T0007', '2026-09-30T0008', '2026-09-30T0009']
const PATHS = ['', 'bk', 'bk/m', 'bk/m/big', 'bk/m/deep', 'bk/m/split', 'bk/m/twin', 'bk/w', 'bk/w/d0000', 'bk/flat', 'nope']
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
  const slicesD1 = await readJson<Record<string, D1Variant>>('v2-slices/d1.json')
  const lensD1 = await readJson<Record<string, D1Variant>>('v2-lens/d1.json')
  for (const date of [S1, S3]) seedGeneration(raw, { date, gen: 'g', dir: `listing/${date}/index/g`, variants: slicesD1, files: files('v2-slices', { path: 'path-index', bysize: 'path-index-bysize' }) })
  seedGeneration(raw, { date: S2, gen: 'g', dir: `listing/${S2}/index/g`, variants: lensD1, files: files('v2-lens', { path: 'path-index', bysize: 'path-index-bysize', 'bysize-user': 'path-index-bysize-by-user' }) })
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

/** The objects that are also a folder (in the scans' own sorts): the interval store folds each into its folder. */
const TWINS = ['bk/m/twin']

describe('`/api/largest` from the interval store = the scan\'s own size sort, less the object-and-folder twins', () => {
  for (const gen of ['g2', 'g5']) {
    it(`${gen}: every scan, path and n`, async () => {
      const cases = [S1, S2, S3].flatMap(date => PATHS.flatMap(path => NS.map(n => [date, path, n] as const)))
      const want: [number, unknown][] = [], got: [number, unknown][] = [], beyond: boolean[] = []
      for (const [date, path, n] of cases) {
        // The scan's own answer one longer, the twins dropped, cut to n: what the interval store can list.
        const [status, b] = await ask(perScan, date, path, n + 1) as [number, Body]
        const files = b.files.filter(f => !TWINS.includes(f.path))
        want.push([status, { date, path, n, files: files.slice(0, n) }])
        beyond.push(files.length > n)
      }
      const gets = GETS.length
      ivReads.length = 0
      for (const [date, path, n] of cases) got.push(await ask(ivEnv(gen), date, path, n))
      const sansMore = ([status, b]: [number, unknown]) => { const { more: _, ...rest } = b as Body; return [status, rest] }
      expect(got.map(sansMore)).toEqual(want)
      // `more`: true wherever more files exist (it may also be where they only may: groups left unread).
      expect(cases.filter((_, i) => beyond[i] && !(got[i][1] as Body).more)).toEqual([])
      // Every answer read the interval store, nothing per-scan.
      expect([GETS.length - gets, ivReads.some(k => k.startsWith(`interval-store/${gen}/`))]).toEqual([0, true])
      // Not vacuous: answers with files, the twins' paths among them.
      expect(want.filter(w => (w[1] as Body).files.length).length).toBeGreaterThan(60)
    })
  }
})

// Folders: the same, but the twin is a folder on both sides — the interval store's carrying the object's bytes and
// count (folded in), the scan's own not — so it is dropped from both.
describe('`/api/largest?kind=dir` from the interval store = the scan\'s own size sort, less the object-and-folder twins', () => {
  for (const gen of ['g2', 'g5']) {
    it(`${gen}: every scan, path and n — the folders exactly, \`more\` wherever more exist`, async () => {
      const cases = [S1, S2, S3].flatMap(date => PATHS.flatMap(path => NS.map(n => [date, path, n] as const)))
      const want: [number, unknown][] = [], got: [number, unknown][] = [], beyond: boolean[] = []
      // Both sides one longer, the twins dropped, cut to n: the folders the two agree on.
      const cut = async (env: Env, date: string, path: string, n: number): Promise<[number, unknown]> => {
        const [status, b] = await askDirs(env, date, path, n + 1) as [number, DirBody]
        return [status, { date, path, n, dirs: b.dirs.filter(d => !TWINS.includes(d.path)).slice(0, n) }]
      }
      for (const [date, path, n] of cases) {
        want.push(await cut(perScan, date, path, n))
        const [, b] = await askDirs(perScan, date, path, n + 1) as [number, DirBody]
        beyond.push(b.dirs.length > n)
      }
      const agreed: [number, unknown][] = []
      for (const [date, path, n] of cases) {
        got.push(await askDirs(ivEnv(gen), date, path, n))
        agreed.push(await cut(ivEnv(gen), date, path, n))
      }
      expect(agreed).toEqual(want)
      expect(cases.filter((_, i) => beyond[i] && !(got[i][1] as DirBody).more)).toEqual([])
      // Not vacuous: answers with folders.
      expect(want.filter(w => (w[1] as DirBody).dirs.length).length).toBeGreaterThan(30)
    })
  }
})
