// VM mode's session pin on the page (specs/vm-pilot.md step 4; the server's side is `functions/_lib/vmServe.ts`).
// The server pins each session to `idx` (today's indexes) or `vm` (`vmd`) by a cookie, re-pinned by `?be=`; the page:
// - carries a `vm` pin in its own URL (`be=vm`), so the links it builds from it (a drill, the prod/dev toggle, a copied
//   or shared link) keep it; an `idx` pin is the default and rides nowhere;
// - adds `be=vm` to its same-origin `/api/` requests, so a browser's copy of one backend's answer (`private, max-age`)
//   is never read for the other's;
// - watches its map requests' answers say which backend gave them (`Server-Timing` `be`, `x-disky-be-note`), for the
//   badge (`BackendBadge`).

export type Backend = 'idx' | 'vm'
export type Answered = 'idx' | 'vm' | 'idx-fallback'

export const BE_PARAM = 'be'
export const BE_COOKIE = 'disky_be'
export const BE_NOTE = 'x-disky-be-note'

const asBackend = (v: string | null | undefined): Backend | null => (v === 'vm' || v === 'idx' ? v : null)

/** A cookie's value from `document.cookie`'s string, else null. */
export function cookieValue(cookies: string, name: string): string | null {
  for (const part of cookies.split(';')) {
    const i = part.indexOf('=')
    if (i > 0 && part.slice(0, i).trim() === name) return part.slice(i + 1).trim()
  }
  return null
}

/** The page's pin: its URL's `be`, else the cookie the server set; null with neither (VM mode off). */
export const pagePin = (search: string, cookies: string): Backend | null =>
  asBackend(new URLSearchParams(search).get(BE_PARAM)) ?? asBackend(cookieValue(cookies, BE_COOKIE))

/** `search` with a `vm` pin added (`be=vm`, appended raw: the rest isn't re-encoded); null when it needs no change
 *  (not pinned `vm`, or the URL already names a backend). */
export function carriedSearch(search: string, pin: Backend | null): string | null {
  if (pin !== 'vm' || new URLSearchParams(search).has(BE_PARAM)) return null
  return `${search && search !== '?' ? `${search}&` : '?'}${BE_PARAM}=vm`
}

/** `url` (relative or absolute) with `be=vm` added when the page is pinned `vm` and it is a same-`origin` `/api/`
 *  request without a `be`. */
export function withBe(url: string, pin: Backend | null, origin: string): string {
  if (pin !== 'vm') return url
  const u = new URL(url, origin)
  if (u.origin !== origin || !u.pathname.startsWith('/api/') || u.searchParams.has(BE_PARAM)) return url
  u.searchParams.set(BE_PARAM, 'vm')
  return /^[a-z]+:/i.test(url) ? u.toString() : u.pathname + u.search + u.hash
}

/** The map requests a badge reports, by path, and how it names each. */
export const KINDS: Record<string, string> = {
  '/api/subtree': 'Map',
  '/api/diff': 'Diff',
  '/api/series': 'Over time',
  '/api/name-summary': 'Search',
}

/** Which backend an answer says gave it (`Server-Timing`'s `be`), and its note. */
export function answeredBy(serverTiming: string | null, note: string | null): { be?: Answered; note?: string } {
  const m = /(?:^|,)\s*be;desc="([^"]*)"/.exec(serverTiming ?? '')
  const be = m && (m[1] === 'vm' || m[1] === 'idx' || m[1] === 'idx-fallback') ? m[1] as Answered : undefined
  return { ...(be ? { be } : {}), ...(note ? { note } : {}) }
}

export interface Seen { be: Answered; note?: string; at: number }

/** The answer the badge shows: the newest map or diff answer, else the newest of any kind. */
export function headline(seen: Record<string, Seen>): Seen | null {
  const newest = (ks: string[]) => ks.map(k => seen[k]).filter(Boolean).sort((a, b) => b.at - a.at)[0] ?? null
  return newest(['/api/subtree', '/api/diff']) ?? newest(Object.keys(seen))
}
export interface BackendState {
  /** The session's pin as the page read it at load; null: VM mode off, or no pin yet. */
  pin: Backend | null
  /** Per request kind (`KINDS`' paths), the latest answer's backend. */
  seen: Record<string, Seen>
}

let state: BackendState = { pin: null, seen: {} }
const subs = new Set<() => void>()

/** The badge's store (`useSyncExternalStore`). */
export const backendStore = {
  subscribe: (f: () => void): (() => void) => {
    subs.add(f)
    return () => { subs.delete(f) }
  },
  get: (): BackendState => state,
  /** Record one answer (a map request's path, its headers' say). */
  saw: (path: string, a: { be?: Answered; note?: string }, at: number = Date.now()): void => {
    if (!a.be || !(path in KINDS)) return
    state = { ...state, seen: { ...state.seen, [path]: { be: a.be, ...(a.note ? { note: a.note } : {}), at } } }
    for (const f of subs) f()
  },
  setPin: (pin: Backend | null): void => {
    state = { ...state, pin }
    for (const f of subs) f()
  },
  /** Tests. */
  reset: (): void => { state = { pin: null, seen: {} } },
}

type Win = Pick<Window, 'fetch' | 'location'> & { document: { cookie: string }; history?: Pick<History, 'replaceState' | 'state'> }

/** At load: read the pin, put a `vm` one in the page's URL (`carriedSearch`, before the router reads it), and wrap
 *  `win.fetch` so `/api/` requests carry it and map answers report their backend. With VM mode off nothing is pinned,
 *  and a `be=vm` the server ignored is said once in the console. */
export function forwardBe(win: Win): void {
  const pin = pagePin(win.location.search, win.document.cookie)
  backendStore.setPin(pin)
  const carried = carriedSearch(win.location.search, pin)
  if (carried != null && win.history) win.history.replaceState(win.history.state, '', `${win.location.pathname}${carried}${win.location.hash}`)
  const origin = win.location.origin
  const inner = win.fetch.bind(win)
  let told = false
  win.fetch = async (input, init) => {
    const url = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url
    const to = withBe(url, pin, origin)
    const res = await inner(to === url ? input : typeof input === 'string' || input instanceof URL ? to : new Request(to, input), init)
    const u = new URL(to, origin)
    if (u.origin === origin && u.pathname in KINDS && res.ok) {
      const a = answeredBy(res.headers.get('server-timing'), res.headers.get(BE_NOTE))
      if (a.be) backendStore.saw(u.pathname, a)
      else if (a.note && !told) {
        told = true
        console.info(`backend: ${a.note}`)
      }
    }
    return res
  }
}

/** The page's URL re-pinned to `to` (a full load: the server re-pins the session's cookie). */
export function repinHref(href: string, to: Backend): string {
  const u = new URL(href)
  u.searchParams.set(BE_PARAM, to)
  return u.toString()
}
