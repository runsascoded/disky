/** What a full card image is backed by (specs/done/dogi.md): the `og=` token
 * (`t`) or the share-link grant (`g`) its page was stamped from, carried in
 * the image URL under the `sig`. The image re-checks it on every fetch, so
 * revoking either reverts every later fetch to the anonymous card. Pure. */

export type Cred = { t: string } | { g: string }

/** Image params → the card's view (no `t` / `g` / `v`), the `/staged` plan
 * version `v`, and the credential (null: none, so the card is anonymous). */
export function splitImageParams(params: Record<string, string>): { view: Record<string, string>; v?: string; cred: Cred | null } {
  const { t, g, v, ...view } = params
  const cred: Cred | null = t ? { t } : g ? { g } : null
  return { view, ...(v ? { v } : {}), cred }
}

/** The image params for a page view: its view, `/staged`'s version, and the
 * credential when the card is full. */
export const imageParams = (view: Record<string, string>, v: string | null, cred: Cred | null): Record<string, string> =>
  ({ ...view, ...(v ? { v } : {}), ...(cred ?? {}) })
