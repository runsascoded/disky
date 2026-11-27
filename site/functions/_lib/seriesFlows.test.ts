/** `/api/series`' change flows (specs/change-metrics.md §4.1) over the interval-store fixture `g2` (three scans; its
 *  root's one depth-1 dir `bk`), its counters served beside it as a generation's `changes.json`: the root's and a
 *  depth-1 path's points carry them, a deeper path's and a scoped series' don't, `split=roots` traces carry their
 *  root's, and a store without counters answers as before. */
import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { sqliteD1, withLedger } from './testD1'
import { type D1Variant, fixture, readJson, seedGeneration } from './testStore'
import type { ChangeEntry } from './series'
import { onRequestGet as seriesGet } from '../api/series'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

const [S1, S2, S3] = ['2026-09-30T0007', '2026-09-30T0008', '2026-09-30T0009']

/** `INDEX_R2` over `fixtures/iv/`, generation `gen` read as `g2`, with `changes` (when given) as its `changes.json`. */
function r2(gen: string, changes: Record<string, ChangeEntry> | null): R2Bucket {
  const real = (key: string) => key.replace(`interval-store/${gen}/`, 'interval-store/g2/')
  const get = async (key: string, o?: { range?: { offset: number; length: number } | { suffix: number } }) => {
    if (key === `interval-store/${gen}/changes.json`) {
      if (!changes) return null
      const text = JSON.stringify({ v: 1, scans: changes })
      return { size: text.length, arrayBuffer: async () => new TextEncoder().encode(text).buffer, json: async () => JSON.parse(text) }
    }
    const mod = 'node:fs'
    const fs = (await import(/* @vite-ignore */ mod)) as { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array }
    const file = fixture(`iv/${real(key)}`)
    if (!fs.existsSync(file)) return null
    const buf = fs.readFileSync(file)
    const r = o?.range
    const { offset = 0, length = buf.byteLength - offset } = r && 'suffix' in r ? { offset: Math.max(0, buf.byteLength - r.suffix) } : r ?? {}
    const part = buf.slice(offset, offset + length)
    return { size: buf.byteLength, arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength), json: async () => JSON.parse(new TextDecoder().decode(part)) }
  }
  // `g2` has no runs: no manifests to list
  const list = async ({ cursor }: { prefix: string; cursor?: string }) => ({ objects: [], truncated: false, cursor })
  const head = async (key: string) => {
    const mod = 'node:fs'
    const fs = (await import(/* @vite-ignore */ mod)) as { existsSync(p: string): boolean }
    return fs.existsSync(fixture(`iv/${real(key)}`)) ? { key } : null
  }
  return { get, list, head } as unknown as R2Bucket
}

const files = (dir: string, sorts: Record<string, string>) =>
  Object.fromEntries(Object.entries(sorts).map(([v, stem]) => [v, { parquet: `${dir}/${stem}.parquet`, groups: `${dir}/${stem}.groups.json` }]))

let base: Env

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db, raw } = await sqliteD1()
  const slicesD1 = await readJson<Record<string, D1Variant>>('v2-slices/d1.json')
  const lensD1 = await readJson<Record<string, D1Variant>>('v2-lens/d1.json')
  for (const date of [S1, S3]) seedGeneration(raw, { date, gen: 'g', dir: `listing/${date}/index/g`, variants: slicesD1, files: files('v2-slices', { path: 'path-index', bysize: 'path-index-bysize' }) })
  seedGeneration(raw, { date: S2, gen: 'g', dir: `listing/${S2}/index/g`, variants: lensD1, files: files('v2-lens', { path: 'path-index', bysize: 'path-index-bysize', 'bysize-user': 'path-index-bysize-by-user' }) })
  // the ledger's tables, empty: an owner-scoped series reads the scans' own attribution
  withLedger(raw)
  base = { DB: db, ROOT_LABEL: 'root', BASE_SCOPE: 'gcs', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'b' } as Env
})

