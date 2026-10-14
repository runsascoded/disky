import { useEffect, useMemo, useState } from 'react'
import type { MouseEvent } from 'react'
import { intParam, useUrlState } from 'use-prms'
import { useCanStage } from './auth'
import { FaRegTrashCan } from 'react-icons/fa6'
import { AGE_BUCKETS, ageBucketColor, dateColor, dateGradientCss, epochDaysToDate, epochDaysToMonthShort } from './colors'
import { useStore } from './store'
import { listingTime } from './listingTime'
import { Tooltip } from './Tooltip'
import { elideMid } from './CopyName'
import { useRowSelection, useRowSelectionKeys } from './rowSelection'
import { PrefixStageAction, tableCoverItem, useStagedIndex } from './StageAction'
import { rowTarget } from './objects'
import type { TreeNode } from './types'
import { fmtN } from './types'
import { useUnits } from './units'
import { usePerfCommit } from './perf'
import { pathText } from './pathCrumbs'
import { rowLabel, type RowMatches, rowsCover } from './filterCover'
import { ActStatus, RowActs, type RowSource, useActDeps, useKeepFocus, useMatchAct } from './MatchActions'

// Sortable, paged listing of the treemap's current node's children — the
// tabular twin of the map above it: every named row is a link, a directory
// drilling like its cell, an object opening in the leaf viewer.
// Row selection + bulk staging / assignment: `gcs:specs/done/children-table-selection.md`.

type SortKey = 'n' | 'b' | 'o' | 'd' | 'a'

// Rows per page: `?n=` (default 20); the pager offers the usual sizes.
const PAGE_SIZES = [20, 50, 100, 200]

/** Names longer than this elide from the middle (the full path is in the
 *  tooltip); ~60 chars fills the column's 480px at 12px mono. */
const NAME_MAX = 60

