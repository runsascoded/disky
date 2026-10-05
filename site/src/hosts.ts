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
