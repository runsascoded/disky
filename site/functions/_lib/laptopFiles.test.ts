import { describe, expect, it } from 'vitest'
import type { Env, Identity } from './auth'
import { gateFor } from './auth'
import { type Audit, DEFAULT_MAX_BYTES, deniedBy, fileSegs, globRegex, serveLaptopFile } from './laptopFiles'
import { sqliteD1 } from './testD1'
import { onRequest } from '../v1/laptop-files/[[path]]'

const ORIGIN = 'https://disk.example'
const TUNNEL = 'https://files.disk.example'
const ENV: Env = {
  BASE_SCOPE: 'laptop',
  LAPTOP_TUNNEL_URL: TUNNEL,
  LAPTOP_TUNNEL_CLIENT_ID: 'cid.access',
  LAPTOP_TUNNEL_CLIENT_SECRET: 'csecret',
  LAPTOP_FILES_DENY: 'Users/*/.ssh/**, **/.env*, **/*.pem, Users/*/Library/Keychains/**',
}

const ident = (via: Identity['via'], admin: boolean, scopes: string[] = ['laptop']): Identity =>
  ({ email: via === 'session' ? 'me@example.org' : null, name: null, scopes: admin ? [...scopes, 'admin'] : scopes, admin, via, subject: null })
const ADMIN = ident('session', true)

interface Sent { url: string; method: string; headers: Record<string, string>; cache?: string }

/** A stub tunnel: records what reached it, answers `respond`. */
function tunnel(respond: (s: Sent) => Response | Promise<Response>) {
  const sent: Sent[] = []
  const fetch = (async (url: string, init: RequestInit) => {
    const s = { url, method: init.method ?? 'GET', headers: { ...(init.headers as Record<string, string>) }, cache: init.cache }
    sent.push(s)
    return respond(s)
  }) as unknown as typeof globalThis.fetch
  return { sent, fetch }
}

const file = (body: string, headers: Record<string, string> = {}, status = 200) =>
  new Response(body, { status, headers: { 'content-type': 'application/pdf', 'content-length': String(body.length), 'accept-ranges': 'bytes', ...headers } })

/** dt-serve's answer for `body`: a 206 for a single `bytes=a-b` range, else the whole file. */
const served = (body: string, type = 'application/pdf') => (s: Sent) => {
  const m = s.headers.range?.match(/^bytes=(\d+)-(\d+)$/)
  if (!m) return file(body, { 'content-type': type })
  const [a, b] = [Number(m[1]), Math.min(Number(m[2]), body.length - 1)]
  return new Response(body.slice(a, b + 1), { status: 206, headers: { 'content-type': type, 'content-length': String(b - a + 1), 'content-range': `bytes ${a}-${b}/${body.length}`, 'accept-ranges': 'bytes' } })
}

async function call(path: string | null, opts: { id?: Identity; env?: Env; method?: string; range?: string; recent?: number; respond?: (s: Sent) => Response | Promise<Response>; inline?: boolean } = {}) {
  const q = new URLSearchParams()
  if (path != null) q.set('path', path)
  if (opts.inline) q.set('inline', '1')
  const req = new Request(`${ORIGIN}/v1/laptop-files/get?${q}`, { method: opts.method ?? 'GET', headers: opts.range ? { range: opts.range } : {} })
  const t = tunnel(opts.respond ?? served('%PDF-1.7 hello'))
  const audits: Audit[] = []
  const res = await serveLaptopFile(req, opts.env ?? ENV, opts.id ?? ADMIN, {
    fetch: t.fetch,
    audit: async a => { audits.push(a) },
    recent: async () => opts.recent ?? 0,
  })
  const type = res.headers.get('content-type') ?? ''
  const body = type.startsWith('application/json') ? await res.json() : await res.text()
  return { status: res.status, body, headers: Object.fromEntries(res.headers), sent: t.sent, audits }
}

