import { useEffect, useMemo, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { intParam, useUrlState } from 'use-prms'
import { elideMid } from './CopyName'
import type { DiffModel } from './DiffTreemap'
import { defaultAsc, diffTableRows, fmtPct, sortDiffRows } from './diffRows'
import type { DiffTableRow, SortKey } from './diffRows'
import { Tooltip } from './Tooltip'
import { usePerfCommit } from './perf'
import { pathText } from './pathCrumbs'

// The diff map's tabular twin (like ChildrenTable under the main map): one
// row per cell of the drilled node — before / after / Δ bytes and objects,
// every column sortable, a named row acting exactly as its cell does (a
// directory drills the page, an object opens in the leaf viewer). Rows are the map's cells, so the two always agree: a fold or a
// filler is listed (and never drills); a depth-1 row the map dropped (an
// unchanged directory in Δ mode, weight 0) is counted in the footer instead.

const PAGE_SIZES = [20, 50, 100, 200]
const NAME_MAX = 60

const STATUS_LABEL: Record<DiffTableRow['status'], string> = {
  added: 'added', first: 'first scanned', removed: 'removed', changed: 'changed', unchanged: 'unchanged',
}

export function DiffTable({ model, scheme, home, segs, onDrill, onOpen }: {
  model: DiffModel
  scheme: string
  /** The store home, read as `~` (`Store.home`). */
  home?: string[]
  /** The page's drill: path segments from the store root to the diffed node. */
  segs: string[]
  /** A named directory row was opened: its segments below the diffed node
   *  (the map's `onDrill` contract). */
  onDrill: (segs: string[]) => void
  /** A named object row was opened: its segments below the diffed node. */
  onOpen: (segs: string[]) => void
}) {
  usePerfCommit('dtable')
  const { root, data, fmtBytes, fmtDelta, fmtN, fmtNDelta } = model
  const [sort, setSort] = useState<{ k: SortKey; asc: boolean }>({ k: 'delta', asc: false })
  const [page, setPage] = useState(0)
  const [nP, setNP] = useUrlState('n', intParam(20))
  const PAGE = PAGE_SIZES.includes(nP) ? nP : 20

  const rows = useMemo(() => sortDiffRows(diffTableRows(root.children ?? []), sort.k, sort.asc), [root, sort])
  // Depth-1 rows (objects or directories) the map left undrawn (weight 0: unchanged, in Δ mode).
  const unlisted = useMemo(() => {
    const listed = new Set(rows.map(r => r.key))
    return data.rows.filter(r => r.d === 1 && !listed.has(r.p)).length
  }, [rows, data])
  useEffect(() => setPage(0), [root, sort])

  const pages = Math.max(1, Math.ceil(rows.length / PAGE))
  const pg = Math.min(page, pages - 1)
  const shown = rows.slice(pg * PAGE, (pg + 1) * PAGE)

  const pick = (k: SortKey) => setSort(s => ({ k, asc: s.k === k ? !s.asc : defaultAsc(k) }))
  const th = (k: SortKey, label: string, num = true) => {
    const on = sort.k === k
    return (
      <th
        className={(num ? 'num ' : '') + 'sortable' + (on ? ' on' : '')}
        aria-sort={on ? (sort.asc ? 'ascending' : 'descending') : 'none'}
        tabIndex={0}
        onClick={() => pick(k)}
        onKeyDown={(e: KeyboardEvent) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(k) } }}
        title="sort"
      >
        {label}{on ? (sort.asc ? ' ▲' : ' ▼') : ''}
      </th>
    )
  }
  const deltaCls = (r: { delta: number; status: DiffTableRow['status'] }, d: number) =>
    'num ' + (r.status === 'first' ? 'first' : d > 0 ? 'grew' : d < 0 ? 'shrank' : 'zero')
  const pager = pages > 1 && (
    <span className="pg">
      <button type="button" disabled={pg === 0} onClick={() => setPage(0)} aria-label="first page">«</button>
      <button type="button" disabled={pg === 0} onClick={() => setPage(pg - 1)} aria-label="previous page">‹</button>
      <span>{pg * PAGE + 1}–{Math.min(rows.length, (pg + 1) * PAGE)} of {rows.length.toLocaleString('en-US')}</span>
      <button type="button" disabled={pg >= pages - 1} onClick={() => setPage(pg + 1)} aria-label="next page">›</button>
      <button type="button" disabled={pg >= pages - 1} onClick={() => setPage(pages - 1)} aria-label="last page">»</button>
      <select className="psize" value={PAGE} onChange={e => { setNP(+e.target.value); setPage(0) }} aria-label="rows per page">
        {PAGE_SIZES.map(n => <option key={n} value={n}>{n} / page</option>)}
      </select>
    </span>
  )
  if (!rows.length) return null
  const rootPct = root.size_old > 0 ? (root.size_new - root.size_old) / root.size_old : null
  return (
    <section className="children-tbl diff-tbl" id="dtbl">
      {pager && <div className="pager top">{pager}</div>}
      <table className="worklist">
        <thead>
          <tr>
            {th('name', 'name', false)}
            {th('status', 'status', false)}
            {th('a', 'before')}
            {th('b', 'after')}
            {th('delta', 'Δ')}
            {th('pct', 'Δ%')}
            {th('oa', 'objects before')}
            {th('ob', 'after')}
            {th('odelta', 'Δ')}
          </tr>
        </thead>
        <tbody>
          {shown.map(r => {
            const uri = pathText(scheme, [...segs, ...r.segs], home)
            const go = () => (r.kind === 'file' ? onOpen : onDrill)(r.segs)
            return (
              <tr key={r.key}>
                <td className="prefix">
                  {r.synthetic ? (
                    <span>{elideMid(r.name, NAME_MAX)}</span>
                  ) : (
                    <Tooltip content={<code className="elide-full">{uri}</code>}>
                      <a role="link" tabIndex={0} onClick={go}
                        onKeyDown={(e: KeyboardEvent) => { if (e.key === 'Enter') go() }}>
                        {elideMid(r.name, NAME_MAX)}
                      </a>
                    </Tooltip>
                  )}
                </td>
                <td><span className={'chip st-' + r.status}>{STATUS_LABEL[r.status]}</span></td>
                <td className={'num' + (r.status === 'added' || r.status === 'first' ? ' none' : '')}>{r.status === 'added' || r.status === 'first' ? '—' : fmtBytes(r.a)}</td>
                <td className={'num' + (r.status === 'removed' ? ' none' : '')}>{r.status === 'removed' ? '—' : fmtBytes(r.b)}</td>
                <td className={deltaCls(r, r.delta)}>{r.delta === 0 ? '0' : fmtDelta(r.delta)}</td>
                <td className={deltaCls(r, r.delta)}>{r.pct == null ? (r.b === 0 ? '—' : 'new') : fmtPct(r.pct)}</td>
                <td className={'num' + (r.status === 'added' || r.status === 'first' ? ' none' : '')}>{r.status === 'added' || r.status === 'first' ? '—' : fmtN(r.oa)}</td>
                <td className={'num' + (r.status === 'removed' ? ' none' : '')}>{r.status === 'removed' ? '—' : fmtN(r.ob)}</td>
                <td className={deltaCls(r, r.odelta)}>{r.odelta === 0 ? '0' : fmtNDelta(r.odelta)}</td>
              </tr>
            )
          })}
        </tbody>
        <tfoot>
          {/* The diffed node itself (both scans' totals) — what the rows partition. */}
          <tr className="total-row">
            <td>total</td>
            <td />
            <td className="num">{fmtBytes(root.size_old)}</td>
            <td className="num">{fmtBytes(root.size_new)}</td>
            <td className={deltaCls({ delta: root.delta, status: 'changed' }, root.delta)}>{root.delta === 0 ? '0' : fmtDelta(root.delta)}</td>
            <td className={deltaCls({ delta: root.delta, status: 'changed' }, root.delta)}>{rootPct == null ? '—' : fmtPct(rootPct)}</td>
            <td className="num">{fmtN(root.n_old)}</td>
            <td className="num">{fmtN(root.n_new)}</td>
            <td className={deltaCls({ delta: root.n_desc_delta, status: 'changed' }, root.n_desc_delta)}>{root.n_desc_delta === 0 ? '0' : fmtNDelta(root.n_desc_delta)}</td>
          </tr>
        </tfoot>
      </table>
      {pager && <div className="pager">{pager}</div>}
      {/* The map draws only what moved (Δ mode): say what the table is
          leaving out, so the rows and the totals agree. */}
      {unlisted > 0 && <p className="tbl-note">{fmtN(unlisted)} unchanged {unlisted === 1 ? 'path' : 'paths'} not listed</p>}
    </section>
  )
}
