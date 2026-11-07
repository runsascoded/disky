/** Every route's gate, per identity (specs/disky-oa-dev.md): the docs public, `/fleet` and `/api/fleet` staff only (a
 *  silent sign-in attempt, then the page's wall; a 401), the card anonymous without a valid `sig`, the Slack route on
 *  its Bearer token. The deployments, the broker and Slack are stubbed `fetch`es, never live ones. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { emailSub, sessionCookie, signSession } from '@open-athena/auth'
import { imagePath, ogKey, expDay } from '@disk-tree/pages-kit/og/sign'
import type { Env } from './_lib/env'
import { readFleet } from './_lib/fleet'
import { ANSWERS, fleetEnv, NOW, stubFetch } from './_lib/testFleet'

const { onRequest: middleware } = await import('./_middleware')
const { onRequestGet: apiFleet } = await import('./api/fleet/index')
const { onRequestPost: slackPost } = await import('./api/fleet/slack')
const { onRequestGet: whoami } = await import('./api/whoami')
const { onRequestPost: logout } = await import('./api/logout')
const { onRequestGet: og } = await import('./og/[[path]]')
const { onRequest: oaStart } = await import('./auth/oa')

interface Fs { readFile(p: string): Promise<Uint8Array> }
const fsp = () => import(/* @vite-ignore */ 'node:fs/promises' as string) as Promise<Fs>

const ORIGIN = 'https://fleet.example.test'
const SECRET = 's'.repeat(32)
const CONFIGURED = { OA_LOGIN_ISSUER: 'https://login.example.test', OA_LOGIN_CLIENT_ID: 'fleet-test', OA_LOGIN_CLIENT_SECRET: 'shh' }
const NAV = { 'sec-fetch-mode': 'navigate', 'sec-fetch-dest': 'document' }
const ATTEMPT = 'oa_sso_skip=attempt; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=300'
const SIGNED_OUT = 'oa_sso_skip=signout; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=34560000'
const SESSION_CLEARED = 'oa_auth=; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=0'
const SHELL = '<!doctype html>shell\n'

/** The app's assets: the card fonts (`site/`'s files, as the build copies them), else 404. */
const ASSETS = {
  fetch: async (r: Request) => {
    const m = /^\/_fonts\/(Inter-\d+\.ttf)$/.exec(new URL(r.url).pathname)
    return m ? new Response(await (await fsp()).readFile(`../site/public/_fonts/${m[1]}`)) : new Response('not found', { status: 404 })
  },
}

const envOf = (extra: Partial<Env> = {}): Env => fleetEnv({ ASSETS, SESSION_SECRET: SECRET, STAFF_DOMAIN: 'example.test', ...CONFIGURED, ...extra })
const sessionFor = async (email: string) => sessionCookie(await signSession(emailSub(email), SECRET, Date.now(), 3600), { name: 'oa_auth', ttlS: 3600, secure: true }).split(';')[0]
const req = (path: string, headers: Record<string, string> = {}, method = 'GET') => new Request(`${ORIGIN}${path}`, { method, headers })
const ctx = (request: Request, env: Env) => ({ request, env })

/** A page visit through the middleware, the shell behind it. */
const visit = (env: Env, path: string, headers: Record<string, string> = NAV) =>
  middleware({ ...ctx(req(path, headers), env), next: async () => new Response(SHELL, { headers: { 'content-type': 'text/html' } }) })
const outcome = (r: Response) => [r.status, r.headers.get('location'), r.headers.getSetCookie()]
const PAGE = [200, null, []]