describe('path parsing', () => {
  it('fileSegs: a relative path of real segments, else null', () => {
    expect([
      'Users/ryan/Downloads/a b+c.pdf', '/Users/ryan', 'Users//ryan', 'Users/./ryan', 'Users/../etc', '', null, 'a\0b',
    ].map(fileSegs)).toEqual([
      ['Users', 'ryan', 'Downloads', 'a b+c.pdf'], null, null, null, null, null, null, null,
    ])
  })
  it('globRegex: `*`/`?` within a segment, `**` across them, `**/x` at the top too, `x/**` x itself', () => {
    const cases: [string, string[]][] = [
      ['a/*/c', ['a/b/c', 'a/b/x/c', 'a/c']],
      ['a/**/c', ['a/c', 'a/b/c', 'a/b/x/c', 'xa/b/c']],
      ['a/**', ['a', 'a/b', 'a/b/c', 'ab']],
      ['**/b?', ['bx', 'a/bx', 'a/bxy', 'a/b']],
    ]
    expect(cases.map(([g, ps]) => [g, ps.map(p => globRegex(g).test(p))])).toEqual([
      ['a/*/c', [true, false, false]],
      ['a/**/c', [true, true, true, false]],
      ['a/**', [true, true, true, false]],
      ['**/b?', [true, true, false, false]],
    ])
  })
  it('deniedBy: the first glob a path matches', () => {
    const globs = ENV.LAPTOP_FILES_DENY!.split(',').map(s => s.trim())
    expect([
      'Users/ryan/.ssh', 'Users/ryan/.ssh/id_ed25519', 'Users/ryan/c/x/.envrc', '.env', 'Users/ryan/c/x/.env.local',
      'Users/ryan/certs/server.pem', 'Users/ryan/Library/Keychains/login.keychain-db',
      'Users/ryan/Downloads/tax.pdf', 'Users/ryan/c/environment.md', 'Users/ryan/.sshx/a',
    ].map(p => deniedBy(p, globs))).toEqual([
      'Users/*/.ssh/**', 'Users/*/.ssh/**', '**/.env*', '**/.env*', '**/.env*',
      '**/*.pem', 'Users/*/Library/Keychains/**',
      null, null, null,
    ])
  })
})

describe('/v1/laptop-files/get — who', () => {
  it('an admin session reads the file: the service token goes upstream, the path segment-encoded', async () => {
    const r = await call('Users/ryan/Downloads/a b+c.pdf')
    expect({ status: r.status, body: r.body, sent: r.sent }).toEqual({
      status: 200,
      body: '%PDF-1.7 hello',
      sent: [{
        url: `${TUNNEL}/files/Users/ryan/Downloads/a%20b%2Bc.pdf`,
        method: 'GET',
        headers: { 'CF-Access-Client-Id': 'cid.access', 'CF-Access-Client-Secret': 'csecret', range: `bytes=0-${DEFAULT_MAX_BYTES - 1}` },
        // Someone's file never lands in the Worker's subrequest cache.
        cache: 'no-store',
      }],
    })
    expect(r.audits).toEqual([{ status: 200, bytes: 14, range: undefined }])
  })
  it('a share link is refused even with admin scope and no hide scopes; a plain viewer too; neither reaches the laptop', async () => {
    const grant = await call('Users/ryan/Downloads/a.pdf', { id: ident('grant', true) })
    const viewer = await call('Users/ryan/Downloads/a.pdf', { id: ident('session', false) })
    expect([grant.status, grant.body, grant.sent, grant.audits]).toEqual([403, { error: 'share links can’t open files' }, [], [{ status: 403, reason: 'grant' }]])
    expect([viewer.status, viewer.body, viewer.sent, viewer.audits]).toEqual([403, { error: 'admins only' }, [], [{ status: 403, reason: 'not admin' }]])
  })
  it('the route: anonymous → 401, a secondary store → 404, an admin session through to the tunnel', async () => {
    const { db } = await sqliteD1()
    await db.prepare('INSERT INTO allowed_emails (email, who, ts) VALUES (?, ?, 1)').bind('ops@example.org', 'x').run()
    await db.prepare('INSERT INTO admin_emails (email, who, ts) VALUES (?, ?, 1)').bind('ops@example.org', 'x').run()
    const env: Env = { ...ENV, DB: db, SESSION_SECRET: 's', ADMIN_EMAILS: '1', LAPTOP_TUNNEL_URL: 'https://tunnel.invalid' }
    const cookie = (await gateFor(env)!.signIn('ops@example.org', new Request(`${ORIGIN}/signin`)))!.cookie.split(';')[0]
    const get = (q: string, headers: Record<string, string> = {}) => onRequest({ request: new Request(`${ORIGIN}/v1/laptop-files/get?${q}`, { headers }), env })
    expect((await get('path=Users/a.pdf')).status).toBe(401)
    expect((await get('path=Users/a.pdf&store=nas', { cookie })).status).toBe(404)
    // `.invalid` never resolves: the admin got past every gate to an unreachable laptop.
    const off = await get('path=Users/a.pdf', { cookie })
    expect([off.status, await off.json()]).toEqual([503, { offline: true, reason: 'unreachable' }])
    expect((await db.prepare("SELECT event, session_sub, path, status FROM access_log WHERE event = 'file'").all()).results).toEqual([
      { event: 'file', session_sub: 'e:ops@example.org', path: 'Users/a.pdf', status: 503 },
    ])
  })
})

