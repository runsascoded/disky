/** The full fleet card's URL: signed (`@disk-tree/pages-kit/og/sign`) under `SESSION_SECRET`, good for
 *  `IMAGE_TTL_DAYS`. `v` names the read it was minted for, so a client that caches by URL (Slack) fetches anew. */
import { expDay, IMAGE_TTL_DAYS, imagePath, ogKey } from '@disk-tree/pages-kit/og/sign'

export async function cardUrl(secret: string, origin: string, now: number, params: { v: string; theme?: 'light' }): Promise<string> {
  return origin + await imagePath(await ogKey(secret), 'fleet', params, 'full', expDay(now, IMAGE_TTL_DAYS))
}
