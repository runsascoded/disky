/**
 * `/v1/laptop-files/get?path=<p>` — a file's bytes from the scanned machine itself
 * (specs/laptop-file-tunnel.md): `HttpStore`'s `get` route, proxied to the
 * machine's `dt-serve /files/<p>` through its Cloudflare Tunnel. The tunnel's
 * hostname answers only the service token sent here (an Access app with no
 * login path); `dt-serve` re-verifies Access's JWT and the path root.
 *
 * Every restriction is enforced here, before anything reaches the machine:
 * admins only, never a share-link grant (even an unrestricted one; hide scopes
 * ride only on grants, so no hidden prefix gets this far), never a deny-listed
 * path (`LAPTOP_FILES_DENY`, deployment config), a whole-file GET at most `LAPTOP_FILES_MAX_BYTES`, at most
 * `LAPTOP_FILES_PER_MIN` requests per session, and each answer logged to
 * `access_log` (`event = 'file'`: path, status, bytes). The machine asleep, its
 * app quit or its file server off is a 503 `{ offline: true }`, which the
 * object panel says plainly rather than as an error.
 */
import { type AccessEventKind, requestMeta } from '@open-athena/auth'
import { d1AuditSink } from '@open-athena/auth/d1'
import { type Env, type Identity, json } from './auth.js'

export const LAPTOP_FILES_API = '/v1/laptop-files'
/** The audit rows' `event` (free text; the gate's union doesn't name it). */
export const FILE_EVENT = 'file'
export const DEFAULT_MAX_BYTES = 25 * 1024 * 1024
export const DEFAULT_PER_MIN = 600

/** Whether this deployment's primary store has a file tunnel. */
export const tunnelConfigured = (env: Env): boolean =>
  !!(env.LAPTOP_TUNNEL_URL && env.LAPTOP_TUNNEL_CLIENT_ID && env.LAPTOP_TUNNEL_CLIENT_SECRET)

/** A store-relative file path's segments, or null when it isn't one: empty,
 * absolute, `.`/`..`/empty segments, or a NUL. */
export function fileSegs(path: string | null): string[] | null {
  if (!path || path.startsWith('/') || path.includes('\0')) return null
  const segs = path.split('/')
  return segs.every(s => s && s !== '.' && s !== '..') ? segs : null
}

/** A deny glob as a regex over the whole path: `*` / `?` stay within a
 * segment, `**` crosses them (`**\/x` also matches a top-level `x`, `x/**` x itself). */
export function globRegex(glob: string): RegExp {
  let re = ''
  for (let i = 0; i < glob.length; i++) {
    const c = glob[i]
    if (glob.startsWith('**/', i)) { re += '(?:.*/)?'; i += 2 }
    else if (glob.startsWith('/**', i) && i + 3 === glob.length) { re += '(?:/.*)?'; i += 2 }
    else if (glob.startsWith('**', i)) { re += '.*'; i += 1 }
    else if (c === '*') re += '[^/]*'
    else if (c === '?') re += '[^/]'
    else re += c.replace(/[.+^${}()|[\]\\]/g, '\\$&')
  }
  return new RegExp(`^${re}$`)
}

export const denyGlobs = (env: Env): string[] =>
  (env.LAPTOP_FILES_DENY ?? '').split(',').map(s => s.trim()).filter(Boolean)

/** The deny glob `path` matches (the first), else null. */
export const deniedBy = (path: string, globs: string[]): string | null =>
  globs.find(g => globRegex(g).test(path)) ?? null

const int = (v: string | undefined, d: number): number => {
  const n = v ? Number(v) : NaN
  return Number.isFinite(n) && n > 0 ? n : d
}

const NO_STORE = { 'cache-control': 'private, no-store' }
const refuse = (status: number, error: string, extra: Record<string, unknown> = {}) =>
  json({ error, ...extra }, status, NO_STORE)
/** What the panel renders as "the laptop is offline". */
export const offline = (reason: string): Response => json({ offline: true, reason }, 503, NO_STORE)

/** Upstream statuses that mean the machine isn't reachable: Cloudflare's own
 * 502/503/504, and 530 (error 1033: the tunnel has no connector). */
