/**
 * Faces Slack and Discord can fetch for an activity post's sender (`icon_url` / `avatar_url`): a profile's or a grant's
 * stored face, else a generated identicon, copied into `public_avatars` keyed by the SHA-256 of its own bytes and
 * served at `/a/<sha256>.<ext>` (`functions/a/[[path]].ts`) with no auth. Only images a post has used are ever stored,
 * and the URL names the bytes alone: no email, grant id or name, no listing.
 *
 * An identicon is seeded by an HMAC of the actor under the deployment's `SESSION_SECRET` (label-separated), never a
 * bare digest of an email someone could guess and hash.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { validateUploadedImage } from '@open-athena/auth'

/** The route's one URL shape: 64 hex digits, optionally an image extension. */
export const AVATAR_PATH = /^\/a\/([0-9a-f]{64})(?:\.(?:png|jpe?g|gif|webp))?$/

const EXT: Record<string, string> = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp' }

const hex = (b: ArrayBuffer | Uint8Array): string => [...new Uint8Array(b)].map(x => x.toString(16).padStart(2, '0')).join('')

export const sha256Hex = async (bytes: Uint8Array): Promise<string> => hex(await crypto.subtle.digest('SHA-256', bytes))

/** A stored face (`data:image/…;base64,…`) as validated raster bytes; null for anything else (an `asset://` ref, a
 *  remote URL, a malformed or non-raster URI). */
export function dataUriImage(uri: string | null | undefined): { type: string; bytes: Uint8Array } | null {
  const m = uri ? /^data:image\/[a-z+.-]+;base64,([A-Za-z0-9+/=]+)$/.exec(uri) : null
  if (!m) return null
  try {
    const bin = atob(m[1])
    const v = validateUploadedImage(Uint8Array.from(bin, c => c.charCodeAt(0)))
    return { type: v.type, bytes: v.bytes }
  } catch {
    return null
  }
}

/** Store `bytes` (once) and return its public URL under `origin`. */
export async function publishAvatar(db: D1Database, origin: string, img: { type: string; bytes: Uint8Array }, nowS: number): Promise<string> {
  const sha = await sha256Hex(img.bytes)
  let bin = ''
  for (const b of img.bytes) bin += String.fromCharCode(b)
  await db.prepare('INSERT OR IGNORE INTO public_avatars (sha256, type, data, created_at) VALUES (?, ?, ?, ?)')
    .bind(sha, img.type, btoa(bin), nowS).run()
  return `${origin}/a/${sha}.${EXT[img.type] ?? 'png'}`
}

/** A stored avatar by its path (`AVATAR_PATH`), or null. */
export async function readAvatar(db: D1Database, pathname: string): Promise<{ type: string; bytes: Uint8Array } | null> {
  const m = AVATAR_PATH.exec(pathname)
  if (!m) return null
  const row = await db.prepare('SELECT type, data FROM public_avatars WHERE sha256 = ?').bind(m[1]).first<{ type: string; data: string }>()
  if (!row) return null
  const bin = atob(row.data)
  return { type: row.type, bytes: Uint8Array.from(bin, c => c.charCodeAt(0)) }
}

/** The identicon seed for `actor`: HMAC-SHA256 under `secret`, label-separated from the secret's other uses. */
export async function identiconSeed(secret: string, actor: string): Promise<Uint8Array> {
  const enc = new TextEncoder()
  const key = await crypto.subtle.importKey('raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
  return new Uint8Array(await crypto.subtle.sign('HMAC', key, enc.encode(`disky-identicon\0${actor}`)))
}

/** HSL (h in degrees, s and l in [0, 1]) to 8-bit RGB. */
function hsl(h: number, s: number, l: number): [number, number, number] {
  const k = (n: number) => (n + h / 30) % 12
  const a = s * Math.min(l, 1 - l)
  const f = (n: number) => l - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)))
  return [f(0), f(8), f(4)].map(x => Math.round(x * 255)) as [number, number, number]
}

/** A 5×5 left-right mirrored identicon from `seed` (≥ 4 bytes): a hue from its first two bytes, the 15 cells of the
 *  left three columns from the next two, on a light ground. `px` square, a PNG. */
export function identiconPng(seed: Uint8Array, px = 120): Uint8Array {
  const fg = hsl(((seed[0] << 8) | seed[1]) % 360, 0.6, 0.48)
  const bg: [number, number, number] = [240, 240, 240]
  const bits = (seed[2] << 8) | seed[3]
  const on = (col: number, row: number): boolean => {
    const c = col < 3 ? col : 4 - col
    return ((bits >> (c * 5 + row)) & 1) === 1
  }
  const margin = Math.round(px / 12)
  const cell = (px - 2 * margin) / 5
  const raw = new Uint8Array(px * (1 + px * 3))
  for (let y = 0; y < px; y++) {
    const o = y * (1 + px * 3)
    raw[o] = 0
    for (let x = 0; x < px; x++) {
      const cx = Math.floor((x - margin) / cell), cy = Math.floor((y - margin) / cell)
      const inside = x >= margin && y >= margin && cx < 5 && cy < 5
      const c = inside && on(cx, cy) ? fg : bg
      raw.set(c, o + 1 + x * 3)
    }
  }
  return png(px, px, raw)
}

const CRC = (() => {
  const t = new Uint32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    t[n] = c >>> 0
  }
  return t
})()

function crc32(parts: Uint8Array[]): number {
  let c = 0xffffffff
  for (const p of parts) for (const b of p) c = CRC[(c ^ b) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

const u32 = (n: number): Uint8Array => new Uint8Array([n >>> 24, (n >>> 16) & 255, (n >>> 8) & 255, n & 255])

function chunk(type: string, data: Uint8Array): Uint8Array[] {
  const t = new TextEncoder().encode(type)
  return [u32(data.length), t, data, u32(crc32([t, data]))]
}

/** zlib with stored (uncompressed) deflate blocks: deterministic, no codec needed in a Worker. */
function zlibStored(data: Uint8Array): Uint8Array {
  const out: number[] = [0x78, 0x01]
  for (let i = 0; i < data.length || i === 0; i += 65535) {
    const len = Math.min(65535, data.length - i)
    const last = i + len >= data.length ? 1 : 0
    out.push(last, len & 255, len >> 8, ~len & 255, (~len >> 8) & 255)
    for (let j = 0; j < len; j++) out.push(data[i + j])
    if (last) break
  }
  let a = 1, b = 0
  for (const x of data) { a = (a + x) % 65521; b = (b + a) % 65521 }
  out.push(...u32(((b << 16) | a) >>> 0))
  return new Uint8Array(out)
}

/** An 8-bit RGB PNG from filter-prefixed scanlines. */
function png(w: number, h: number, raw: Uint8Array): Uint8Array {
  const ihdr = new Uint8Array([...u32(w), ...u32(h), 8, 2, 0, 0, 0])
  const parts = [new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]), ...chunk('IHDR', ihdr), ...chunk('IDAT', zlibStored(raw)), ...chunk('IEND', new Uint8Array())]
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0))
  let o = 0
  for (const p of parts) { out.set(p, o); o += p.length }
  return out
}
