// Under `ADMIN_LIST` no path mints a grant carrying `admin` or `*`; a grant already holding one keeps working and stays
// listed. With the list off, minting is as it was.
import { describe, expect, it } from 'vitest'
import { ADMIN_GRANT_REFUSED, ADMIN_ROTATE_REFUSED, ADMIN_WIDEN_REFUSED, AdminGrantRefused, type Env, GRANT_TERMS_REFUSED, gateFor, identify } from '../_lib/auth'
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
  const existing = (scopes: string[], terms: { expiresAt?: number | null; maxRedeems?: number | null; expiryEndsSessions?: boolean } = {}) =>
    gateFor(base)!.mint({ email: null, scopes, name: 'old link', createdBy: LEAD, expiresAt: null, maxRedeems: null, ...terms })
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
    expect([await w.call(authRoute, '/api/auth/request', null, 'POST', { email: STAFF }), scopesOfGrants(w.raw)]).toEqual([[200, { status: 'auto' }], ['gcs cw admin gcs:assign']])
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

describe('an admin grant\'s terms', () => {
  const NOW = Math.floor(Date.now() / 1000)
  const [PAST, SOON, LATER] = [NOW - 3600, NOW + 3600, NOW + 7200]
  const WIDEN = [403, { error: ADMIN_WIDEN_REFUSED }]
  type W = Awaited<ReturnType<typeof world>>
  const patch = (w: W, id: string, edit: unknown) => w.call(authRoute, `/api/auth/grants/${id}`, LEAD, 'PATCH', edit)
  const verb = (w: W, id: string, v: 'enable' | 'disable') => w.call(authRoute, `/api/auth/grants/${id}/${v}`, LEAD)
  /** The grant's terms as stored. */
  const terms = (w: W, id: string) => ({ ...w.raw.prepare('SELECT expires_at, max_redeems, expiry_ends_sessions, disabled_at IS NOT NULL AS disabled, revoked_at IS NOT NULL AS revoked FROM grants WHERE id = ?').all(id)[0] })
  const stored = (o: Partial<{ expires_at: number; max_redeems: number; expiry_ends_sessions: number; disabled: number; revoked: number }>) => ({ expires_at: null, max_redeems: null, expiry_ends_sessions: 1, disabled: 0, revoked: 0, ...o })
  const isAdmin = async (w: W, token: string) => (await identify({ request: new Request(`${SITE}/api/me`, { headers: { authorization: `Bearer ${token}` } }), env: w.env }))?.admin ?? null

  it.each([[['gcs', 'admin']], [['*']]])('the list on, %j: reviving an expired one, by a later expiry or none, is refused', async scopes => {
    const w = await world()
    const { grant, token } = await w.existing(scopes, { expiresAt: PAST })
    expect([await patch(w, grant.id, { expiresAt: LATER }), await patch(w, grant.id, { expiresAt: null }), terms(w, grant.id), await isAdmin(w, token)])
      .toEqual([WIDEN, WIDEN, stored({ expires_at: PAST }), null])
  })

  it('the list on: letting sessions outlive the expiry is refused too: it revives one as surely', async () => {
    const w = await world()
    const { grant } = await w.existing(['gcs', 'admin'], { expiresAt: PAST })
    expect([await patch(w, grant.id, { expiryEndsSessions: false }), terms(w, grant.id)]).toEqual([WIDEN, stored({ expires_at: PAST })])
  })

  it('the list on: a live one\'s expiry can\'t move later or go, and a value that isn\'t a time is refused', async () => {
    const w = await world()
    const { grant } = await w.existing(['gcs', 'admin'], { expiresAt: SOON })
    const { grant: never } = await w.existing(['gcs', 'admin'])
    expect([
      await patch(w, grant.id, { expiresAt: LATER }), await patch(w, grant.id, { expiresAt: null }), await patch(w, grant.id, { expiresAt: String(LATER) }),
      await patch(w, never.id, { expiresAt: 'never' }),
      terms(w, grant.id), terms(w, never.id),
    ]).toEqual([WIDEN, WIDEN, WIDEN, WIDEN, stored({ expires_at: SOON }), stored({})])
  })

  it('the list on: a redemption cap can\'t rise or go', async () => {
    const w = await world()
    const { grant } = await w.existing(['gcs', 'admin'], { maxRedeems: 1 })
    expect([await patch(w, grant.id, { maxRedeems: 2 }), await patch(w, grant.id, { maxRedeems: null }), terms(w, grant.id)]).toEqual([WIDEN, WIDEN, stored({ max_redeems: 1 })])
  })

  it('the list on: disabling is allowed; re-enabling is refused, and it stays disabled', async () => {
    const w = await world()
    const { grant } = await w.existing(['*'])
    expect([await verb(w, grant.id, 'disable'), await verb(w, grant.id, 'enable'), terms(w, grant.id)]).toEqual([[200, { ok: true }], WIDEN, stored({ disabled: 1 })])
  })

  it('the list on: shortening an expiry, setting one, lowering a cap and editing a memo are allowed', async () => {
    const w = await world()
    const { grant, token } = await w.existing(['gcs', 'admin'])
    const steps = [
      await patch(w, grant.id, { expiresAt: LATER }), await patch(w, grant.id, { expiresAt: SOON }),
      await patch(w, grant.id, { expiresAt: SOON, note: 'on call' }),
    ].map(([status, body]) => [status, (body.grant as { expiresAt: number; note: string | null }).expiresAt, (body.grant as { note: string | null }).note])
    expect(steps).toEqual([[200, LATER, null], [200, SOON, null], [200, SOON, 'on call']])
    const { grant: capped } = await w.existing(['gcs', 'admin'], { maxRedeems: 5 })
    expect([(await patch(w, capped.id, { maxRedeems: 1 }))[0], terms(w, grant.id), terms(w, capped.id), await isAdmin(w, token)])
      .toEqual([200, stored({ expires_at: SOON }), stored({ max_redeems: 1 }), true])
  })

  it('the list on: an expiry can be moved into the past: that ends it', async () => {
    const w = await world()
    const { grant, token } = await w.existing(['gcs', 'admin'], { expiresAt: SOON })
    expect([(await patch(w, grant.id, { expiresAt: PAST }))[0], await isAdmin(w, token)]).toEqual([200, null])
  })

  it('the list on: a revoke stays revoked, whatever is asked after it', async () => {
    const w = await world()
    const { grant, token } = await w.existing(['gcs', 'admin'], { expiresAt: SOON })
    expect((await w.call(authRoute, `/api/auth/grants/${grant.id}/revoke`, LEAD))[0]).toBe(200)
    expect([
      await patch(w, grant.id, { expiresAt: LATER }), await verb(w, grant.id, 'enable'), (await patch(w, grant.id, { expiresAt: PAST }))[0],
      terms(w, grant.id), await isAdmin(w, token),
    ]).toEqual([WIDEN, [200, { ok: false }], 200, stored({ expires_at: PAST, revoked: 1 }), null])
  })

  it('the list on: other grants\' terms move freely', async () => {
    const w = await world()
    const { grant } = await w.existing(['gcs'], { expiresAt: PAST })
    expect([(await patch(w, grant.id, { expiresAt: null }))[0], await verb(w, grant.id, 'disable'), await verb(w, grant.id, 'enable'), terms(w, grant.id)])
      .toEqual([200, [200, { ok: true }], [200, { ok: true }], stored({})])
  })

  it('the list off: an admin grant is revived and re-enabled as before', async () => {
    const w = await world(false)
    const { grant, token } = await w.existing(['gcs', 'admin'], { expiresAt: PAST })
    expect([
      (await patch(w, grant.id, { expiresAt: LATER }))[0], (await patch(w, grant.id, { expiresAt: null }))[0],
      await verb(w, grant.id, 'disable'), await verb(w, grant.id, 'enable'), terms(w, grant.id), await isAdmin(w, token),
    ]).toEqual([200, 200, [200, { ok: true }], [200, { ok: true }], stored({}), true])
  })
})

