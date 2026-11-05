// The app's pager: one `.pg` strip (« ‹ 51–100 of 5,404 › », a page-size picker when there's a choice) and the state
// behind it, for any list that can outgrow a screen. `Paginated` wraps a table or list; `usePaged` is the hook, for a
// list that also has to reveal a row by index (a permalink, a jump).
import { type ReactNode, useState } from 'react'

const { ceil, floor, max, min } = Math

export interface PageWindow {
  page: number
  pages: number
  /** The page's rows, half-open. */
  from: number
  to: number
}

/** `page` of a `total`-row list, clamped: a list that shrinks under its page lands on its last. */
export function pageWindow(total: number, size: number, page: number): PageWindow {
  const pages = max(1, ceil(total / size))
  const p = min(max(page, 0), pages - 1)
  return { page: p, pages, from: p * size, to: min(total, (p + 1) * size) }
}

/** The page holding row `index`. */
export const pageOfIndex = (index: number, size: number): number => floor(index / size)

export interface Paged<T> extends PageWindow {
  rows: T[]
  total: number
  size: number
  sizes: readonly number[]
  setPage: (page: number) => void
  setSize: (size: number) => void
  /** Go to the page holding row `index`. */
  reveal: (index: number) => void
}

export const DEFAULT_SIZES: readonly number[] = [50]

/** `rows`' current page. `sizes`: the page sizes on offer, the first the default. */
export function usePaged<T>(rows: readonly T[], sizes: readonly number[] = DEFAULT_SIZES): Paged<T> {
  const [page, setPage] = useState(0)
  const [size, setSize] = useState(sizes[0])
  const w = pageWindow(rows.length, size, page)
  return {
    ...w,
    rows: rows.slice(w.from, w.to),
    total: rows.length,
    size,
    sizes,
    setPage,
    setSize: s => { setSize(s); setPage(0) },
    reveal: index => setPage(pageOfIndex(index, size)),
  }
}

/** The strip; nothing when the whole list fits on the smallest page. */
export function Pager<T>({ paged, label }: { paged: Paged<T>; label: string }) {
  const { page, pages, from, to, total, size, sizes, setPage, setSize } = paged
  if (total <= sizes[0]) return null
  return (
    <div className="pg" role="navigation" aria-label={`${label} pages`}>
      <button type="button" disabled={page === 0} onClick={() => setPage(0)} aria-label="first page">«</button>
      <button type="button" disabled={page === 0} onClick={() => setPage(page - 1)} aria-label="previous page">‹</button>
      <span>{(from + 1).toLocaleString('en-US')}–{to.toLocaleString('en-US')} of {total.toLocaleString('en-US')}</span>
      <button type="button" disabled={page >= pages - 1} onClick={() => setPage(page + 1)} aria-label="next page">›</button>
      <button type="button" disabled={page >= pages - 1} onClick={() => setPage(pages - 1)} aria-label="last page">»</button>
      {sizes.length > 1 && (
        <select className="psize" value={size} onChange={e => setSize(+e.target.value)} aria-label="rows per page">
          {sizes.map(n => <option key={n} value={n}>{n.toLocaleString('en-US')} / page</option>)}
        </select>
      )}
    </div>
  )
}

/** A list and its pager: `children` renders the current page (`from`: its first row's index in `rows`). */
export function Paginated<T>({ rows, sizes, label, children }: { rows: readonly T[]; sizes?: readonly number[]; label: string; children: (page: T[], from: number) => ReactNode }) {
  const paged = usePaged(rows, sizes)
  return <>{children(paged.rows, paged.from)}<Pager paged={paged} label={label} /></>
}