interface SlackCall { method: string; body: Record<string, unknown> }
let slackCalls: SlackCall[]
let slackAnswers: Record<string, unknown>[]

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW * 1000)
  slackCalls = []
  slackAnswers = []
  const deployments = stubFetch(ANSWERS)
  vi.stubGlobal('fetch', async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const slack = /^https:\/\/slack\.com\/api\/(.+)$/.exec(url)
    if (!slack) return deployments(input, init)
    slackCalls.push({ method: slack[1], body: JSON.parse(String(init?.body)) })
    return Response.json(slackAnswers.shift() ?? { ok: false, error: 'unexpected' })
  })
})
afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('the docs are public; the fleet page is walled', () => {
  it('a signed-out visit to the docs is served; to /fleet, one silent try at the broker first', async () => {
    const env = envOf()
    const got = []
    for (const p of ['/', '/docs/disky', '/signin', '/fleet', '/fleet/']) got.push(outcome(await visit(env, p)))
    expect(got).toEqual([PAGE, PAGE, PAGE, [302, '/auth/oa?prompt=none&next=%2Ffleet', [ATTEMPT]], [302, '/auth/oa?prompt=none&next=%2Ffleet%2F', [ATTEMPT]]])
  })
  it('a staff session goes straight to /fleet; another domain\'s session, unconfigured sign-in, a fetch or a tried browser gets the page (its wall)', async () => {
    const staff = await sessionFor('ann@example.test')
    const other = await sessionFor('bob@elsewhere.test')
    expect([
      outcome(await visit(envOf(), '/fleet', { ...NAV, cookie: staff })),
      outcome(await visit(envOf(), '/fleet', { ...NAV, cookie: other })),
      outcome(await visit(envOf({ OA_LOGIN_ISSUER: undefined }), '/fleet')),
      outcome(await visit(envOf(), '/fleet', {})),
      outcome(await visit(envOf(), '/fleet', { ...NAV, cookie: 'oa_sso_skip=attempt' })),
      outcome(await visit(envOf(), '/fleet?sso=login_required')),
    ]).toEqual([PAGE, [302, '/auth/oa?prompt=none&next=%2Ffleet', [ATTEMPT]], PAGE, PAGE, PAGE, PAGE])
  })
  it('/auth/oa starts at the broker when configured, else 404s', async () => {
    const r = await oaStart(ctx(req('/auth/oa?next=%2Ffleet'), envOf()))
    const loc = new URL(r.headers.get('location')!)
    const unconfigured = await oaStart(ctx(req('/auth/oa'), envOf({ OA_LOGIN_CLIENT_SECRET: undefined })))
    expect([r.status, `${loc.origin}${loc.pathname}`, loc.searchParams.get('redirect_uri'), unconfigured.status])
      .toEqual([302, 'https://login.example.test/oidc/authorize', `${ORIGIN}/auth/oa/callback`, 404])
  })
  it('whoami says who, and whether sign-in is offered; logout clears the session and marks the browser signed out', async () => {
    const staff = await sessionFor('ann@example.test')
    expect([
      await (await whoami(ctx(req('/api/whoami', { cookie: staff }), envOf()))).json(),
      await (await whoami(ctx(req('/api/whoami'), envOf()))).json(),
      await (await whoami(ctx(req('/api/whoami'), envOf({ OA_LOGIN_ISSUER: undefined })))).json(),
    ]).toEqual([{ email: 'ann@example.test', signIn: true }, { email: null, signIn: true }, { email: null, signIn: false }])
    const out = logout(ctx(req('/api/logout', { cookie: staff }, 'POST'), envOf()))
    expect([out.status, out.headers.getSetCookie()]).toEqual([204, [SESSION_CLEARED, SIGNED_OUT]])
  })
})

const sigless = (url: string) => url.replace(/sig=[A-Za-z0-9]{12}/, 'sig=<sig>')

describe('/api/fleet', () => {
  it('401s without a staff session: none, another domain\'s, or a Bearer token', async () => {
    const other = await sessionFor('bob@elsewhere.test')
    const got = []
    for (const headers of [{}, { cookie: other }, { authorization: 'Bearer tok-gcs' }] as Record<string, string>[]) {
      const r = await apiFleet(ctx(req('/api/fleet', headers), envOf()))
      got.push([r.status, await r.json(), r.headers.get('cache-control')])
    }
    expect(got).toEqual(Array(3).fill([401, { error: 'unauthenticated' }, 'no-store']))
  })
  it('serves a staff session the document read from each deployment, and the full card\'s signed URLs; never cached by the browser', async () => {
    const staff = await sessionFor('ann@example.test')
    const r = await apiFleet(ctx(req('/api/fleet', { cookie: staff }), envOf()))
    const body = await r.json() as { card: { dark: string; light: string } }
    expect([r.status, r.headers.get('cache-control')]).toEqual([200, 'private, no-store'])
    expect({ ...body, card: { dark: sigless(body.card.dark), light: sigless(body.card.light) } }).toEqual({
      ...await readFleet(envOf(), NOW, stubFetch(ANSWERS)),
      card: { dark: `${ORIGIN}/og/fleet.png?v=${NOW}&sig=<sig>`, light: `${ORIGIN}/og/fleet.png?theme=light&v=${NOW}&sig=<sig>` },
    })
  })
})

