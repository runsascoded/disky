import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { type D1Variant, readJson, seedGeneration } from '../_lib/testStore'
import { A as UA, C as UC, unionDeployment } from '../_lib/testUnion'
import { fakeVmd, type FakeVmd, VM_ENV } from '../_lib/testVmd'
import { subtreeBody, type VmAnswer, type VmPins } from '../_lib/vmAdapter'
import { beOfResponse, resetVmd, VM_NODE_NOTE, VM_OFF_NOTE, vmDiffBody, VMD, type VmdStatus, vmMeta } from '../_lib/vmServe'
import { onRequestGet as diff } from './diff'
import { onRequestGet as subtree } from './subtree'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// VM mode beside the indexes (specs/vm-pilot.md step 4) through the routes, over `fixtures/v2-diff/`'s two scans of
// bucket `bk` and a fake `vmd` (`testVmd.ts`): a session pinned `vm` is answered by `vmd` and says so; an `idx` one by
// the index reader, never from a `vm` answer's cache entry; a `vm` session `vmd` can't answer falls back, saying why.

const A = '2026-09-28T0001'
const B = '2026-09-29T0001'
const PINS: VmPins = { gen: 'g9', manifest: 'm9.json' }
const FILES = (scan: string) => ({ path: { parquet: `v2-diff/${scan}/path-index.parquet`, groups: `v2-diff/${scan}/path-index.groups.json` }, bysize: { parquet: `v2-diff/${scan}/path-index-bysize.parquet`, groups: `v2-diff/${scan}/path-index-bysize.groups.json` } })

let base: Env
let kv: Map<string, string>

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db, raw } = await sqliteD1()
  for (const [scan, date] of [['a', A], ['b', B]]) seedGeneration(raw, { date, gen: `g${scan}`, dir: `cw-l2/${date}/index/g${scan}`, variants: await readJson<Record<string, D1Variant>>(`v2-diff/${scan}/d1.json`), files: FILES(scan) })
  base = { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data' } as Env
})

const HOT: VmdStatus = { tenant: 't1', state: 'hot', current: { pins: PINS, scans: 2, newest: B, classes: [1, 2, 3, 4] } }
const VIEW: VmAnswer = {
  v: 1, tenant: 't1', pins: PINS, scan: A, path: '', cell: { share_log2: 17, atten_log2: 1 },
  total: { bytes: 2048, objects: 9 }, nodes: 2,
  tree: { name: '', kind: 'dir', bytes: 2048, objects: 9, children: [{ name: 'bk', kind: 'dir', bytes: 2048, objects: 9, children: [{ name: 'keep', kind: 'dir', bytes: 2048, objects: 9 }] }] },
  timing: { pass_ms: 3, merge_ms: 0, total_ms: 3.2, rows: 9, dirs: 4, shards: 1 },
}
const DIFF: VmAnswer = {
  v: 1, tenant: 't1', pins: PINS, from: A, to: B, path: 'bk', cell: { share_log2: 17, atten_log2: 1 },
  total: { bytes: 300, objects: 3, bytes_to: 100, objects_to: 1 }, nodes: 2,
  tree: { name: 'bk', kind: 'dir', bytes: 300, objects: 3, bytes_to: 100, objects_to: 1, children: [{ name: 'del', kind: 'dir', bytes: 200, objects: 2, bytes_to: 0, objects_to: 0 }, { name: 'keep', kind: 'dir', bytes: 100, objects: 1, bytes_to: 100, objects_to: 1 }] },
  timing: { pass_ms: 4, merge_ms: 0, total_ms: 4.4, rows: 4, dirs: 2, shards: 1 },
}

const REAL = VMD.fetch
let vmd: FakeVmd
beforeEach(() => {
  resetVmd()
  kv = new Map()
  vmd = fakeVmd({ status: () => HOT, view: b => ({ status: 200, body: b.scan ? VIEW : DIFF }) })
})
afterEach(() => { VMD.fetch = REAL })

const envOf = (vm: boolean): Env => ({
  ...base,
  CACHE_KV: { get: async (k: string) => kv.get(k) ?? null, put: async (k: string, body: string) => { kv.set(k, body) } } as unknown as KVNamespace,
  ...(vm ? VM_ENV : {}),
})
const get = (route: typeof subtree, qs: string, o: { vm?: boolean; cookie?: string } = {}) =>
  route({ request: new Request(`http://localhost/api/x?${qs}`, o.cookie ? { headers: { cookie: o.cookie } } : {}), env: envOf(o.vm ?? true) })
const read = async (r: Response) => ({ status: r.status, ...beOfResponse(r), body: await r.json() as Record<string, unknown> })

const MAP = `date=${A}&path=&w=1280&h=768`
const MAP_VM = { ...subtreeBody(VIEW, { date: A, w: 1280, h: 768, minArea: 12, atten: 2, rootLabel: 'root' }), vm: vmMeta(VIEW) }