export function ChildrenTable({ node, segs, scheme, home, onOpen, onOpenObject, filter, rowMatches, brush, onBrush }: {
  /** The treemap's currently-viewed node. */
  node: TreeNode
  /** Path segments from the tree root to `node` (no scheme, no root). */
  segs: string[]
  scheme: string
  /** The store home, read as `~` in row tooltips (`Store.home`). */
  home?: string[]
  /** A directory row was opened: drill there (segments from the root). */
  onOpen: (segs: string[]) => void
  /** An object row was opened: show it in the leaf viewer. */
  onOpenObject: (segs: string[]) => void
  /** Under a filter: where a row's matches come from (`/api/filter-cover`, as the fewest exact prefixes) —
   *  what its trash and assign, and the selection's, act on (the row's own matches, never its whole
   *  prefix). Every row offers them at once; the matches are fetched on intent and awaited on the click.
   *  Absent: no filter, every row acts on its own prefix. */
  filter?: RowSource
  /** Under a filter: each row's listed match roots (`rowMatchesOf`), `null` when unknown — a row names the path
   *  to its match only when it holds exactly one (an exact list), and says how many when several (`9+` from a
   *  capped list). */
  rowMatches?: (row: string) => RowMatches | null
  /** Brushing with the treemap: the child (by name) lit as hovered, and the
   *  row under the pointer, `null` on leave. */
  brush?: string | null
  onBrush?: (name: string | null) => void
}) {
  usePerfCommit('table')
  const { fmtBytes } = useUnits()
  const [sort, setSort] = useState<{ k: SortKey; asc: boolean }>({ k: 'b', asc: false })
  const [page, setPage] = useState(0)
  const [nP, setNP] = useUrlState('n', intParam(20))
  const PAGE = PAGE_SIZES.includes(nP) ? nP : 20
  // A staging store (`Store.staging`): full viewers — not read-only guest
  // links — select + trash (stage for deletion; an admin approves later).
  // The subtree's store's flag: a secondary store has none.
  const store = useStore()
  const time = listingTime(scheme)
  const staging = store.staging
  const canStage = useCanStage()
  // At the store root every row is a whole bucket (on a `*` store, a whole
  // top-level root), which a plan item can't name: no trash there.
  const canTrash = staging && (segs.length > 0 || !!filter)
  // The open plan: rows already staged (or under a staged folder) say so, and a folder holding staged items
  // counts them — the trash's "✓ staged" alone is gone on the next load.
  const staged = useStagedIndex()
  // A filtered view whose matches can't be acted on (a refusal, a rollup, a catalog or approximate answer,
  // `actBlock`): no row offers an action, and nothing selects — a row's whole prefix is never the stand-in.
  const showSel = filter?.blocked ? false : staging && canStage && canTrash
  // One memo for the whole multi-select gesture (stored on the stage batch).
  const [memo, setMemo] = useState('')

  const kids = useMemo(() => {
    const ks = (node.c ?? []).slice()
    const dir = sort.asc ? 1 : -1
    const val = (n: TreeNode): number | string =>
      sort.k === 'n' ? n.n
      : sort.k === 'b' ? n.b
      : sort.k === 'o' ? n.o
      : sort.k === 'a' ? n.a ?? -Infinity
      : n.d ?? -Infinity
    return ks.sort((a, b) => {
      const va = val(a)
      const vb = val(b)
      return (typeof va === 'string' ? (va as string).localeCompare(vb as string) : (va as number) - (vb as number)) * dir
    })
  }, [node, sort])
  // A new listing (drill, sort) starts on page 1.
  useEffect(() => setPage(0), [node, sort])

  // Columns the data can't fill are left out (no access logs → no `read`): a
  // store without that axis shouldn't read as a table of dashes.
  const hasRead = kids.some(k => k.a != null)

  // Created-month ink: an age gradient over the listed rows' range, so a
  // column of "May / Jun / Apr" also reads at a glance as older ↔ newer.
  const [dMin, dMax] = useMemo(() => {
    const ds = kids.map(k => k.d).filter((d): d is number => d != null)
    return ds.length ? [Math.min(...ds), Math.max(...ds)] : [0, 0]
  }, [kids])
  const ageInk = (d: number) => dateColor(dMax > dMin ? (d - dMin) / (dMax - dMin) : 1)

  const th = (k: SortKey, label: string, num = true) => (
    <th
      className={(num ? 'num ' : '') + 'sortable' + (sort.k === k ? ' on' : '')}
      onClick={() => setSort(s => ({ k, asc: s.k === k ? !s.asc : k === 'n' }))}
      title="sort"
    >
      {label}{sort.k === k ? (sort.asc ? ' ▲' : ' ▼') : ''}
    </th>
  )
  const pages = Math.max(1, Math.ceil(kids.length / PAGE))
  const pg = Math.min(page, pages - 1)
  const shown = useMemo(() => kids.slice(pg * PAGE, (pg + 1) * PAGE), [kids, pg, PAGE])
  const uriOfKid = (k: TreeNode) => scheme + [...segs, k.n].join('/')
  // Rows select (click / shift / ⌘, checkboxes, j/k) into one set keyed by
  // uri; the bar above the table stages the whole selection at once.
  // Under a filter a row stands for its matches (resolved on the click), never its whole prefix.
  const acts = (k: TreeNode) => !k.n.startsWith('(')
  const selectable = useMemo(() => shown.filter(acts), [shown])
  const sel = useRowSelection(selectable, uriOfKid)
  useRowSelectionKeys(sel, 'tbl', 'Children table')
  // Selection survives paging and sort by key, but not a drill: the rows
  // picked under one path aren't candidates for a bulk action under another.
  const path = segs.join('/')
  const clearSel = sel.clear
  useEffect(() => clearSel(), [path, clearSel])
  // Deselect on a click anywhere outside the table (or its docked bar) — the
  // plotly "click empty space to clear" convention; without it a selection
  // could only be dropped from inside the section.
  const selCount = sel.selected.size
  useEffect(() => {
    if (!selCount) return
    const onDown = (e: Event) => {
      if (!(e.target as HTMLElement)?.closest('.children-tbl, .sel-bar')) clearSel()
    }
    document.addEventListener('mousedown', onDown)
    return () => document.removeEventListener('mousedown', onDown)
  }, [selCount, clearSel])
  const selKids = kids.filter(k => sel.selected.has(uriOfKid(k)))
  const sendDeps = useActDeps()
  const selPaths = selKids.map(k => [...segs, k.n].join('/'))
  const selAct = useMatchAct({
    ...sendDeps, scheme,
    resolve: () => filter ? rowsCover(filter.resolve, selPaths) : Promise.resolve({ items: selKids.map(k => tableCoverItem(k, [...segs, k.n].join('/'))), complete: true }),
    landed: k => { sendDeps.landed?.(k); sel.clear(); setMemo('') },
  }, `${filter?.key ?? sendDeps.asOf?.()}|${path}`)
  const selFocus = useKeepFocus(selAct.state.s)
  const [hovered, setHovered] = useState<string | null>(null)
  const selBytes = selKids.reduce((s, k) => s + k.b, 0)
  // Everything a row derives from the tree, computed once per page of rows, so a
  // selection change (which re-renders the table) rebuilds only the JSX.
  const rowData = useMemo(() => shown.map(k => {
    const synthetic = k.n.startsWith('(')
    const kidSegs = [...segs, k.n]
    const uri = scheme + kidSegs.join('/')
    // Under a filter a row holding exactly one match shows (and opens) the path down to it, as its treemap
    // tile does (`marin-eu-west4/tomat`); one holding several says how many (`marin-us-east5 · 9 matches`).
    const lab = filter && !synthetic ? rowLabel(k, kidSegs.join('/'), rowMatches?.(kidSegs.join('/')) ?? null) : null
    let end: TreeNode = k
    for (const _ of lab?.segs.slice(1) ?? []) end = end.c![0]
    const to = lab && lab.segs.length > 1 ? rowTarget([...segs, ...lab.segs.slice(0, -1)], end.n, end.k, false) : rowTarget(segs, k.n, k.k, synthetic)
    const label = lab?.label ?? k.n
    const fullSegs = lab ? [...segs, ...lab.segs] : kidSegs
    const mark = staged && !synthetic ? staged.markOf(uri, k.k !== 'file') : null
    return { k, synthetic, kidSegs, fullSegs, label, count: lab?.count, atLeast: !!lab?.atLeast, uri, to, si: selectable.indexOf(k), mark }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [shown, path, scheme, selectable, filter, rowMatches, staged])
  // Every hook above runs on every render: an empty page (a drill can leave
  // no children) must not shorten the hook list, or React throws "Rendered
  // fewer hooks than expected" on the way in.
  if (!kids.length) return null
  // The created cell is three table columns (swatch · month · year), so each
  // part lines up down the page; the year column only exists when a listed
  // row is outside the current year.
  const createdParts = (d: number) => epochDaysToMonthShort(d).split(' ')
  // A generation with bytes-by-age (`ag`) shows each row's age mix as a bar
  // — a mean of mixed stamps (`.cargo`: 2006 crate sources + this month's
  // builds) describes neither — else the mean's swatch · month · year.
  const hasAg = shown.some(k => k.ag)
  const hasYr = !hasAg && shown.some(k => k.d != null && createdParts(k.d).length > 1)
  const createdCols = hasAg ? 1 : hasYr ? 3 : 2
  // Under a filter the selection's trash resolves the selected rows' matches on the click, then turn
  // into the action's progress and accepted state (which outlives the selection it cleared).
  const selBar = showSel && (sel.selected.size > 0 || selAct.state.s !== 'idle') && (
    <span className="sel-bar">
      {sel.selected.size > 0 && <><b>{sel.selected.size}</b> selected · {fmtBytes(selBytes)}</>}
      <span className="acts">
        <span className="mact sel-acts" tabIndex={-1} ref={selFocus.ref} {...selFocus.handlers}>
          {selAct.state.s === 'idle' && <>
            {canTrash && (<>
              <Tooltip content={`Optional: one note for this deletion — why these ${filter ? 'matches' : 'prefixes'} go. Stored with the batch, visible to the admin who dispatches.`}>
                <input className="memo" value={memo} onChange={e => setMemo(e.target.value)} placeholder="note (optional)" aria-label="deletion note" />
              </Tooltip>
              <Tooltip content={filter ? <>Stage the selected rows’ matches for deletion (not the rest of each row) — an admin approves and dispatches from <b>/staged</b></> : <>Stage every selected folder and file for deletion — an admin approves and dispatches from <b>/staged</b></>}>
                <button type="button" className="trash" onClick={() => void selAct.start({ kind: 'stage', memo })} aria-label="trash selected"><FaRegTrashCan /> trash</button>
              </Tooltip>
            </>)}
          </>}
          <ActStatus state={selAct.state} confirmT={selAct.confirmT} act={selAct} fmtBytes={fmtBytes} />
        </span>
        {sel.selected.size > 0 && <button type="button" className="quiet" onClick={sel.clear}>deselect</button>}
      </span>
    </span>
  )
  const pager = pages > 1 && (
    <span className="pg">
      <button type="button" disabled={pg === 0} onClick={() => setPage(0)} aria-label="first page">«</button>
      <button type="button" disabled={pg === 0} onClick={() => setPage(pg - 1)} aria-label="previous page">‹</button>
      <span>{pg * PAGE + 1}–{Math.min(kids.length, (pg + 1) * PAGE)} of {kids.length.toLocaleString('en-US')}</span>
      <button type="button" disabled={pg >= pages - 1} onClick={() => setPage(pg + 1)} aria-label="next page">›</button>
      <button type="button" disabled={pg >= pages - 1} onClick={() => setPage(pages - 1)} aria-label="last page">»</button>
      <select className="psize" value={PAGE} onChange={e => { setNP(+e.target.value); setPage(0) }} aria-label="rows per page">
        {PAGE_SIZES.map(n => <option key={n} value={n}>{n} / page</option>)}
      </select>
    </span>
  )
  // The top bar holds only the pager now; the selection bar docks below the
  // table (sel-bar-dock) so a selection never shifts the rows being clicked.
  // A click on the section's own dead space (not a row / control) deselects.
  const clearOnDeadClick = (e: MouseEvent) => {
    if (sel.selected.size && !(e.target as HTMLElement).closest('tr, button, input, select, a, .sel-bar')) sel.clear()
  }
  return (
    <section className="children-tbl" onClick={clearOnDeadClick}>
      {pager && <div className="pager top">{pager}</div>}
      <table className="worklist selectable">
        <thead>
          <tr>
            {showSel && <th className="col-sel" onClick={e => { e.stopPropagation(); if (!(e.target as HTMLElement).closest('input')) sel.togglePage() }}><input type="checkbox" aria-label="select / deselect this page" title="select / deselect this page (⇧x)" checked={sel.pageAll} onChange={sel.togglePage} /></th>}
            {th('n', 'name', false)}
            {th('b', 'bytes')}
            <th className="num">share</th>
            {th('o', 'objects')}
            <th
              colSpan={createdCols}
              className={(hasAg ? 'agebar ' : 'created ') + 'sortable' + (sort.k === 'd' ? ' on' : '')}
              onClick={() => setSort(s => ({ k: 'd', asc: s.k === 'd' ? !s.asc : false }))}
              title="sort"
            >
              {hasAg ? 'age' : time.label}{sort.k === 'd' ? (sort.asc ? ' ▲' : ' ▼') : ''}
              {dMax > dMin && (
                <Tooltip content={<>
                  <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, whiteSpace: 'nowrap' }}>
                    {epochDaysToMonthShort(dMin)}
                    <span className="gradbar" style={{ background: dateGradientCss(), width: 90, height: 8, borderRadius: 2, display: 'inline-block' }} />
                    {epochDaysToMonthShort(dMax)}
                  </span>
                  <div style={{ opacity: 0.7, marginTop: 3 }}>Swatch colour = each row’s {time.description} (a directory’s byte-weighted mean), old → new, over the rows listed here.</div>
                </>}>
                  <span className="info" tabIndex={0} onClick={e => e.stopPropagation()} aria-label={`about the ${time.label} colour`}> ⓘ</span>
                </Tooltip>
              )}
            </th>
            {hasRead && th('a', 'read', false)}
            {showSel && <th className="actions" aria-label="actions" />}
          </tr>
        </thead>
        <tbody>
          {rowData.map(({ k, synthetic, kidSegs, fullSegs, label, count, atLeast, uri, to, si, mark }) => {
            // A row's hover is intent to act on it (under a filter: its matches are fetched after a short dwell).
            const enter = () => { onBrush?.(k.n); if (filter) setHovered(k.n) }
            const leave = () => { onBrush?.(null); if (filter) setHovered(h => (h === k.n ? null : h)) }
            return (
              <tr key={k.n} ref={si >= 0 ? sel.rowRef(si) : undefined} {...(si >= 0 && showSel ? sel.rowProps(si) : {})}
                {...((onBrush || filter) && !synthetic ? { onMouseEnter: enter, onMouseLeave: leave } : {})}
                data-brushed={brush === k.n || undefined}
                className={mark && mark.as !== 'inside' ? 'staged' : undefined}>
                {showSel && <td className="col-sel" onClick={e => { e.stopPropagation(); if (si >= 0 && !(e.target as HTMLElement).closest('input')) sel.toggle(si) }}>{si >= 0 && <input type="checkbox" aria-label={`select ${label}`} checked={sel.isSelected(k)} onChange={() => sel.toggle(si)} />}</td>}
                <td className="prefix">
                  {(() => {
                    const name = to ? (
                      <a role="link" tabIndex={0}
                        onClick={() => (to.kind === 'open' ? onOpenObject : onOpen)(to.segs)}
                        onKeyDown={e => { if (e.key === 'Enter') (to.kind === 'open' ? onOpenObject : onOpen)(to.segs) }}>
                        {elideMid(label, NAME_MAX)}
                      </a>
                    ) : <span>{elideMid(label, NAME_MAX)}</span>
                    // The full path, only when it says more than the cell: the
                    // name is elided, or the view is drilled (at the root a row
                    // is just its bucket). To the right, clear of the row above.
                    const cell = label.length > NAME_MAX || segs.length > 0
                      ? <Tooltip content={<code className="elide-full">{pathText(scheme, fullSegs, home)}</code>} placement="right">{name}</Tooltip>
                      : name
                    const inside = mark?.as === 'inside' && <Tooltip content={`${mark.n.toLocaleString('en-US')} staged ${mark.n === 1 ? 'item' : 'items'} inside: review them at /staged`}><a className="row-n staged-n" href="/staged"> · {mark.n.toLocaleString('en-US')} staged</a></Tooltip>
                    return count ? <>{cell}<span className="row-n"> · {count.toLocaleString('en-US')}{atLeast ? '+' : ''} {count === 1 && !atLeast ? 'match' : 'matches'}</span>{inside}</> : <>{cell}{inside}</>
                  })()}
                </td>
                <td className="num">{fmtBytes(k.b)}</td>
                <td className="num">{node.b ? ((100 * k.b) / node.b).toFixed(1) : 0}%</td>
                <td className="num">{fmtN(k.o)}</td>
                {hasAg ? <td className="created agebar">{k.ag ? <AgeBar ag={k.ag} d={k.d} /> : '—'}</td>
                : k.d != null ? (() => {
                  const [mon, yr] = createdParts(k.d)
                  return <>
                    <td className="created sw"><i style={{ background: ageInk(k.d) }} /></td>
                    <td className="created mon">{mon}</td>
                    {hasYr && <td className="created yr">{yr ?? ''}</td>}
                  </>
                })() : <td className="created none num" colSpan={createdCols}>—</td>}
                {hasRead && <td title={k.a != null ? 'most recent GET/HEAD/LIST under this prefix (access logs)' : undefined}>
                  {k.a != null ? epochDaysToDate(k.a) : '—'}
                </td>}
                {showSel && (
                  <td className="actions">
                    {!synthetic && filter && (
                      <RowActs key={filter.key} path={kidSegs.join('/')} src={filter} intent={hovered === k.n} scheme={scheme}
                        canTrash={canTrash} fmtBytes={fmtBytes} />
                    )}
                    {!synthetic && !filter && canTrash && (
                      <PrefixStageAction item={tableCoverItem(k, kidSegs.join('/'))} scheme={scheme} fmtBytes={fmtBytes} mark={mark} />
                    )}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
        <tfoot>
          {/* Totals of the LISTED rows. */}
          <tr className="total-row">
            {showSel && <td />}
            <td>total{kids.length !== (node.c ?? []).length ? ` (${kids.length} shown)` : ''}</td>
            <td className="num">{fmtBytes(kids.reduce((s, k) => s + k.b, 0))}</td>
            <td className="num">{node.b ? ((100 * kids.reduce((s, k) => s + k.b, 0)) / node.b).toFixed(1) : 0}%</td>
            <td className="num">{kids.reduce((s, k) => s + k.o, 0).toLocaleString('en-US')}</td>
            <td colSpan={createdCols + (hasRead ? 1 : 0) + (showSel ? 1 : 0)} />
          </tr>
        </tfoot>
      </table>
      {pager && <div className="pager">{pager}</div>}
      {/* The selection bar docks BELOW the table (sticky), so making a
          selection never shifts the rows you're clicking. The dock is ALWAYS
          rendered when the table is actionable — its height is reserved even
          with nothing selected, so selecting/deselecting doesn't jump the rest
          of the page either. */}
      {showSel && <div className="sel-bar-dock">{selBar}</div>}
    </section>
  )
}

/** A row's bytes by age (`TreeNode.ag`) as one stacked bar, newest (yellow)
 * to oldest (purple), each bucket's width its share of the row's bytes; the
 * tip lists the buckets and the mean written date. */
function AgeBar({ ag, d }: { ag: number[]; d?: number }) {
  const { fmtBytes } = useUnits()
  const total = ag.reduce((s, v) => s + v, 0)
  if (!total) return <>—</>
  return (
    <Tooltip content={<div className="agebar-tip">
      {ag.map((v, i) => v > 0 && (
        <div key={i}><i style={{ background: ageBucketColor(i) }} /> {AGE_BUCKETS[i]} <b>{fmtBytes(v)}</b> <span className="dim">{(100 * v / total).toFixed(v / total < 0.1 ? 1 : 0)}%</span></div>
      ))}
      {d != null && <div className="dim">mean written {epochDaysToMonthShort(d)}</div>}
    </div>}>
      <span className="bar" role="img" aria-label={ag.map((v, i) => v > 0 ? `${AGE_BUCKETS[i]} ${Math.round(100 * v / total)}%` : '').filter(Boolean).join(', ')}>
        {ag.map((v, i) => v > 0 && <span key={i} style={{ flexGrow: v, background: ageBucketColor(i) }} />)}
      </span>
    </Tooltip>
  )
}
