/** Pure helpers for the Staged page (spec `specs/done/staged-page-ux.md` §1):
 *  how a plan's paths are shown on a phone-width table. */

/** The directory every URI in the plan sits under (`…/Downloads/`), so rows can
 *  show what differs. Whole path segments only, and never a whole item: with
 *  `a/b` and `a/b/c` staged together the shared dir is `a/`. `''` when nothing
 *  is shared. */
export function commonDir(uris: string[]): string {
  if (uris.length === 0) return ''
  let prefix = uris[0]
  for (const u of uris.slice(1)) {
    let i = 0
    while (i < prefix.length && i < u.length && prefix[i] === u[i]) i++
    prefix = prefix.slice(0, i)
  }
  // back off to a segment boundary; if that swallows an item, back off once more
  let cut = prefix.lastIndexOf('/')
  if (cut >= 0 && uris.some(u => u.length <= cut + 1)) cut = prefix.lastIndexOf('/', cut - 1)
  return cut < 0 ? '' : prefix.slice(0, cut + 1)
}

/** Keep the head and the extension-bearing tail of a long name; the full text
 *  lives in the tooltip. */
export function elideMiddle(s: string, max = 48, tail = 16): string {
  if (s.length <= max) return s
  return s.slice(0, max - tail - 1) + '…' + s.slice(-tail)
}
