// Under `ADMIN_LIST` no path mints a grant carrying `admin` or `*`; a grant already holding one keeps working and stays
// listed. With the list off, minting is as it was.
import { describe, expect, it } from 'vitest'
import { ADMIN_GRANT_REFUSED, ADMIN_ROTATE_REFUSED, type Env, gateFor, identify } from '../_lib/auth'
import { type Sqlite, sqliteD1, withAdminEmails } from '../_lib/testD1'
import { onRequest as adminsRoute } from './admins'
import { onRequest as authRoute } from './auth/[[path]]'
import { onRequest as tokenRoute } from './token'

const SITE = 'https://gcs.example.test'
const [LEAD, STAFF, GUEST] = ['lead@example.org', 'dev@staff.example.test', 'guest@example.org']
const REFUSED = [403, { error: ADMIN_GRANT_REFUSED }]

/** A deployment with `LEAD` listed and `GUEST` allowlisted; the list on unless `listOn` is false. The caller is an admin
 *  either way: `LEAD` on the list (`ADMIN_EMAILS` admits the table, list off). */
async function world(listOn = true) {
  const { db, raw } = await sqliteD1()
  withAdminEmails(raw)
  raw.prepare("INSERT INTO admin_emails (email, note, who, ts) VALUES (?, NULL, 'seed', 1)").run(LEAD)
  for (const e of [LEAD, GUEST]) raw.prepare("INSERT INTO allowed_emails (email, note, who, ts) VALUES (?, NULL, 'seed', 1)").run(e)
  const base = { DB: db, SESSION_SECRET: 'secret', BASE_SCOPE: 'gcs', STAFF_DOMAIN: 'staff.example.test', ADMIN_EMAILS: '1' } as Env
  const env = (listOn ? { ...base, ADMIN_LIST: '1' } : base) as Env
  const gate = gateFor(env)!
  /** A grant from before the flip: minted with the list off, on the same database. */
  const existing = (scopes: string[]) => gateFor(base)!.mint({ email: null, scopes, name: 'old link', createdBy: LEAD, expiresAt: null, maxRedeems: null })
  const cookie = async (email: string) => (await gate.signIn(email, new Request(SITE)))!.cookie.split(';')[0]
  const call = async (route: typeof authRoute, path: string, as: string | null, method = 'POST', body?: unknown): Promise<[number, Record<string, unknown>]> => {
    const headers: Record<string, string> = body === undefined ? {} : { 'content-type': 'application/json' }
    if (as) headers.cookie = await cookie(as)
    const res = await route({ request: new Request(`${SITE}${path}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) }), env })
    return [res.status, await res.json() as Record<string, unknown>]
  }
  return { env, raw, gate, existing, call }
}

const scopesOfGrants = (raw: Sqlite) => raw.prepare('SELECT scopes FROM grants ORDER BY created_at, id').all().map(r => r.scopes)
const mint = (w: Awaited<ReturnType<typeof world>>, scopes: unknown) => w.call(authRoute, '/api/auth/grants', LEAD, 'POST', { name: 'a link', scopes })

describe('`POST /api/auth/grants`', () => {
  it.each([
    ['`admin`', ['admin']],
    ['`*`', ['*']],
    ['`admin` beside others', ['gcs', 'admin']],
    ['`admin` inside a space-joined entry (the column splits on it)', ['gcs admin']],
    ['`*` inside a comma-joined entry', ['gcs,*']],
  ])('the list on: %s is refused, and nothing is written', async (_, scopes) => {
    const w = await world()
    expect(await mint(w, scopes)).toEqual(REFUSED)
    expect(scopesOfGrants(w.raw)).toEqual([])
  })

  it('the list on: other scopes mint', async () => {
    const w = await world()
    const [status, body] = await mint(w, ['gcs', 'gcs:read'])
    expect([status, (body.grant as { scopes: string[] }).scopes, typeof body.token, scopesOfGrants(w.raw)]).toEqual([200, ['gcs', 'gcs:read'], 'string', ['gcs gcs:read']])
  })

  it('the list off: `admin` and `*` mint as they did', async () => {
    const w = await world(false)
    const [[a, admin], [s, all]] = [await mint(w, ['gcs', 'admin']), await mint(w, ['*'])]
    expect([a, (admin.grant as { scopes: string[] }).scopes, s, (all.grant as { scopes: string[] }).scopes]).toEqual([200, ['gcs', 'admin'], 200, ['*']])
  })
})

describe('`POST /api/auth/requests/<id>/approve`', () => {
  const pending = async (w: Awaited<ReturnType<typeof world>>) => {
    // Nobody the policy admits: a pending request, no grant.
    expect(await w.call(authRoute, '/api/auth/request', null, 'POST', { email: 'new@example.org' })).toEqual([200, { status: 'pending' }])
    const [, { requests }] = await w.call(authRoute, '/api/auth/requests', LEAD, 'GET')
    return (requests as { id: string }[])[0].id
  }

  it('the list on: approving with `admin` or `*` is refused and the request stays pending; other scopes approve', async () => {
    const w = await world()
    const id = await pending(w)
    expect([
      await w.call(authRoute, `/api/auth/requests/${id}/approve`, LEAD, 'POST', { scopes: ['gcs', 'admin'] }),
      await w.call(authRoute, `/api/auth/requests/${id}/approve`, LEAD, 'POST', { scopes: ['*'] }),
      scopesOfGrants(w.raw),
      w.raw.prepare('SELECT status FROM access_requests').all().map(r => r.status),
    ]).toEqual([REFUSED, REFUSED, [], ['pending']])
    const [status] = await w.call(authRoute, `/api/auth/requests/${id}/approve`, LEAD, 'POST', { scopes: ['gcs:read'] })
    expect([status, scopesOfGrants(w.raw)]).toEqual([200, ['gcs:read']])
  })

  it('the list off: approving with `admin` mints as it did', async () => {
    const w = await world(false)
    const [status] = await w.call(authRoute, `/api/auth/requests/${await pending(w)}/approve`, LEAD, 'POST', { scopes: ['gcs', 'admin'] })
    expect([status, scopesOfGrants(w.raw)]).toEqual([200, ['gcs admin']])
  })
})

describe('`POST /api/auth/request`: an address the policy admits gets a grant by itself', () => {
  it('the list on: a listed admin\'s grant carries their scopes without `admin`; the answer is unchanged', async () => {
    const w = await world()
    expect([await w.call(authRoute, '/api/auth/request', null, 'POST', { email: LEAD }), scopesOfGrants(w.raw)]).toEqual([[200, { status: 'auto' }], ['gcs gcs:assign']])
  })

  it('the list off: staff\'s carries `admin`, as it did', async () => {
    const w = await world(false)
    expect([await w.call(authRoute, '/api/auth/request', null, 'POST', { email: STAFF }), scopesOfGrants(w.raw)]).toEqual([[200, { status: 'auto' }], ['gcs cw admin requests gcs:assign']])
  })
})

it('`POST /api/token`: a listed admin\'s agent token mints, without `admin`', async () => {
  const w = await world()
  const [status, body] = await w.call(tokenRoute, '/api/token', LEAD)
  expect([status, Object.keys(body).sort(), scopesOfGrants(w.raw)]).toEqual([200, ['created', 'token'], ['gcs gcs:assign']])
})

describe('a live admin grant, under the list', () => {
  it('still authenticates as an admin, and stays listed with its id for the revoke control', async () => {
    const w = await world()
    const { grant, token } = await w.existing(['gcs', 'admin'])
    const headers = { authorization: `Bearer ${token}` }
    const id = await identify({ request: new Request(`${SITE}/api/me`, { headers }), env: w.env })
    const res = await adminsRoute({ request: new Request(`${SITE}/api/admins`, { headers }), env: w.env })
    const body = await res.json() as { grants: { id: string; scopes: string[] }[] }
    expect([id?.admin, id?.via, id?.scopes, res.status, body.grants.map(g => [g.id, g.scopes])]).toEqual([true, 'grant', ['gcs', 'admin'], 200, [[grant.id, ['gcs', 'admin']]]])
  })

  it('can be revoked, and stops being an admin at once', async () => {
    const w = await world()
    const { grant, token } = await w.existing(['*'])
    const headers = { authorization: `Bearer ${token}` }
    const revoked = await w.call(authRoute, `/api/auth/grants/${grant.id}/revoke`, LEAD)
    expect([revoked, await identify({ request: new Request(`${SITE}/api/me`, { headers }), env: w.env })]).toEqual([[200, { ok: true }], null])
  })

  it('can\'t be re-keyed: a rotation hands out a new secret carrying `admin`; a viewer link can, and the list off, any', async () => {
    const w = await world()
    const off = await world(false)
    const [admin, viewer, offAdmin] = [await w.existing(['gcs', 'admin']), await w.existing(['gcs']), await off.existing(['gcs', 'admin'])]
    const [refused, [viewerStatus], [offStatus]] = [
      await w.call(authRoute, `/api/auth/grants/${admin.grant.id}/rotate`, LEAD, 'POST', {}),
      await w.call(authRoute, `/api/auth/grants/${viewer.grant.id}/rotate`, LEAD, 'POST', {}),
      await off.call(authRoute, `/api/auth/grants/${offAdmin.grant.id}/rotate`, LEAD, 'POST', {}),
    ]
    expect([refused, viewerStatus, offStatus]).toEqual([[403, { error: ADMIN_ROTATE_REFUSED }], 200, 200])
    // The old token still works: the refusal changed nothing.
    expect((await identify({ request: new Request(`${SITE}/api/me`, { headers: { authorization: `Bearer ${admin.token}` } }), env: w.env }))?.admin).toBe(true)
  })
})

it('the refusals say what to do', () => {
  expect([ADMIN_GRANT_REFUSED, ADMIN_ROTATE_REFUSED]).toEqual([
    'with `ADMIN_LIST` on, a grant can\'t carry `admin` or `*`: add the person to the admin list instead',
    'with `ADMIN_LIST` on, an admin grant can\'t be re-keyed: revoke it, and add the person to the admin list instead',
  ])
})