// `PATCH /api/auth/grants/<id>` takes `scopes`: the column read on every request, so a live link's sessions get them too.
describe('editing a grant\'s scopes', () => {
  const NOW = Math.floor(Date.now() / 1000)
  const WIDEN = [403, { error: ADMIN_WIDEN_REFUSED }]
  const NOT_NAMES = [400, { error: 'scopes must be a non-empty list of names' }]
  type W = Awaited<ReturnType<typeof world>>
  const patch = (w: W, id: string, edit: unknown) => w.call(authRoute, `/api/auth/grants/${id}`, LEAD, 'PATCH', edit)
  const stored = (w: W, id: string) => w.raw.prepare('SELECT scopes FROM grants WHERE id = ?').all(id)[0].scopes
  const identity = async (w: W, token: string) => {
    const id = await identify({ request: new Request(`${SITE}/api/me`, { headers: { authorization: `Bearer ${token}` } }), env: w.env })
    return id && { scopes: id.scopes, admin: id.admin }
  }
  /** A `PATCH`'s status and what the grant's row then says. */
  const edited = async (w: W, id: string, scopes: unknown) => {
    const [status, body] = await patch(w, id, { scopes })
    return [status, status === 200 ? (body.grant as { scopes: string[] }).scopes : body, stored(w, id)]
  }

  it.each([
    [['admin']],
    [['*']],
    [['gcs', 'admin']],
    [['admin', 'gcs']],
    [['gcs', '*']],
  ])('the list on: %j on an ordinary link is refused, its row unmoved and its holder no admin', async scopes => {
    const w = await world()
    const { grant, token } = await w.existing(['gcs'])
    expect([await patch(w, grant.id, { scopes }), stored(w, grant.id), await identity(w, token)]).toEqual([REFUSED, 'gcs', { scopes: ['gcs'], admin: false }])
  })

  it.each([[['gcs admin']], [['gcs,*']], [['gcs,admin']], ['admin'], ['gcs admin'], [[]], [[1]], [null]])('the list on: %j never reaches the store: the package\'s route refuses what isn\'t a list of names', async scopes => {
    const w = await world()
    const { grant } = await w.existing(['gcs'])
    expect([await patch(w, grant.id, { scopes }), stored(w, grant.id)]).toEqual([NOT_NAMES, 'gcs'])
  })

  // What `carriesAdmin` must catch when the route's check isn't in front of it (`gate.update`, the store itself): the
  // column reads back split on spaces and commas, and an array stringifies with commas.
  it.each([
    [['a', 'admin']],
    [['admin', 'a']],
    [['a admin']],
    [['a,admin']],
    [['a', '*']],
    [['a *']],
    [['a,*']],
    ['admin'],
    ['a admin'],
    ['a,admin'],
    ['*'],
    ['a,*'],
  ])('the list on: `gate.update` with %j is refused at the store', async scopes => {
    const w = await world()
    const { grant } = await w.existing(['gcs'])
    const thrown = await w.gate.update(grant.id, { scopes: scopes as string[] }).catch((e: Error) => [e.constructor, e.message])
    expect([thrown, stored(w, grant.id)]).toEqual([[AdminGrantRefused, ADMIN_GRANT_REFUSED], 'gcs'])
  })

  it('the list on: other scopes move freely on an ordinary link, either way, and its holder has them on the next request', async () => {
    const w = await world()
    const { grant, token } = await w.existing(['gcs', 'gcs:read'])
    expect([await edited(w, grant.id, ['gcs:read']), await identity(w, token)]).toEqual([[200, ['gcs:read'], 'gcs:read'], { scopes: ['gcs:read'], admin: false }])
    expect([await edited(w, grant.id, ['gcs:read', 'gcs']), await identity(w, token)]).toEqual([[200, ['gcs:read', 'gcs'], 'gcs:read gcs'], { scopes: ['gcs:read', 'gcs'], admin: false }])
  })

  it.each([
    [['gcs', 'admin'], ['gcs'], 'gcs'],
    [['gcs', 'gcs:read', 'admin'], ['gcs:read'], 'gcs:read'],
    [['*'], ['gcs'], 'gcs'],
  ])('the list on: an admin grant, %j, can drop to %j, and is no admin from then on', async (from, to, row) => {
    const w = await world()
    const { grant, token } = await w.existing(from)
    expect([await edited(w, grant.id, to), await identity(w, token)]).toEqual([[200, to, row], { scopes: to, admin: false }])
  })

  it.each([
    [['admin'], ['gcs'], 'admin'],
    [['gcs', 'admin'], ['gcs', 'gcs:read'], 'gcs admin'],
  ])('the list on: an admin grant, %j, can\'t trade it for a scope it didn\'t hold (%j)', async (from, to, row) => {
    const w = await world()
    const { grant } = await w.existing(from)
    expect([await patch(w, grant.id, { scopes: to }), stored(w, grant.id)]).toEqual([WIDEN, row])
  })

  it.each([
    [['gcs', 'admin'], ['gcs', 'admin'], 'gcs admin'],
    [['gcs', 'admin'], ['admin'], 'gcs admin'],
    [['gcs', 'admin'], ['gcs', 'admin', 'gcs:read'], 'gcs admin'],
    [['gcs', 'admin'], ['*'], 'gcs admin'],
    [['*'], ['admin'], '*'],
    [['admin'], ['admin', '*'], 'admin'],
  ])('the list on: an admin grant, %j, keeping `admin` or `*` through an edit (%j) is refused like any other', async (from, to, row) => {
    const w = await world()
    const { grant } = await w.existing(from)
    expect([await patch(w, grant.id, { scopes: to }), stored(w, grant.id)]).toEqual([REFUSED, row])
  })

  it('the list on: an expired admin grant dropping `admin` is allowed, and stays expired', async () => {
    const w = await world()
    const { grant, token } = await w.existing(['gcs', 'admin'], { expiresAt: NOW - 3600 })
    expect([await edited(w, grant.id, ['gcs']), await identity(w, token)]).toEqual([[200, ['gcs'], 'gcs'], null])
  })

  it('the list on: a revoked link is the package\'s 409, whatever the scopes', async () => {
    const w = await world()
    const { grant } = await w.existing(['gcs'])
    await w.gate.revoke(grant.id)
    expect([await patch(w, grant.id, { scopes: ['admin'] }), stored(w, grant.id)]).toEqual([[409, { error: 'link is revoked' }], 'gcs'])
  })

  it.each([[{ expiresAt: 'never' }], [{ maxRedeems: 'many' }], [{ sessionTtlS: 'forever' }]])('the list on: bad terms beside good scopes (%j) are refused, and the scopes don\'t land', async terms => {
    const w = await world()
    const { grant } = await w.existing(['gcs'])
    expect([await patch(w, grant.id, { scopes: ['gcs:read'], ...terms }), stored(w, grant.id)]).toEqual([[400, { error: GRANT_TERMS_REFUSED }], 'gcs'])
  })

  it.each([[{ expiresAt: 'never' }], [{ maxRedeems: 'many' }], [{ sessionTtlS: 'forever' }]])('the list off: bad terms beside good scopes (%j) are refused, and the scopes don\'t land', async terms => {
    const w = await world(false)
    const { grant } = await w.existing(['gcs'])
    expect([await patch(w, grant.id, { scopes: ['gcs:read'], ...terms }), stored(w, grant.id)]).toEqual([[400, { error: GRANT_TERMS_REFUSED }], 'gcs'])
  })

  it.each([
    [['admin'], 'admin'],
    [['*'], '*'],
    [['gcs', 'admin'], 'gcs admin'],
  ])('the list off: %j lands, as a mint with it does', async (scopes, row) => {
    const w = await world(false)
    const { grant } = await w.existing(['gcs'])
    expect(await edited(w, grant.id, scopes)).toEqual([200, scopes, row])
  })
})

