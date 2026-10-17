// The deployment's prod ↔ dev host pair (`PROD_HOST` / `DEV_HOST` in the
// branch's wrangler.toml `[vars]`, read at build time by vite.config.ts). The
// dev host is the Pages preview-branch alias (`infra/cf`'s `BranchAlias`).

export interface HostPair { prod: string; dev: string }

/** Both set → the pair; either unset → none (a deployment with no dev alias). */
export function hostPair(prod: string | undefined, dev: string | undefined): HostPair | null {
  return prod && dev ? { prod, dev } : null
}

/** The same page on the other host: from the dev host to prod, from anywhere
 * else (prod, localhost) to dev. Path, query and hash carry over. */
export function otherHostUrl(href: string, pair: HostPair): string {
  const url = new URL(href)
  const toProd = url.hostname === pair.dev
  url.hostname = toProd ? pair.prod : pair.dev
  url.protocol = 'https:'
  url.port = ''
  return url.toString()
}

/** A sibling deployment: another host serving the same site over other buckets (its own scans, users and grants). */
export interface Peer { label: string; prod: string; dev?: string }

/** Routes whose path means the same thing on every deployment; any other path is a drill into this one's buckets. */
const PAGES = new Set(['admin', 'assignments', 'health', 'names', 'privacy', 'runs', 'scans', 'signin', 'staged', 'user', 'users'])
/** Query params naming this deployment's own things: a scan (`d`), an owner (`o`), an object (`open`), a bucket. */
const LOCAL_PARAMS = ['bucket', 'd', 'o', 'open']

/** The page a peer serves for this one, on the peer's dev host from this deployment's dev host, else its prod.
 * A page keeps its route up to the first id of this deployment's (a scan run, a deletion run, a session), and its
 * hash; a drill path becomes the peer's root, since no two deployments share a bucket. Either keeps its query,
 * less `LOCAL_PARAMS`: the axes, units and filters mean the same everywhere. */
export function peerUrl(href: string, peer: Peer, fromDev: boolean): string {
  const from = new URL(href)
  const url = new URL(`https://${fromDev && peer.dev ? peer.dev : peer.prod}/`)
  const segs = from.pathname.split('/').filter(Boolean)
  const page = segs.length > 0 && PAGES.has(segs[0])
  if (page) {
    const [root, sub] = segs
    url.pathname = root === 'runs' ? '/staged'
      : root === 'user' || (root === 'admin' && sub === 'db') ? `/${segs.join('/')}`
      : root === 'admin' && sub ? `/admin/${sub}`
      : `/${root}`
    url.hash = from.hash
  }
  // Filtered raw, not through `searchParams`, which would re-encode the rest (`,` → `%2C`, `%20` → `+`).
  url.search = from.search.slice(1).split('&').filter(kv => kv && !LOCAL_PARAMS.includes(decodeURIComponent(kv.split('=')[0]))).join('&')
  return url.toString()
}