describe('/v1/laptop-files/get — what', () => {
  it('bad paths and deny-listed paths never reach the laptop', async () => {
    const rs = await Promise.all([
      call('../etc/passwd'),
      call(null),
      call('Users/ryan/.ssh/id_ed25519'),
      call('Users/ryan/c/disky/.envrc'),
    ])
    expect(rs.map(r => [r.status, r.body, r.sent.length, r.audits])).toEqual([
      [400, { error: 'bad path' }, 0, [{ status: 400, reason: 'bad path' }]],
      [400, { error: 'bad path' }, 0, [{ status: 400, reason: 'bad path' }]],
      [403, { error: 'this path is never previewed', deny: 'Users/*/.ssh/**' }, 0, [{ status: 403, reason: 'deny Users/*/.ssh/**' }]],
      [403, { error: 'this path is never previewed', deny: '**/.env*' }, 0, [{ status: 403, reason: 'deny **/.env*' }]],
    ])
  })
  it('no tunnel configured → 503, nothing sent; a non-GET → 405; another sub-route → 404', async () => {
    const rs = await Promise.all([
      call('Users/a.pdf', { env: { BASE_SCOPE: 'laptop' } }),
      call('Users/a.pdf', { method: 'POST' }),
    ])
    expect(rs.map(r => [r.status, r.body, r.sent.length])).toEqual([
      [503, { error: 'no file tunnel configured' }, 0],
      [405, { error: 'method' }, 0],
    ])
  })
})

