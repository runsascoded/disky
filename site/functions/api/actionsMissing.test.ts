import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1, withLedger } from '../_lib/testD1'
import { type D1Variant, readJson, seedGeneration } from '../_lib/testStore'
import { onRequest as actionsRoute } from './actions'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// An assignment must name something the newest scan holds: the `v2-search` fixture's buckets `bk` and `zz`
// (`bk/tmp/ttl=14d/` holds two objects; `bk/path/to/my-run/` holds nothing).
describe('POST /api/actions: a pattern the newest scan holds nothing under', () => {
  const A = '2026-10-01T0001'
  const HELD = 'gs://bk/tmp/ttl=14d/'
  const NONE = 'gs://bk/path/to/my-run/'
  let env: Env
  let raw: Awaited<ReturnType<typeof sqliteD1>>['raw']

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
    const d = await sqliteD1()
    raw = d.raw
    withLedger(raw)
    const dir = 'v2-search'
    const v = await readJson<Record<string, D1Variant>>(`${dir}/d1.json`)
    const files = { path: { parquet: `${dir}/path-index.parquet`, groups: `${dir}/path-index.groups.json` }, bysize: { parquet: `${dir}/path-index-bysize.parquet`, groups: `${dir}/path-index-bysize.groups.json` } }
    seedGeneration(raw, { date: A, gen: 'g', dir: `cw-l2/${A}/index/g`, variants: v, files })
    env = { DB: d.db, GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data', STORE_SCHEME: 'gs://', STORE_BUCKETS: 'bk,zz', DEV_EMAIL: 'ann@example.test' } as Env
  })

  const post = async (body: unknown) => {
    const r = await actionsRoute({ request: new Request('http://localhost/api/actions', { method: 'POST', body: JSON.stringify(body) }), env } as never)
    const j = await r.json() as Record<string, unknown>
    return [r.status, 'ts' in j ? { ...j, ts: '<ts>' } : j]
  }
  const patterns = () => raw.prepare('SELECT pattern, owner FROM actions ORDER BY id').all()

  it('refuses the batch, naming the missing patterns, and writes nothing; `allow_missing` and a clear go through', async () => {
    expect([
      await post([{ pattern: HELD, owner: 'ann' }, { pattern: NONE, owner: 'ann' }]),
      patterns(),
      await post([{ pattern: HELD, owner: 'ann' }, { pattern: NONE, owner: 'ann', allow_missing: true }]),
      await post({ pattern: NONE, owner: null }),
      patterns(),
    ]).toEqual([
      [400, { error: `nothing under this pattern in the newest scan (${A}); check the path, or pass \`allow_missing: true\` to assign it before its data lands`, missing: [NONE], scan: A }],
      [],
      [200, { ok: true, count: 2, ts: '<ts>' }],
      [200, { ok: true, count: 1, ts: '<ts>' }],
      [{ pattern: HELD, owner: 'ann' }, { pattern: NONE, owner: 'ann' }, { pattern: NONE, owner: null }],
    ])
  })
})
