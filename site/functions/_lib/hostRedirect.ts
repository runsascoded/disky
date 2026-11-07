// A deployment's renamed hosts (`HOST_REDIRECTS = "old=new,…"`, specs/rename-cw.md): a GET / HEAD to an old host is
// a 301 to the same path and query on its new one, so every link already posted keeps working. Other methods pass
// through on the old host: a webhook still pointed at it (Slack's `/slack/actions` POST) must not be bounced.

export interface HostRedirectEnv { HOST_REDIRECTS?: string }

/** `old=new,…` → old host → new host; blanks and malformed entries skipped. */
export function hostRedirects(spec: string | undefined): Map<string, string> {
  const m = new Map<string, string>()
  for (const part of (spec ?? '').split(',')) {
    const [from, to] = part.split('=').map(s => s.trim().toLowerCase())
    if (from && to && from !== to) m.set(from, to)
  }
  return m
}

/** The redirect for this request, or null to serve it. */
export function hostRedirect(request: Request, env: HostRedirectEnv): Response | null {
  if (request.method !== 'GET' && request.method !== 'HEAD') return null
  const url = new URL(request.url)
  const to = hostRedirects(env.HOST_REDIRECTS).get(url.host.toLowerCase())
  if (!to) return null
  url.host = to
  return new Response(null, { status: 301, headers: { location: url.href, 'cache-control': 'public, max-age=3600' } })
}