describe('/og/fleet.png', () => {
  const fetchCard = async (path: string) => {
    const r = await og(ctx(req(path), envOf()))
    const png = new Uint8Array(await r.arrayBuffer())
    return [r.status, r.headers.get('content-type'), r.headers.get('x-og-tier'), r.headers.get('cache-control'), [...png.slice(1, 4)].map(c => String.fromCharCode(c)).join('')]
  }
  it('without a valid sig: the anonymous card, never an error; with one: the full card, not cached', async () => {
    const key = await ogKey(SECRET)
    const full = await imagePath(key, 'fleet', { v: '1' }, 'full', expDay(NOW, 7))
    const expiredSig = await imagePath(key, 'fleet', { v: '1' }, 'full', expDay(NOW, -2))
    expect([
      await fetchCard('/og/fleet.png'),
      await fetchCard(full.replace('v=1', 'v=2')),
      await fetchCard(expiredSig),
      await fetchCard(full),
    ]).toEqual([
      [200, 'image/png', 'anon', 'public, max-age=3600', 'PNG'],
      [200, 'image/png', 'anon (bad signature)', 'public, max-age=3600', 'PNG'],
      [200, 'image/png', 'anon (expired)', 'public, max-age=3600', 'PNG'],
      [200, 'image/png', 'full', 'private, no-store', 'PNG'],
    ])
  })
  it('any other card is not found', async () => {
    expect((await og(ctx(req('/og/map.png'), envOf()))).status).toBe(404)
  })
})

describe('POST /api/fleet/slack', () => {
  const post = (env: Env, query = '', token = 'job-token') => slackPost(ctx(req(`/api/fleet/slack${query}`, { authorization: `Bearer ${token}` }, 'POST'), env))
  const SLACK = { FLEET_JOB_TOKEN: 'job-token', DISKY_SLACK_BOT_TOKEN: 'xoxb-test', SLACK_FLEET_CHANNEL: 'C0FLEET' }

  it('takes only its Bearer token, and says what isn\'t configured', async () => {
    const got = []
    for (const [env, token] of [[envOf(), 'job-token'], [envOf(SLACK), 'wrong'], [envOf({ FLEET_JOB_TOKEN: 'job-token' }), 'job-token']] as const) {
      const r = await post(env, '', token)
      got.push([r.status, await r.json()])
    }
    expect(got).toEqual([
      [503, { error: 'FLEET_JOB_TOKEN is not set' }],
      [401, { error: 'unauthenticated' }],
      [503, { error: 'not configured: DISKY_SLACK_BOT_TOKEN, SLACK_FLEET_CHANNEL' }],
    ])
    expect(slackCalls).toEqual([])
  })

  it('posts once, keeping the post in FLEET_KV, then edits it in place; each run with a newly signed card', async () => {
    const kv = new Map<string, string>()
    const FLEET_KV = { get: async (k: string) => (kv.has(k) ? JSON.parse(kv.get(k)!) : null), put: async (k: string, v: string) => { kv.set(k, v) } } as unknown as KVNamespace
    const env = envOf({ ...SLACK, FLEET_KV })
    slackAnswers = [{ ok: true, channel: 'C0FLEET', ts: '1762524000.000100' }, { ok: true, channel: 'C0FLEET', ts: '1762524000.000100' }]
    const first = await (await post(env)).json() as Record<string, string>
    vi.setSystemTime((NOW + 3600) * 1000)
    const second = await (await post(env)).json() as Record<string, string>
    const image = (now: number) => `${ORIGIN}/og/fleet.png?v=${now}&sig=<sig>`
    expect([{ ...first, image: sigless(first.image) }, { ...second, image: sigless(second.image) }]).toEqual([
      { action: 'posted', channel: 'C0FLEET', ts: '1762524000.000100', image: image(NOW) },
      { action: 'updated', channel: 'C0FLEET', ts: '1762524000.000100', image: image(NOW + 3600) },
    ])
    expect(slackCalls.map(c => [c.method, c.body.channel, c.body.ts ?? null, sigless((c.body.blocks as { image_url?: string }[])[1].image_url!)])).toEqual([
      ['chat.postMessage', 'C0FLEET', null, image(NOW)],
      ['chat.update', 'C0FLEET', '1762524000.000100', image(NOW + 3600)],
    ])
    expect([...kv]).toEqual([['slack-fleet-post', '{"channel":"C0FLEET","ts":"1762524000.000100"}']])
  })

  it('with nowhere to keep the post, refuses to post a new one each run; `?new=1` posts one; `?dry=1` posts nothing', async () => {
    slackAnswers = [{ ok: true, channel: 'C0FLEET', ts: '1.2' }]
    const refused = await post(envOf(SLACK))
    const fresh = await post(envOf(SLACK), '?new=1')
    const dry = await (await post(envOf(SLACK), '?dry=1')).json() as { action: string; text: string }
    expect([
      [refused.status, await refused.json()],
      [fresh.status, ((await fresh.json()) as { action: string }).action],
      [dry.action, dry.text],
      slackCalls.map(c => c.method),
    ]).toEqual([
      [502, { error: 'no post to update: bind FLEET_KV, or post one with `?new=1` and set SLACK_FLEET_TS to its ts' }],
      [200, 'posted'],
      ['dry', 'disky fleet: gcs ok, cw 1 step issue open (warm-cache), hf ok, r2 ok'],
      ['chat.postMessage'],
    ])
  })
})