it('the refusals say what to do', () => {
  expect([ADMIN_GRANT_REFUSED, ADMIN_ROTATE_REFUSED, ADMIN_WIDEN_REFUSED]).toEqual([
    'with `ADMIN_LIST` on, a grant can\'t carry `admin` or `*`: add the person to the admin list instead',
    'with `ADMIN_LIST` on, an admin grant can\'t be re-keyed: revoke it, and add the person to the admin list instead',
    'with `ADMIN_LIST` on, an admin grant can only lose access: it can\'t be re-enabled, or given new scopes, a later expiry, a higher redemption cap or sessions that outlive its expiry. Add the person to the admin list instead',
  ])
})

// The package's routes store what they're given: a PATCH's `expiresAt` as is, a mint's `expiresInS` added to the clock
// (`'x'` concatenates). A stored string compares as never reached (`expires_at <= now` is false), so a link would
// outlive any expiry it was given. Refused at `grantStore`, for every grant, with the list on or off.
describe.each([[true], [false]])('a grant\'s terms that aren\'t numbers (the list on: %j)', listOn => {
  const BAD = [400, { error: GRANT_TERMS_REFUSED }]
  const NOW = Math.floor(Date.now() / 1000)
  const row = (raw: Sqlite, id: string) => raw.prepare('SELECT expires_at, max_redeems, session_ttl FROM grants WHERE id = ?').all(id)[0]
  const isLive = async (env: Env, token: string) => (await identify({ request: new Request(`${SITE}/api/me`, { headers: { authorization: `Bearer ${token}` } }), env })) !== null

  it.each([
    [{ expiresAt: 'never' }], [{ expiresAt: '1893456000' }], [{ expiresAt: true }], [{ expiresAt: {} }],
    [{ maxRedeems: 'many' }], [{ maxRedeems: '5' }],
    [{ sessionTtlS: 'forever' }],
  ])('`PATCH` %j on a plain link is refused and nothing moves', async edit => {
    const w = await world(listOn)
    const { grant } = await w.existing(['gcs'], { expiresAt: NOW + 3600 })
    expect([await w.call(authRoute, `/api/auth/grants/${grant.id}`, LEAD, 'PATCH', edit), row(w.raw, grant.id)])
      .toEqual([BAD, { expires_at: NOW + 3600, max_redeems: null, session_ttl: null }])
  })

  it('an expired plain link stays expired under `expiresAt: "never"`', async () => {
    const w = await world(listOn)
    const { grant, token } = await w.existing(['gcs'], { expiresAt: NOW - 3600 })
    expect([await w.call(authRoute, `/api/auth/grants/${grant.id}`, LEAD, 'PATCH', { expiresAt: 'never' }), await isLive(w.env, token)]).toEqual([BAD, false])
  })

  it.each([[{ expiresInS: 'x' }], [{ expiresInS: '60' }], [{ maxRedeems: 'many' }], [{ sessionTtlS: 'forever' }]])('minting with %j is refused and nothing is written', async terms => {
    const w = await world(listOn)
    expect([await w.call(authRoute, '/api/auth/grants', LEAD, 'POST', { name: 'a link', scopes: ['gcs'], ...terms }), scopesOfGrants(w.raw)]).toEqual([BAD, []])
  })

  it('numbers and null pass, on a mint and a `PATCH`', async () => {
    const w = await world(listOn)
    const [minted, { grant }] = [await w.call(authRoute, '/api/auth/grants', LEAD, 'POST', { name: 'a link', scopes: ['gcs'], expiresInS: 60, maxRedeems: 2 }), await w.existing(['gcs'])]
    const [edited, cleared] = [
      await w.call(authRoute, `/api/auth/grants/${grant.id}`, LEAD, 'PATCH', { expiresAt: NOW + 60, maxRedeems: 2, sessionTtlS: 600 }),
      await w.call(authRoute, `/api/auth/grants/${grant.id}`, LEAD, 'PATCH', { expiresAt: null, maxRedeems: null, sessionTtlS: null }),
    ]
    expect([minted[0], edited[0], cleared[0], row(w.raw, grant.id)]).toEqual([200, 200, 200, { expires_at: null, max_redeems: null, session_ttl: null }])
  })
})
