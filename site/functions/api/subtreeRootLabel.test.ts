import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { type D1Variant, readJson, seedGeneration } from '../_lib/testStore'
import { onRequestGet as subtree } from './subtree'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// Two deployments of one store (a prod and its dev stack) share the KV cache
// tier but label the store root differently (`ROOT_LABEL`). The root's answer
// carries that label, so neither may be served the other's cached copy.

const A = '2026-10-01T0001'
let base: Env

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const { db, raw } = await sqliteD1('cw')
  const dir = 'v2-search'
  const v = await readJson<Record<string, D1Variant>>(`${dir}/d1.json`)
  const files = { path: { parquet: `${dir}/path-index.parquet`, groups: `${dir}/path-index.groups.json` }, bysize: { parquet: `${dir}/path-index-bysize.parquet`, groups: `${dir}/path-index-bysize.groups.json` } }
  seedGeneration(raw, { date: A, gen: 'g', dir: `cw-l2/${A}/index/g`, variants: v, files })
  const kv = new Map<string, string>()
  const CACHE_KV = {
    get: async (k: string) => kv.get(k) ?? null,
    put: async (k: string, body: string) => { kv.set(k, body) },
  } as unknown as KVNamespace
  base = { DB: db, CACHE_KV, GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data' } as Env
})

const rootName = async (label: string, path: string) => {
  const r = await subtree({ request: new Request(`http://localhost/api/subtree?date=${A}&path=${path}`), env: { ...base, ROOT_LABEL: label } })
  return ((await r.json()) as { tree: { n: string } }).tree.n
}

describe('the root label is part of the cached answer', () => {
  it('each deployment gets its own label at the root', async () => {
    expect([await rootName('prod', ''), await rootName('prod (dev)', ''), await rootName('prod', '')]).toEqual(['prod', 'prod (dev)', 'prod'])
  })
  it('below the root, the answer (and its key) is label-independent', async () => {
    expect([await rootName('prod', 'bk'), await rootName('prod (dev)', 'bk')]).toEqual(['bk', 'bk'])
  })
})
