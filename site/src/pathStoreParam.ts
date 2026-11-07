// The page's `ps` (the path store a request reads: `iv` the interval store under `PATH_STORE=opt-in`, `scan` the
// per-scan sorts under `intervals`; `withPathStore`) rides on every same-origin `/api/` request the page makes, so a
// `?ps=…` page is answered by the store it names.

/** The page URL's `ps`, else null. */
export const pagePs = (search: string): string | null => new URLSearchParams(search).get('ps') || null

/** `url` (relative or absolute) with `ps` added when it is a same-`origin` `/api/` request without one. */
export function withPs(url: string, ps: string | null, origin: string): string {
  if (!ps) return url
  const u = new URL(url, origin)
  if (u.origin !== origin || !u.pathname.startsWith('/api/') || u.searchParams.has('ps')) return url
  u.searchParams.set('ps', ps)
  return /^[a-z]+:/i.test(url) ? u.toString() : u.pathname + u.search + u.hash
}

/** Wrap `win.fetch` so the page's `ps` (read once, at load) reaches each `/api/` request; nothing without one. */
export function forwardPs(win: Pick<Window, 'fetch' | 'location'>): void {
  const ps = pagePs(win.location.search)
  if (!ps) return
  const origin = win.location.origin
  const inner = win.fetch.bind(win)
  win.fetch = (input, init) => {
    if (typeof input === 'string') return inner(withPs(input, ps, origin), init)
    if (input instanceof URL) return inner(withPs(input.toString(), ps, origin), init)
    const url = withPs(input.url, ps, origin)
    return inner(url === input.url ? input : new Request(url, input), init)
  }
}
