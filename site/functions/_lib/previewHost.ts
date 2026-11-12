/** Whether `request` came to a preview host: one DNS label under the project's `pages.dev` subdomain (`PAGES_SUBDOMAIN`,
 *  from config, never the request), as a PR's `pr-N` branch, the `dev` alias or a deployment hash. A custom domain
 *  (prod, `dev.<host>`) never is, nor the subdomain itself, nor anything deeper or longer. Such a host serves code no
 *  one reviewed for production: it signs in only through the broker's preview client, never silently, and its response
 *  cache is its own (`edgeCache.ts` `cacheEnvTag`). Dependency-free, so the cache can ask it without the auth module. */
export function isPreviewHost(env: { PAGES_SUBDOMAIN?: string }, request: Request): boolean {
  const sub = env.PAGES_SUBDOMAIN?.toLowerCase()
  if (!sub || !/^[a-z0-9-]+\.pages\.dev$/.test(sub)) return false
  const host = new URL(request.url).hostname
  return host.endsWith(`.${sub}`) && /^[a-z0-9-]+$/.test(host.slice(0, -sub.length - 1))
}
