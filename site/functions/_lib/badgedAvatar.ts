// the `.wasm` import's type, also where the page's typecheck reaches this file (`activity.ts`, through `auth.ts`)
/// <reference path="./og/wasm.d.ts" />
/**
 * A post's sender face when the bot posts as a person: the person's photo with a corner fold in the color of what they
 * did (a staged deletion's staged, dispatched, finished, …; `activity` for an activity burst) and its glyph. Rendered by resvg to a PNG and published as a public avatar
 * (`publishAvatar`: `/a/<sha256>.png`), so Slack can fetch it as `icon_url`. Anything that fails (no face, a face
 * that won't decode, no `public_avatars` table, an http origin Slack can't reach) leaves the plain face: a post is
 * never held up by its avatar.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { validateUploadedImage } from '@open-athena/auth'
import RESVG from './og/vendor/resvg.wasm'
import { ensureWasm, svgToPng } from './og/render.js'
import { identiconPng, identiconSeed, publishAvatar } from './publicAvatar.js'

export type Badge = 'staged' | 'dry' | 'dispatched' | 'finished' | 'failed' | 'undone' | 'activity'

/** Each badge's fold color and glyph: a 24-unit path, white, stroked unless `fill`. */
export const BADGES: Record<Badge, { color: string; glyph: string; fill?: boolean }> = {
  staged: { color: '#d9922b', glyph: 'M5 7h14M9.5 7V5h5v2M7 7l1 13h8l1-13M10.5 11v6M13.5 11v6' },
  dry: { color: '#6b7a90', glyph: 'M9 3h6M10 3v6.5L5.2 18a2 2 0 0 0 1.8 3h10a2 2 0 0 0 1.8-3L14 9.5V3M7.5 15h9' },
  dispatched: { color: '#ec6a1f', glyph: 'M13.5 2.5L5.5 13.5h6l-1 8 8-11h-6z', fill: true },
  finished: { color: '#2e9e62', glyph: 'M5 12.5l4.5 4.5L19 7' },
  failed: { color: '#d93844', glyph: 'M7 7l10 10M17 7L7 17' },
  undone: { color: '#8a63e0', glyph: 'M9 14L4 9l5-5M4 9h10a6 6 0 0 1 0 12h-3' },
  // an activity burst: what they looked at (an eye), on a neutral slate fold
  activity: { color: '#475569', glyph: 'M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12zM12 9.25a2.75 2.75 0 1 0 0 5.5 2.75 2.75 0 1 0 0-5.5z' },
}

/** The badge a sender's verb carries; none for a verb without one. */
export function badgeOf(verb: string, mode?: string | null): Badge | null {
  switch (verb) {
    case 'staged': case 'added': return 'staged'
    case 'dispatched': return mode && mode !== 'real' ? 'dry' : 'dispatched'
    case 'finished': return 'finished'
    case 'failed': return 'failed'
    case 'unstaged': case 'rejected': case 'undone': return 'undone'
    default: return null
  }
}

export type BadgeStyle = 'fold' | 'ring'

/** The badged face as SVG: `face` (a raster data URI) on a 100-unit square. `fold`: a folded-down lower-right corner
 *  in the badge's color, its glyph on it. `ring`: the face framed in the badge's color, the glyph on a chip in the
 *  corner. Self-contained (no fonts, no external refs), so any SVG rasterizer draws it: resvg here, `sharp` in Node. */
export function badgedSvg(face: string, badge: Badge, style: BadgeStyle = 'fold', px = 256): string {
  const b = BADGES[badge]
  const glyph = (cx: number, size: number) => `<g transform="translate(${cx - size / 2} ${cx - size / 2}) scale(${size / 24})"><path d="${b.glyph}" ${
    b.fill ? 'fill="#fff" stroke="none"' : 'fill="none" stroke="#fff" stroke-width="2.7" stroke-linecap="round" stroke-linejoin="round"'}/></g>`
  const mark = style === 'fold'
    ? `<path d="M100 44V100H44z" fill="${b.color}"/><path d="M100 44L44 100" stroke="#000" stroke-opacity="0.18" stroke-width="2"/>${glyph(82, 24)}`
    : `<rect x="3.5" y="3.5" width="93" height="93" rx="19" fill="none" stroke="${b.color}" stroke-width="7"/><circle cx="79" cy="79" r="18" fill="${b.color}"/>${glyph(79, 20)}`
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${px}" height="${px}" viewBox="0 0 100 100">` +
    `<image href="${face}" width="100" height="100" preserveAspectRatio="xMidYMid slice"/>${mark}</svg>`
}

const b64 = (bytes: Uint8Array): string => {
  let s = ''
  for (const c of bytes) s += String.fromCharCode(c)
  return btoa(s)
}

/** Raster faces resvg decodes. */
const DRAWABLE = new Set(['image/png', 'image/jpeg'])

/** Published URLs by face and badge: one render per isolate (the stored bytes are content-addressed anyway). */
const made = new Map<string, string>()
export const resetBadgedAvatars = (): void => made.clear()

/** The badged face's public URL: over the photo at `image`, else over the actor's identicon (`SESSION_SECRET`).
 *  Null when it can't be made; the caller keeps the plain face. */
export async function badgedAvatarUrl(
  env: { SESSION_SECRET?: string },
  db: D1Database | null,
  origin: string,
  who: { email: string; image: string | null },
  badge: Badge,
): Promise<string | null> {
  if (!db || !origin.startsWith('https://')) return null
  const key = `${who.image ?? `id:${who.email.toLowerCase()}`}|${badge}`
  const hit = made.get(key)
  if (hit) return hit
  try {
    let face: { type: string; bytes: Uint8Array } | null = null
    if (who.image) {
      const r = await fetch(who.image, { signal: AbortSignal.timeout(5000) })
      if (r.ok) {
        const v = validateUploadedImage(new Uint8Array(await r.arrayBuffer()))
        if (DRAWABLE.has(v.type)) face = { type: v.type, bytes: v.bytes }
      }
    } else if (env.SESSION_SECRET) {
      face = { type: 'image/png', bytes: identiconPng(await identiconSeed(env.SESSION_SECRET, who.email.toLowerCase()), 128) }
    }
    if (!face) return null
    await ensureWasm(RESVG)
    const png = svgToPng(badgedSvg(`data:${face.type};base64,${b64(face.bytes)}`, badge), [])
    const url = await publishAvatar(db, origin, { type: 'image/png', bytes: png }, Math.floor(Date.now() / 1000))
    made.set(key, url)
    return url
  } catch (e) {
    console.log(`badged avatar (${badge}) failed: ${(e as Error).message}`)
    return null
  }
}
