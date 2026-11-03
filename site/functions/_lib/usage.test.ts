import { describe, expect, it, vi } from 'vitest'
import { type Ctx, type Env, gateFor, identify } from './auth'
import { cacheKeyFor, cacheMatch } from './edgeCache'
import { withStore } from './stores'
import { sqliteD1 } from './testD1'
import { anonymousCaller, grantCaller, Meter, METER, meterOf, ROUTES, routeOf, type UsagePoint, writeUsage } from './usage'

const stampPage = vi.fn(async (_env: unknown, _url: URL, res: Response) => res)
const anonymousPage = vi.fn(async (): Promise<Response | null> => null)
vi.mock('./og/serve.js', () => ({ stampPage, anonymousPage }))
const { onRequest } = await import('../_middleware')

describe('routeOf: a fixed table, never a raw path', () => {
  it('maps paths to their patterns; an HTML GET outside /api/ is `page`, anything else `other`', () => {
    const cases: [string, string, boolean][] = [
      ['/api/subtree', 'GET', false],
      ['/api/subtree/extra', 'GET', false],
      ['/api/plans', 'GET', false],
      ['/api/plans/42/items', 'POST', false],
      ['/api/plans/progress', 'GET', false],
      ['/api/auth/whoami', 'GET', false],
      ['/auth/google', 'GET', false],
      ['/auth/google/callback', 'GET', false],
      ['/data/snapshots/2026-10-20/meta.json', 'GET', false],
      ['/og', 'GET', false],
      ['/og/map.png', 'GET', false],
      ['/user/someone', 'GET', true],
      ['/assets/index-abc123.js', 'GET', false],
      ['/', 'GET', true],
      ['/scans', 'GET', true],
      ['/meta/some/dir', 'GET', true],
      ['/scans', 'POST', true],
      ['/api/nope', 'GET', true],
      ['/wp-login.php', 'GET', false],
    ]
    expect(cases.map(([p, m, h]) => routeOf(p, m, h))).toEqual([
      '/api/subtree', 'other', '/api/plans/*', '/api/plans/*', '/api/plans/progress', '/api/auth/*', '/auth/google/*',
      '/auth/google/*', '/data/*', '/og/*', '/og/*', '/user/*', '/assets/*', 'page', 'page', 'page', 'other', 'other',
      'other',
    ])
  })

  it('every Function file answers under a pattern of its own', async () => {
    // Node's fs, typed locally: the Functions' tsconfig is Workers-only (as in `wranglerExample.test.ts`).
    const { readdirSync } = (await import(/* @vite-ignore */ 'node:' + 'fs')) as { readdirSync(p: string, o: { recursive: true }): string[] }
    const root = new URL('..', (import.meta as unknown as { url: string }).url).pathname
    const files = readdirSync(root, { recursive: true })
      .filter(f => f.endsWith('.ts') && !f.endsWith('.test.ts') && !f.startsWith('_lib/') && f !== '_middleware.ts' && f !== 'tsconfig.json')
    const paths = files.map(f => `/${f.replace(/\.ts$/, '').replace(/(^|\/)index$/, '').replace(/\[\[?\w+\]?\]/g, 'x')}`)
    expect(paths.length > 50).toBe(true)
    const unmatched = paths.filter(p => routeOf(p, 'POST', false) === 'other')
    expect(unmatched).toEqual([])
    expect(new Set(ROUTES).size).toEqual(ROUTES.length)
  })
})

