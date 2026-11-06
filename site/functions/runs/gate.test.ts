/** `/runs/<id>` and `/og/run.png` tell an anonymous caller nothing about a deletion run: the shell's unfurl is one fixed
 * stamp for a real id and an unknown one alike, with no D1 read; the card needs a viewer. */
import { emailSub, sessionCookie, signSession } from '@open-athena/auth'
import { describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'

vi.mock('../_lib/unfurl.js', () => ({
  unfurlShell: async (_ctx: unknown, meta: unknown) => new Response(JSON.stringify(meta)),
  assetImage: async () => null,
}))
vi.mock('../_lib/og/runData.js', async importOriginal => ({
  ...await importOriginal<typeof import('../_lib/og/runData.js')>(),
  runShapeAt: async () => ({
    complete: true,
    shape: {
      mode: 'real', state: 'running', started_ts: 1793796765, done: 4889000, total: 36062888, unanswered: 0, n_buckets: 3,
      as_of: 1793800365, series: [], peak: null, latest: null, avg: null,
    },
  }),
}))

vi.mock('../_lib/og/serve.js', () => ({
  serveCard: async () => new Response('card', { status: 200 }),
  serveRunCard: async () => new Response('run card', { status: 200 }),
}))

const { onRequest } = await import('./[[id]]')
const { onRequestGet: ogGet } = await import('../og/[[path]]')

const SECRET = 'test-session-secret-0123456789abcdef'
const ORIGIN = 'https://gcs.example.org'
const REAL = 'plan-1/20261104T125245Z'
const UNKNOWN = 'nope/20260101T000000Z'
const GENERIC = { title: 'Deletion run', desc: 'A deletion run. Sign in to see it.', image: `${ORIGIN}/og.jpg` }

async function world() {
  const { db, raw } = await sqliteD1()
  raw.prepare("INSERT INTO allowed_emails (email, who, ts) VALUES ('viewer@example.org', 'admin@example.org', 1)").run()
  raw.prepare(
    "INSERT INTO deletion_runs (run_id, plan, scan, head, exec_head, actor, mode, started_ts, log_dir) VALUES (?, 'p', '2026-11-03T1622', 1, 1, 'a@example.org', 'real', 1793796765, 'gs://x/sweep/p/r')",
  ).run(REAL)
  const calls: string[] = []
  const spy = new Proxy(db, {
    get(target, prop) {
      const v = Reflect.get(target, prop) as unknown
      if (typeof v !== 'function') return v
      return (...args: unknown[]) => {
        calls.push(`${String(prop)}: ${String(args[0]).slice(0, 60)}`)
        return (v as (...a: unknown[]) => unknown).apply(target, args)
      }
    },
  })
  const env = {
    DB: spy, SESSION_SECRET: SECRET, BASE_SCOPE: 'gcs', STAFF_DOMAIN: 'openathena.ai', OG_CARDS: '1',
    ASSETS: { fetch: async () => new Response('', { status: 404 }) },
  } as unknown as Env & { ASSETS: { fetch: (r: Request) => Promise<Response> }; OG_CARDS: string }
  const cookie = sessionCookie(await signSession(emailSub('viewer@example.org'), SECRET, Date.now(), 3600), { name: 'oa_auth', ttlS: 3600, secure: true }).split(';')[0]
  return { env, calls, cookie }
}
type World = Awaited<ReturnType<typeof world>>

const page = async (env: World['env'], id: string, headers: Record<string, string> = {}) => {
  const r = await onRequest({ request: new Request(`${ORIGIN}/runs/${id}`, { headers }), env })
  return { status: r.status, cache: r.headers.get('cache-control'), body: await r.text() }
}

describe('/runs/<id> for an anonymous caller', () => {
  it('a real id and an unknown one get the same generic stamp, and no D1 call is made', async () => {
    const { env, calls } = await world()
    const real = await page(env, REAL)
    const unknown = await page(env, UNKNOWN)
    expect([real, unknown, calls]).toEqual([
      { status: 200, cache: 'private, no-store', body: JSON.stringify({ ...GENERIC, page: `${ORIGIN}/runs/${REAL}` }) },
      { status: 200, cache: 'private, no-store', body: JSON.stringify({ ...GENERIC, page: `${ORIGIN}/runs/${UNKNOWN}` }) },
      [],
    ])
  })
})

describe('/runs/<id> for a signed-in viewer', () => {
  it('still carries the run\'s state and counts', async () => {
    const { env, cookie } = await world()
    const r = JSON.parse((await page(env, REAL, { cookie })).body) as Record<string, string>
    const version = r.image.split('v=')[1].split('&')[0]
    expect(r).toEqual({
      title: 'Deletion running',
      desc: '4,889,000 of 36,062,888 objects attempted · as of 2026-11-04 13:52 UTC · 3 buckets · scan 2026-11-03T1622',
      image: `${ORIGIN}/og/run.png?id=plan-1%2F20261104T125245Z&v=${version}&c=2`,
      imageType: 'image/png',
      page: `${ORIGIN}/runs/${REAL}`,
    })
  })
})

describe('/og/run.png', () => {
  it('refuses an anonymous caller with the gate\'s 401, real id or not, and reads no run', async () => {
    const { env, calls } = await world()
    const get = async (id: string) => {
      const r = await ogGet({ request: new Request(`${ORIGIN}/og/run.png?id=${encodeURIComponent(id)}`), env: env as never })
      return [r.status, await r.text()]
    }
    expect([await get(REAL), await get(UNKNOWN), calls]).toEqual([
      [401, '{"error":"unauthenticated"}\n'],
      [401, '{"error":"unauthenticated"}\n'],
      [],
    ])
  })

  it('serves a signed-in viewer\'s', async () => {
    const { env, cookie } = await world()
    const r = await ogGet({ request: new Request(`${ORIGIN}/og/run.png?id=${encodeURIComponent(REAL)}`, { headers: { cookie } }), env: env as never })
    expect([r.status, await r.text()]).toEqual([200, 'run card'])
  })
})
