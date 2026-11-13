/** PR-preview sign-in: a preview host (`isPreviewHost`, one label under `PAGES_SUBDOMAIN`) signs in through the broker's
 *  preview client with a redirect on its own origin, or not at all, and never silently; `AUTH_HOST_COOKIES` names the
 *  auth cookies `__Host-…` on https. Through the real routes and middleware over an in-memory D1; the broker is a stubbed
 *  `fetch`. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cookieNames, type Env, isPreviewHost, presentsCredentials } from './auth'
import { sqliteD1 } from './testD1'

vi.mock('./og/vendor/resvg.wasm', () => ({ default: new Uint8Array() }))

const { onRequest: middleware } = await import('../_middleware')
const { onRequest: oaStart } = await import('../auth/oa')
const { onRequest: oaCallback } = await import('../auth/oa/callback')
const { onRequestGet: oaClient } = await import('../auth/oa/client')
const { onRequest: googleStart } = await import('../auth/google')

const SUB = 'example-proj.pages.dev'
const PREVIEW = 'https://pr-7.example-proj.pages.dev'
const DEV_PREVIEW = 'https://dev.example-proj.pages.dev'
const PROD = 'https://my.example.test'
const DEV = 'https://dev.my.example.test'
const SECRET = 's'.repeat(32)
const ISSUER = 'https://login.example.test'
const CLIENT = { OA_LOGIN_ISSUER: ISSUER, OA_LOGIN_CLIENT_ID: 'disky-test', OA_LOGIN_CLIENT_SECRET: 'shh' }
const PREVIEW_CLIENT = { OA_LOGIN_PREVIEW_CLIENT_ID: 'disky-test-preview', OA_LOGIN_PREVIEW_CLIENT_SECRET: 'shh-preview' }
const NAV = { 'sec-fetch-mode': 'navigate', 'sec-fetch-dest': 'document' }
const SHELL = '<!doctype html>shell\n'

beforeEach(() => { vi.stubGlobal('HTMLRewriter', class { on() { return this } transform(r: Response) { return r } }) })
afterEach(() => { vi.unstubAllGlobals() })

const html = () => new Response(SHELL, { headers: { 'content-type': 'text/html; charset=utf-8' } })
const envOf = async (extra: Record<string, unknown> = {}): Promise<Env> => ({
  DB: (await sqliteD1()).db,
  SESSION_SECRET: SECRET,
  STAFF_DOMAIN: 'example.test',
  PAGES_SUBDOMAIN: SUB,
  ASSETS: { fetch: async () => html() },
  ...CLIENT,
  ...extra,
}) as unknown as Env
const req = (url: string, headers: Record<string, string> = {}) => new Request(url, { headers })
type Route = (ctx: never) => Response | Promise<Response>
const call = (route: Route, env: Env, url: string, headers: Record<string, string> = {}) => route({ request: req(url, headers), env } as never)
const visit = (env: Env, url: string, headers: Record<string, string> = NAV) => {
  const request = req(url, headers)
  return middleware({ request, env: env as never, next: async () => html() })
}
const cookieName = (c: string) => c.split('=')[0]

describe('`isPreviewHost`: one label under the configured `pages.dev` subdomain', () => {
  const hosts = [
    'https://pr-7.example-proj.pages.dev/',
    'https://dev.example-proj.pages.dev/',
    'https://0a1b2c3d.example-proj.pages.dev/x',
    'https://PR-7.Example-Proj.pages.dev/',
    'https://example-proj.pages.dev/',
    'https://a.b.example-proj.pages.dev/',
    'https://pr-1.example-proj.pages.dev.evil.com/',
    'https://pr-1.notexample-proj.pages.dev/',
    'https://pr-1-example-proj.pages.dev/',
    'https://pr-1.other.pages.dev/',
    'https://my.example.test/',
    'https://dev.my.example.test/',
    'http://localhost:3253/',
  ]
  const table = (env: Partial<Env>) => hosts.map(h => [h, isPreviewHost(env as Env, req(h))])

  it('branch, `dev` alias and deployment-hash hosts are; the subdomain itself, deeper hosts, look-alikes and custom domains are not', () => {
    expect(table({ PAGES_SUBDOMAIN: SUB })).toEqual([
      ['https://pr-7.example-proj.pages.dev/', true],
      ['https://dev.example-proj.pages.dev/', true],
      ['https://0a1b2c3d.example-proj.pages.dev/x', true],
      ['https://PR-7.Example-Proj.pages.dev/', true],
      ['https://example-proj.pages.dev/', false],
      ['https://a.b.example-proj.pages.dev/', false],
      ['https://pr-1.example-proj.pages.dev.evil.com/', false],
      ['https://pr-1.notexample-proj.pages.dev/', false],
      ['https://pr-1-example-proj.pages.dev/', false],
      ['https://pr-1.other.pages.dev/', false],
      ['https://my.example.test/', false],
      ['https://dev.my.example.test/', false],
      ['http://localhost:3253/', false],
    ])
  })

  it('unset, or not a `pages.dev` subdomain: no host is one', () => {
    for (const env of [{}, { PAGES_SUBDOMAIN: 'example-proj' }, { PAGES_SUBDOMAIN: 'my.example.test' }, { PAGES_SUBDOMAIN: 'pages.dev' }]) {
      expect(table(env).filter(([, preview]) => preview)).toEqual([])
    }
  })
})

describe('which client and redirect each host signs in with', () => {
  /** `/auth/oa`'s answer on `origin`: status, and the broker's `client_id` / `redirect_uri`; and `/auth/oa/client`'s. */
  async function startOn(env: Env, origin: string) {
    const r = await call(oaStart as Route, env, `${origin}/auth/oa?next=%2Fscans`)
    const enabled = ((await (await call(oaClient as Route, env, `${origin}/auth/oa/client`)).json()) as { enabled: boolean }).enabled
    if (r.status !== 302) return [origin, r.status, enabled]
    const q = new URL(r.headers.get('location')!).searchParams
    return [origin, r.status, enabled, q.get('client_id'), q.get('redirect_uri')]
  }

  it('with the preview client configured: preview hosts use it on their own origin; custom domains keep the deployment\'s', async () => {
    const env = await envOf(PREVIEW_CLIENT)
    const got = []
    for (const o of [PROD, DEV, PREVIEW, DEV_PREVIEW]) got.push(await startOn(env, o))
    expect(got).toEqual([
      [PROD, 302, true, 'disky-test', `${PROD}/auth/oa/callback`],
      [DEV, 302, true, 'disky-test', `${DEV}/auth/oa/callback`],
      [PREVIEW, 302, true, 'disky-test-preview', `${PREVIEW}/auth/oa/callback`],
      [DEV_PREVIEW, 302, true, 'disky-test-preview', `${DEV_PREVIEW}/auth/oa/callback`],
    ])
  })

  it('without it (either half unset): no OA sign-in on preview hosts, never the deployment\'s client there', async () => {
    const got = []
    for (const drop of [null, 'OA_LOGIN_PREVIEW_CLIENT_ID', 'OA_LOGIN_PREVIEW_CLIENT_SECRET'] as const) {
      const env = await envOf(drop ? { ...PREVIEW_CLIENT, [drop]: undefined } : {})
      for (const o of [PROD, PREVIEW]) {
        const cb = await call(oaCallback as Route, env, `${o}/auth/oa/callback?code=x&state=y`)
        got.push([drop, ...await startOn(env, o), cb.status])
      }
    }
    expect(got).toEqual([
      [null, PROD, 302, true, 'disky-test', `${PROD}/auth/oa/callback`, 302],
      [null, PREVIEW, 404, false, 404],
      ['OA_LOGIN_PREVIEW_CLIENT_ID', PROD, 302, true, 'disky-test', `${PROD}/auth/oa/callback`, 302],
      ['OA_LOGIN_PREVIEW_CLIENT_ID', PREVIEW, 404, false, 404],
      ['OA_LOGIN_PREVIEW_CLIENT_SECRET', PROD, 302, true, 'disky-test', `${PROD}/auth/oa/callback`, 302],
      ['OA_LOGIN_PREVIEW_CLIENT_SECRET', PREVIEW, 404, false, 404],
    ])
  })

  it('the preview client alone (no deployment client, as a preview environment might hold) still serves preview hosts only', async () => {
    const env = await envOf({ ...PREVIEW_CLIENT, OA_LOGIN_CLIENT_ID: undefined, OA_LOGIN_CLIENT_SECRET: undefined })
    const got = []
    for (const o of [DEV, PREVIEW]) got.push(await startOn(env, o))
    expect(got).toEqual([
      [DEV, 404, false],
      [PREVIEW, 302, true, 'disky-test-preview', `${PREVIEW}/auth/oa/callback`],
    ])
  })
})

