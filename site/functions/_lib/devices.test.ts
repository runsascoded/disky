import type { D1Database } from '@cloudflare/workers-types'
import { describe, expect, it } from 'vitest'
import { type Env, gateFor } from './auth'
import { disconnect, enroll, hostStatus, uploads } from './devices'
import { loadHosts } from './hosts'
import { sqliteD1 } from './testD1'

// The device API (specs/device-api.md): enroll → device token → presigned uploads, status, disconnect.

const ORIGIN = 'https://disk.example.test'
const ADMIN = 'ops@example.test'
const VIEWER = 'viewer@example.test'
const NOW = Date.parse('2026-10-16T20:00:00Z')

async function setup() {
  const { db, raw } = await sqliteD1()
  for (const email of [ADMIN, VIEWER]) await db.prepare('INSERT INTO allowed_emails (email, who, ts) VALUES (?, ?, 1)').bind(email, ADMIN).run()
  await db.prepare('INSERT INTO admin_emails (email, who, ts) VALUES (?, ?, 1)').bind(ADMIN, ADMIN).run()
  raw.exec(`INSERT INTO hosts (id, store, label, home, created_at) VALUES ('m3', 'primary', 'm3', 'Users/ryan', '2026-10-16T00:00:00Z'), ('nas', 'nas', 'NAS', NULL, '2026-10-16T00:00:01Z')`)
  const env = {
    DB: db, SESSION_SECRET: 'test-secret', BASE_SCOPE: 'laptop', ADMIN_EMAILS: '1',
    STORE_ENDPOINT: 'https://acct.r2.example.test', STORE_BUCKET: 'disk-tree', STORE_REGION: 'auto',
    STORE_ACCESS_KEY_ID: 'AKID', STORE_SECRET_ACCESS_KEY: 'SECRET',
  } as Env
  const gate = gateFor(env)!
  const session = async (email: string) => (await gate.signIn(email, new Request(`${ORIGIN}/signin`)))!.cookie.split(';')[0]
  return { db, env, session }
}

const req = (path: string, init: { method?: string; cookie?: string; bearer?: string; body?: unknown; origin?: string | null } = {}) => {
  const headers: Record<string, string> = {}
  if (init.cookie) headers.cookie = init.cookie
  if (init.bearer) headers.authorization = `Bearer ${init.bearer}`
  if (init.origin !== null) { headers.origin = init.origin ?? ORIGIN; headers['sec-fetch-site'] = init.origin && init.origin !== ORIGIN ? 'cross-site' : 'same-origin' }
  if (init.body !== undefined) headers['content-type'] = 'application/json'
  return new Request(`${ORIGIN}${path}`, { method: init.method ?? 'GET', headers, body: init.body === undefined ? undefined : JSON.stringify(init.body) })
}
const res = async (r: Response) => ({ status: r.status, body: await r.json() as Record<string, unknown> })
const M1 = { host: 'm1', label: 'M1', home: 'Users/ryan', os: 'macOS 27.0', version: '0.9.3' }

const hostRows = async (db: D1Database) =>
  (await db.prepare('SELECT id, store, label, home, os, version, created_by, revoked_at, grant_id IS NOT NULL AS has_grant, seen_at FROM hosts ORDER BY created_at, id').all()).results

