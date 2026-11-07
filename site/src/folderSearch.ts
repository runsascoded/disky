// The map's omnibar search (specs/done/omnibar-folder-search.md): `⌘K grug` lists the folders and files named for
// `grug`, largest first (`/api/name-matches`: the map's own match roots, at the map's scan), and picking one drills
// the map there (a file opens in the leaf viewer); a "Filter map by …" row sets `?f=` instead.
import { useOmnibarEndpoint, type EndpointResponse, type OmnibarEntry } from 'use-kbd'

export interface NameMatch { path: string; kind: 'dir' | 'file' | null; b: number; o: number }
export interface NameMatches { date: string; path: string; q: string; items: NameMatch[]; total: number; complete: boolean; reason?: string }

/** The scan the omnibar searches: the map's, unless the static name index doesn't cover it yet (a just-published
 *  scan on an indexed-only deployment, `indexed` its covered scans), then the newest covered one at or before it, as
 *  the filtered map opens on (`useIndexedScans`); none while that list loads or when none is that old. */
export function searchScan(date: string | null, indexed: string[] | null | 'loading'): string | null {
  if (!date || indexed === null) return date
  if (indexed === 'loading') return null
  if (indexed.includes(date)) return date
  return indexed.filter(s => s <= date).sort().at(-1) ?? null
}

/** The omnibar's minimum query before it asks the server (the filter row shows from one character). */
export const MIN_QUERY = 2

/** A result's label: its name, then where it is (the parent path, elided from the middle past `max`). */
export function matchLabel(path: string, max = 48): string {
  const i = path.lastIndexOf('/')
  const name = path.slice(i + 1)
  const parent = i < 0 ? '' : path.slice(0, i)
  if (!parent) return name
  const room = Math.max(12, max - name.length - 4)
  const where = parent.length <= room ? parent : `${parent.slice(0, room - 13)}…${parent.slice(-12)}`
  return `${name} — ${where}/`
}

/** The entries for one `/api/name-matches` answer: each match (`go`), then, when the server couldn't list every one,
 *  a row saying so whose pick filters the map. */
export function matchEntries(body: NameMatches, o: { go: (m: NameMatch) => void; filter: () => void; size: (b: number) => string; scanNote?: string }): OmnibarEntry[] {
  const out: OmnibarEntry[] = body.items.map(m => ({
    id: `match:${m.path}`,
    label: matchLabel(m.path),
    description: `${m.kind === 'file' ? 'file' : 'folder'} · ${o.size(m.b)}${m.kind !== 'file' && m.o > 1 ? ` · ${m.o.toLocaleString()} objects` : ''}${o.scanNote ? ` · ${o.scanNote}` : ''}`,
    keywords: [m.path],
    handler: () => o.go(m),
  }))
  if (!body.complete && body.reason) out.push({ id: 'match:incomplete', label: body.reason, description: 'Pick to filter the map instead', handler: o.filter })
  return out
}

/** `/api/name-matches` for `q` at a scan (`date`) or union instant (`at`); null when the search is refused or fails (a
 *  query the index can't answer, a scan it doesn't cover): the filter row still applies the query, and the map says why.
 *  `fetcher` is called bare: the primary store's is the global `fetch`, which throws "Illegal invocation" when called as
 *  a method (`o.fetcher(…)`), and use-kbd shows a throwing endpoint as empty. */
export async function fetchMatches(
  fetcher: (url: string, init?: RequestInit) => Promise<Response>,
  a: { date?: string; at?: string; q: string; qs: string; limit: number },
  signal?: AbortSignal,
): Promise<NameMatches | null> {
  const sp = new URLSearchParams({ ...(a.at ? { at: a.at } : { date: a.date! }), q: a.q, qs: a.qs, limit: String(a.limit) })
  const r = await fetcher(`/api/name-matches?${sp}`, { credentials: 'include', signal })
  return r.ok ? await r.json() as NameMatches : null
}

export function useFolderSearch(o: {
  fetcher: (url: string, init?: RequestInit) => Promise<Response>
  /** The map's scan (`date`), or a union view's instant (`at`); none: the search waits. */
  date: string | null
  /** The scans the static name index covers (`useIndexedScans`): the search falls back to the newest of them (`searchScan`). */
  indexed?: string[] | null | 'loading'
  at?: string | null
  /** The filter syntax the map parses with (`?qs=`). */
  qs: string
  drill: (segs: string[]) => void
  open: (segs: string[]) => void
  filter: (q: string) => void
  size: (b: number) => string
}) {
  const date = o.at ? o.date : searchScan(o.date, o.indexed ?? null)
  const ready = !!(o.at ?? date)
  // Searching an older scan than the map shows (the newest isn't indexed yet): each result says which.
  const scanNote = !o.at && date && date !== o.date ? `as of scan ${date}` : undefined
  useOmnibarEndpoint('map:filter', {
    group: 'Map',
    // Above the page's actions (100), below the matches: a typed name is most likely a place, and the actions' loose
    // fuzzy matches ("crash" ⊂ "…manual sCans dRAwn…") otherwise come first.
    priority: 110,
    minQueryLength: 1,
    filter: (query): EndpointResponse => {
      const q = query.trim()
      return { entries: q ? [{ id: 'map:filter', label: `Filter map by “${q}”`, description: 'Sets the map’s name filter (?f=)', handler: () => o.filter(q) }] : [] }
    },
  })
  useOmnibarEndpoint('map:matches', {
    group: 'Folders & files',
    priority: 120,
    minQueryLength: MIN_QUERY,
    enabled: ready,
    pageSize: 10,
    sort: 'none',
    fetch: async (query, signal, { limit }): Promise<EndpointResponse> => {
      const q = query.trim()
      const body = await fetchMatches(o.fetcher, { ...(o.at ? { at: o.at } : { date: date! }), q, qs: o.qs, limit }, signal)
      if (!body) return { entries: [] }
      const go = (m: NameMatch) => (m.kind === 'file' ? o.open : o.drill)(m.path.split('/'))
      return { entries: matchEntries(body, { go, filter: () => o.filter(q), size: o.size, scanNote }), total: body.total }
    },
  })
}
