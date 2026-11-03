// `/api/usage` (specs/usage-metrics.md): admins only; the newest `days` usage documents from the served store
// (`INDEX_R2` under `SERVED_R2`), oldest first, other keys and other versions left out.
import { beforeAll, describe, expect, it } from 'vitest'
import { gateFor } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { onRequestGet } from './usage'

/** An `R2Bucket`'s `list` / `get`, enough for `R2Store` (one page of `limit` keys at a time). */
function fakeR2(files: Record<string, string>) {
  return {
    async list({ prefix, cursor, limit }: { prefix: string; cursor?: string; limit: number }) {
      const keys = Object.keys(files).filter(k => k.startsWith(prefix)).sort()
      const at = cursor ? Number(cursor) : 0
      const page = keys.slice(at, at + Math.min(limit, 2))
      const more = at + page.length < keys.length
      return {
        objects: page.map(key => ({ key, size: files[key].length, uploaded: new Date(0) })),
        delimitedPrefixes: [], truncated: more, cursor: more ? String(at + page.length) : undefined,
      }
    },
    async get(key: string) {
      const v = files[key]
      return v === undefined ? null : { size: v.length, arrayBuffer: async () => new TextEncoder().encode(v).buffer }
    },
  }
}

const doc = (date: string, version = 1) => JSON.stringify({ version, date, project: 'p' })
let env: Record<string, unknown>
let admin: string
let viewer: string

beforeAll(async () => {
  const { db } = await sqliteD1()
  env = {
    DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'gcs', SERVED_R2: '1',
    INDEX_R2: fakeR2({
      'snapshots/usage/2026-10-17.json': doc('2026-10-17'),
      'snapshots/usage/2026-10-18.json': doc('2026-10-18', 2),
      'snapshots/usage/2026-10-19.json': doc('2026-10-19'),
      'snapshots/usage/2026-10-20.json': doc('2026-10-20'),
      'snapshots/usage/notes.txt': 'x',
      'snapshots/2026-10-20/meta.json': '{}',
    }),
  }
  const gate = gateFor(env as never)!
  const tok = async (email: string, scopes: string[]) =>
    (await gate.mint({ email, scopes, name: email, createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  admin = await tok('ops@example.test', ['gcs', 'admin'])
  viewer = await tok('ann@example.test', ['gcs'])
})

const get = async (qs: string, token?: string) => {
  const res = await onRequestGet({ request: new Request(`https://site.example.test/api/usage${qs}`, { headers: token ? { authorization: `Bearer ${token}` } : {} }), env } as never)
  return [res.status, await res.json()] as const
}

describe('/api/usage', () => {
  it('serves the newest days, oldest first, version 1 only', async () => {
    expect(await get('', admin)).toEqual([200, { days: [
      { version: 1, date: '2026-10-17', project: 'p' },
      { version: 1, date: '2026-10-19', project: 'p' },
      { version: 1, date: '2026-10-20', project: 'p' },
    ] }])
    // the newest 3 keys, 10-18's (version 2) dropped
    expect(await get('?days=3', admin)).toEqual([200, { days: [
      { version: 1, date: '2026-10-19', project: 'p' },
      { version: 1, date: '2026-10-20', project: 'p' },
    ] }])
  })

  it('admins only, and a bounded window', async () => {
    expect((await get(''))[0]).toBe(401)
    expect((await get('', viewer))[0]).toBe(403)
    expect(await get('?days=0', admin)).toEqual([400, { error: 'days: an integer 1..92' }])
  })
})
