/** Per-path Open Graph metadata for the SPA shell (dynamic link cards).
 *
 *  Crawlers (Slackbot / Twitterbot / Discordbot / facebookexternalhit) don't
 *  run JS, so every SPA route would otherwise unfurl with the one static
 *  `index.html` card. `_middleware.ts` rewrites the og/twitter tags per request
 *  path using these pure helpers; the `/og/<key>` endpoint serves the matching
 *  treemap image (pre-rendered in R2, tier A; edge-rendered, tier B). */

/** URI schemes rendered as `<scheme>://…` (mirror of `src/schemes.ts`). */
export const URI_SCHEMES = ['s3', 'gcs', 'r2', 'ssh'] as const
export type Scheme = (typeof URI_SCHEMES)[number]

export interface OgRoute {
  /** Canonical scan uri, e.g. `r2://ctbk/avail-v3` or `/local/path`. */
  uri: string
  /** Path key the `/og/<key>` endpoint parses back to `uri`
   *  (`r2/ctbk/avail-v3`, `file/Users/ryan`). No leading/trailing slash. */
  key: string
  /** `og:title` / `<title>` for the card. */
  title: string
}

const trimSlashes = (s: string): string => s.replace(/^\/+/, '').replace(/\/+$/, '')

/** Map a SPA `location.pathname` to its scan uri + og-image key, or `null` for
 *  non-scan routes (`/`, `/access`, `/staged`, `/s3` bucket list, `/recent`,
 *  `/compare/*`) which keep the site-default card. `pathname` must be decoded
 *  and query-free. Bucket roots (`/r2/ctbk`) and drilled paths both match. */
export function ogRoute(pathname: string): OgRoute | null {
  const p = pathname.replace(/\/+$/, '')
  for (const scheme of URI_SCHEMES) {
    if (p.startsWith(`/${scheme}/`)) {
      const rest = trimSlashes(p.slice(scheme.length + 1))
      if (!rest) return null
      const uri = `${scheme}://${rest}`
      return { uri, key: `${scheme}/${rest}`, title: `disk-tree — ${uri}` }
    }
  }
  if (p.startsWith('/file/')) {
    const rest = trimSlashes(p.slice('/file'.length))
    if (!rest) return null
    const uri = `/${rest}`
    return { uri, key: `file/${rest}`, title: `disk-tree — ${uri}` }
  }
  return null
}

/** Inverse of `ogRoute().key`: the `/og/<key>` splat segments back to a uri.
 *  `['r2','ctbk','avail-v3']` → `r2://ctbk/avail-v3`; `['file','x','y']` →
 *  `/x/y`. Returns `null` for an unrecognized leading segment. */
export function keyToUri(segments: string[]): string | null {
  if (segments.length < 2) return null
  const [head, ...rest] = segments
  const tail = rest.join('/')
  if ((URI_SCHEMES as readonly string[]).includes(head)) return `${head}://${tail}`
  if (head === 'file') return `/${tail}`
  return null
}