describe('enroll', () => {
  it('an admin’s session enrolls a new host: a row, a grant scoped to it, the token once', async () => {
    const { db, env, session } = await setup()
    const cookie = await session(ADMIN)
    const r = await res(await enroll({ request: req('/api/hosts', { method: 'POST', cookie, body: M1 }), env }, NOW))
    expect([r.status, r.body.id, r.body.store, typeof r.body.token]).toEqual([201, 'm1', 'm1', 'string'])
    expect(await hostRows(db)).toEqual([
      { id: 'm3', store: 'primary', label: 'm3', home: 'Users/ryan', os: null, version: null, created_by: null, revoked_at: null, has_grant: 0, seen_at: null },
      { id: 'nas', store: 'nas', label: 'NAS', home: null, os: null, version: null, created_by: null, revoked_at: null, has_grant: 0, seen_at: null },
      { id: 'm1', store: 'm1', label: 'M1', home: 'Users/ryan', os: 'macOS 27.0', version: '0.9.3', created_by: ADMIN, revoked_at: null, has_grant: 1, seen_at: null },
    ])
    expect((await db.prepare('SELECT name, email, scopes, max_redeems, expires_at, created_by FROM grants').all()).results).toEqual([
      { name: 'disky device m1', email: null, scopes: 'host:m1', max_redeems: null, expires_at: null, created_by: ADMIN },
    ])
    expect((await loadHosts(env)).map(h => h.id)).toEqual(['m3', 'nas', 'm1'])
  })
  it('refuses: cross-site, a non-admin, a grant, bad fields, the primary, a seeded host, a reserved id', async () => {
    const { env, session } = await setup()
    const admin = await session(ADMIN)
    const call = async (init: Parameters<typeof req>[1]) => { const r = await res(await enroll({ request: req('/api/hosts', { method: 'POST', ...init }), env }, NOW)); return [r.status, r.body.error] }
    const first = await res(await enroll({ request: req('/api/hosts', { method: 'POST', cookie: admin, body: M1 }), env }, NOW))
    expect([
      await call({ cookie: admin, body: M1, origin: 'https://evil.example.test' }),
      await call({ cookie: await session(VIEWER), body: M1 }),
      await call({ bearer: first.body.token as string, body: { ...M1, host: 'm2' } }),
      await call({ body: M1 }),
      await call({ cookie: admin, body: { ...M1, host: 'M2' } }),
      await call({ cookie: admin, body: { ...M1, host: 'api' } }),
      await call({ cookie: admin, body: { ...M1, label: '' } }),
      await call({ cookie: admin, body: { ...M1, host: 'm2', home: '../etc' } }),
      await call({ cookie: admin, body: { ...M1, host: 'm3' } }),
      await call({ cookie: admin, body: { ...M1, host: 'nas' } }),
    ]).toEqual([
      [403, 'cross-site request refused'],
      [403, 'enrolling a host takes an admin’s signed-in session'],
      [403, 'enrolling a host takes an admin’s signed-in session'],
      [401, 'unauthenticated'],
      [400, "bad host id 'M2' (want ^[a-z0-9][a-z0-9-]{0,31}$, not a site route)"],
      [400, "bad host id 'api' (want ^[a-z0-9][a-z0-9-]{0,31}$, not a site route)"],
      [400, 'bad label (1–64 chars)'],
      [400, 'bad home (a relative path, e.g. Users/ryan)'],
      [409, "'m3' is this deployment’s own host"],
      [409, "'nas' is registered without enrollment (its captures come from the deployment’s own keys)"],
    ])
  })
  it('re-enrolling replaces the token (the old one dies) and reconnects a disconnected host', async () => {
    const { db, env, session } = await setup()
    const cookie = await session(ADMIN)
    const enrollM1 = async (body = M1) => res(await enroll({ request: req('/api/hosts', { method: 'POST', cookie, body }), env }, NOW))
    const t1 = (await enrollM1()).body.token as string
    const status = async (token: string) => (await hostStatus({ request: req('/api/hosts/m1', { bearer: token, origin: null }), env }, 'm1', NOW)).status
    expect(await status(t1)).toBe(200)
    await disconnect({ request: req('/api/hosts/m1', { method: 'DELETE', bearer: t1, origin: null }), env }, 'm1', NOW)
    expect(await status(t1)).toBe(401)
    const again = await enrollM1({ ...M1, version: '0.9.4' })
    expect([again.status, again.body.id]).toEqual([200, 'm1'])
    expect([await status(t1), await status(again.body.token as string)]).toEqual([401, 200])
    expect((await db.prepare("SELECT version, revoked_at FROM hosts WHERE id = 'm1'").first())).toEqual({ version: '0.9.4', revoked_at: null })
  })
})

