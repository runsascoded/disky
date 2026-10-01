import type { D1Database } from '@cloudflare/workers-types'
import { describe, expect, it } from 'vitest'
import { APP_LINK_NAME, APP_LINK_TTL_S, mintAppLink, redeemAppLink } from './appLink'
import { type Env, gateFor, identify } from './auth'
import { sqliteD1 } from './testD1'

const ORIGIN = 'https://disk.example'
const VIEWER = 'viewer@example.org'
const OPS = 'ops@example.org'
const STAFF = 'ryan@openathena.ai'
const TOKEN_RE = /^[A-Za-z0-9_-]{20,}$/

async function setup() {
  const { db } = await sqliteD1('cw')
  for (const email of [VIEWER, OPS]) {
    await db.prepare('INSERT INTO allowed_emails (email, who, ts) VALUES (?, ?, 1)').bind(email, STAFF).run()
  }
  await db.prepare('INSERT INTO admin_emails (email, who, ts) VALUES (?, ?, 1)').bind(OPS, STAFF).run()
  const env: Env = { DB: db, SESSION_SECRET: 'test-secret', BASE_SCOPE: 'cw', STAFF_DOMAIN: 'openathena.ai', ADMIN_EMAILS: '1' }
  const gate = gateFor(env)!
  /** A signed-in browser's cookie pair (`oa_auth=…`). */
  const session = async (email: string) =>
    (await gate.signIn(email, new Request(`${ORIGIN}/signin`)))!.cookie.split(';')[0]
  return { db, env, gate, session }
}

const sameOrigin = { origin: ORIGIN, 'sec-fetch-site': 'same-origin' }

const post = (headers: Record<string, string>, body?: unknown) => new Request(`${ORIGIN}/api/app-link`, {
  method: 'POST',
  headers: body === undefined ? headers : { ...headers, 'content-type': 'application/json' },
  body: body === undefined ? undefined : JSON.stringify(body),
})

const bodyOf = async (res: Response) => ({ status: res.status, body: await res.json() as { url: string; expires_at: number; error?: string } })

/** The minted link with its (random) token pulled out, so the rest compares exactly. */
async function minted(res: Response) {
  const { status, body } = await bodyOf(res)
  const url = new URL(body.url)
  const token = url.searchParams.get('token')!
  url.searchParams.set('token', '<token>')
  return { status, url: decodeURIComponent(url.toString()), expires_at: body.expires_at, token }
}

const grants = async (db: D1Database) =>
  (await db.prepare('SELECT name, email, scopes, max_redeems, redeems, expires_at, created_by, revoked_at IS NOT NULL AS revoked FROM grants ORDER BY created_at, id').all()).results

const log = async (db: D1Database) =>
  (await db.prepare('SELECT event, session_sub, reason FROM access_log ORDER BY id').all()).results

const redeem = (url: string, cookie?: string) => new Request(url, cookie ? { headers: { cookie } } : {})

