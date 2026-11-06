/** specs/oa-login-rp.md phase 2: an unauthenticated page visit tries the broker once, silently, before the wall. Through
 *  the middleware and the real routes over an in-memory D1; the broker is a stubbed `fetch`, never a live one. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { emailSub, sessionCookie, signSession } from '@open-athena/auth'
import type { Env } from './auth'
import { sqliteD1 } from './testD1'

vi.mock('./og/vendor/resvg.wasm', () => ({ default: new Uint8Array() }))

const { onRequest } = await import('../_middleware')
const { onRequest: oaStart } = await import('../auth/oa')
const { onRequest: oaCallback } = await import('../auth/oa/callback')
const { onRequest: authRoutes } = await import('../api/auth/[[path]]')

const ORIGIN = 'https://dev.example.test'
const SECRET = 's'.repeat(32)
const CONFIGURED = { OA_LOGIN_ISSUER: 'https://login.example.test', OA_LOGIN_CLIENT_ID: 'disky-test', OA_LOGIN_CLIENT_SECRET: 'shh' }
const NAV = { 'sec-fetch-mode': 'navigate', 'sec-fetch-dest': 'document' }
const ATTEMPT = 'oa_sso_skip=attempt; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=300'
const SIGNED_OUT = 'oa_sso_skip=signout; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=34560000'
const CLEARED = 'oa_sso_skip=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0'
const SESSION_CLEARED = 'oa_auth=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0'
const SHELL = '<!doctype html>shell\n'

// `stampMeta`'s rewriter is the Workers runtime's: the shell passes through as it is.
beforeEach(() => { vi.stubGlobal('HTMLRewriter', class { on() { return this } transform(r: Response) { return r } }) })
afterEach(() => { vi.unstubAllGlobals() })

const html = () => new Response(SHELL, { headers: { 'content-type': 'text/html; charset=utf-8' } })
const envOf = async (extra: Record<string, unknown> = {}): Promise<Env> => ({
  DB: (await sqliteD1()).db,
  SESSION_SECRET: SECRET,
  STAFF_DOMAIN: 'example.test',
  ASSETS: { fetch: async () => html() },
  ...CONFIGURED,
  ...extra,
}) as unknown as Env

type Route = (ctx: never) => Response | Promise<Response>
/** A request through the middleware, with `route` as what it hands on to (the SPA fallback's shell by default). */
const visit = (env: Env, path: string, headers: Record<string, string> = NAV, method = 'GET', route: Route = () => html()) => {
  const request = new Request(`${ORIGIN}${path}`, { method, headers })
  return onRequest({ request, env: env as never, next: async () => route({ request, env } as never) })
}
/** What decides the case: status, where it goes, what it sets. */
const outcome = (r: Response) => [r.status, r.headers.get('location'), r.headers.getSetCookie()]
const WALL = [200, null, []]
const attemptFor = (next: string) => [302, `/auth/oa?prompt=none&next=${encodeURIComponent(next)}`, [ATTEMPT]]
const sessionFor = async (email: string, ttlS = 3600) =>
  sessionCookie(await signSession(emailSub(email), SECRET, Date.now(), ttlS), { name: 'oa_auth', ttlS, secure: true }).split(';')[0]

describe('a person\'s unauthenticated page visit goes to the broker once, silently', () => {
  it('a top-level navigation is sent to `/auth/oa?prompt=none`, keeping its path and query, and marked attempted', async () => {
    const env = await envOf()
    const got = []
    for (const p of ['/', '/scans?a=1&b=two%20words', '/marin-a/b', '/staged', '/runs/plan-1/20261104T125245Z']) got.push(outcome(await visit(env, p)))
    expect(got).toEqual([
      attemptFor('/'),
      attemptFor('/scans?a=1&b=two%20words'),
      attemptFor('/marin-a/b'),
      attemptFor('/staged'),
      attemptFor('/runs/plan-1/20261104T125245Z'),
    ])
  })
  it('the redirect is never cached', async () => {
    expect((await visit(await envOf(), '/')).headers.get('cache-control')).toBe('no-store')
  })
  it('a path `next` couldn\'t carry (`//host`, what an open redirect looks like) gets no attempt', async () => {
    expect(outcome(await visit(await envOf(), '//evil.example.test/x'))).toEqual(WALL)
  })
})

