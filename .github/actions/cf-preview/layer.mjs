// The trusted layer of a `ui` preview (specs/cf-pr-previews.md § Modes): the PR's built static assets, plus this
// file from the base branch, which forwards only safe reads to the deployment's production origin. It has no
// imports: `uiWorker` inlines it into the preview's `_worker.js` with the publish job's configuration.

export const REVISION_PATH = '/__preview/revision'
export const TOKEN_VAR = 'CF_PREVIEW_READ_TOKEN'
export const DEFAULT_FORWARD = ['/api/', '/data/', '/v1/files/']
export const DEFAULT_DENY = ['/api/auth/', '/api/token']
export const DEFAULT_ALLOW = ['/api/auth/whoami']

const READ = new Set(['GET', 'HEAD'])
const FORWARD_HEADERS = ['accept', 'accept-language', 'if-modified-since', 'if-none-match', 'range']
const ENCODED_SEPARATOR = /%(2f|5c|2e)/i
const KEYS_TTL_MS = 5 * 60 * 1000

/** A rule ending in `/` is a prefix; any other rule is that path or a path below it. */
export const pathMatches = (rule, path) => rule.endsWith('/') ? path.startsWith(rule) : path === rule || path.startsWith(`${rule}/`)

/** What one request may do, from its method and URL alone: `forward` to production, `asset` from the PR's build, or `refuse`. */
export function classify(config, method, url) {
  if (!READ.has(method)) return { kind: 'refuse', status: 405, reason: 'Read-only preview: only GET and HEAD' }
  const path = url.pathname
  if (!config.forward.some(rule => pathMatches(rule, path))) return { kind: 'asset' }
  if (config.deny.some(rule => pathMatches(rule, path)) && !config.allow.includes(path)) {
    return { kind: 'refuse', status: 403, reason: 'Not forwarded by the read-only preview' }
  }
  if (ENCODED_SEPARATOR.test(path)) return { kind: 'refuse', status: 400, reason: 'Encoded path separators are refused' }
  if (url.searchParams.has('key')) return { kind: 'refuse', status: 400, reason: 'Credentials in the URL are refused' }
  return { kind: 'forward' }
}

export const allowedViewer = (email, viewers) => {
  const address = String(email ?? '').toLowerCase()
  const at = address.lastIndexOf('@')
  return at > 0 && ((viewers.emails ?? []).includes(address) || (viewers.domains ?? []).includes(address.slice(at + 1)))
}

const bytes = text => new TextEncoder().encode(text)
const fromB64u = text => Uint8Array.from(atob(text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - text.length % 4) % 4)), c => c.charCodeAt(0))
const json64 = text => JSON.parse(new TextDecoder().decode(fromB64u(text)))

/** A Cloudflare Access assertion (RS256 JWT) checked against the team's keys, the application's AUD tag and the trusted viewer list. */
export async function verifyAccess(assertion, viewers, keys, nowMs) {
  const parts = typeof assertion === 'string' ? assertion.split('.') : []
  if (parts.length !== 3) return { ok: false, reason: 'Cloudflare Access sign-in required' }
  let header, payload
  try {
    header = json64(parts[0])
    payload = json64(parts[1])
  } catch {
    return { ok: false, reason: 'Malformed Access assertion' }
  }
  if (header.alg !== 'RS256') return { ok: false, reason: 'Unexpected Access assertion algorithm' }
  const jwk = keys.find(key => key.kid === header.kid)
  if (!jwk) return { ok: false, reason: 'Unknown Access signing key' }
  const key = await crypto.subtle.importKey('jwk', { kty: jwk.kty, n: jwk.n, e: jwk.e, alg: 'RS256', ext: true },
    { name: 'RSASSA-PKCS1-v1_5', hash: 'SHA-256' }, false, ['verify'])
  if (!await crypto.subtle.verify('RSASSA-PKCS1-v1_5', key, fromB64u(parts[2]), bytes(`${parts[0]}.${parts[1]}`))) {
    return { ok: false, reason: 'Bad Access assertion signature' }
  }
  const now = nowMs / 1000
  if (payload.iss !== `https://${viewers.access.team_domain}`) return { ok: false, reason: 'Access assertion from another team' }
  if (![payload.aud].flat().includes(viewers.access.aud)) return { ok: false, reason: 'Access assertion for another application' }
  if (typeof payload.exp !== 'number' || payload.exp <= now || (payload.nbf !== undefined && payload.nbf > now)) {
    return { ok: false, reason: 'Expired Access assertion' }
  }
  if (!allowedViewer(payload.email, viewers)) return { ok: false, reason: 'Not a preview viewer' }
  return { ok: true, email: payload.email.toLowerCase() }
}