describe('POST /api/app-link — mint', () => {
  it("a signed-in viewer gets a single-use link carrying exactly their email and scopes, expiring in APP_LINK_TTL_S", async () => {
    const s = await setup()
    const now = Date.now()
    const nowS = Math.floor(now / 1000)
    const res = await mintAppLink({ request: post({ ...sameOrigin, cookie: await s.session(VIEWER) }, { next: '/c/data?x=1' }), env: s.env }, now)
    expect(res.headers.get('cache-control')).toBe('no-store')
    const m = await minted(res)
    expect(m.token).toMatch(TOKEN_RE)
    expect({ ...m, token: undefined }).toEqual({
      status: 200,
      url: `${ORIGIN}/auth/app-link?token=<token>&next=/c/data?x=1`,
      expires_at: nowS + 60,
      token: undefined,
    })
    expect(APP_LINK_TTL_S).toBe(60)
    expect(await grants(s.db)).toEqual([
      { name: APP_LINK_NAME, email: VIEWER, scopes: 'cw', max_redeems: 1, redeems: 0, expires_at: nowS + 60, created_by: VIEWER, revoked: 0 },
    ])
  })

  it('never more than the caller has: an admin_emails viewer carries admin, a plain viewer does not, staff carry all', async () => {
    const s = await setup()
    for (const email of [VIEWER, OPS, STAFF]) {
      await mintAppLink({ request: post({ ...sameOrigin, cookie: await s.session(email) }), env: s.env })
    }
    const rows = (await grants(s.db)) as { email: string; scopes: string }[]
    expect(rows.map(r => [r.email, r.scopes]).sort()).toEqual([
      [OPS, 'cw admin'],
      [STAFF, 'gcs cw admin requests'],
      [VIEWER, 'cw'],
    ])
  })

  it('audits the mint under its own kind, and never records the token', async () => {
    const s = await setup()
    const res = await mintAppLink({ request: post({ ...sameOrigin, cookie: await s.session(VIEWER) }), env: s.env })
    const { token } = await minted(res)
    expect(await log(s.db)).toEqual([
      { event: 'signin', session_sub: `e:${VIEWER}`, reason: null },   // the test's own browser sign-in
      { event: 'mint', session_sub: `e:${VIEWER}`, reason: null },     // the gate's generic row
      { event: 'app-link', session_sub: `e:${VIEWER}`, reason: 'mint' },
    ])
    const everything = JSON.stringify((await s.db.prepare('SELECT * FROM access_log').all()).results)
    expect(everything.includes(token)).toBe(false)
  })

  it('refuses a grant caller (403), an anonymous one (401), a cross-site request (403) and a GET (405)', async () => {
    const s = await setup()
    const share = await s.gate.mint({ email: VIEWER, scopes: ['cw'], createdBy: STAFF })
    const cookie = await s.session(VIEWER)
    const cases = {
      grant: post({ ...sameOrigin, authorization: `Bearer ${share.token}` }),
      anonymous: post(sameOrigin),
      foreignOrigin: post({ origin: 'https://evil.example', 'sec-fetch-site': 'cross-site', cookie }),
      nullOrigin: post({ origin: 'null', cookie }),
      noOrigin: post({ cookie }),
      sameSite: post({ origin: ORIGIN, 'sec-fetch-site': 'same-site', cookie }),
      get: new Request(`${ORIGIN}/api/app-link`, { headers: { ...sameOrigin, cookie } }),
    }
    const out: Record<string, unknown> = {}
    for (const [k, req] of Object.entries(cases)) out[k] = await bodyOf(await mintAppLink({ request: req, env: s.env }))
    expect(out).toEqual({
      grant: { status: 403, body: { error: 'only a signed-in session can open the app' } },
      anonymous: { status: 401, body: { error: 'unauthenticated' } },
      foreignOrigin: { status: 403, body: { error: 'cross-site request refused' } },
      nullOrigin: { status: 403, body: { error: 'cross-site request refused' } },
      noOrigin: { status: 403, body: { error: 'cross-site request refused' } },
      sameSite: { status: 403, body: { error: 'cross-site request refused' } },
      get: { status: 405, body: { error: 'method not allowed' } },
    })
    // Only the admin's share link exists — nothing was minted.
    expect(((await grants(s.db)) as { name: string | null }[]).map(g => g.name)).toEqual([null])
  })

  it('an off-origin `next` falls back to home', async () => {
    const s = await setup()
    const cookie = await s.session(VIEWER)
    const urls = []
    for (const next of ['//evil.example/x', 'https://evil.example/', '/\\evil.example', 42]) {
      urls.push((await minted(await mintAppLink({ request: post({ ...sameOrigin, cookie }, { next }), env: s.env }))).url)
    }
    expect(urls).toEqual(Array(4).fill(`${ORIGIN}/auth/app-link?token=<token>`))
  })
})