/** Each test its own generation name: the reader holds a generation's state per isolate. */
let n = 0
const env = (changes: Record<string, ChangeEntry> | null): Env => {
  const gen = `gflows${n++}`
  return { ...base, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: gen, INDEX_R2: r2(gen, changes) } as Env
}

type Point = { date: string; bytes: number; objects: number; [k: string]: unknown }
type Body = { path: string; points: Point[]; roots?: { path: string; points: Point[] }[]; changes?: unknown }
const get = async (e: Env, q: string): Promise<Body> => {
  const r = await seriesGet({ request: new Request(`http://localhost/api/series?${q}`), env: e } as never)
  expect(r.status).toBe(200)
  return await r.json() as Body
}

const counts = (added: number, deleted: number, modified: number, ab: number, db: number, mb: number, token: number, size: number) =>
  ({ added, deleted, modified, added_bytes: ab, deleted_bytes: db, modified_bytes: mb, mods_by: { token, size, mtime: 0 } })
/** S2 against S1, at the root and `bk`; S3 against S1 (as if a scan between them were missing), at the root only
 *  (nothing changed under `bk`: zero there). S1 is the first: no counters. */
const CHANGES: Record<string, ChangeEntry> = {
  [S2]: { since: S1, mtime: 'write', paths: { '': counts(5, 2, 3, 500, 20, 300, 1, 2), bk: counts(4, 2, 3, 400, 20, 300, 1, 2) } },
  [S3]: { since: S1, mtime: 'write', paths: { '': counts(1, 0, 0, 7, 0, 0, 0, 0) } },
}
const flows = (added: number, deleted: number, modified: number, ab: number, db: number, mb: number) =>
  ({ added, deleted, modified, added_bytes: ab, deleted_bytes: db, modified_bytes: mb })

describe('/api/series change flows', () => {
  it("the root's points carry each counted scan's, `since` where it isn't the point before; the body its basis", async () => {
    const plain = await get(env(null), 'path=')
    expect(plain.points.map(p => p.date)).toEqual([S1, S2, S3])
    expect(await get(env(CHANGES), 'path=')).toEqual({
      ...plain,
      points: [plain.points[0], { ...plain.points[1], ...flows(5, 2, 3, 500, 20, 300) }, { ...plain.points[2], ...flows(1, 0, 0, 7, 0, 0), since: S1 }],
      changes: { from: S2, mods_by: { token: 1, size: 2, mtime: 0 } },
    })
  })
  it("a depth-1 path's are its own (zero where nothing changed under it); a deeper path's and a scoped series' have none", async () => {
    const e = env(CHANGES)
    const plain = await get(env(null), 'path=bk')
    expect(await get(e, 'path=bk')).toEqual({
      ...plain,
      points: plain.points.map(p => (p.date === S2 ? { ...p, ...flows(4, 2, 3, 400, 20, 300) } : p.date === S3 ? { ...p, ...flows(0, 0, 0, 0, 0, 0), since: S1 } : p)),
      changes: { from: S2, mods_by: { token: 1, size: 2, mtime: 0 } },
    })
    for (const q of ['path=bk/flat', 'path=&o=unowned', 'path=bk&o=owned']) expect(await get(e, q)).toEqual(await get(env(null), q))
  })
  it("`split=roots`: each root's trace carries its own", async () => {
    const plain = await get(env(null), 'path=&split=roots')
    const got = await get(env(CHANGES), 'path=&split=roots')
    expect(plain.roots!.map(t => t.path)).toEqual(['bk'])
    expect(got.roots).toEqual(plain.roots!.map(t => ({
      ...t,
      points: t.points.map(p => (p.date === S2 ? { ...p, ...flows(4, 2, 3, 400, 20, 300) } : p.date === S3 ? { ...p, ...flows(0, 0, 0, 0, 0, 0), since: S1 } : p)),
    })))
  })
})