const keyCache = new Map()
async function accessKeys(teamDomain, fetcher, nowMs) {
  const held = keyCache.get(teamDomain)
  if (held && nowMs - held.at < KEYS_TTL_MS) return held.keys
  const response = await fetcher(`https://${teamDomain}/cdn-cgi/access/certs`, { redirect: 'error' })
  if (!response.ok) throw new Error(`Access keys: HTTP ${response.status}`)
  const { keys } = await response.json()
  keyCache.set(teamDomain, { at: nowMs, keys })
  return keys
}

const plain = (status, text, extra = {}) => new Response(`${text}\n`, {
  status, headers: { 'content-type': 'text/plain; charset=utf-8', 'cache-control': 'no-store', ...extra },
})

function stamped(response, config) {
  const out = new Response(response.body, response)
  out.headers.set('x-robots-tag', 'noindex, nofollow')
  out.headers.set('x-cf-preview-revision', config.sha)
  return out
}

/** Every binding other than the static assets is refused at run time, whatever the project's settings say. */
export function unexpectedBindings(env) {
  return Object.entries(env).filter(([name, value]) => name !== 'ASSETS' && typeof value !== 'string').map(([name]) => name).sort()
}

async function forward(request, url, config, env, fetcher) {
  const upstream = new URL(`${url.pathname}${url.search}`, config.upstream)
  const headers = new Headers()
  for (const name of FORWARD_HEADERS) if (request.headers.has(name)) headers.set(name, request.headers.get(name))
  if (config.viewers !== 'public') headers.set('authorization', `Bearer ${env[TOKEN_VAR]}`)
  const response = await fetcher(upstream.href, { method: request.method, headers, redirect: 'manual' })
  const out = new Headers(response.headers)
  out.delete('set-cookie')
  out.set('cache-control', 'private, no-store')
  const location = out.get('location')
  if (location) {
    const target = new URL(location, upstream)
    if (target.origin === upstream.origin) out.set('location', `${target.pathname}${target.search}${target.hash}`)
  }
  return new Response(request.method === 'HEAD' ? null : response.body, { status: response.status, statusText: response.statusText, headers: out })
}

/** One request to the preview host. `deps` are the network and clock; the generated worker passes the runtime's. */
export async function handle(request, env, config, { fetcher = fetch, now = Date.now } = {}) {
  const extra = unexpectedBindings(env)
  if (extra.length) return stamped(plain(503, `Preview refuses bindings: ${extra.join(', ')}`), config)
  if (config.viewers !== 'public' && !env[TOKEN_VAR]) return stamped(plain(503, `Preview read grant ${TOKEN_VAR} is not set`), config)
  const url = new URL(request.url)
  if (url.hostname !== config.hostname) return stamped(plain(421, 'Preview serves only its own hostname'), config)
  if (url.pathname === REVISION_PATH) {
    return stamped(READ.has(request.method) ? plain(200, config.sha) : plain(405, 'Read-only preview: only GET and HEAD', { allow: 'GET, HEAD' }), config)
  }
  if (config.viewers !== 'public') {
    const keys = await accessKeys(config.viewers.access.team_domain, fetcher, now())
    const viewer = await verifyAccess(request.headers.get('cf-access-jwt-assertion'), config.viewers, keys, now())
    if (!viewer.ok) return stamped(plain(403, viewer.reason), config)
  }
  const decision = classify(config, request.method, url)
  if (decision.kind === 'refuse') {
    return stamped(plain(decision.status, decision.reason, decision.status === 405 ? { allow: 'GET, HEAD' } : {}), config)
  }
  if (decision.kind === 'asset') return stamped(await env.ASSETS.fetch(request), config)
  return stamped(await forward(request, url, config, env, fetcher), config)
}