const OFFLINE_STATUSES = new Set([502, 503, 504, 530])

/** Response headers passed through from `dt-serve`. */
const PASS = ['content-type', 'content-length', 'content-range', 'accept-ranges', 'last-modified', 'etag']

export interface Audit {
  status: number
  bytes?: number
  range?: string
  reason?: string
}

export interface LaptopFilesDeps {
  /** The upstream fetch (the tunnel); tests stub it. */
  fetch: typeof fetch
  /** Writes one audit row; null skips auditing (no D1 / secret). */
  audit: ((a: Audit) => Promise<void>) | null
  /** This session's `file` rows in the last minute (the rate limit's count). */
  recent: () => Promise<number>
}

/** The route's decision for an identified request (`requireAdmin` already
 * passed for a session; this re-checks so a mis-wired caller still refuses). */
export async function serveLaptopFile(request: Request, env: Env, id: Identity, deps: LaptopFilesDeps): Promise<Response> {
  const url = new URL(request.url)
  const out = async (res: Response, a: Omit<Audit, 'status'> = {}) => {
    await deps.audit?.({ status: res.status, ...a })
    return res
  }
  if (request.method !== 'GET' && request.method !== 'HEAD') return refuse(405, 'method')
  if (url.pathname !== `${LAPTOP_FILES_API}/get`) return refuse(404, 'not found')
  if (id.via === 'grant') return out(refuse(403, 'share links can’t open files'), { reason: 'grant' })
  if (id.via !== 'session' || !id.admin) return out(refuse(403, 'admins only'), { reason: 'not admin' })
  if (!tunnelConfigured(env)) return refuse(503, 'no file tunnel configured')
  const segs = fileSegs(url.searchParams.get('path'))
  if (!segs) return out(refuse(400, 'bad path'), { reason: 'bad path' })
  const path = segs.join('/')
  const deny = deniedBy(path, denyGlobs(env))
  if (deny) return out(refuse(403, 'this path is never previewed', { deny }), { reason: `deny ${deny}` })
  if (await deps.recent() >= int(env.LAPTOP_FILES_PER_MIN, DEFAULT_PER_MIN)) return out(refuse(429, 'too many file requests; try again in a minute'), { reason: 'rate' })

  const range = request.headers.get('range')
  const cap = int(env.LAPTOP_FILES_MAX_BYTES, DEFAULT_MAX_BYTES)
  // A whole-file GET asks for its first `cap` bytes: through the tunnel Cloudflare sends whole
  // responses without `content-length`, so the file's size has to come from a 206's `content-range`.
  const whole = !range && request.method === 'GET'
  const upstreamRange = whole ? `bytes=0-${cap - 1}` : range
  const upstream = `${env.LAPTOP_TUNNEL_URL!.replace(/\/+$/, '')}/files/${segs.map(encodeURIComponent).join('/')}`
  // Unbound: the Workers runtime's `fetch` throws "Illegal invocation" when `this` is `deps`.
  const { fetch: upstreamFetch } = deps
  const ask = () => upstreamFetch(upstream, {
    method: request.method,
    headers: {
      'CF-Access-Client-Id': env.LAPTOP_TUNNEL_CLIENT_ID!,
      'CF-Access-Client-Secret': env.LAPTOP_TUNNEL_CLIENT_SECRET!,
      ...(upstreamRange ? { range: upstreamRange } : {}),
    },
    redirect: 'manual',
    // Someone's file never lands in this Worker's subrequest cache.
    cache: 'no-store',
  })
  // Cloudflare's cache drops `Range` on a URL's first miss for a cacheable extension (`.pdf`, `.mp4`)
  // and answers whole; it bypasses from then on. So a ranged ask answered whole is cancelled and asked again.
  const ignoredRange = (r: Response) => !!upstreamRange && r.status === 200 && !r.headers.get('content-range')
  let res: Response
  try {
    res = await ask()
    if (ignoredRange(res)) {
      await res.body?.cancel()
      res = await ask()
    }
  } catch (e) {
    return out(offline('unreachable'), { reason: `fetch: ${(e as Error).message}` })
  }
  if (OFFLINE_STATUSES.has(res.status)) {
    await res.body?.cancel()
    return out(offline(`tunnel ${res.status}`), { reason: `upstream ${res.status}` })
  }
  // dt-serve with its file route off: the machine is up, previews aren't.
  if (res.status === 404 && (await res.clone().text()).startsWith('not served locally')) {
    return out(offline('file serving off'), { reason: 'not served locally' })
  }
  // Access refusing the service token (or a login redirect): a config fault, not the user's.
  if (res.status >= 300 && res.status < 400 || res.status === 401 || res.status === 403) {
    await res.body?.cancel()
    return out(refuse(502, 'the file tunnel refused this site', { upstream: res.status }), { reason: `upstream ${res.status}` })
  }
  if (ignoredRange(res)) {
    await res.body?.cancel()
    return out(refuse(502, 'the file tunnel ignored the byte range'), { reason: 'range ignored' })
  }
  const tooLarge = async (bytes: number | null) => {
    await res.body?.cancel()
    return out(refuse(413, 'too large to open whole; media can still stream', { bytes, cap }), { bytes: 0, reason: 'cap' })
  }
  let status = res.status
  let length = Number(res.headers.get('content-length') ?? NaN)
  let contentRange = res.headers.get('content-range')
  if (whole) {
    const total = Number(contentRange?.match(/\/(\d+)$/)?.[1] ?? NaN)
    if (status === 206) {
      // The first `cap` bytes were the whole file, or the file is too large.
      if (!(total <= cap)) return tooLarge(Number.isFinite(total) ? total : null)
      status = 200
      length = total
      contentRange = null
    } else if (status === 416 && total === 0) {
      await res.body?.cancel()
      res = new Response('', { headers: res.headers })
      status = 200
      length = 0
      contentRange = null
    } else if (status === 200 && !(length <= cap)) {
      return tooLarge(Number.isFinite(length) ? length : null)
    }
  }
  const headers = new Headers(NO_STORE)
  for (const h of PASS) {
    const v = res.headers.get(h)
    if (v != null) headers.set(h, v)
  }
  if (Number.isFinite(length)) headers.set('content-length', String(length))
  if (contentRange) headers.set('content-range', contentRange)
  else headers.delete('content-range')
  headers.set('x-content-type-options', 'nosniff')
  // Bytes from someone's disk are never a page on this origin.
  headers.set('content-security-policy', 'sandbox; default-src \'none\'; img-src \'self\' data:; media-src \'self\'; style-src \'unsafe-inline\'')
  const name = segs[segs.length - 1]
  headers.set('content-disposition', `${url.searchParams.get('inline') ? 'inline' : 'attachment'}; filename*=UTF-8''${encodeURIComponent(name)}`)
  return out(new Response(request.method === 'HEAD' ? null : res.body, { status, headers }), {
    bytes: request.method === 'HEAD' || !Number.isFinite(length) ? 0 : length,
    range: contentRange ?? undefined,
  })
}