describe('a device token', () => {
  async function enrolled() {
    const s = await setup()
    const cookie = await s.session(ADMIN)
    const token = (await res(await enroll({ request: req('/api/hosts', { method: 'POST', cookie, body: M1 }), env: s.env }, NOW))).body.token as string
    return { ...s, cookie, token }
  }
  it('reads its own status (stamping `seen_at`) with its store’s newest ingest; not another host’s', async () => {
    const { db, env, token } = await enrolled()
    const status = async (id: string) => res(await hostStatus({ request: req(`/api/hosts/${id}`, { bearer: token, origin: null }), env }, id, NOW))
    expect(await status('m1')).toEqual({ status: 200, body: {
      id: 'm1', store: 'm1', label: 'M1', home: 'Users/ryan', os: 'macOS 27.0', version: '0.9.3',
      created_at: '2026-10-16T20:00:00Z', seen_at: '2026-10-16T20:00:00Z', revoked: false, last_ingest: null,
    } })
    await db.prepare("INSERT INTO scan_runs (run_id, store, scan, kind, status, started_ts, finished_ts, source, updated_ts, error) VALUES ('r1', 'm1', '2026-10-16T2000', 'ingest', 'failed', 1792180000, 1792180600, 'job', 1792180600, 'exit 3: boom')").run()
    expect((await status('m1')).body.last_ingest).toEqual({ scan: '2026-10-16T2000', status: 'failed', at: '2026-10-16T19:56:40Z', error: 'exit 3: boom' })
    expect(await status('nas')).toEqual({ status: 403, body: { error: 'not this host’s token' } })
  })
  it('gets presigned PUTs under `captures/<id>/<root>/<stamp>/`, 900 s', async () => {
    const { env, token } = await enrolled()
    const r = await res(await uploads({ request: req('/api/hosts/m1/uploads', { method: 'POST', bearer: token, origin: null, body: { root: 'root', stamp: '2026-10-16T19-59-58Z', keys: ['shard-00000.parquet', '_SUCCESS.json'] } }), env }, 'm1', NOW))
    const urls = r.body.urls as Record<string, string>
    expect([r.status, r.body.expires, Object.keys(urls)]).toEqual([200, '2026-10-16T20:15:00Z', ['shard-00000.parquet', '_SUCCESS.json']])
    const u = new URL(urls['_SUCCESS.json'])
    expect([u.origin + u.pathname, Object.fromEntries([...u.searchParams].map(([k, v]) => [k, k === 'X-Amz-Signature' ? v.length : v]))]).toEqual([
      'https://acct.r2.example.test/disk-tree/captures/m1/root/2026-10-16T19-59-58Z/_SUCCESS.json',
      { 'X-Amz-Algorithm': 'AWS4-HMAC-SHA256', 'X-Amz-Credential': 'AKID/20261016/auto/s3/aws4_request', 'X-Amz-Date': '20261016T200000Z', 'X-Amz-Expires': '900', 'X-Amz-SignedHeaders': 'host', 'X-Amz-Signature': 64 },
    ])
  })
  it('refuses bad uploads: root, stamp, key count and shape; an admin session; another host', async () => {
    const { env, token, cookie } = await enrolled()
    const call = async (body: unknown, init: { bearer?: string; cookie?: string } = { bearer: token }, id = 'm1') => {
      const r = await res(await uploads({ request: req(`/api/hosts/${id}/uploads`, { method: 'POST', origin: null, body, ...init }), env }, id, NOW))
      return [r.status, r.body.error]
    }
    const ok = { root: 'root', stamp: '2026-10-16T19-59-58Z', keys: ['a.parquet'] }
    expect([
      await call({ ...ok, root: 'Volumes/x' }),
      await call({ ...ok, stamp: '2026-10-16T19:59:58Z' }),
      await call({ ...ok, stamp: '2026-02-30T00-00-00Z' }),
      await call({ ...ok, stamp: '2026-10-14T19-59-58Z' }),
      await call({ ...ok, keys: [] }),
      await call({ ...ok, keys: Array.from({ length: 101 }, (_, i) => `s${i}`) }),
      await call({ ...ok, keys: ['../x'] }),
      await call({ ...ok, keys: ['..'] }),
      await call(ok, { cookie }),
      await call(ok, { bearer: token }, 'nas'),
      await call(ok, { bearer: 'not-a-token' }),
    ]).toEqual([
      [400, 'bad root (want ^[A-Za-z0-9._-]{1,64}$)'],
      [400, 'bad stamp (want %Y-%m-%dT%H-%M-%SZ)'],
      [400, 'bad stamp (want %Y-%m-%dT%H-%M-%SZ)'],
      [400, 'stamp more than a day from now'],
      [400, 'want 1–100 keys'],
      [400, 'want 1–100 keys'],
      [400, 'bad key "../x" (want ^[A-Za-z0-9._-]{1,128}$)'],
      [400, 'bad key ".." (want ^[A-Za-z0-9._-]{1,128}$)'],
      [403, 'forbidden'],
      [403, 'not this host’s token'],
      [401, 'unauthenticated'],
    ])
  })
  it('an admin reads and disconnects a host; a seeded one can’t be disconnected', async () => {
    const { env, cookie, token } = await enrolled()
    const st = await res(await hostStatus({ request: req('/api/hosts/m1', { cookie, origin: null }), env }, 'm1', NOW))
    expect([st.status, st.body.seen_at]).toEqual([200, null])
    const del = async (id: string) => res(await disconnect({ request: req(`/api/hosts/${id}`, { method: 'DELETE', cookie, origin: null }), env }, id, NOW))
    expect([await del('nas'), await del('m1'), await del('m9')]).toEqual([
      { status: 409, body: { error: "'nas' was never enrolled" } },
      { status: 200, body: { revoked: true } },
      { status: 404, body: { error: "no host 'm9'" } },
    ])
    expect((await hostStatus({ request: req('/api/hosts/m1', { bearer: token, origin: null }), env }, 'm1', NOW)).status).toBe(401)
  })
})