describe('GET /auth/app-link — redeem', () => {
  async function mintFor(email: string, now = Date.now(), next?: string) {
    const s = await setup()
    const res = await mintAppLink({ request: post({ ...sameOrigin, cookie: await s.session(email) }, next ? { next } : undefined), env: s.env }, now)
    return { ...s, url: (await res.json() as { url: string }).url }
  }
  const redeemed = (res: Response) => ({
    status: res.status,
    location: res.headers.get('location'),
    cookie: res.headers.getSetCookie().map(c => c.split('=')[0]),
  })

  it('once → an ordinary email session for the same email and scopes, landing on `next`', async () => {
    const s = await mintFor(OPS, Date.now(), '/c/data')
    const res = await redeemAppLink({ request: redeem(s.url), env: s.env })
    expect(redeemed(res)).toEqual({ status: 303, location: '/c/data', cookie: ['oa_auth'] })
    const cookie = res.headers.getSetCookie()[0].split(';')[0]
    const id = await identify({ request: new Request(`${ORIGIN}/`, { headers: { cookie } }), env: s.env })
    expect(id).toEqual({ email: OPS, name: null, scopes: ['cw', 'admin'], admin: true, via: 'session', subject: null })
  })

  it('twice → the second is refused; the spent token no longer works as a Bearer either', async () => {
    const s = await mintFor(VIEWER)
    const first = await redeemAppLink({ request: redeem(s.url), env: s.env })
    const second = await redeemAppLink({ request: redeem(s.url), env: s.env })
    expect([redeemed(first), redeemed(second)]).toEqual([
      { status: 303, location: '/', cookie: ['oa_auth'] },
      { status: 303, location: '/signin?error=app-link%3Arevoked', cookie: [] },
    ])
    const token = new URL(s.url).searchParams.get('token')!
    expect(await identify({ request: new Request(`${ORIGIN}/`, { headers: { authorization: `Bearer ${token}` } }), env: s.env })).toBe(null)
    expect(await grants(s.db)).toEqual([
      expect.objectContaining({ max_redeems: 1, redeems: 1, revoked: 1 }),
    ])
  })

  it('after expiry → refused, unspent', async () => {
    const now = Date.now()
    const s = await mintFor(VIEWER, now)
    const res = await redeemAppLink({ request: redeem(s.url), env: s.env }, now + (APP_LINK_TTL_S + 1) * 1000)
    expect(redeemed(res)).toEqual({ status: 303, location: '/signin?error=app-link%3Aexpired', cookie: [] })
    expect(await grants(s.db)).toEqual([expect.objectContaining({ redeems: 0, revoked: 0 })])
  })

  it('an address delisted between mint and redeem gets no session', async () => {
    const s = await mintFor(VIEWER)
    await s.db.prepare('DELETE FROM allowed_emails WHERE email = ?').bind(VIEWER).run()
    const res = await redeemAppLink({ request: redeem(s.url), env: s.env })
    expect(redeemed(res)).toEqual({ status: 303, location: '/signin?error=app-link%3Anot-allowed', cookie: [] })
  })

  it("an admin's share link is not an app link: refused, its redemption left unspent", async () => {
    const s = await setup()
    const share = await s.gate.mint({ email: VIEWER, scopes: ['cw:read'], maxRedeems: 1, createdBy: STAFF })
    const res = await redeemAppLink({ request: redeem(`${ORIGIN}/auth/app-link?token=${share.token}`), env: s.env })
    expect(redeemed(res)).toEqual({ status: 303, location: '/signin?error=app-link%3Abad-token', cookie: [] })
    expect(await grants(s.db)).toEqual([expect.objectContaining({ redeems: 0, revoked: 0 })])
  })

  it('audits the redemption under its own kind (beside the gate rows), never the token', async () => {
    const s = await mintFor(VIEWER)
    await redeemAppLink({ request: redeem(s.url), env: s.env })
    expect(await log(s.db)).toEqual([
      { event: 'signin', session_sub: `e:${VIEWER}`, reason: null },
      { event: 'mint', session_sub: `e:${VIEWER}`, reason: null },
      { event: 'app-link', session_sub: `e:${VIEWER}`, reason: 'mint' },
      { event: 'redeem', session_sub: expect.stringMatching(/^g:/), reason: null },
      { event: 'revoke', session_sub: null, reason: null },
      { event: 'signin', session_sub: `e:${VIEWER}`, reason: null },
      { event: 'app-link', session_sub: `e:${VIEWER}`, reason: 'redeem' },
    ])
    const token = new URL(s.url).searchParams.get('token')!
    expect(JSON.stringify((await s.db.prepare('SELECT * FROM access_log').all()).results).includes(token)).toBe(false)
  })

  it('a POST is 405', async () => {
    const s = await mintFor(VIEWER)
    const res = await redeemAppLink({ request: new Request(s.url, { method: 'POST' }), env: s.env })
    expect(res.status).toBe(405)
  })
})
