// Prefixes with their numbers at a scan (`POST /api/prefixes`), for any table
// of arbitrary prefixes — `/staged`'s items, the action log — plus the
// helpers `PrefixTable` renders them with.
import { useQuery } from '@tanstack/react-query'

/** One prefix's numbers at a scan, in `TreeNode`'s wire names. */
export interface PrefixStat { b: number; o: number; d?: number; a?: number; us?: [string, number][]; cb?: Record<string, number> }

/** The endpoint's per-request cap (`functions/_lib/prefixes.ts`). */
const CHUNK = 1000

/** `prefixes` at scan `date`, chunked under the endpoint's cap; a prefix
 *  empty at that scan is absent from the result. */
export function usePrefixes(date: string, prefixes: string[]) {
  const key = [...prefixes].sort()
  return useQuery<Record<string, PrefixStat>, Error>({
    queryKey: ['prefixes', date, key],
    enabled: !!date && key.length > 0,
    staleTime: 5 * 60_000,
    queryFn: async () => {
      const out: Record<string, PrefixStat> = {}
      for (let i = 0; i < key.length; i += CHUNK) {
        const r = await fetch('/api/prefixes', {
          method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ date, prefixes: key.slice(i, i + CHUNK) }),
        })
        const j = (await r.json().catch(() => null)) as { stats?: Record<string, PrefixStat>; error?: string } | null
        if (!r.ok || !j?.stats) throw new Error(j?.error ?? `prefixes: ${r.status}`)
        Object.assign(out, j.stats)
      }
      return out
    },
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

/** The columns every prefix table sorts by, and any page-specific extras. */
export type PrefixSortKey = 'name' | 'b' | 'o' | 'd' | 'a' | (string & {})

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
    : key === 'b' || key === 'o' || key === 'd' || key === 'a' ? r.stat?.[key as 'b' | 'o' | 'd' | 'a']
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