/** The D1-backed audit + rate-limit deps for a session (`e:<email>`). */
export function d1Deps(request: Request, env: Env, id: Identity, path: string | null): Pick<LaptopFilesDeps, 'audit' | 'recent'> {
  const db = env.DB
  const sub = id.email ? `e:${id.email}` : null
  const now = () => Math.floor(Date.now() / 1000)
  return {
    audit: db && env.SESSION_SECRET
      ? async a => {
        const meta = await requestMeta(request, env.SESSION_SECRET!)
        await d1AuditSink(db).log({
          ...meta,
          ts: now(),
          event: FILE_EVENT as AccessEventKind,
          sessionSub: sub ?? undefined,
          path: path ?? undefined,
          status: a.status,
          reason: [a.bytes != null ? `bytes=${a.bytes}` : null, a.range ? `range=${a.range}` : null, a.reason ?? null].filter(Boolean).join(' ') || undefined,
        })
      }
      : null,
    recent: async () => {
      if (!db || !sub) return 0
      const row = await db.prepare('SELECT COUNT(*) AS n FROM access_log WHERE event = ? AND session_sub = ? AND ts >= ?')
        .bind(FILE_EVENT, sub, now() - 60).first<{ n: number }>()
      return row?.n ?? 0
    },
  }
}
