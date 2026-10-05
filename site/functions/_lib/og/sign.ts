/** Card URLs and view tokens (specs/done/dogi.md).
 *
 * - **Anonymous card URL**: `/og/<kind>.png?<view>`, unsigned. Signing it
 *   would protect nothing: any page fetch yields any view's anonymous card.
 * - **Full card URL**: `/og/<kind>.png?<view>&sig=<ee><tag>` (12 base62
 *   chars). Images are fetched unauthenticated, so this one is signed: the tag
 *   is HMAC-SHA256 over kind + canonical view + expiry under a key derived
 *   from `SESSION_SECRET`, as 10 base62 digits (~59.5 bits; the key never
 *   leaves the server, so only online guessing applies). A missing, bad or
 *   expired `sig` serves the anonymous card, never an error.
 * - **View token** (`og=`): a random 10-char base62 token (`randomToken`), no
 *   MAC. The `og_tokens` row is the whole truth (`tokens.ts`): valid iff a row
 *   with that token exists for this exact view, unexpired and unrevoked.
 *
 * Base62 throughout: no `+` `/` `-` `_`, which Slack and linkifiers mangle.
 * Expiries are whole days since `EPOCH`; in a `sig` they pack into 2 base62
 * chars (3,844 days ≈ 10.5 years).
 *
 * Pure WebCrypto: runs in the Worker and in Node tests. */

export type OgTier = 'anon' | 'full'

/** Day 0 of expiries: 2026-01-01T00:00Z. */
export const EPOCH = 1_767_225_600
export const DAY_S = 86400
/** How long a stamped full image URL stays valid (a week of Slack scrollback). */
export const IMAGE_TTL_DAYS = 7

export const B62 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'

/** A day count as 2 base62 chars (0 ≤ days < 3844). */
export function packDays(days: number): string {
  if (!Number.isInteger(days) || days < 0 || days >= 62 * 62) throw new Error(`expiry day out of range: ${days}`)
  return B62[Math.floor(days / 62)] + B62[days % 62]
}

export function unpackDays(s: string): number | null {
  if (s.length !== 2) return null
  const hi = B62.indexOf(s[0])
  const lo = B62.indexOf(s[1])
  return hi < 0 || lo < 0 ? null : hi * 62 + lo
}

/** `n` uniformly random base62 chars: random bytes, rejecting those ≥ 248
 * (= 4·62) so `% 62` carries no modulo bias. `rand` is injectable for tests. */
export function randomToken(n = 10, rand: (b: Uint8Array) => Uint8Array = b => crypto.getRandomValues(b)): string {
  let out = ''
  while (out.length < n) {
    for (const x of rand(new Uint8Array(n * 2))) {
      if (x >= 248) continue
      out += B62[x % 62]
      if (out.length === n) break
    }
  }
  return out
}

/** The day index (since `EPOCH`) whose end a credential made at `now` (unix s)
 * expires at, `ttlDays` whole days later. Valid through the end of that day. */
export const expDay = (now: number, ttlDays: number): number => Math.floor((now - EPOCH) / DAY_S) + ttlDays

/** Expired once `now` passes the end of `day`. */
export const expired = (day: number, now: number): boolean => now >= EPOCH + (day + 1) * DAY_S

/** The view's params, canonical: keys sorted, empty values dropped,
 * URL-encoded as `URLSearchParams` would. Two URLs for the same view
 * canonicalize equal however their params were ordered. */
export function canonical(params: Record<string, string | null | undefined>): string {
  const sp = new URLSearchParams()
  for (const k of Object.keys(params).sort()) {
    const v = params[k]
    if (v != null && v !== '') sp.set(k, v)
  }
  return sp.toString()
}

const enc = new TextEncoder()

async function hmac(key: CryptoKey, msg: string): Promise<Uint8Array> {
  return new Uint8Array(await crypto.subtle.sign('HMAC', key, enc.encode(msg)))
}

/** A mac's first 64 bits as 10 base62 digits, most significant first (the
 * value mod 62¹⁰: ~59.5 bits). */
export const TAG_CHARS = 10
const tag62 = (mac: Uint8Array): string => {
  let v = 0n
  for (const x of mac.slice(0, 8)) v = (v << 8n) | BigInt(x)
  let out = ''
  for (let i = 0; i < TAG_CHARS; i++) { out = B62[Number(v % 62n)] + out; v /= 62n }
  return out
}

/** The OG key: HMAC(`SESSION_SECRET`, `og-card:v1`), imported for HMAC.
 * Rotating the session secret rotates it (and voids every full image URL). */
export async function ogKey(secret: string): Promise<CryptoKey> {
  const base = await crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
  const derived = await hmac(base, 'og-card:v1')
  return crypto.subtle.importKey('raw', derived, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
}

/** Constant-time equality of two short ASCII strings. */
function same(a: string, b: string): boolean {
  if (a.length !== b.length) return false
  let d = 0
  for (let i = 0; i < a.length; i++) d |= a.charCodeAt(i) ^ b.charCodeAt(i)
  return d === 0
}

async function imageTag(key: CryptoKey, kind: string, view: string, day: number): Promise<string> {
  return tag62(await hmac(key, `img\n${kind}\n${view}\nfull\n${day}`))
}

/** A card's path (the caller adds the origin): anonymous = the bare view;
 * full = the view plus a `sig` good through `day`. */
export async function imagePath(key: CryptoKey | null, kind: string, params: Record<string, string | null | undefined>, tier: OgTier, day?: number): Promise<string> {
  const view = canonical(params)
  const base = `/og/${kind}.png${view ? `?${view}` : ''}`
  if (tier === 'anon') return base
  if (!key || day == null) throw new Error('a full card URL needs the key and an expiry day')
  return `${base}${view ? '&' : '?'}sig=${packDays(day)}${await imageTag(key, kind, view, day)}`
}

export type Resolved = { kind: string; params: Record<string, string>; tier: OgTier; day?: number; why?: string }

/** Resolve a card request: its kind and view, and `full` only for a `sig`
 * that matches the kind + canonical view + expiry and hasn't expired; any
 * other `sig` (or none) is the anonymous card, with `why` for the log. Null
 * only for a non-card path. */
export async function resolveImage(key: CryptoKey | null, url: URL, now: number): Promise<Resolved | null> {
  const m = /^\/og\/([a-z]+)\.png$/.exec(url.pathname)
  if (!m) return null
  const sp = new URLSearchParams(url.search)
  const sig = sp.get('sig')
  sp.delete('sig')
  const params: Record<string, string> = {}
  for (const [k, v] of sp) params[k] = v
  const anon = (why?: string): Resolved => ({ kind: m[1], params, tier: 'anon', ...(why ? { why } : {}) })
  if (sig == null) return anon()
  const day = unpackDays(sig.slice(0, 2))
  if (!key || day == null || sig.length !== 2 + TAG_CHARS) return anon('bad sig')
  if (expired(day, now)) return anon('expired')
  if (!same(sig.slice(2), await imageTag(key, m[1], canonical(params), day))) return anon('bad signature')
  return { kind: m[1], params, tier: 'full', day }
}
