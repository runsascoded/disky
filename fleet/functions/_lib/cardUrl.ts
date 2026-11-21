/** The full fleet card's URL: signed (`@disk-tree/pages-kit/og/sign`) under `SESSION_SECRET`, good for
 *  `IMAGE_TTL_DAYS`. `v` names the read it was minted for, so a client that caches by URL (Slack) fetches anew; `view`
 *  (and `d`) pick one of the replies' images (`replies.ts` `viewSvg`). */
import { canonical, expDay, IMAGE_TTL_DAYS, imagePath, ogKey } from '@disk-tree/pages-kit/og/sign'
import type { Env } from './env.js'

export async function cardUrl(secret: string, origin: string, now: number, params: { v: string; theme?: 'light'; view?: string; d?: string }): Promise<string> {
  return origin + await imagePath(await ogKey(secret), 'fleet', params, 'full', expDay(now, IMAGE_TTL_DAYS))
}

/** A reply image's URL: signed when `SESSION_SECRET` is set; unsigned when the card is public (`FLEET_CARD_PUBLIC`, which
 *  draws the full image for any request); else null, since an unsigned URL would draw the anonymous frame. */
export async function imageUrl(env: Env, origin: string, now: number, params: { v: string; view?: string; d?: string }): Promise<string | null> {
  if (env.SESSION_SECRET) return cardUrl(env.SESSION_SECRET, origin, now, params)
  if (env.FLEET_CARD_PUBLIC === '1') return `${origin}/og/fleet.png?${canonical(params)}`
  return null
}