describe('/v1/laptop-files/get — how much', () => {
  it('a whole-file GET over the cap is a 413 (body cancelled); a Range read of the same file passes through as a 206', async () => {
    const big = String(DEFAULT_MAX_BYTES + 1)
    const respond = (s: Sent) => s.headers.range
      ? new Response('0123', { status: 206, headers: { 'content-type': 'video/mp4', 'content-length': '4', 'content-range': `bytes 0-3/${big}`, 'accept-ranges': 'bytes' } })
      : new Response('x', { headers: { 'content-type': 'video/mp4', 'content-length': big } })
    const whole = await call('Users/ryan/Movies/a.mp4', { respond })
    const part = await call('Users/ryan/Movies/a.mp4', { respond, range: 'bytes=0-3', inline: true })
    expect([whole.status, whole.body, whole.audits]).toEqual([413, { error: 'too large to open whole; media can still stream', bytes: DEFAULT_MAX_BYTES + 1, cap: DEFAULT_MAX_BYTES }, [{ status: 413, bytes: 0, reason: 'cap' }]])
    expect([part.status, part.body, part.sent[0].headers.range, part.audits]).toEqual([206, '0123', 'bytes=0-3', [{ status: 206, bytes: 4, range: `bytes 0-3/${big}` }]])
    expect(part.headers).toEqual({
      'accept-ranges': 'bytes',
      'cache-control': 'private, no-store',
      'content-disposition': "inline; filename*=UTF-8''a.mp4",
      'content-length': '4',
      'content-range': `bytes 0-3/${big}`,
      'content-security-policy': "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'",
      'content-type': 'video/mp4',
      'x-content-type-options': 'nosniff',
    })
  })
  it('a whole-file GET asks upstream for the first `cap` bytes: through the tunnel Cloudflare drops `content-length` on whole responses, so the size comes from `content-range`', async () => {
    const env = { ...ENV, LAPTOP_FILES_MAX_BYTES: '8' }
    // dt-serve behind Cloudflare: a 206 for a range, a length-less 200 otherwise.
    const respond = (body: string) => (s: Sent) => {
      const m = s.headers.range?.match(/^bytes=0-(\d+)$/)
      if (!m) return new Response(body, { headers: { 'content-type': 'application/pdf' } })
      if (!body.length) return new Response('', { status: 416, headers: { 'content-range': 'bytes */0' } })
      const end = Math.min(Number(m[1]), body.length - 1)
      return new Response(body.slice(0, end + 1), { status: 206, headers: { 'content-type': 'application/pdf', 'content-length': String(end + 1), 'content-range': `bytes 0-${end}/${body.length}` } })
    }
    const small = await call('Users/a.pdf', { env, respond: respond('%PDF') })
    const exact = await call('Users/a.pdf', { env, respond: respond('12345678') })
    const empty = await call('Users/a.pdf', { env, respond: respond('') })
    const big = await call('Users/a.pdf', { env, respond: respond('123456789') })
    expect([small, exact, empty, big].map(r => [r.status, r.body, r.headers['content-length'], r.headers['content-range'], r.sent[0].headers.range, r.audits])).toEqual([
      [200, '%PDF', '4', undefined, 'bytes=0-7', [{ status: 200, bytes: 4, range: undefined }]],
      [200, '12345678', '8', undefined, 'bytes=0-7', [{ status: 200, bytes: 8, range: undefined }]],
      [200, '', '0', undefined, 'bytes=0-7', [{ status: 200, bytes: 0, range: undefined }]],
      [413, { error: 'too large to open whole; media can still stream', bytes: 9, cap: 8 }, undefined, undefined, 'bytes=0-7', [{ status: 413, bytes: 0, reason: 'cap' }]],
    ])
  })
  it('a ranged request answered whole (Cloudflare’s cache drops `Range` on a URL’s first miss) is cancelled and retried once; a second whole answer is a 502, never the wrong bytes', async () => {
    const env = { ...ENV, LAPTOP_FILES_MAX_BYTES: '8' }
    const body = '0123456789'
    // The tunnel's first answer per URL ignores `Range`; later ones honor it.
    const learning = () => {
      let seen = 0
      return (s: Sent) => {
        const m = s.headers.range?.match(/^bytes=(\d+)-(\d+)$/)
        if (seen++ === 0 || !m) return new Response(body, { headers: { 'content-type': 'video/mp4' } })
        const [a, b] = [Number(m[1]), Math.min(Number(m[2]), body.length - 1)]
        return new Response(body.slice(a, b + 1), { status: 206, headers: { 'content-type': 'video/mp4', 'content-length': String(b - a + 1), 'content-range': `bytes ${a}-${b}/${body.length}` } })
      }
    }
    const part = await call('Users/a.mp4', { env, range: 'bytes=2-4', respond: learning() })
    const whole = await call('Users/a.mp4', { env, respond: learning() })
    const stubborn = await call('Users/a.mp4', { env, range: 'bytes=2-4', respond: () => new Response(body) })
    expect([part, whole, stubborn].map(r => [r.status, r.body, r.headers['content-range'], r.sent.map(s => s.headers.range), r.audits])).toEqual([
      [206, '234', 'bytes 2-4/10', ['bytes=2-4', 'bytes=2-4'], [{ status: 206, bytes: 3, range: 'bytes 2-4/10' }]],
      [413, { error: 'too large to open whole; media can still stream', bytes: 10, cap: 8 }, undefined, ['bytes=0-7', 'bytes=0-7'], [{ status: 413, bytes: 0, reason: 'cap' }]],
      [502, { error: 'the file tunnel ignored the byte range' }, undefined, ['bytes=2-4', 'bytes=2-4'], [{ status: 502, reason: 'range ignored' }]],
    ])
  })
  it('the per-session rate limit: at LAPTOP_FILES_PER_MIN recent reads, a 429 without touching the laptop', async () => {
    const r = await call('Users/a.pdf', { env: { ...ENV, LAPTOP_FILES_PER_MIN: '5' }, recent: 5 })
    expect([r.status, r.sent.length, r.audits]).toEqual([429, 0, [{ status: 429, reason: 'rate' }]])
  })
  it('a HEAD forwards as a HEAD and answers no body (the panel’s reachability probe)', async () => {
    const r = await call('Users/a.pdf', { method: 'HEAD' })
    expect([r.status, r.body, r.sent[0].method, r.audits]).toEqual([200, '', 'HEAD', [{ status: 200, bytes: 0, range: undefined }]])
  })
})

describe('/v1/laptop-files/get — the upstream fetch', () => {
  it('is called unbound: the Workers runtime’s `fetch` throws "Illegal invocation" when `this` is the deps object', async () => {
    const req = new Request(`${ORIGIN}/v1/laptop-files/get?path=Users/a.pdf`)
    const res = await serveLaptopFile(req, ENV, ADMIN, {
      fetch: function (this: unknown) {
        if (this !== undefined && this !== globalThis) throw new TypeError('Illegal invocation')
        return Promise.resolve(new Response('%PDF', { status: 206, headers: { 'content-length': '4', 'content-range': 'bytes 0-3/4' } }))
      } as unknown as typeof fetch,
      audit: null,
      recent: async () => 0,
    })
    expect([res.status, await res.text()]).toEqual([200, '%PDF'])
  })
})

