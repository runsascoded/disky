import { describe, expect, it } from 'vitest'
import { sqliteD1 } from '../_lib/testD1'
import { gateFor, type Env } from '../_lib/auth'
import { OA_HINT_COOKIE, OA_WHO_COOKIE, rememberAccount } from '@disk-tree/pages-kit/oa'

const { onRequest: start } = await import('./oa')
const { onRequest: callback } = await import('./oa/callback')
const { onRequestGet: client } = await import('./oa/client')

const CONFIGURED = { OA_LOGIN_ISSUER: 'https://login.oa.test/', OA_LOGIN_CLIENT_ID: 'disky-test', OA_LOGIN_CLIENT_SECRET: 'shh' }
const envOf = async (extra: Partial<Env> = {}): Promise<Env> => ({ DB: (await sqliteD1()).db, SESSION_SECRET: 's'.repeat(32), ...extra }) as Env
const ctx = (env: Env, url: string) => ({ request: new Request(url), env }) as never

describe('"Sign in with OA" (`/auth/oa`, specs/oa-login-rp.md)', () => {
  it('unconfigured (any of issuer / client id / secret unset): no route, no button', async () => {
    const out = []
    for (const drop of [null, 'OA_LOGIN_ISSUER', 'OA_LOGIN_CLIENT_ID', 'OA_LOGIN_CLIENT_SECRET'] as const) {
      const env = await envOf(drop ? { ...CONFIGURED, [drop]: undefined } : {})
      const s = await start(ctx(env, 'https://dev.example.test/auth/oa'))
      const c = await callback(ctx(env, 'https://dev.example.test/auth/oa/callback?code=x&state=y'))
      const b = await (await client(ctx(env, 'https://dev.example.test/auth/oa/client'))).json()
      out.push([drop, s.status, c.status, b])
    }
    expect(out).toEqual([
      [null, 404, 404, { enabled: false, google: [] }],
      ['OA_LOGIN_ISSUER', 404, 404, { enabled: false, google: [] }],
      ['OA_LOGIN_CLIENT_ID', 404, 404, { enabled: false, google: [] }],
      ['OA_LOGIN_CLIENT_SECRET', 404, 404, { enabled: false, google: [] }],
    ])
  })

  it("configured: the button shows, and the start redirects to the broker's authorize endpoint with this deployment's callback, PKCE S256 and a nonce", async () => {
    const env = await envOf(CONFIGURED)
    expect(await (await client(ctx(env, 'https://dev.example.test/auth/oa/client'))).json()).toEqual({ enabled: true, google: [] })
    const r = await start(ctx(env, 'https://dev.example.test/auth/oa?next=%2Fscans'))
    const loc = new URL(r.headers.get('location')!)
    const q = Object.fromEntries(loc.searchParams)
    expect([r.status, `${loc.origin}${loc.pathname}`, q.client_id, q.redirect_uri, q.response_type, q.scope, q.code_challenge_method, !!q.code_challenge, !!q.nonce, !!q.state, 'hd' in q, 'login_hint' in q])
      .toEqual([302, 'https://login.oa.test/oidc/authorize', 'disky-test', 'https://dev.example.test/auth/oa/callback', 'code', 'openid email profile', 'S256', true, true, true, false, false])
  })

  it('a failed callback lands on the sign-in page: the broker\'s own `error=` or the adapter\'s, with `oa:<reason>`; `prompt=none`\'s `login_required` with no state to follow, quietly (`_lib/silentSignIn.test.ts`)', async () => {
    const env = await envOf(CONFIGURED)
    const cases = [
      'https://dev.example.test/auth/oa/callback?error=login_required&state=y',
      'https://dev.example.test/auth/oa/callback?error=access_denied',
      'https://dev.example.test/auth/oa/callback',
      'https://dev.example.test/auth/oa/callback?code=x&state=forged',
    ]
    const got = []
    for (const u of cases) {
      const r = await callback(ctx(env, u))
      got.push([r.status, r.headers.get('location'), r.headers.get('cache-control')])
    }
    expect(got).toEqual([
      [302, '/signin?sso=login_required', 'no-store'],
      [302, '/signin?error=oa%3Aaccess_denied', 'no-store'],
      [302, '/signin?error=oa%3Amissing%20code%20or%20state', 'no-store'],
      [302, '/signin?error=oa%3Abad%20state', 'no-store'],
    ])
  })

  it('the remembered account: a completed sign-in signs the address the adapter hinted, and the client route names it (with its profile) only from that signed cookie; the Google hints are echoed', async () => {
    const env = await envOf(CONFIGURED)
    const url = 'https://dev.example.test/auth/oa/callback'
    const signedIn = new Response(null, { status: 302, headers: { location: '/' } })
    signedIn.headers.append('set-cookie', `${OA_HINT_COOKIE}=ada%40oa.test; Path=/; HttpOnly`)
    signedIn.headers.append('set-cookie', `${OA_HINT_COOKIE}_accounts=%5B%22ada%40oa.test%22%5D; Path=/; HttpOnly`)
    const res = await rememberAccount(signedIn, new Request(url), { hintCookie: OA_HINT_COOKIE, whoCookie: OA_WHO_COOKIE, secret: env.SESSION_SECRET! })
    const who = res.headers.getSetCookie().find(c => c.startsWith(`${OA_WHO_COOKIE}=`))!.split(';')[0]
    const none = new Response(null, { status: 302, headers: { location: '/' } })
    const untouched = await rememberAccount(none, new Request(url), { hintCookie: OA_HINT_COOKIE, whoCookie: OA_WHO_COOKIE, secret: env.SESSION_SECRET! })
    await gateFor(env, new Request(url))!.seedProfileFromClaims('ada@oa.test', { name: 'Ada Lovelace' })
    const google = 'oa_google_hint=ada%40oa.test; oa_google_hint_accounts=%5B%22ada%40oa.test%22%2C%22ada%40gmail.test%22%5D'
    const ask = async (cookie: string) => (await client({ request: new Request('https://dev.example.test/auth/oa/client', { headers: { cookie } }), env } as never)).json()
    expect([
      untouched.headers.getSetCookie(),
      await ask(who),
      await ask(`${who}; ${google}`),
      await ask(`${OA_WHO_COOKIE}=forged; ${google}`),
      await ask(`${OA_HINT_COOKIE}=ada%40oa.test`),
    ]).toEqual([
      [],
      { enabled: true, account: { email: 'ada@oa.test', name: 'Ada Lovelace' }, google: [] },
      { enabled: true, account: { email: 'ada@oa.test', name: 'Ada Lovelace' }, google: ['ada@oa.test', 'ada@gmail.test'] },
      { enabled: true, google: ['ada@oa.test', 'ada@gmail.test'] },
      { enabled: true, google: [] },
    ])
  })
})
