import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from 'react'
import { GREW_GREEN, SHRANK_RED, TOUCHED_HATCH, UNCHANGED_GREY, deltaTextColor, statusColors } from './colors'
import type { DiffMetric, DiffTableProps, DiffTableRow } from './types'

type SortKind = 'old' | 'new' | 'delta'
/** `${metricId}_${SortKind}` */
type SortColumn = string
type SortDirection = 'asc' | 'desc'

/** FontAwesome `sort-up` / `sort-down` glyph paths, inlined so the header caret
 *  stays pixel-identical without pulling react-icons into the core package. */
function SortIcon({ dir }: { dir: SortDirection }) {
  return (
    <svg width={10} height={10} viewBox="0 0 320 512" fill="currentColor" style={{ verticalAlign: 'middle' }} aria-hidden>
      {dir === 'desc'
        ? <path d="M41 288h238c21.4 0 32.1 25.9 17 41L177 448c-9.4 9.4-24.6 9.4-34 0L24 329c-15.1-15.1-4.4-41 17-41z" />
        : <path d="M279 224H41c-21.4 0-32.1-25.9-17-41L143 64c9.4-9.4 24.6-9.4 34 0l119 119c15.1 15.1 4.4 41-17 41z" />}
    </svg>
  )
}

// Delta bar component - visual representation of size change
function DeltaBar({ delta, maxDelta }: { delta: number; maxDelta: number }) {
  if (maxDelta === 0) return <div style={{ width: '50px' }} />
  const pct = Math.min(Math.abs(delta) / maxDelta * 100, 100)
  const color = delta === 0 ? 'transparent' : deltaTextColor(delta)
  return (
    <div style={{
      width: '50px',
      height: '8px',
      backgroundColor: 'rgba(255,255,255,0.1)',
      borderRadius: '4px',
      overflow: 'hidden',
      flexShrink: 0,
    }}>
      <div style={{
        width: `${pct}%`,
        height: '100%',
        backgroundColor: color,
        borderRadius: '4px',
      }} />
    </div>
  )
}

// Sortable column header component
function SortHeader({
  label,
  column,
  sortColumn,
  sortDirection,
  onSort,
  style,
}: {
  label: string
  column: SortColumn
  sortColumn: SortColumn | null
  sortDirection: SortDirection
  onSort: (col: SortColumn) => void
  style?: CSSProperties
}) {
  const isActive = sortColumn === column
  return (
    <th
      onClick={() => onSort(column)}
      style={{
        ...style,
        cursor: 'pointer',
        userSelect: 'none',
        fontWeight: 'normal',
        color: isActive ? '#e6edf3' : '#8b949e',
      }}
    >
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: '2px' }}>
        {label}
        {isActive && <SortIcon dir={sortDirection} />}
      </span>
    </th>
  )
}

const td: CSSProperties = { padding: '8px 6px', textAlign: 'right', fontFamily: 'monospace', fontSize: '0.85em', whiteSpace: 'nowrap' }
const dim: CSSProperties = { color: '#8b949e' }

/** Per-metric old/new/Δ/bar cells for one row (added/removed dashes,
 *  dir-only metrics render `-` for files). */
function metricCells(
  metric: DiffMetric,
  vals: { old: number | null; new: number | null; delta: number },
  status: DiffTableRow['status'],
  kind: 'file' | 'dir',
  maxDelta: number,
) {
  const blank = metric.dirOnly && kind !== 'dir'
  const before = blank ? '-' : status === 'added' ? '-' : metric.fmt(vals.old)
  const after = blank ? '-' : status === 'removed' ? '-' : metric.fmt(vals.new)
  const beforeColor = status === 'removed' ? SHRANK_RED : '#8b949e'
  const afterColor = status === 'added' ? GREW_GREEN : undefined
  return (
    <>
      <td style={{ ...td, color: beforeColor, borderLeft: '1px solid rgba(255,255,255,0.1)', paddingLeft: '12px' }}>{before}</td>
      <td style={{ ...td, color: afterColor }}>{after}</td>
      <td style={{ ...td, color: deltaTextColor(vals.delta), fontWeight: vals.delta !== 0 ? 'bold' : undefined }}>
        {blank ? '-' : metric.fmtDelta(vals.delta)}
      </td>
      <td style={{ padding: metric.dirOnly ? '8px 4px' : '8px 12px 8px 4px' }}>
        {blank ? null : <DeltaBar delta={vals.delta} maxDelta={maxDelta} />}
      </td>
    </>
  )
}