describe('caller resolution', () => {
  const req = (headers: Record<string, string> = {}) => new Request('https://gcs.example.test/api/subtree', { headers })
  it('a Bearer grant is a `token`, a cookie-borne one a `grant`; named by name, else note, else the via', () => {
    expect([
      grantCaller(req({ authorization: 'Bearer abc' }), { name: 'sheet sync', note: 'GSheet mirror' }),
      grantCaller(req({ cookie: 'oa_auth=x' }), { name: null, note: 'board deck link' }),
      grantCaller(req({ cookie: 'oa_auth=x' }), { name: '  ', note: null }),
      grantCaller(req({ authorization: 'bearer abc' }), { name: null, note: null }),
    ]).toEqual([
      { via: 'token', caller: 'sheet sync' },
      { via: 'grant', caller: 'board deck link' },
      { via: 'grant', caller: 'grant' },
      { via: 'token', caller: 'token' },
    ])
  })

  it('never carries an email, and fits the 96-byte index', () => {
    expect([
      grantCaller(req(), { name: 'Bob Smith (bob.smith@example.com)', note: null }).caller,
      grantCaller(req(), { name: 'x'.repeat(200), note: null }).caller,
    ]).toEqual(['Bob Smith (<email>)', 'x'.repeat(96)])
  })

  it('an unidentified request is `bot` by user agent, else `public`', () => {
    expect([
      anonymousCaller(req({ 'user-agent': 'Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)' })),
      anonymousCaller(req({ 'user-agent': 'Mozilla/5.0 (compatible; Googlebot/2.1)' })),
      anonymousCaller(req({ 'user-agent': 'facebookexternalhit/1.1' })),
      anonymousCaller(req({ 'user-agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/141.0' })),
      anonymousCaller(req()),
    ]).toEqual([
      { via: 'bot', caller: 'bot' },
      { via: 'bot', caller: 'bot' },
      { via: 'bot', caller: 'bot' },
      { via: 'public', caller: 'public' },
      { via: 'public', caller: 'public' },
    ])
  })

  it('`identify` records what the gate proved on the request\'s meter: a minted token by its name', async () => {
    const { db } = await sqliteD1()
    const env = { DB: db, SESSION_SECRET: 'test-secret', BASE_SCOPE: 'gcs' } as Env
    const { token } = await gateFor(env)!.mint({ email: null, scopes: ['gcs:read'], name: 'sheet sync', createdBy: 'test', expiresAt: null, maxRedeems: null })
    const m = new Meter()
    const ctx = { request: new Request('https://gcs.example.test/api/subtree', { headers: { authorization: `Bearer ${token}` } }), env, data: { [METER]: m } } as Ctx
    const id = await identify(ctx)
    expect([id?.via, m.via, m.caller]).toEqual(['grant', 'token', 'sheet sync'])
  })

  it('a failed identification records nothing (the middleware falls back to public / bot)', async () => {
    const { db } = await sqliteD1()
    const m = new Meter()
    const ctx = { request: new Request('https://gcs.example.test/api/subtree', { headers: { authorization: 'Bearer nope' } }), env: { DB: db, SESSION_SECRET: 's' } as Env, data: { [METER]: m } } as Ctx
    expect([await identify(ctx), m.via, m.caller]).toEqual([null, null, null])
  })
})

// --- the middleware's point, end to end, with stand-in bindings ------------------------------------------------

class FakeStmt {
  constructor(readonly sql: string, readonly args: unknown[] = []) {}
  bind(...a: unknown[]) { return new FakeStmt(this.sql, a) }
  async first() { return { n: 1 } }
  async all() { return { results: [], success: true, meta: { rows_read: 7, rows_written: 1 } } }
  async run() { return { results: [], success: true, meta: { rows_read: 2 } } }
  async raw() { return [] }
}

function bindings() {
  const points: UsagePoint[] = []
  const batched: unknown[][] = []
  const env: Record<string, unknown> = {
    USAGE: { writeDataPoint: (p: UsagePoint) => { points.push(p) } },
    DB: {
      prepare: (sql: string) => new FakeStmt(sql),
      batch: async (stmts: unknown[]) => {
        batched.push(stmts)
        if (!stmts.every(s => s instanceof FakeStmt)) throw new Error('batch got a wrapper, not a statement')
        return stmts.map(() => ({ results: [], success: true, meta: { rows_read: 5, rows_written: 3 } }))
      },
      exec: async () => ({ count: 0, duration: 0 }),
    },
    INDEX_R2: {
      get: async (key: string, o?: { range?: { offset: number; length: number } }) =>
        key === 'missing' ? null : { size: 10_000, range: o?.range, body: null },
      head: async () => ({ size: 10_000 }),
      put: async () => null,
      list: async () => ({ objects: [] }),
      createMultipartUpload: async () => null,
    },
    CACHE_KV: {
      get: async () => null,
      getWithMetadata: async () => ({ value: null, metadata: null }),
      put: async () => undefined,
      list: async () => ({ keys: [] }),
    },
  }
  return { env, points, batched }
}

type Handler = (ctx: { request: Request; env: Env; data: Record<string, unknown> }) => Promise<Response>

async function serveOne(request: Request, env: Record<string, unknown>, handler: Handler): Promise<number> {
  const pending: Promise<unknown>[] = []
  const data: Record<string, unknown> = {}
  const ctx = {
    request,
    env: env as never,
    data,
    waitUntil: (p: Promise<unknown>) => { pending.push(p) },
    next: () => handler({ request, env: env as Env, data }),
  }
  const res = await onRequest(ctx)
  await Promise.all(pending)
  return res.status
}

/** The point with its wall time (`doubles[1]`) normalized. */
const shape = (p: UsagePoint) => ({ ...p, doubles: p.doubles.map((d, i) => (i === 1 ? -1 : d)) })

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } })

