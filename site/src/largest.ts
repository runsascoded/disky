import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useStore, useStoreFetch } from './store'
import type { TreeNode } from './types'

// The children table's flat modes (`ChildrenTable`'s files | folders): the biggest objects, or folders, anywhere
// under the drilled directory, however deep, size-descending (`/api/largest`, read from the store's `bysize` sort).

/** What a flat listing holds: objects, or the folders strictly below the path. */
export type LargestKind = 'file' | 'dir'

/** One listed path: its full store path, bytes, newest stamp (epoch seconds) and, for a folder, its objects. */
export interface LargeItem { path: string; size: number; mtime: number | null; n_files?: number }
export interface Largest { date: string; path: string; n: number; kind?: 'dir'; files?: LargeItem[]; dirs?: LargeItem[]; more: boolean; groups: number; rows: number; capped?: boolean }

/** The most `/api/largest` lists at once (`LARGEST_MAX`); a flat listing grows toward it a step at a time. */
export const LARGEST_MAX = 500
export const LARGEST_STEP = 100

/** `/api/largest` for a store path at a scan; `enabled` false = not asked. A larger `n` keeps showing the
 *  smaller answer until it lands (paging past the listed rows asks for more). */
export function useLargest(path: string, scan: string | null | undefined, n: number, kind: LargestKind, enabled = true) {
  const store = useStore()
  const sfetch = useStoreFetch()
  return useQuery({
    queryKey: ['largest', store.key, scan, path, n, kind],
    enabled: !!scan && enabled,
    staleTime: Infinity,
    placeholderData: keepPreviousData,
    // 409 (a scan indexed before objects were listed) is deterministic.
    retry: (k: number, e: Error) => !/^4\d\d/.test(e.message) && k < 2,
    queryFn: async ({ signal }) => {
      const r = await sfetch(`/api/largest?date=${scan}&path=${encodeURIComponent(path)}&n=${n}${kind === 'dir' ? '&kind=dir' : ''}`, { credentials: 'include', signal })
      if (!r.ok) throw new Error(`${r.status} ${await r.text()}`)
      return await r.json() as Largest
    },
  })
}

/** A flat listing as table rows: each named by its path below `path` (the drilled dir; `''` = the store root), so a
 *  row's segments are the dir's plus its name split on `/`; `d` is the stamp's epoch day, `o` a folder's objects
 *  (an object's 1). */
export function flatRows(path: string, items: LargeItem[], kind: LargestKind): TreeNode[] {
  const cut = path ? path.length + 1 : 0
  return items.map(x => ({
    n: x.path.slice(cut),
    k: kind,
    b: x.size,
    o: x.n_files ?? 1,
    ...(x.mtime ? { d: Math.floor(x.mtime / 86400) } : {}),  // an mtime of 0 is unknown
  }))
}
