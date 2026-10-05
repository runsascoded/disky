// A table of arbitrary prefixes with their numbers at a scan: name, bytes,
// share, objects, owner(s), created, read — the columns the map's
// ChildrenTable shows — plus each page's own extra columns (who staged it and
// when; who assigned it) and a leading / trailing cell (selection, actions).
// `/staged` renders its items with it; the action log and ChildrenTable are
// meant to converge on it.
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { Tooltip } from './Tooltip'
import { OwnerFactChip } from './OwnerFactChip'
import { OwnerBar, ownerShares } from './OwnerBar'
import { useUnits } from './units'
import { fmtN, type TreeNode } from './types'
import { dateColor } from './colors'
import type { OwnerIndex } from './owners'
import type { UserIndexEntry } from './colors'
import { type PrefixSortKey, type PrefixStat, relAgo } from './prefixes'

export interface PrefixRow {
  /** The prefix itself (`gs://bucket/path/`) — the row's key and name. */
  name: string
  /** Where the name links (the map at that path); absent = plain text. */
  to?: string
  stat?: PrefixStat
}

/** A page's own column: its header, its cell, and (sortable) its value. */
export interface ExtraCol<R> {
  key: string
  label: string
  cell: (r: R) => ReactNode
  sort?: (r: R) => number | string | undefined
  className?: string
}

const iso = (s: number) => new Date(s * 1000).toISOString().replace('.000Z', 'Z')

/** A time cell: relative ("5w ago"), the absolute time on hover (the date
 *  alone for a day-precision value), and an optional age-ink swatch (oldest →
 *  newest across the listed rows). */
export function TimeCell({ ts, ink, day }: { ts?: number; ink?: string; day?: boolean }) {
  if (ts == null) return <span className="dim">—</span>
  return (
    <Tooltip content={day ? iso(ts).slice(0, 10) : iso(ts)}>
      <span className="when">{ink && <i className="sw" style={{ background: ink }} />}{relAgo(ts)}</span>
    </Tooltip>
  )
}

export function PrefixTable<R extends PrefixRow>({ rows, sort, onSort, shareOf, userIdx, ownerIdx, extra = [], lead, trail, rowProps, loading, emptyLabel = 'empty' }: {
  rows: R[]
  sort: { k: PrefixSortKey; asc: boolean }
  onSort: (k: PrefixSortKey) => void
  /** The bytes a row's share is of (absent: no share column). */
  shareOf?: number
  userIdx?: Map<string, UserIndexEntry>
  /** The ownership ledger: an assigned prefix shows its assignee. */
  ownerIdx?: OwnerIndex | null
  extra?: ExtraCol<R>[]
  /** A leading column (selection): its header cell and each row's cell. */
  lead?: { header: ReactNode; cell: (r: R, i: number) => ReactNode }
  /** A trailing cell (row actions). */
  trail?: (r: R, i: number) => ReactNode
  /** Per-row `<tr>` props (selection handlers, ref). */
  rowProps?: (r: R, i: number) => Record<string, unknown>
  /** Stats still loading: numeric cells show an ellipsis, not "empty". */
  loading?: boolean
  /** A row with no stats once loaded (nothing under it at this scan). */
  emptyLabel?: string
}) {
  const { fmtBytes } = useUnits()
  // Created ink across the listed rows' range, as the map's table does.
  const ds = rows.map(r => r.stat?.d).filter((d): d is number => d != null)
  const [dMin, dMax] = ds.length ? [Math.min(...ds), Math.max(...ds)] : [0, 0]
  const ink = (d: number) => dateColor(dMax > dMin ? (d - dMin) / (dMax - dMin) : 1)
  const th = (k: PrefixSortKey, label: string, num = true) => (
    <th key={k} className={(num ? 'num ' : '') + 'sortable' + (sort.k === k ? ' on' : '')} title="sort" onClick={() => onSort(k)}>
      {label}{sort.k === k ? (sort.asc ? ' ▲' : ' ▼') : ''}
    </th>
  )
  return (
    <table className="prefix-table">
      <thead>
        <tr>
          {lead && <th className="col-lead">{lead.header}</th>}
          {th('name', 'prefix', false)}
          {th('b', 'size')}
          {shareOf != null && <th className="num">share</th>}
          {th('o', 'objects')}
          <th>owner(s)</th>
          {th('d', 'created')}
          {th('a', 'read')}
          {extra.map(c => c.sort ? th(c.key, c.label, false) : <th key={c.key}>{c.label}</th>)}
          {trail && <th />}
        </tr>
      </thead>
      <tbody>
        {rows.map((r, i) => {
          const s = r.stat
          const cl = ownerIdx?.count ? ownerIdx.claimOf(r.name) : null
          const node: TreeNode | null = s ? { n: r.name, b: s.b, o: s.o, ...(s.us ? { us: s.us } : {}) } : null
          const shares = node ? ownerShares(node) : []
          const none = loading ? '…' : <span className="dim">{emptyLabel}</span>
          return (
            <tr key={r.name} {...rowProps?.(r, i)}>
              {lead && <td className="col-lead">{lead.cell(r, i)}</td>}
              <td className="pfx">{r.to ? <Link to={r.to} title="open in the map"><code>{r.name}</code></Link> : <code>{r.name}</code>}</td>
              <td className="num">{s ? fmtBytes(s.b) : none}</td>
              {shareOf != null && <td className="num">{s && shareOf > 0 ? `${((100 * s.b) / shareOf).toFixed(1)}%` : ''}</td>}
              <td className="num">{s ? fmtN(s.o) : ''}</td>
              <td className="owners">
                {cl ? <OwnerFactChip who={cl.who} assigned={{ by: cl.by, ts: cl.ts, memo: cl.memo }} />
                  : node && shares.length === 1 && shares[0][1] >= 0.98 * node.b ? <OwnerFactChip who={shares[0][0]} />
                  : node && shares.length ? <OwnerBar node={node} userIdx={userIdx} width={70} />
                  : <span className="dim">—</span>}
              </td>
              <td className="created"><TimeCell day ts={s?.d != null ? s.d * 86400 : undefined} ink={s?.d != null ? ink(s.d) : undefined} /></td>
              <td className="read"><TimeCell day ts={s?.a != null ? s.a * 86400 : undefined} /></td>
              {extra.map(c => <td key={c.key} className={c.className}>{c.cell(r)}</td>)}
              {trail && <td className="actions">{trail(r, i)}</td>}
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