describe('the middleware writes one point per request', () => {
  it('a signed-in browser\'s subtree: route, session, the reads it made, a cache miss; no raw path or query', async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => undefined } }
    const { env, points, batched } = bindings()
    const status = await serveOne(new Request('http://localhost/api/subtree?date=2026-10-20&path=secret%2Fdir'), env, async ctx => {
      await identify(ctx as Ctx)
      expect(await cacheMatch(ctx.env, cacheKeyFor('subtree', '', 'k'))).toBeNull()
      const r2 = ctx.env.INDEX_R2!
      await r2.get('interval-store/g/path.parquet', { range: { offset: 100, length: 300 } })
      await r2.get('missing')
      await r2.head('interval-store/g/path.parquet')
      await ctx.env.DB!.prepare('SELECT 1 WHERE x = ?').bind(1).all()
      await ctx.env.DB!.prepare('SELECT 1').first()
      const db = ctx.env.DB!
      await db.batch([db.prepare('SELECT a').bind(1), db.prepare('SELECT b')])
      return json({ ok: true })
    })
    expect(status).toEqual(200)
    expect(batched.map(b => b.map(s => (s as FakeStmt).sql))).toEqual([['SELECT a', 'SELECT b']])
    expect(points.map(shape)).toEqual([{
      blobs: ['/api/subtree', 'GET', 'session', 'session', '2xx', 'miss', 'primary', 'dev'],
      doubles: [1, -1, 3, 300, 4, 17, 1, 7],
      indexes: ['session'],
    }])
  })

  it('a crawler\'s page view: `page`, `bot`, nothing read', async () => {
    const { env, points } = bindings()
    env.PROD_HOST = 'gcs.example.test'
    const req = new Request('https://gcs.example.test/scans?x=1', { headers: { 'user-agent': 'Discordbot/2.0' } })
    await serveOne(req, env, async () => new Response('<!doctype html>\n', { headers: { 'content-type': 'text/html; charset=utf-8' } }))
    expect(points.map(shape)).toEqual([{
      blobs: ['page', 'GET', 'bot', 'bot', '2xx', 'none', 'primary', 'prod'],
      doubles: [1, -1, 0, 0, 0, 0, 0, 0],
      indexes: ['bot'],
    }])
  })

  it('an unauthenticated write: the route, `public`, its 4xx', async () => {
    const { env, points } = bindings()
    await serveOne(new Request('https://gcs.example.test/api/actions', { method: 'POST' }), env, async ctx => {
      expect(await identify(ctx as Ctx)).toBeNull()
      return json({ error: 'unauthenticated' }, 401)
    })
    expect(points.map(shape)).toEqual([{
      blobs: ['/api/actions', 'POST', 'public', 'public', '4xx', 'none', 'primary', 'dev'],
      doubles: [1, -1, 0, 0, 0, 0, 0, 0],
      indexes: ['public'],
    }])
  })

  it('a secondary store\'s cached subtree: the store\'s key, an edge hit', async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => json({ cached: true }), put: async () => undefined } }
    const { env, points } = bindings()
    env.STORES_JSON = '{"meta":{"path":"/meta","vars":{}}}'
    await serveOne(new Request('http://localhost/api/subtree?store=meta&path=x'), env, async ctx => {
      const c = withStore(ctx)
      if (c instanceof Response) return c
      await identify(c as Ctx)
      return (await cacheMatch(c.env, cacheKeyFor('subtree', '', 'k', 'meta')))!
    })
    expect(points.map(shape)).toEqual([{
      blobs: ['/api/subtree', 'GET', 'session', 'session', '2xx', 'edge-hit', 'meta', 'dev'],
      doubles: [1, -1, 0, 0, 0, 0, 0, 0],
      indexes: ['session'],
    }])
  })

  it('a meter\'s cache field keeps the costliest of several lookups', () => {
    const tiers = (...ts: ('edge-hit' | 'kv-hit' | 'miss')[]) => { const m = new Meter(); for (const t of ts) m.cached(t); return m.cache }
    expect([tiers(), tiers('edge-hit'), tiers('edge-hit', 'kv-hit'), tiers('miss', 'edge-hit'), tiers('kv-hit', 'edge-hit')]).toEqual(['none', 'edge-hit', 'kv-hit', 'miss', 'kv-hit'])
  })
})

describe('no binding, no point', () => {
  it('without `USAGE` the middleware schedules only its own work and the response is untouched', async () => {
    const { env } = bindings()
    delete env.USAGE
    const pending: Promise<unknown>[] = []
    const res = await onRequest({
      request: new Request('https://gcs.example.test/api/health'),
      env: env as never,
      data: {},
      waitUntil: p => { pending.push(p) },
      next: async () => json({ ok: true }),
    })
    expect([res.status, await res.text(), pending.length]).toEqual([200, '{"ok":true}', 1])
  })

  it('a throwing binding is logged and dropped, never thrown', () => {
    const log = vi.spyOn(console, 'log').mockImplementation(() => undefined)
    const env = { USAGE: { writeDataPoint: () => { throw new Error('quota') } } } as never
    writeUsage(env, { blobs: [], doubles: [], indexes: [] })
    expect(log.mock.calls).toEqual([['usage point dropped: quota']])
    log.mockRestore()
  })

  it('`meterOf` finds the Pages context\'s meter outside any async context', () => {
    const m = new Meter()
    expect([meterOf({ data: { [METER]: m } }) === m, meterOf({}), meterOf({ request: new Request('https://x.test/') })]).toEqual([true, undefined, undefined])
  })
})