describe('what never gets an attempt', () => {
  it('unconfigured (any of issuer, client id, secret unset; no gate): the same response, byte for byte, as without this feature', async () => {
    const got = []
    for (const extra of [{ OA_LOGIN_ISSUER: undefined }, { OA_LOGIN_CLIENT_ID: undefined }, { OA_LOGIN_CLIENT_SECRET: undefined }, { DB: undefined }, { SESSION_SECRET: undefined }]) {
      const r = await visit(await envOf(extra), '/runs/1')
      got.push([...outcome(r), await r.text()])
    }
    expect(got).toEqual(Array(5).fill([200, null, [], SHELL]))
  })
  it('a fetch without both navigation headers: a crawler or unfurler (no `Sec-Fetch-*`) keeps the page; so does anything else not a top-level document', async () => {
    const env = await envOf()
    const slack = { 'user-agent': 'Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)' }
    const discord = { 'user-agent': 'Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)' }
    const got = []
    for (const [p, h] of [
      ['/', {}], ['/', slack], ['/staged', slack], ['/runs/1', discord], ['/marin-a', discord],
      ['/', { 'sec-fetch-mode': 'navigate' }],
      ['/', { 'sec-fetch-dest': 'document' }],
      ['/', { 'sec-fetch-mode': 'navigate', 'sec-fetch-dest': 'iframe' }],
      ['/', { 'sec-fetch-mode': 'cors', 'sec-fetch-dest': 'empty' }],
      ['/', { 'sec-fetch-mode': 'no-cors', 'sec-fetch-dest': 'document' }],
      ['/', { ...NAV, 'sec-purpose': 'prefetch;prerender' }],
      ['/', { ...NAV, purpose: 'prefetch' }],
    ] as [string, Record<string, string>][]) got.push(outcome(await visit(env, p, h)))
    expect(got).toEqual(Array(12).fill(WALL))
  })
  it('a crawler still gets the page\'s unfurl: the anonymous shell, not a redirect', async () => {
    const r = await visit(await envOf(), '/', { 'user-agent': 'Slackbot-LinkExpanding 1.0' })
    expect([r.status, r.headers.get('cache-control'), await r.text()]).toEqual([200, 'private, no-store', SHELL])
  })
  it('anything but `GET`', async () => {
    const env = await envOf()
    const got = []
    for (const m of ['HEAD', 'POST', 'PUT', 'DELETE']) got.push(outcome(await visit(env, '/', NAV, m)))
    expect(got).toEqual(Array(4).fill(WALL))
  })
  it('what isn\'t a walled page, even opened directly: the APIs, assets, sign-in itself, cards, faces, the ungated pages', async () => {
    const env = await envOf()
    const paths = [
      '/api/subtree?path=x', '/api', '/data/scans.json', '/assets/index-abc.js', '/v1/files/list', '/slack/actions',
      '/auth/oa', '/auth/oa/callback', '/auth/google', '/auth/google/callback', '/auth/email/verify?token=x',
      '/og', '/og/map.png', '/a/abc.png', '/signin', '/signin?next=%2Fscans', '/privacy', '/users/og', '/user/ann/og', '/marin-a/og',
    ]
    const got = []
    for (const p of paths) got.push([p, ...outcome(await visit(env, p, NAV, 'GET', () => new Response('x')))])
    expect(got).toEqual(paths.map(p => [p, 200, null, []]))
  })
  it('a visit that goes its own way: a share link (`?key=`), a preview link (`?og=`), a denied bounce, and `?sso=` (the marker alone stops a second attempt, the cookie blocked or gone)', async () => {
    const env = await envOf()
    const got = []
    for (const p of ['/?key=k', '/scans?a=1&key=k', '/?og=TokAAAAAAA', '/?denied=a%40example.test', '/?sso=login_required', '/scans?a=1&sso=login_required', '/?sso=error']) got.push(outcome(await visit(env, p)))
    expect(got).toEqual(Array(7).fill(WALL))
  })
  it('a browser that tried or signed out (the no-attempt cookie), or that has a live session; a forged or expired session, and a stranger\'s cookie, count for nothing', async () => {
    const env = await envOf()
    const live = await sessionFor('a@example.test')
    const expired = sessionCookie(await signSession(emailSub('a@example.test'), SECRET, Date.now() - 7200_000, 3600), { name: 'oa_auth', ttlS: 3600, secure: true }).split(';')[0]
    const got = []
    for (const cookie of ['oa_sso_skip=attempt', 'oa_sso_skip=signout', `other=1; ${live}`, 'oa_auth=forged', expired, 'other=1']) got.push(outcome(await visit(env, '/', { ...NAV, cookie })))
    expect(got).toEqual([WALL, WALL, WALL, attemptFor('/'), attemptFor('/'), attemptFor('/')])
  })
  it('a presented token (`Bearer`), which is no browser\'s visit', async () => {
    expect(outcome(await visit(await envOf(), '/', { ...NAV, authorization: 'Bearer t' }))).toEqual(WALL)
  })
  it('a deployment with nothing walled: `PUBLIC_READ`, `AUTH_MODE=public`, or an anonymous policy that serves content', async () => {
    const got = []
    for (const extra of [{ PUBLIC_READ: '1' }, { AUTH_MODE: 'public' }, { OG_ANONYMOUS: 'shape' }, { OG_ANONYMOUS: 'full' }]) got.push(outcome(await visit(await envOf(extra), '/runs/1')))
    expect(got).toEqual(Array(4).fill(WALL))
  })
  it('local dev, whose every request is the dev identity', async () => {
    const env = await envOf()
    const request = new Request('http://localhost:3253/', { headers: NAV })
    expect(outcome(await onRequest({ request, env: env as never, next: async () => html() }))).toEqual(WALL)
  })
})