describe('/v1/laptop-files/get — offline', () => {
  it('unreachable, the tunnel without a connector (530), Cloudflare 502, and dt-serve with files off all read as offline', async () => {
    const rs = await Promise.all([
      call('Users/a.pdf', { respond: () => { throw new TypeError('network') } }),
      call('Users/a.pdf', { respond: () => new Response('error 1033', { status: 530 }) }),
      call('Users/a.pdf', { respond: () => new Response('bad gateway', { status: 502 }) }),
      call('Users/a.pdf', { respond: () => new Response('not served locally\n', { status: 404 }) }),
    ])
    expect(rs.map(r => [r.status, r.body])).toEqual([
      [503, { offline: true, reason: 'unreachable' }],
      [503, { offline: true, reason: 'tunnel 530' }],
      [503, { offline: true, reason: 'tunnel 502' }],
      [503, { offline: true, reason: 'file serving off' }],
    ])
  })
  it('a real 404 from the laptop stays a 404; Access refusing the token is a 502 naming the upstream status', async () => {
    const gone = await call('Users/a.pdf', { respond: () => new Response('not found\n', { status: 404, headers: { 'content-type': 'text/plain' } }) })
    const refused = await call('Users/a.pdf', { respond: () => new Response('forbidden', { status: 403 }) })
    expect([[gone.status, gone.body], [refused.status, refused.body]]).toEqual([
      [404, 'not found\n'],
      [502, { error: 'the file tunnel refused this site', upstream: 403 }],
    ])
  })
})

describe('/v1/laptop-files/status — is the laptop reachable', () => {
  const status = async (respond: (s: Sent) => Response | Promise<Response>, id: Identity = ADMIN, env: Env = ENV) => {
    const t = tunnel(respond)
    const res = await serveLaptopFile(new Request(`${ORIGIN}/v1/laptop-files/status`), env, id, { fetch: t.fetch, audit: null, recent: async () => 0 })
    return { status: res.status, body: await res.json(), cc: res.headers.get('cache-control'), sent: t.sent.map(s => [s.url, s.method, s.headers.range ?? null]) }
  }
  it('probes the tunnel’s `/files/` with the service token: 200 `ok` → online; offline answers say why; cached privately for 15 s', async () => {
    const rs = await Promise.all([
      status(() => new Response(JSON.stringify({ ok: true, root: '/' }), { headers: { 'content-type': 'application/json' } })),
      status(() => new Response('error 1033', { status: 530 })),
      status(() => new Response('not served locally', { status: 404 })),
      status(() => { throw new TypeError('network') }),
      status(() => new Response('<html>old dt-serve</html>', { status: 200 })),
    ])
    expect(rs).toEqual([
      { status: 200, body: { online: true }, cc: 'private, max-age=15', sent: [[`${TUNNEL}/files/`, 'GET', null]] },
      { status: 200, body: { online: false, reason: 'tunnel 530' }, cc: 'private, max-age=15', sent: [[`${TUNNEL}/files/`, 'GET', null]] },
      { status: 200, body: { online: false, reason: 'file serving off' }, cc: 'private, max-age=15', sent: [[`${TUNNEL}/files/`, 'GET', null]] },
      { status: 200, body: { online: false, reason: 'unreachable' }, cc: 'private, max-age=15', sent: [[`${TUNNEL}/files/`, 'GET', null]] },
      { status: 200, body: { online: false, reason: 'upstream 200' }, cc: 'private, max-age=15', sent: [[`${TUNNEL}/files/`, 'GET', null]] },
    ])
  })
  it('the same gates as a file: grants and non-admins refused, nothing probed; no tunnel → 503', async () => {
    const ok = () => new Response('{"ok":true}')
    const rs = await Promise.all([status(ok, ident('grant', true)), status(ok, ident('session', false)), status(ok, ADMIN, { BASE_SCOPE: 'laptop' })])
    expect(rs.map(r => [r.status, r.body, r.sent])).toEqual([
      [403, { error: 'share links can’t open files' }, []],
      [403, { error: 'admins only' }, []],
      [503, { error: 'no file tunnel configured' }, []],
    ])
  })
})
