// Prefixes with their numbers at a scan (`POST /api/prefixes`), for any table
// of arbitrary prefixes — `/staged`'s items, the action log — plus the
// helpers `PrefixTable` renders them with.
import { useQuery } from '@tanstack/react-query'
import type { TreeNode } from './types'

/** One prefix's numbers at a scan, a map node's measures (`TreeNode`, no children). */
export type PrefixStat = Pick<TreeNode, 'bytes' | 'objects' | 'mtime' | 'last_read' | 'owners' | 'class_bytes'>

/** The endpoint's per-request cap (`functions/_lib/prefixes.ts`). */
export const CHUNK = 1000

/** The longest `retry-after` (seconds) a refused chunk waits out before its one resend. The reader's refusals
 *  (`functions/_lib/index.ts` `ivRefusal`) say 1 s for an unreadable pointer or state, and what is left of a broken
 *  run's window (up to 30 s): a short one is cheaper waited out than shown, a long one fails with its reason. */
export const RETRY_AFTER_MAX_S = 5

const sleep = (ms: number): Promise<void> => new Promise(r => setTimeout(r, ms))

/** A response's JSON body; null when it isn't JSON (a refusal's text, an edge error page). */
function parseBody(text: string): { stats?: Record<string, PrefixStat>; error?: string } | null {
  try {
    return JSON.parse(text) as { stats?: Record<string, PrefixStat>; error?: string } | null
  } catch {
    return null
  }
}

/** `keys` at scan `date`: every chunk (under the endpoint's cap) in flight at once, so the slowest one sets the wait
 *  (one chunk costs the Worker seconds of CPU; serial, `/staged`'s ~5K prefixes took ~23 s, in parallel ~7 s). A chunk
 *  is sent once more by itself, not the whole set, when its connection drops (a `TypeError` from `fetch`: a tab waking
 *  from sleep, a network change) or the server refuses it with a 503 saying when to retry (`retry-after`, at most
 *  `RETRY_AFTER_MAX_S`: waited out first); any other HTTP error fails at once. An error carries the server's reason:
 *  a JSON `error`, or a plain-text refusal's line. */
export async function fetchPrefixStats(date: string, keys: string[], f: typeof fetch = fetch, wait: (ms: number) => Promise<void> = sleep): Promise<Record<string, PrefixStat>> {
  const one = async (prefixes: string[], retry = true): Promise<Record<string, PrefixStat>> => {
    let r: Response
    try {
      r = await f('/api/prefixes', {
        method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ date, prefixes }),
      })
    } catch (e) {
      if (retry && e instanceof TypeError) return one(prefixes, false)
      throw e
    }
    const after = r.status === 503 && r.headers.get('retry-after') !== null ? Number(r.headers.get('retry-after')) : NaN
    if (retry && after >= 0 && after <= RETRY_AFTER_MAX_S) {
      await wait(after * 1000)
      return one(prefixes, false)
    }
    const text = await r.text().catch(() => '')
    const plain = (r.headers.get('content-type') ?? '').startsWith('text/plain') ? text.trim() : ''
    const j = parseBody(text)
    if (!r.ok || !j?.stats) throw new Error(j?.error ?? (plain || `prefixes: ${r.status}`))
    return j.stats
  }
  const chunks: string[][] = []
  for (let i = 0; i < keys.length; i += CHUNK) chunks.push(keys.slice(i, i + CHUNK))
  return Object.assign({}, ...await Promise.all(chunks.map(c => one(c))))
}

/** `prefixes` at scan `date` (`fetchPrefixStats`); a prefix empty at that scan is absent from the result. */
export function usePrefixes(date: string, prefixes: string[]) {
  const key = [...prefixes].sort()
  return useQuery<Record<string, PrefixStat>, Error>({
    queryKey: ['prefixes', date, key],
    enabled: !!date && key.length > 0,
    staleTime: 5 * 60_000,
    queryFn: () => fetchPrefixStats(date, key),
  })
}

/** "45s ago" … "5h ago", "3d ago", "5w ago", "4mo ago", "2y ago" — `ts` in
 *  epoch seconds. Weeks from two weeks, months from ~two months, years from
 *  two years: each unit only once its count is unambiguous. */
export function relAgo(ts: number, now = Date.now() / 1000): string {
  const s = Math.max(0, Math.floor(now - ts))
  const d = s / 86400
  const unit = s < 60 ? `${s}s`
    : s < 3600 ? `${Math.floor(s / 60)}m`
    : s < 86400 ? `${Math.floor(s / 3600)}h`
    : d < 14 ? `${Math.floor(d)}d`
    : d < 61 ? `${Math.floor(d / 7)}w`
    : d < 730 ? `${Math.floor(d / 30.44)}mo`
    : `${Math.floor(d / 365.25)}y`
  return `${unit} ago`
}

/** The columns every prefix table sorts by, and any page-specific extras. The keys are the tables' URL sort
 *  tokens (kept short: shared links carry them); `STAT_SORT` names the stat each reads. */
export type PrefixSortKey = 'name' | 'b' | 'o' | 'd' | 'a' | (string & {})
const STAT_SORT: Record<string, 'bytes' | 'objects' | 'mtime' | 'last_read'> = { b: 'bytes', o: 'objects', d: 'mtime', a: 'last_read' }

/** Rows by `key` (an extra column's via `extra`), missing values last either
 *  way, ties by name. */
export function sortPrefixRows<T extends { name: string; stat?: PrefixStat }>(
  rows: T[],
  key: PrefixSortKey,
  asc: boolean,
  extra?: (r: T) => number | string | undefined,
): T[] {
  const val = (r: T): number | string | undefined =>
    key === 'name' ? r.name
    : key in STAT_SORT ? r.stat?.[STAT_SORT[key]]
    : extra?.(r)
  return [...rows].sort((x, y) => {
    const a = val(x)
    const b = val(y)
    if (a === undefined || b === undefined) {
      if (a === b) return x.name.localeCompare(y.name)
      return a === undefined ? 1 : -1
    }
    const c = typeof a === 'string' ? a.localeCompare(b as string) : a - (b as number)
    return (asc ? c : -c) || x.name.localeCompare(y.name)
  })
}
