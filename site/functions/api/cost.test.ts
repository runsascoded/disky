// `/api/cost/<cloud>`: admins only; a known cloud's newest `days` cost documents from the served store (`INDEX_R2`
// under `SERVED_R2`), oldest first, other keys, other clouds' and other versions left out; none written → `[]`.
import { beforeAll, describe, expect, it } from 'vitest'
import { gateFor } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { onRequestGet } from './cost/[cloud]'

/** An `R2Bucket`'s `list` / `get`, enough for `R2Store` (one page of 2 keys at a time). */
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

const doc = (date: string, version = 1) => JSON.stringify({ version, cloud: 'gcp', date })
const FILES = {
  'snapshots/cost/gcp/2026-10-22.json': doc('2026-10-22'),
  'snapshots/cost/gcp/2026-10-23.json': doc('2026-10-23', 2),
  'snapshots/cost/gcp/2026-10-24.json': doc('2026-10-24'),
  'snapshots/cost/gcp/2026-10-25.json': doc('2026-10-25'),
  'snapshots/cost/gcp/notes.txt': 'x',
  'snapshots/cost/gcp/old/2026-10-01.json': doc('2026-10-01'),
  'snapshots/cost/aws/2026-10-25.json': doc('2026-10-25'),
  'snapshots/usage/2026-10-25.json': doc('2026-10-25'),
}
let env: Record<string, unknown>
let admin: string
let viewer: string

beforeAll(async () => {
  const { db } = await sqliteD1()
  env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'gcs', SERVED_R2: '1', INDEX_R2: fakeR2(FILES) }
  const gate = gateFor(env as never)!
  const tok = async (email: string, scopes: string[]) =>
    (await gate.mint({ email, scopes, name: email, createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  admin = await tok('ops@example.test', ['gcs', 'admin'])
  viewer = await tok('ann@example.test', ['gcs'])
})

const get = async (cloud: string, qs: string, token?: string, e = env) => {
  const res = await onRequestGet({
    request: new Request(`https://site.example.test/api/cost/${cloud}${qs}`, { headers: token ? { authorization: `Bearer ${token}` } : {} }),
    env: e, params: { cloud },
  } as never)
  return [res.status, await res.json()] as const
}

describe('/api/cost/<cloud>', () => {
  it('serves the newest days, oldest first, its version only', async () => {
    expect(await get('gcp', '', admin)).toEqual([200, { days: [
      { version: 1, cloud: 'gcp', date: '2026-10-22' },
      { version: 1, cloud: 'gcp', date: '2026-10-24' },
      { version: 1, cloud: 'gcp', date: '2026-10-25' },
    ] }])
    // the newest 3 keys, 10-23's (version 2) dropped
    expect(await get('gcp', '?days=3', admin)).toEqual([200, { days: [
      { version: 1, cloud: 'gcp', date: '2026-10-24' },
      { version: 1, cloud: 'gcp', date: '2026-10-25' },
    ] }])
  })

  it('answers an empty window where the job writes none (a deployment without GCP)', async () => {
    expect(await get('gcp', '', admin, { ...env, INDEX_R2: fakeR2({ 'snapshots/usage/2026-10-25.json': doc('2026-10-25') }) })).toEqual([200, { days: [] }])
  })

  it('admins only, known clouds, and a bounded window', async () => {
    expect((await get('gcp', ''))[0]).toBe(401)
    expect((await get('gcp', '', viewer))[0]).toBe(403)
    expect(await get('aws', '', admin)).toEqual([404, { error: 'unknown cloud: aws (known: gcp)' }])
    expect(await get('constructor', '', admin)).toEqual([404, { error: 'unknown cloud: constructor (known: gcp)' }])
    expect(await get('gcp', '?days=93', admin)).toEqual([400, { error: 'days: an integer 1..92' }])
  })
})