/** Parent directory summary row — totals for the directory being compared. */
function ParentSummaryRow({
  parent,
  metrics,
  maxDelta,
  renderIcon,
  rowAction,
}: {
  parent: DiffTableProps['parent']
  metrics: DiffMetric[]
  maxDelta: Record<string, number>
  renderIcon?: DiffTableProps['renderIcon']
  rowAction?: DiffTableProps['rowAction']
}) {
  const { uri } = parent
  // Get the directory name for display
  const dirName = uri === '/' ? '/' : uri.split('/').pop() || uri

  return (
    <tr style={{ backgroundColor: 'rgba(88, 166, 255, 0.1)', borderBottom: '2px solid rgba(255,255,255,0.2)' }}>
      {/* Path */}
      <td style={{ padding: '8px', fontWeight: 'bold' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          {renderIcon?.({ kind: 'dir' })}
          <span style={{ fontFamily: 'monospace', fontSize: '0.9em' }}>. ({dirName})</span>
        </div>
      </td>
      {metrics.map(m => {
        const v = parent.values[m.id] ?? { old: null, new: null, delta: 0 }
        // The parent is the compared dir itself: no added/removed dashes.
        return (
          <MetricGroup key={m.id}>
            <td style={{ ...td, ...dim, borderLeft: '1px solid rgba(255,255,255,0.1)', paddingLeft: '12px' }}>{m.fmt(v.old)}</td>
            <td style={td}>{m.fmt(v.new)}</td>
            <td style={{ ...td, color: deltaTextColor(v.delta), fontWeight: v.delta !== 0 ? 'bold' : undefined }}>
              {m.fmtDelta(v.delta)}
            </td>
            <td style={{ padding: m.dirOnly ? '8px 4px' : '8px 12px 8px 4px' }}>
              <DeltaBar delta={v.delta} maxDelta={Math.max(maxDelta[m.id], Math.abs(v.delta))} />
            </td>
          </MetricGroup>
        )
      })}
      {/* Action */}
      <td style={{ padding: '4px 8px', textAlign: 'center', borderLeft: '1px solid rgba(255,255,255,0.1)' }}>
        {rowAction?.({ uri, kind: 'dir', path: uri })}
      </td>
    </tr>
  )
}

// `<>` wrapper that keeps `<td>`s adjacent for TS without adding DOM nodes.
function MetricGroup({ children }: { children: ReactNode }) {
  return <>{children}</>
}

function DiffRow({
  row,
  metrics,
  maxDelta,
  renderIcon,
  renderPathLink,
  rowAction,
}: {
  row: DiffTableRow
  metrics: DiffMetric[]
  maxDelta: Record<string, number>
  renderIcon?: DiffTableProps['renderIcon']
  renderPathLink?: DiffTableProps['renderPathLink']
  rowAction?: DiffTableProps['rowAction']
}) {
  const { bg } = statusColors[row.status]
  const label = <span style={{ fontFamily: 'monospace', fontSize: '0.9em' }}>{row.path}</span>
  const pathEl = row.kind === 'dir' && renderPathLink ? renderPathLink(row, row.path) : label

  return (
    <tr style={{ backgroundColor: bg, borderBottom: '1px solid rgba(255,255,255,0.05)' }}>
      {/* Path */}
      <td style={{ padding: '8px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
          {renderIcon?.({ kind: row.kind })}
          {pathEl}
          {row.status === 'touched' && (
            <span
              title="same bytes & count, mtime moved (rename / net-zero churn / touch)"
              style={{
                fontSize: '0.7em', color: '#8b949e', padding: '0 6px', borderRadius: 3,
                background: UNCHANGED_GREY, backgroundImage: TOUCHED_HATCH,
              }}
            >
              touched
            </span>
          )}
        </div>
      </td>
      {metrics.map(m => (
        <MetricGroup key={m.id}>
          {metricCells(m, row.values[m.id] ?? { old: null, new: null, delta: 0 }, row.status, row.kind, maxDelta[m.id])}
        </MetricGroup>
      ))}
      {/* Action */}
      <td style={{ padding: '4px 8px', textAlign: 'center', borderLeft: '1px solid rgba(255,255,255,0.1)' }}>
        {rowAction?.({ uri: row.uri, kind: row.kind, path: row.path })}
      </td>
    </tr>
  )
}

export function DiffTable({
  rows,
  parent,
  metrics,
  unchangedCount,
  pageSize = 50,
  renderIcon,
  renderPathLink,
  rowAction,
}: DiffTableProps) {
  const [sortColumn, setSortColumn] = useState<SortColumn | null>(`${metrics[0].id}_delta`)
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc')
  const [page, setPage] = useState(0)

  useEffect(() => { setPage(0) }, [rows, sortColumn, sortDirection])

  const handleSort = (col: SortColumn) => {
    if (sortColumn === col) {
      setSortDirection(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortColumn(col)
      setSortDirection('desc')
    }
  }

  // Filter out unchanged rows for cleaner view
  const changedRows = rows.filter(r => r.status !== 'unchanged')

  // Sort rows
  const sortedRows = useMemo(() => {
    if (!sortColumn) return changedRows
    const i = sortColumn.lastIndexOf('_')
    const mid = sortColumn.slice(0, i)
    const kind = sortColumn.slice(i + 1) as SortKind
    const valOf = (r: DiffTableRow) => {
      const v = r.values[mid] ?? { old: null, new: null, delta: 0 }
      return kind === 'delta' ? Math.abs(v.delta) : kind === 'old' ? (v.old ?? 0) : (v.new ?? 0)
    }
    return [...changedRows].sort((a, b) => {
      const aVal = valOf(a), bVal = valOf(b)
      return sortDirection === 'desc' ? bVal - aVal : aVal - bVal
    })
  }, [changedRows, sortColumn, sortDirection])

  // Find max deltas for scaling bars (per metric)
  const maxDelta: Record<string, number> = {}
  for (const m of metrics) {
    maxDelta[m.id] = Math.max(...changedRows.map(r => Math.abs((r.values[m.id]?.delta ?? 0))), 1)
  }

  const subTh: CSSProperties = { padding: '4px 6px', textAlign: 'right', fontSize: '0.75em', whiteSpace: 'nowrap' }
  const cols = 1 + metrics.length * 4 + 1

  return (
    <table style={{ width: '100%', borderCollapse: 'collapse' }}>
      {/* Header rows stick to the top of the viewport while the page scrolls
          past a long table (`#0d1117` so rows don't show through). */}
      <thead style={{ position: 'sticky', top: 0, zIndex: 2, background: '#0d1117' }}>
        <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.1)' }}>
          <th style={{ textAlign: 'left', padding: '8px', width: '100%' }}>Path</th>
          {metrics.map(m => (
            <th key={m.id} colSpan={4} style={{ textAlign: 'center', padding: '8px 12px', borderLeft: '1px solid rgba(255,255,255,0.1)', whiteSpace: 'nowrap' }}>{m.label}</th>
          ))}
          <th style={{ padding: '8px', borderLeft: '1px solid rgba(255,255,255,0.1)', whiteSpace: 'nowrap' }}></th>
        </tr>
        <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.1)' }}>
          <th style={{ width: '100%' }}></th>
          {metrics.map(m => (
            <MetricGroup key={m.id}>
              <SortHeader label="old" column={`${m.id}_old`} sortColumn={sortColumn} sortDirection={sortDirection} onSort={handleSort} style={{ ...subTh, borderLeft: '1px solid rgba(255,255,255,0.1)', paddingLeft: '12px' }} />
              <SortHeader label="new" column={`${m.id}_new`} sortColumn={sortColumn} sortDirection={sortDirection} onSort={handleSort} style={subTh} />
              <SortHeader label="Δ" column={`${m.id}_delta`} sortColumn={sortColumn} sortDirection={sortDirection} onSort={handleSort} style={subTh} />
              <th style={subTh}></th>
            </MetricGroup>
          ))}
          <th style={{ whiteSpace: 'nowrap' }}></th>
        </tr>
      </thead>
      <tbody>
        {/* Parent directory summary row */}
        <ParentSummaryRow
          parent={parent}
          metrics={metrics}
          maxDelta={maxDelta}
          renderIcon={renderIcon}
          rowAction={rowAction}
        />
        {sortedRows.slice(page * pageSize, (page + 1) * pageSize).map((row) => (
          <DiffRow
            key={row.key}
            row={row}
            metrics={metrics}
            maxDelta={maxDelta}
            renderIcon={renderIcon}
            renderPathLink={renderPathLink}
            rowAction={rowAction}
          />
        ))}
        {sortedRows.length === 0 && (
          <tr>
            <td colSpan={cols} style={{ padding: '24px', textAlign: 'center', color: '#8b949e' }}>
              No changes detected between scans
            </td>
          </tr>
        )}
        {/* The treemap draws unchanged children as grey context; the table
            lists changes only — say what it's leaving out so they agree. */}
        {(sortedRows.length > pageSize || unchangedCount > 0) && (
          <tr>
            <td colSpan={cols} style={{ padding: '6px 8px', color: '#8b949e', fontSize: '0.8em' }}>
              {sortedRows.length > pageSize && (
                <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, marginRight: 16 }}>
                  {page * pageSize + 1}–{Math.min((page + 1) * pageSize, sortedRows.length)} of {sortedRows.length.toLocaleString()}
                  {(['«', '‹', '›', '»'] as const).map((g, i) => {
                    const last = Math.ceil(sortedRows.length / pageSize) - 1
                    const to = [0, page - 1, page + 1, last][i]
                    const off = to < 0 || to > last || to === page
                    return (
                      <button
                        key={g}
                        disabled={off}
                        onClick={() => setPage(to)}
                        style={{ background: 'none', border: 'none', color: off ? '#555' : '#54aeff', cursor: off ? 'default' : 'pointer', fontSize: '1em', padding: '0 2px' }}
                      >
                        {g}
                      </button>
                    )
                  })}
                </span>
              )}
              {unchangedCount > 0 && (
                <>{unchangedCount.toLocaleString()} unchanged {unchangedCount === 1 ? 'entry' : 'entries'} not listed</>
              )}
            </td>
          </tr>
        )}
      </tbody>
    </table>
  )
}
