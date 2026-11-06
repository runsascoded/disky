import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { type D1Variant, readJson, seedGeneration } from '../_lib/testStore'
import { onRequestGet as subtree } from './subtree'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// The reader refuses a read over its row-group cap in two spellings: `query too wide` (a plan) and `lookup too
// wide` (a point lookup, `readAsks`). Both are the caller's to narrow: a 413, never a 500.
const thrown = vi.hoisted(() => ({ msg: '' }))
vi.mock('../_lib/view.js', async orig => ({
  ...(await orig<typeof import('../_lib/view.js')>()),
  buildView: async () => { throw new Error(thrown.msg) },
}))

describe('/api/subtree: a read over its cap is a 413', () => {
  const A = '2026-10-01T0001'
  let env: Env

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
    const { db, raw } = await sqliteD1()
    const dir = 'v2-search'
    const v = await readJson<Record<string, D1Variant>>(`${dir}/d1.json`)
    const files = { path: { parquet: `${dir}/path-index.parquet`, groups: `${dir}/path-index.groups.json` }, bysize: { parquet: `${dir}/path-index-bysize.parquet`, groups: `${dir}/path-index-bysize.groups.json` } }
    seedGeneration(raw, { date: A, gen: 'g', dir: `cw-l2/${A}/index/g`, variants: v, files })
    env = { DB: db, GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data' } as Env
  })

  const get = async (msg: string) => {
    thrown.msg = msg
    const r = await subtree({ request: new Request(`http://localhost/api/subtree?date=${A}&path=bk`), env })
    return [r.status, await r.text()]
  }

  it('maps both spellings to 413 with the refusal text', async () => {
    expect([
      await get('query too wide: drill deeper or raise minArea'),
      await get('lookup too wide: ≥3 row groups (cap 1)'),
    ]).toEqual([
      [413, 'query too wide: drill deeper or raise minArea'],
      [413, 'lookup too wide: ≥3 row groups (cap 1)'],
    ])
  })
})