/** `/auth/oa`'s redirect, as the browser would present its cookies back. */
async function startAttempt(env: Env, next: string, cookie: string) {
  const r = await visit(env, `/auth/oa?prompt=none&next=${encodeURIComponent(next)}`, cookie ? { ...NAV, cookie } : NAV, 'GET', oaStart as Route)
  const broker = new URL(r.headers.get('location')!)
  const nonce = r.headers.getSetCookie().find(c => c.startsWith('oa_login_nonce='))!.split(';')[0]
  return { r, broker, state: broker.searchParams.get('state')!, nonce }
}
const callbackAt = (env: Env, query: string, cookie = '') =>
  visit(env, `/auth/oa/callback?${query}`, cookie ? { ...NAV, cookie } : NAV, 'GET', oaCallback as Route)

describe('the broker\'s answer', () => {
  it('`/auth/oa?prompt=none` asks the broker for no page, and leaves the attempt marked', async () => {
    const { r, broker } = await startAttempt(await envOf(), '/scans', 'oa_sso_skip=attempt')
    expect([r.status, broker.origin, broker.searchParams.get('prompt'), r.headers.getSetCookie().filter(c => c.startsWith('oa_sso_skip='))]).toEqual([302, CONFIGURED.OA_LOGIN_ISSUER, 'none', []])
  })
  it('`login_required`: back where they were with `?sso=`, and that visit is the wall, no second attempt, with or without the cookie', async () => {
    const env = await envOf()
    const { state, nonce } = await startAttempt(env, '/scans?a=1', 'oa_sso_skip=attempt')
    const cb = await callbackAt(env, `error=login_required&state=${encodeURIComponent(state)}`, `oa_sso_skip=attempt; ${nonce}`)
    const to = cb.headers.get('location')!
    expect([cb.status, to]).toEqual([302, '/scans?a=1&sso=login_required'])
    expect([outcome(await visit(env, to, { ...NAV, cookie: 'oa_sso_skip=attempt' })), outcome(await visit(env, to))]).toEqual([WALL, WALL])
  })
  it('any other refusal of the attempt is the wall too, not an error page', async () => {
    const env = await envOf()
    const { state, nonce } = await startAttempt(env, '/scans', 'oa_sso_skip=attempt')
    const got = []
    for (const e of ['access_denied', 'interaction_required', 'server_error']) {
      got.push((await callbackAt(env, `error=${e}&state=${encodeURIComponent(state)}`, `oa_sso_skip=attempt; ${nonce}`)).headers.get('location'))
    }
    expect(got).toEqual(['/scans?sso=access_denied', '/scans?sso=interaction_required', '/scans?sso=error'])
  })
  it('`next` keeps the existing open-redirect protection: an off-site one comes back home', async () => {
    const env = await envOf()
    const got = []
    for (const next of ['//evil.example.test/x', 'https://evil.example.test/x']) {
      const { state, nonce } = await startAttempt(env, next, 'oa_sso_skip=attempt')
      got.push((await callbackAt(env, `error=login_required&state=${encodeURIComponent(state)}`, `oa_sso_skip=attempt; ${nonce}`)).headers.get('location'))
    }
    expect(got).toEqual(['/?sso=login_required', '/?sso=login_required'])
  })
  it('cookies blocked (the nonce and the attempt both gone): a quiet `/signin`, which never bounces and shows no error', async () => {
    const env = await envOf()
    const { state } = await startAttempt(env, '/scans', '')
    const cb = await callbackAt(env, `error=login_required&state=${encodeURIComponent(state)}`)
    const to = cb.headers.get('location')!
    expect([cb.status, to, outcome(await visit(env, to))]).toEqual([302, '/signin?sso=login_required', WALL])
  })
  it('a failed exchange on a silent attempt (the attempt cookie still here): no error page, home with the marker', async () => {
    const cb = await callbackAt(await envOf(), 'code=x&state=forged', 'oa_sso_skip=attempt')
    expect([cb.status, cb.headers.get('location')]).toEqual([302, '/?sso=error'])
  })
  it('an explicit attempt\'s failures still say why, on `/signin`', async () => {
    const env = await envOf()
    const got = []
    for (const q of ['error=access_denied', 'code=x&state=forged']) got.push((await callbackAt(env, q)).headers.get('location'))
    expect(got).toEqual(['/signin?error=oa%3Aaccess_denied', '/signin?error=oa%3Abad%20state'])
  })

  describe('a broker with a session', () => {
    const b64u = (b: ArrayBuffer | Uint8Array) => Buffer.from(b as Uint8Array).toString('base64url')
    const utf8 = (o: unknown) => new TextEncoder().encode(JSON.stringify(o))
    async function stubBroker(email: string, nonce: string) {
      const { privateKey, publicKey } = await crypto.subtle.generateKey({ name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' }, true, ['sign', 'verify']) as CryptoKeyPair
      const jwk = { ...await crypto.subtle.exportKey('jwk', publicKey), kid: 'k1' }
      vi.stubGlobal('fetch', async (u: string) => {
        if (String(u).endsWith('/oidc/jwks')) return Response.json({ keys: [jwk] })
        const claims = { iss: CONFIGURED.OA_LOGIN_ISSUER, aud: CONFIGURED.OA_LOGIN_CLIENT_ID, exp: Math.floor(Date.now() / 1000) + 300, nonce, email, email_verified: true }
        const head = `${b64u(utf8({ alg: 'RS256', kid: 'k1' }))}.${b64u(utf8(claims))}`
        const sig = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', privateKey, new TextEncoder().encode(head))
        return Response.json({ id_token: `${head}.${b64u(sig)}` })
      })
    }
    it('lands back signed in on the page they asked for, the no-attempt state gone', async () => {
      const env = await envOf()
      const { broker, state, nonce } = await startAttempt(env, '/scans?a=1', 'oa_sso_skip=attempt')
      await stubBroker('a@example.test', broker.searchParams.get('nonce')!)
      const cb = await callbackAt(env, `code=c&state=${encodeURIComponent(state)}`, `oa_sso_skip=attempt; ${nonce}`)
      const set = cb.headers.getSetCookie()
      const session = set.find(c => c.startsWith('oa_auth='))!.split(';')[0]
      expect([cb.status, cb.headers.get('location'), set.map(c => c.split('=')[0]), set.at(-1)]).toEqual([302, '/scans?a=1', ['oa_auth', 'oa_login_nonce', 'oa_sso_skip'], CLEARED])
      expect(outcome(await visit(env, '/scans?a=1', { ...NAV, cookie: session }))).toEqual(WALL)
    })
    it('a person the gate doesn\'t admit lands on the wall\'s request-access form, and stays there', async () => {
      const env = await envOf()
      const { broker, state, nonce } = await startAttempt(env, '/scans', 'oa_sso_skip=attempt')
      await stubBroker('stranger@elsewhere.test', broker.searchParams.get('nonce')!)
      const cb = await callbackAt(env, `code=c&state=${encodeURIComponent(state)}`, `oa_sso_skip=attempt; ${nonce}`)
      const to = cb.headers.get('location')!
      expect([to, outcome(await visit(env, to, { ...NAV, cookie: 'oa_sso_skip=attempt' })), outcome(await visit(env, to))]).toEqual(['/?denied=stranger%40elsewhere.test', WALL, WALL])
    })
  })
})

describe('after a deliberate sign-out', () => {
  const logout = (env: Env, cookie = '') => visit(env, '/api/auth/logout', cookie ? { cookie } : {}, 'POST', authRoutes as Route)

  it('logout marks the browser no-attempt for as long as a cookie can last, beside clearing the session', async () => {
    const env = await envOf()
    const r = await logout(env, await sessionFor('a@example.test'))
    expect([r.status, r.headers.getSetCookie()]).toEqual([200, [SESSION_CLEARED, SIGNED_OUT]])
  })
  it('unconfigured: logout sets only what it always did', async () => {
    const env = await envOf({ OA_LOGIN_ISSUER: undefined })
    const r = await logout(env, await sessionFor('a@example.test'))
    expect([r.status, r.headers.getSetCookie()]).toEqual([200, [SESSION_CLEARED]])
  })
  it('logout → a page visit is the wall, no bounce → an explicit `/auth/oa` goes to the broker and clears it → the next visit may try again', async () => {
    const env = await envOf()
    const signedOut = (await logout(env)).headers.getSetCookie().find(c => c.startsWith('oa_sso_skip='))!.split(';')[0]
    expect(signedOut).toBe('oa_sso_skip=signout')
    expect(outcome(await visit(env, '/scans', { ...NAV, cookie: signedOut }))).toEqual(WALL)
    const explicit = await visit(env, '/auth/oa?next=%2Fscans', { ...NAV, cookie: signedOut }, 'GET', oaStart as Route)
    const broker = new URL(explicit.headers.get('location')!)
    expect([explicit.status, `${broker.origin}${broker.pathname}`, broker.searchParams.get('prompt'), explicit.headers.getSetCookie().filter(c => c.startsWith('oa_sso_skip='))])
      .toEqual([302, `${CONFIGURED.OA_LOGIN_ISSUER}/oidc/authorize`, null, [CLEARED]])
    expect(outcome(await visit(env, '/scans'))).toEqual(attemptFor('/scans'))
  })
  it('the suppression isn\'t resumed by a `prompt=none` start: only an explicit one clears it', async () => {
    const r = await visit(await envOf(), '/auth/oa?prompt=none&next=%2F', { ...NAV, cookie: 'oa_sso_skip=signout' }, 'GET', oaStart as Route)
    expect(r.headers.getSetCookie().map(c => c.split('=')[0])).toEqual(['oa_login_nonce'])
  })
  it('an explicit start with nothing to clear sets nothing new', async () => {
    const r = await visit(await envOf(), '/auth/oa?next=%2F', NAV, 'GET', oaStart as Route)
    expect(r.headers.getSetCookie().map(c => c.split('=')[0])).toEqual(['oa_login_nonce'])
  })
  it('any completed sign-in clears it (a Google callback, an emailed code, an exchange: anything minting the session); nothing else does', async () => {
    const env = await envOf()
    const mint = () => new Response(null, { status: 302, headers: { 'set-cookie': 'oa_auth=minted; Path=/', location: '/' } })
    const clearOnly = () => new Response(null, { status: 200, headers: { 'set-cookie': 'oa_auth=; Path=/; Max-Age=0' } })
    const cases: [string, string, string, Route, string[]][] = [
      ['/auth/google/callback?code=c&state=s', 'GET', 'oa_sso_skip=signout', mint, ['oa_auth=minted; Path=/', CLEARED]],
      ['/auth/email/code', 'POST', 'oa_sso_skip=attempt', mint, ['oa_auth=minted; Path=/', CLEARED]],
      ['/api/auth/exchange', 'POST', 'oa_sso_skip=signout', mint, ['oa_auth=minted; Path=/', CLEARED]],
      ['/auth/google/callback?code=c&state=s', 'GET', '', mint, ['oa_auth=minted; Path=/']],
      ['/api/auth/other', 'POST', 'oa_sso_skip=signout', clearOnly, ['oa_auth=; Path=/; Max-Age=0']],
    ]
    const got = []
    for (const [p, m, cookie, route] of cases) got.push((await visit(env, p, cookie ? { cookie } : {}, m, route)).headers.getSetCookie())
    expect(got).toEqual(cases.map(c => c[4]))
  })
  it('unconfigured: a completed sign-in\'s response is left alone', async () => {
    const env = await envOf({ OA_LOGIN_ISSUER: undefined })
    const mint = new Response(null, { status: 302, headers: { 'set-cookie': 'oa_auth=minted; Path=/', location: '/' } })
    const r = await visit(env, '/auth/google/callback', { cookie: 'oa_sso_skip=signout' }, 'GET', () => mint)
    expect(r.headers.getSetCookie()).toEqual(['oa_auth=minted; Path=/'])
  })
})
