/** What a full card image is backed by (specs/done/dogi.md): the `og=` token
 * (`t`) or the share-link grant (`g`) its page was stamped from, carried in
 * the image URL under the `sig`. The image re-checks it on every fetch, so
 * revoking either reverts every later fetch to the anonymous card. Pure. */

export type Cred = { t: string } | { g: string }

/** Split signed render metadata (version, policy, pinned scan) from the
 * original credential view and its explicit share credential. */
export function splitImageParams(params: Record<string, string>): { view: Record<string, string>; v?: string; scan?: string; before?: string; cred: Cred | null } {
  const { t, g, v, s, b, policy: _policy, r: _render, ...view } = params
  const cred: Cred | null = t ? { t } : g ? { g } : null
  return { view, ...(v ? { v } : {}), ...(s ? { scan: s } : {}), ...(b ? { before: b } : {}), cred }
}

/** The image params for a page view: its view, `/staged`'s version, and the
 * credential when the card is full. */
export const imageParams = (view: Record<string, string>, v: string | null, cred: Cred | null): Record<string, string> =>
  ({ ...view, ...(v ? { v } : {}), ...(cred ?? {}) })