describe('no silent sign-in on a preview host', () => {
  it('an unauthenticated page visit there gets the wall; the same visit on a custom domain tries the broker', async () => {
    const env = await envOf(PREVIEW_CLIENT)
    const got = []
    for (const o of [PREVIEW, DEV_PREVIEW, DEV]) {
      const r = await visit(env, `${o}/scans`)
      got.push([o, r.status, r.headers.get('location')])
    }
    expect(got).toEqual([
      [PREVIEW, 200, null],
      [DEV_PREVIEW, 200, null],
      [DEV, 302, '/auth/oa?prompt=none&next=%2Fscans'],
    ])
  })
})

describe('`AUTH_HOST_COOKIES`: `__Host-` cookie names on https', () => {
  const PLAIN = { session: 'oa_auth', googleNonce: 'oa_oidc', oaNonce: 'oa_login_nonce', accountHint: 'oa_google_hint', oaHint: 'oa_login_hint', oaWho: 'oa_login_who' }
  const HOST = { session: '__Host-oa_auth', googleNonce: '__Host-oa_oidc', oaNonce: '__Host-oa_login_nonce', accountHint: '__Host-oa_google_hint', oaHint: '__Host-oa_login_hint', oaWho: '__Host-oa_login_who' }

  it('names per flag, scheme and host; a preview host keeps no account hint', async () => {
    const got = []
    for (const flag of [undefined, '1', '0']) {
      for (const url of [`${PROD}/`, 'http://m3:3253/', `${PREVIEW}/`]) got.push([flag, url, cookieNames({ PAGES_SUBDOMAIN: SUB, AUTH_HOST_COOKIES: flag } as Env, req(url))])
    }
    expect(got).toEqual([
      [undefined, `${PROD}/`, PLAIN],
      [undefined, 'http://m3:3253/', PLAIN],
      [undefined, `${PREVIEW}/`, { ...PLAIN, accountHint: false, oaHint: false, oaWho: false }],
      ['1', `${PROD}/`, HOST],
      ['1', 'http://m3:3253/', PLAIN],
      ['1', `${PREVIEW}/`, { ...HOST, accountHint: false, oaHint: false, oaWho: false }],
      ['0', `${PROD}/`, PLAIN],
      ['0', 'http://m3:3253/', PLAIN],
      ['0', `${PREVIEW}/`, { ...PLAIN, accountHint: false, oaHint: false, oaWho: false }],
    ])
  })

  it('the Google round trip\'s nonce cookie follows the flag', async () => {
    const got = []
    for (const flag of [undefined, '1']) {
      const env = await envOf({ GOOGLE_CLIENT_ID: 'g-id', GOOGLE_CLIENT_SECRET: 'g-secret', AUTH_HOST_COOKIES: flag })
      const r = await call(googleStart as Route, env, `${PROD}/auth/google`)
      got.push([flag, r.status, r.headers.getSetCookie().map(cookieName)])
    }
    expect(got).toEqual([
      [undefined, 302, ['oa_oidc']],
      ['1', 302, ['__Host-oa_oidc']],
    ])
  })

  describe('a whole sign-in on a preview host, flag on', () => {
    const b64u = (b: ArrayBuffer | Uint8Array) => Buffer.from(b as Uint8Array).toString('base64url')
    const utf8 = (o: unknown) => new TextEncoder().encode(JSON.stringify(o))
    /** The broker: its JWKS, and a token endpoint that records what it was sent and answers for `aud`. */
    async function stubBroker(email: string, nonce: string, aud: string) {
      const { privateKey, publicKey } = await crypto.subtle.generateKey({ name: 'RSASSA-PKCS1-v1_5', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' }, true, ['sign', 'verify']) as CryptoKeyPair
      const jwk = { ...await crypto.subtle.exportKey('jwk', publicKey), kid: 'k1' }
      const sent: Record<string, string>[] = []
      vi.stubGlobal('fetch', async (u: string, init?: RequestInit) => {
        if (String(u).endsWith('/oidc/jwks')) return Response.json({ keys: [jwk] })
        const body = Object.fromEntries(new URLSearchParams(String(init?.body)))
        sent.push({ url: String(u), client_id: body.client_id, client_secret: body.client_secret, redirect_uri: body.redirect_uri })
        const claims = { iss: ISSUER, aud, exp: Math.floor(Date.now() / 1000) + 300, nonce, email, email_verified: true }
        const head = `${b64u(utf8({ alg: 'RS256', kid: 'k1' }))}.${b64u(utf8(claims))}`
        const sig = await crypto.subtle.sign('RSASSA-PKCS1-v1_5', privateKey, new TextEncoder().encode(head))
        return Response.json({ id_token: `${head}.${b64u(sig)}` })
      })
      return sent
    }

    it('the preview client trades the code for its own origin\'s callback, and the session is a `__Host-` cookie the gate reads', async () => {
      const env = await envOf({ ...PREVIEW_CLIENT, AUTH_HOST_COOKIES: '1' })
      const start = await call(oaStart as Route, env, `${PREVIEW}/auth/oa?next=%2Fscans`)
      const broker = new URL(start.headers.get('location')!)
      const nonce = start.headers.getSetCookie().map(c => c.split(';')[0])
      const sent = await stubBroker('a@example.test', broker.searchParams.get('nonce')!, PREVIEW_CLIENT.OA_LOGIN_PREVIEW_CLIENT_ID)
      const cb = await call(oaCallback as Route, env, `${PREVIEW}/auth/oa/callback?code=c&state=${encodeURIComponent(broker.searchParams.get('state')!)}`, { cookie: nonce.join('; ') })
      const set = cb.headers.getSetCookie()
      const session = set.find(c => c.startsWith('__Host-oa_auth='))!
      expect([
        nonce.map(cookieName),
        sent,
        cb.status,
        cb.headers.get('location'),
        set.map(cookieName),
        session.split('; ').slice(1).filter(a => /^(Secure|Path=|Domain=)/.test(a)),
      ]).toEqual([
        ['__Host-oa_login_nonce'],
        [{ url: `${ISSUER}/oidc/token`, client_id: 'disky-test-preview', client_secret: 'shh-preview', redirect_uri: `${PREVIEW}/auth/oa/callback` }],
        302,
        '/scans',
        ['__Host-oa_auth', '__Host-oa_login_nonce'],
        ['Secure', 'Path=/'],
      ])

      const value = session.split(';')[0].split('=').slice(1).join('=')
      const presents = async (url: string, cookie: string) => presentsCredentials({ request: req(url, { cookie }), env } as never)
      expect([
        await presents(`${PREVIEW}/`, `__Host-oa_auth=${value}`),
        await presents(`${PREVIEW}/`, `oa_auth=${value}`),
        await presents(`${PROD}/`, `__Host-oa_auth=${value}`),
        await presentsCredentials({ request: req(`${PROD}/`, { cookie: `__Host-oa_auth=${value}` }), env: { ...env, AUTH_HOST_COOKIES: undefined } } as never),
      ]).toEqual([true, false, true, false])
    })
  })
})
