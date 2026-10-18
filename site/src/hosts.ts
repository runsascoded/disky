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

/** A sibling deployment: another host serving the same site over other buckets (its own scans, users and grants),
 * and which of the optional pages it serves (its `Store.owners` / `Store.staging`, a `/meta` tree). */
export interface Peer { label: string; prod: string; dev?: string; owners?: boolean; staging?: boolean; meta?: boolean }

/** Routes whose path means the same thing on every deployment that serves them (`needs`); any other path is a drill
 * into this one's buckets. */
const PAGES = new Set(['admin', 'assignments', 'health', 'names', 'privacy', 'runs', 'scans', 'signin', 'staged', 'user', 'users'])
/** The pages a peer serves only with that feature; without it they go to its root. */
const needs = (peer: Peer, root: string): boolean =>
  ['assignments', 'user', 'users'].includes(root) ? !!peer.owners
    : ['runs', 'staged'].includes(root) ? !!peer.staging
    : true
/** Query params naming this deployment's own things: a scan (`d`; the legacy `date`/`from`), an owner (`o`), an
 * assigner (`by`), an object (`open`), a bucket, a user page's drill (`p`), a staged run (`run`), a sign-in's
 * return path (`next`). */
const LOCAL_PARAMS = ['bucket', 'by', 'd', 'date', 'from', 'next', 'o', 'open', 'p', 'run']

/** The page a peer serves for this one, on the peer's dev host from this deployment's dev host, else its prod.
 * A page keeps its route up to the first id of this deployment's (a scan run, a deletion run, a session), and its
 * hash; the meta tree keeps only its root; a drill path becomes the peer's root, since no two deployments share a
 * bucket. Each keeps its query, less `LOCAL_PARAMS`: the axes, units and filters mean the same everywhere. A page the
 * peer doesn't serve goes to its root bare (its query was that page's). */
export function peerUrl(href: string, peer: Peer, fromDev: boolean): string {
  const from = new URL(href)
  const url = new URL(`https://${fromDev && peer.dev ? peer.dev : peer.prod}/`)
  const segs = from.pathname.split('/').filter(Boolean)
  const [root, sub] = segs
  if (root === 'meta') {
    if (peer.meta) url.pathname = '/meta'
  } else if (root && PAGES.has(root)) {
    if (!needs(peer, root)) return url.toString()
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