describe('/api/subtree', () => {
  it('a `vm` session is answered by vmd; an `idx` one by the index reader, never from the `vm` entry', async () => {
    const plain = await read(await get(subtree, MAP, { vm: false }))
    expect([plain.status, plain.be, plain.note]).toEqual([200, undefined, undefined])
    const idx = await read(await get(subtree, MAP))
    expect(idx).toEqual({ ...plain, be: 'idx' })
    expect(vmd.calls).toEqual([])

    expect(await read(await get(subtree, MAP, { cookie: 'disky_be=vm' }))).toEqual({ status: 200, be: 'vm', note: VM_NODE_NOTE, body: MAP_VM })
    expect(vmd.views).toEqual([{ pins: PINS, scan: A, path: '', cell: { share_log2: 17, atten_log2: 1 }, deadline_ms: VMD.deadlineMs }])

    // Each session again, both answers now in the cache: the `idx` session still gets the index reader's, and the `vm`
    // one `vmd`'s from its own entry (no second view).
    expect(await read(await get(subtree, MAP))).toEqual({ ...plain, be: 'idx' })
    expect(await read(await get(subtree, `${MAP}&be=vm`))).toEqual({ status: 200, be: 'vm', note: VM_NODE_NOTE, body: MAP_VM })
    expect(vmd.calls).toEqual(['GET /v1/status', 'POST /v1/view'])
  })

  it('a `vm` session vmd can\'t answer falls back, saying why', async () => {
    const plain = await read(await get(subtree, MAP, { vm: false }))
    VMD.fetch = async () => { throw new Error('connect refused') }
    expect(await read(await get(subtree, MAP, { cookie: 'disky_be=vm' }))).toEqual({ ...plain, be: 'idx-fallback', note: 'vmd unreachable: connect refused' })
    resetVmd()
    vmd = fakeVmd({ status: () => ({ ...HOT, current: { ...HOT.current!, newest: '2026-09-01T0000' } }) })
    expect(await read(await get(subtree, MAP, { cookie: 'disky_be=vm' }))).toEqual({ ...plain, be: 'idx-fallback', note: `scan ${A} is newer than vmd's pack (newest 2026-09-01T0000)` })
  })

  it('VM mode off: `?be=vm` is ignored with a note, and no backend is named', async () => {
    const plain = await read(await get(subtree, MAP, { vm: false }))
    expect(await read(await get(subtree, `${MAP}&be=vm`, { vm: false }))).toEqual({ ...plain, note: VM_OFF_NOTE })
    expect(vmd.calls).toEqual([])
  })
})

describe('/api/diff', () => {
  const D = `from=${A}&to=${B}&path=bk&w=1280&h=768&dm=max`

  it('a `vm` session\'s diff and its summary come from vmd', async () => {
    expect(await read(await get(diff, D, { cookie: 'disky_be=vm' }))).toEqual({ status: 200, be: 'vm', body: vmDiffBody(DIFF, { from: A, to: B, path: 'bk', top: 500, summary: false }) })
    expect(await read(await get(diff, `${D}&summary=1`, { cookie: 'disky_be=vm' }))).toEqual({ status: 200, be: 'vm', body: vmDiffBody(DIFF, { from: A, to: B, path: 'bk', top: 500, summary: true }) })
    expect(vmd.views).toEqual([
      { pins: PINS, from: A, to: B, path: 'bk', cell: { share_log2: 17, atten_log2: 1 }, deadline_ms: VMD.deadlineMs },
      { pins: PINS, from: A, to: B, path: 'bk', cell: { share_log2: 17, atten_log2: 1, depth: 1 }, deadline_ms: VMD.deadlineMs },
    ])
  })

  it('an `idx` session\'s diff is the index reader\'s', async () => {
    const plain = await read(await get(diff, D, { vm: false }))
    expect([plain.status, (plain.body.rows as unknown[]).length > 0]).toEqual([200, true])
    await get(diff, D, { cookie: 'disky_be=vm' })
    expect(await read(await get(diff, D))).toEqual({ ...plain, be: 'idx' })
  })
})

describe('a union root', () => {
  it('its primary member is answered by vmd under the union\'s one floor, the store without a tenant by the index reader', async () => {
    const env = { ...(await unionDeployment())(), CACHE_KV: envOf(true).CACHE_KV }
    const at = (cookie?: string, vm = true) => subtree({ request: new Request(`http://localhost/api/subtree?date=${UA}&path=&w=1280&h=768`, cookie ? { headers: { cookie } } : {}), env: vm ? { ...env, ...VM_ENV } : env })
    vmd = fakeVmd({ status: () => ({ ...HOT, current: { ...HOT.current!, newest: UC } }), view: () => ({ status: 200, body: { ...VIEW, scan: UA } }) })
    const plain = await read(await at(undefined, false))
    expect(plain.status).toEqual(200)
    const total = (plain.body.tree as { bytes: number }).bytes
    const r = await read(await at('disky_be=vm'))
    expect([r.status, r.be, r.note]).toEqual([200, 'vm', `${VM_NODE_NOTE}; store 'meta' has no vmd tenant`])
    expect(vmd.views).toEqual([{ pins: PINS, scan: UA, path: '', cell: { atten_log2: 1, floor_bytes: Math.ceil(total * 12 / (1280 * 768)), depth: 18 }, deadline_ms: VMD.deadlineMs }])
  })
})
