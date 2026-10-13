import { DEFAULT_PALETTE } from '@rdub/treemap'
import { exactInteger, type HotView } from './hotModel'

const BUCKET_COLORS = ['#3c8cdd', '#e88c30', '#34b288', '#d7425b', ...DEFAULT_PALETTE.slice(4)]

export interface HotMapNode {
  path: string
  b: number
  o: number
  color?: string
  children?: HotMapNode[]
}

export interface HotMapSnapshot {
  date: string
  pattern: string
  root: HotMapNode
  empty: string | null
}

/** Stable logical path order/colors, independent dated areas; zero-byte counts stay in the table. */
export function hotMap(view: HotView): HotMapSnapshot {
  const buckets = [...view.buckets].sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0)
  const children = buckets.map((row, i) => ({ path: row.path, b: row.b, o: row.o, color: BUCKET_COLORS[i] }))
    .filter(row => row.b > 0)
  return {
    date: view.date,
    pattern: view.pattern,
    root: { path: 'All buckets', ...view.root, children },
    empty: view.root.b > 0 ? null : view.root.o > 0
      ? `${exactInteger(view.root.o)} matching objects total 0 bytes, so no byte area is drawn. Exact counts remain in the table.`
      : 'No matching bytes or objects in this snapshot.',
  }
}
