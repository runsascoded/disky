import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactElement } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { Alert, Box, Button, Checkbox, CircularProgress, Collapse, TextField, Tooltip } from '@mui/material'
import { FaChevronDown, FaChevronRight, FaExclamationTriangle, FaExchangeAlt, FaFileAlt, FaFolder, FaFolderOpen, FaSync, FaSortUp, FaSortDown, FaTrash, FaSearch, FaRegCopy, FaCheck } from 'react-icons/fa'
import { useRowSelection, useRowSelectionKeys } from '../hooks/useRowSelection'
import type { RowSelection } from '../hooks/useRowSelection'
import { AgeHistograms, age01, ageDomain, ageFade, BytesOverTime, dimUnmatched, parseQuery, StalenessScatter, Treemap as DTTreemap } from '@disk-tree/react'
import '@rdub/treemap/styles.css'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { fetchScanDetails, fetchScanHistory, fetchHistogram, fetchFilter, startScan, fetchScanStatus, deletePath, stageUris, revealPath, fetchFilePreview, DEFAULT_MAX_ROWS } from '../api'
import type { FilterResult, HistogramChild, Row, ScanJob, ScanProgress, CollapsedRow } from '../api'
import { VoronoiTreemap } from '@rdub/treemap/voronoi'
import { VizBoundary } from './VizBoundary'
import { useCapabilities } from '../hooks/useCapabilities'
import { useNarrow } from '../hooks/useNarrow'
import { elideMiddle } from '../staged'
import { useDeleteMethod } from '../hooks/useDeleteMethod'
import { useScanProgress } from '../hooks/useScanProgress'
import { useRecentPaths } from '../hooks/useRecentPaths'
import { formatSize, formatCount, timeAgo, elapsed } from '../utils/format'
import { useTiling } from '../utils/tiling'
import { useRenderer } from '../utils/renderer'
import {
  childLinkPrefix,
  detectRouteType,
  isSchemeRoot,
  SCHEMES,
  schemeLanding,
  segmentsToUri,
  supportsDelete,
  uriToPath,
  type RouteType,
} from '../schemes'

type SortKey = 'kind' | 'path' | 'size' | 'mtime' | 'n_children' | 'n_desc' | 'scanned'

/** Which visualization the panel under the table renders. */
type Viz = 'treemap' | 'scatter' | 'histograms' | 'voronoi'

const VIZ_LABELS: Record<Viz, string> = {
  treemap: 'treemap',
  scatter: 'staleness scatter',
  histograms: 'age histograms',
  voronoi: 'voronoi treemap',
}
type SortDir = 'asc' | 'desc'
type SortSpec = { key: SortKey; dir: SortDir }

function ScanProgressBanner({ progress, currentUri }: { progress: ScanProgress[]; currentUri: string }) {
  const [, setTick] = useState(0)

  // Find scans that are relevant to this path (exact match, ancestor, or descendant)
  const relevantScans = progress.filter(p => {
    const scanPath = p.path
    // Exact match
    if (scanPath === currentUri) return true
    // Scan is ancestor of current (current is inside scan)
    if (currentUri.startsWith(scanPath + '/')) return true
    // Scan is descendant of current (scan is inside current)
    if (scanPath.startsWith(currentUri + '/')) return true
    return false
  })

  // Tick every second so elapsed times update live
  useEffect(() => {
    if (relevantScans.length === 0) return
    const id = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(id)
  }, [relevantScans.length])

  if (relevantScans.length === 0) return null

  return (
    <Alert severity="info" sx={{ mb: 2 }} icon={<CircularProgress size={20} />}>
      {relevantScans.map(scan => (
        <Box key={scan.id} sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
          <strong>Scanning:</strong>
          <code style={{ fontSize: '0.9em' }}>{scan.path}</code>
          <span>{formatCount(scan.items_found)} items</span>
          {scan.items_per_sec && <span>({formatCount(Math.round(scan.items_per_sec))}/sec)</span>}
          {scan.error_count > 0 && <span style={{ color: '#ed6c02' }}>{scan.error_count} errors</span>}
          <span>{elapsed(scan.started)}</span>
        </Box>
      ))}
    </Alert>
  )
}

function Breadcrumbs({ uri, routeType }: { uri: string; routeType: RouteType }) {
  const isFile = routeType === 'file'
  const prefix = isFile ? '/file' : `/${routeType}`
  const displayUri = isFile ? uri : uri.replace(`${routeType}://`, '')

  const segments = displayUri.split('/').filter(Boolean)
  const paths = segments.reduce((acc, seg) => {
    const prev = acc[acc.length - 1]
    acc.push(prev ? `${prev}/${seg}` : seg)
    return acc
  }, [] as string[])

  return (
    <div className="breadcrumbs">
      {!isFile && (() => {
        // The top crumb links up to the scheme's landing (a dedicated
        // bucket-list page like /s3, else the `/` union root) — driven by the
        // SCHEMES registry, so no scheme is special-cased here.
        const up = schemeLanding(routeType)
        const { label } = SCHEMES[routeType]
        return up ? <Link to={up}>{label}</Link> : <span>{label}</span>
      })()}
      {isFile && <Link to="/file/" className="breadcrumb-sep">/</Link>}
      {paths.map((path, idx) => (
        <span key={idx}>
          {isFile && idx > 0 && <span className="breadcrumb-sep">/</span>}
          {idx === paths.length - 1 ? (
            <span>{segments[idx]}</span>
          ) : (
            <Link to={`${prefix}/${path}`}>{segments[idx]}</Link>
          )}
          {!isFile && idx < paths.length - 1 && <span className="breadcrumb-sep">/</span>}
        </span>
      ))}
    </div>
  )
}

function RowIcon({ row }: { row: Row }) {
  if (row.kind === 'file') return <FaFileAlt />
  if (row.scanned === true) return <FaFolder style={{ color: '#4caf50' }} />
  if (row.scanned === 'partial') return <FaFolderOpen style={{ color: '#ff9800' }} />
  return <FaFolder style={{ opacity: 0.5 }} />
}

function scanTimeAgo(scanTime: string | undefined): string {
  if (!scanTime) return '-'
  const date = new Date(scanTime)
  const now = new Date()
  const seconds = Math.floor((now.getTime() - date.getTime()) / 1000)

  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

function SortableHeader({
  label,
  sortKey,
  sorts,
  onSort,
  tooltip,
  className,
}: {
  label: string
  sortKey: SortKey
  sorts: SortSpec[]
  onSort: (key: SortKey) => void
  tooltip?: string
  className?: string
}) {
  const primarySort = sorts[0]
  const isPrimary = primarySort?.key === sortKey
  const isSecondary = sorts.length > 1 && sorts[1]?.key === sortKey

  const header = (
    <th
      className={className}
      onClick={() => onSort(sortKey)}
      style={{ cursor: 'pointer', userSelect: 'none' }}
    >
      {label}
      {isPrimary && (
        primarySort.dir === 'asc'
          ? <FaSortUp style={{ marginLeft: 4, verticalAlign: 'middle' }} />
          : <FaSortDown style={{ marginLeft: 4, verticalAlign: 'middle' }} />
      )}
      {isSecondary && (
        <span style={{ opacity: 0.4, marginLeft: 2 }}>
          {sorts[1].dir === 'asc' ? <FaSortUp style={{ verticalAlign: 'middle', fontSize: '0.7em' }} /> : <FaSortDown style={{ verticalAlign: 'middle', fontSize: '0.7em' }} />}
        </span>
      )}
    </th>
  )

  return tooltip ? <Tooltip title={tooltip}>{header}</Tooltip> : header
}

/**
 * Small click-to-copy button. Writes `text` to the clipboard and briefly
 * swaps the icon to a check. Clipboard access can throw (insecure origin,
 * denied permission) — swallow it and leave the icon unchanged.
 */
function CopyButton({ text, title = 'Copy', size = 12 }: { text: string; title?: string; size?: number }) {
  const [copied, setCopied] = useState(false)
  return (
    <Tooltip title={copied ? 'Copied!' : title}>
      <button
        type="button"
        aria-label={title}
        onClick={async e => {
          e.stopPropagation()
          try {
            await navigator.clipboard.writeText(text)
            setCopied(true)
            setTimeout(() => setCopied(false), 1200)
          } catch {
            /* clipboard unavailable (insecure origin / denied) — no-op */
          }
        }}
        style={{
          background: 'none', border: 'none', padding: '0 4px', cursor: 'pointer',
          color: 'inherit', opacity: copied ? 1 : 0.6, display: 'inline-flex',
          alignItems: 'center', verticalAlign: 'middle',
        }}
      >
        {copied ? <FaCheck size={size} color="#4caf50" /> : <FaRegCopy size={size} />}
      </button>
    </Tooltip>
  )
}

function PermissionErrorWarning({ errorCount, errorPaths, scanPath }: { errorCount: number; errorPaths: string[] | null; scanPath: string | null }) {
  const [expanded, setExpanded] = useState(false)
  const displayPaths = errorPaths?.slice(0, 10) ?? []
  const hasMore = errorPaths && errorPaths.length > 10

  return (
    <Alert
      severity="warning"
      icon={<FaExclamationTriangle />}
      sx={{ mb: 2, '& .MuiAlert-message': { width: '100%' } }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, cursor: 'pointer' }} onClick={() => setExpanded(!expanded)}>
        {displayPaths.length > 0 && (expanded ? <FaChevronDown size={12} /> : <FaChevronRight size={12} />)}
        <span>
          <strong>{errorCount} permission error{errorCount !== 1 ? 's' : ''}</strong> during scan.
          Some directories were not indexed.
        </span>
      </Box>
      <Collapse in={expanded}>
        {displayPaths.length > 0 && (
          <Box sx={{ mt: 1, ml: 2 }}>
            <div style={{ fontFamily: 'monospace', fontSize: '0.8rem', opacity: 0.9 }}>
              {displayPaths.map((p, i) => <div key={i}>{p}</div>)}
              {hasMore && <div style={{ opacity: 0.6 }}>... and {errorPaths!.length - 10} more</div>}
            </div>
          </Box>
        )}
      </Collapse>
      <Box sx={{ mt: 1, fontSize: '0.85rem', opacity: 0.85 }}>
        Tip: Run <code style={{ background: 'rgba(0,0,0,0.1)', padding: '2px 4px', borderRadius: 3 }}>disk-tree index --sudo {scanPath ?? '<path>'}</code>
        <CopyButton text={`disk-tree index --sudo ${scanPath ?? '<path>'}`} title="Copy command" />
        for full access.
      </Box>
    </Alert>
  )
}

function ChildScanStatus({ row, scanStatus, parentScanTime }: { row: Row; scanStatus: 'full' | 'partial' | 'none'; parentScanTime: string | null }) {
  // If parent was fully scanned, children inherit that scan time (shown grayed)
  if (scanStatus === 'full' && !row.scan_time && parentScanTime) {
    return <span style={{ opacity: 0.5 }}>{scanTimeAgo(parentScanTime)}</span>
  }
  if (row.scanned === 'partial') {
    return <span style={{ color: '#ff9800' }}>partial</span>
  }
  if (row.scan_time) {
    return <span>{scanTimeAgo(row.scan_time)}</span>
  }
  return <span style={{ opacity: 0.4 }}>-</span>
}

/** A row's name; at phone width middle-elided (the extension survives) with
 *  the full name on hover / long-press. */
function PathName({ name, narrow }: { name: string; narrow: boolean }) {
  const shown = narrow ? elideMiddle(name, 26, 9) : name
  if (shown === name) return <code>{name}</code>
  return (
    <Tooltip title={name} placement="top" arrow enterTouchDelay={0} leaveTouchDelay={3000}>
      <code>{shown}</code>
    </Tooltip>
  )
}

function DetailsTable({ root, rootLabel, children, uri, routeType, onScanChild, scanningPaths, scanStatus, scanTime, onRescan, isScanning, sorts, onSort, onDelete, deletingPaths, sel, collapsedRows, tableRef }: {
  root: Row
  /** The root row's Path cell — the location's basename (`gbfs`), not `.`. */
  rootLabel: string
  children: Row[]
  uri: string
  routeType: RouteType
  onScanChild: (path: string) => void
  scanningPaths: Set<string>
  scanStatus: 'full' | 'partial' | 'none'
  scanTime: string | null
  onRescan: () => void
  isScanning: boolean
  sorts: SortSpec[]
  onSort: (key: SortKey) => void
  onDelete: (path: string) => void
  deletingPaths: Set<string>
  sel: RowSelection<Row>
  collapsedRows?: CollapsedRow[] | null
  tableRef?: React.RefObject<HTMLTableElement | null>
}) {
  const caps = useCapabilities()
  // The trash gesture syncs or stages per the deployment's `deleteApproval`
  // policy (spec `staged-delete.md` CP6), not the scheme.
  const { method } = useDeleteMethod()
  const deletable = supportsDelete(routeType)
  const canSync = deletable && caps?.delete === true && method === 'sync'
  const staging = deletable && caps?.stageDelete === true && method === 'staged'
  const canAct = canSync || staging
  const actionVerb = staging ? 'Stage' : 'Delete'
  const actionColor = staging ? '#ed6c02' : '#d32f2f'
  // Track whether the collapsed (auto-expanded) rows are shown expanded
  const [collapsedExpanded, setCollapsedExpanded] = useState(true)

  // Build prefix for child links, avoiding double slashes
  // For root (/), prefix should be /file not /file/
  const prefix = childLinkPrefix(uri)
  const allSelected = children.length > 0 && children.every(r => sel.isSelected(r))
  const someSelected = children.some(r => sel.isSelected(r))
  // Phone width: the name is what matters — Modified/Children/Desc/Scanned
  // hide (`.col-wide`) and names middle-elide with the full name in a tooltip.
  const ownRef = useRef<HTMLTableElement | null>(null)
  const narrow = useNarrow(tableRef ?? ownRef)

  return (
    <table className={`scan-details-table${narrow ? ' narrow' : ''}`} ref={tableRef ?? ownRef}>
      <thead>
        <tr>
          <th className="col-checkbox">
            <Checkbox
              size="small"
              checked={allSelected}
              indeterminate={someSelected && !allSelected}
              onChange={sel.togglePage}
              sx={{ padding: 0 }}
            />
          </th>
          <SortableHeader className="col-icon" label="" sortKey="kind" sorts={sorts} onSort={onSort} tooltip="Sort by type (file/folder)" />
          <SortableHeader className="col-path" label="Path" sortKey="path" sorts={sorts} onSort={onSort} />
          <SortableHeader className="col-numeric" label="Size" sortKey="size" sorts={sorts} onSort={onSort} tooltip="Total size including all nested files and directories" />
          <SortableHeader className="col-numeric col-wide" label="Modified" sortKey="mtime" sorts={sorts} onSort={onSort} tooltip="Most recent modification time of any file in this directory tree" />
          <SortableHeader className="col-numeric col-wide" label="Children" sortKey="n_children" sorts={sorts} onSort={onSort} tooltip="Number of direct children (files and subdirectories)" />
          <SortableHeader className="col-numeric col-wide" label="Desc." sortKey="n_desc" sorts={sorts} onSort={onSort} tooltip="Total number of descendants (all nested files and directories)" />
          <SortableHeader className="col-numeric col-wide" label="Scanned" sortKey="scanned" sorts={sorts} onSort={onSort} tooltip="When this directory was last scanned" />
          <th className="col-action"></th>
          {canAct && <th className="col-action"></th>}
        </tr>
      </thead>
      <tbody>
        <tr className="root">
          <td className="col-checkbox">
            {caps?.compare && (
              <Tooltip title="Compare scans">
                <Link to={`/compare${uriToPath(uri)}`}>
                  <Button size="small" sx={{ minWidth: 0, padding: '2px 4px' }}>
                    <FaExchangeAlt size={12} />
                  </Button>
                </Link>
              </Tooltip>
            )}
          </td>
          <td className="col-icon">{root.kind === 'file' ? <FaFileAlt /> : <FaFolder />}</td>
          <td className="col-path"><code>{rootLabel}</code></td>
          <td className="col-numeric">{formatSize(root.size)}</td>
          <td className="col-numeric col-wide">{timeAgo(root.mtime)}</td>
          <td className="col-numeric col-wide">{root.n_children?.toLocaleString()}</td>
          <td className="col-numeric col-wide">{root.n_desc && root.n_desc > 1 ? root.n_desc.toLocaleString() : null}</td>
          <td className="col-numeric col-wide">
            {scanStatus === 'full' && scanTime ? (
              <span>{scanTimeAgo(scanTime)}</span>
            ) : scanStatus === 'partial' ? (
              <Tooltip title="Some subdirectories have been scanned, but not this directory itself">
                <span style={{ color: '#ff9800' }}>partial</span>
              </Tooltip>
            ) : (
              <Tooltip title="This directory has not been scanned yet">
                <span style={{ opacity: 0.4 }}>-</span>
              </Tooltip>
            )}
          </td>
          <td className="col-action" style={{ display: 'flex', gap: '4px' }}>
            {caps?.scan && (
              <Tooltip title={scanStatus === 'full' ? 'Rescan this directory' : 'Scan this directory'}>
                <span>
                  <Button
                    size="small"
                    onClick={onRescan}
                    disabled={isScanning}
                    sx={{ minWidth: 0, padding: '2px 4px' }}
                  >
                    {isScanning ? <CircularProgress size={14} /> : <FaSync size={12} />}
                  </Button>
                </span>
              </Tooltip>
            )}
          </td>
          {canAct && (
            <td className="col-action">
              <Tooltip title={`${actionVerb} this ${root.kind === 'file' ? 'file' : 'directory'}`}>
                <span>
                  <Button
                    size="small"
                    onClick={() => onDelete(uri)}
                    disabled={deletingPaths.has(uri)}
                    sx={{ minWidth: 0, padding: '2px 4px', color: actionColor }}
                  >
                    {deletingPaths.has(uri) ? <CircularProgress size={14} /> : <FaTrash size={12} />}
                  </Button>
                </span>
              </Tooltip>
            </td>
          )}
        </tr>
        {/* Render collapsed/expanded parent rows (auto-expanded single-child dirs) */}
        {collapsedRows && collapsedRows.map((collapsedRow, depth) => {
          const indentPx = depth * 20
          const collapsedUri = collapsedRow.uri
          const isScanning = scanningPaths.has(collapsedUri)
          const isDeleting = deletingPaths.has(collapsedUri)
          const isFirst = depth === 0
          // Extract just the segment name from the path (last part)
          const segment = collapsedRow.original_path.split('/').pop() || collapsedRow.original_path
          // First row: clickable chevron to toggle; only show subsequent rows when expanded
          if (!isFirst && !collapsedExpanded) return null
          const ChevronIcon = collapsedExpanded ? FaChevronDown : FaChevronRight
          return (
            <tr key={`collapsed-${depth}`} className="collapsed-parent" style={{ background: 'rgba(100, 100, 100, 0.08)' }}>
              <td className="col-checkbox"></td>
              <td className="col-icon">
                <span style={{ paddingLeft: indentPx, display: 'inline-flex', alignItems: 'center' }}>
                  {isFirst ? (
                    <ChevronIcon
                      size={10}
                      style={{ marginRight: 4, opacity: 0.7, cursor: 'pointer' }}
                      onClick={() => setCollapsedExpanded(prev => !prev)}
                    />
                  ) : (
                    <FaChevronDown size={10} style={{ marginRight: 4, opacity: 0.5 }} />
                  )}
                  <FaFolderOpen style={{ color: '#ff9800' }} />
                </span>
              </td>
              <td className="col-path">
                <Link to={`${prefix}/${collapsedRow.original_path}`}>
                  <code>{segment}</code>
                </Link>
              </td>
              <td className="col-numeric">{formatSize(collapsedRow.size)}</td>
              <td className="col-numeric col-wide">{timeAgo(collapsedRow.mtime)}</td>
              <td className="col-numeric col-wide">{collapsedRow.n_children?.toLocaleString()}</td>
              <td className="col-numeric col-wide">{collapsedRow.n_desc && collapsedRow.n_desc > 1 ? collapsedRow.n_desc.toLocaleString() : null}</td>
              <td className="col-numeric col-wide">{scanStatus === 'full' && scanTime ? scanTimeAgo(scanTime) : null}</td>
              <td className="col-action">
                {caps?.scan && (
                  <Tooltip title="Rescan this directory">
                    <span>
                      <Button
                        size="small"
                        onClick={() => onScanChild(collapsedUri)}
                        disabled={isScanning}
                        sx={{ minWidth: 0, padding: '2px 4px' }}
                      >
                        {isScanning ? <CircularProgress size={14} /> : <FaSync size={12} />}
                      </Button>
                    </span>
                  </Tooltip>
                )}
              </td>
              {canAct && (
                <td className="col-action">
                  <Tooltip title={`${actionVerb} directory`}>
                    <span>
                      <Button
                        size="small"
                        onClick={() => onDelete(collapsedUri)}
                        disabled={isDeleting}
                        sx={{ minWidth: 0, padding: '2px 4px', color: actionColor }}
                      >
                        {isDeleting ? <CircularProgress size={14} /> : <FaTrash size={12} />}
                      </Button>
                    </span>
                  </Tooltip>
                </td>
              )}
            </tr>
          )
        })}
        {/* Only show children when collapsed rows are expanded (or there are no collapsed rows) */}
        {(!collapsedRows || collapsedExpanded) && children.map((row, idx) => {
          const childUri = row.uri
          const isChildScanning = scanningPaths.has(childUri)
          const isSelected = sel.isSelected(row)
          // Indent children under collapsed rows when expanded
          const indentPx = collapsedRows && collapsedExpanded ? collapsedRows.length * 20 : 0
          // Build the collapsed path prefix from the last collapsed row's original_path
          const collapsedPrefix = collapsedRows?.length
            ? collapsedRows[collapsedRows.length - 1].original_path
            : ''
          return (
            <tr
              key={row.path}
              ref={sel.rowRef(idx)}
              {...sel.rowProps(idx)}
              style={{ opacity: row.scanned || scanStatus === 'full' ? 1 : 0.6 }}
            >
              <td className="col-checkbox" onClick={e => e.stopPropagation()}>
                <Checkbox
                  size="small"
                  checked={isSelected}
                  onChange={() => sel.toggle(idx)}
                  sx={{ padding: 0 }}
                />
              </td>
              <td className={`col-icon${indentPx ? ' indented' : ''}`}>
                {routeType === 'file' && caps?.reveal ? (
                  <span
                    className="reveal-icon"
                    title="Reveal in Finder"
                    onClick={e => { e.stopPropagation(); revealPath(childUri) }}
                  >
                    <RowIcon row={row} />
                  </span>
                ) : (
                  <RowIcon row={row} />
                )}
              </td>
              <td className="col-path">
                <Link to={`${prefix}/${collapsedPrefix ? collapsedPrefix + '/' : ''}${row.path}`} onClick={e => e.stopPropagation()}>
                  <PathName name={row.path} narrow={narrow} />
                </Link>
                {row.expand_preview && row.expand_preview.split('/').map((segment, idx, arr) => {
                  const pathToSegment = `${row.path}/${arr.slice(0, idx + 1).join('/')}`
                  return (
                    <span key={idx}>
                      {' / '}
                      <Link to={`${prefix}/${collapsedPrefix ? collapsedPrefix + '/' : ''}${pathToSegment}`} onClick={e => e.stopPropagation()}>
                        <code>{segment}</code>
                      </Link>
                    </span>
                  )
                })}
              </td>
              <td className="col-numeric">{formatSize(row.size)}</td>
              <td className="col-numeric col-wide">{timeAgo(row.mtime)}</td>
              <td className="col-numeric col-wide">{row.n_children ? row.n_children.toLocaleString() : null}</td>
              <td className="col-numeric col-wide">{row.n_desc && row.n_desc > 1 ? row.n_desc.toLocaleString() : null}</td>
              <td className="col-numeric col-wide">
                <ChildScanStatus row={row} scanStatus={scanStatus} parentScanTime={scanTime} />
              </td>
              <td className="col-action" onClick={e => e.stopPropagation()}>
                {row.kind === 'dir' && caps?.scan && (
                  <Tooltip title={row.scanned ? 'Rescan this directory' : 'Scan this directory'}>
                    <span>
                      <Button
                        size="small"
                        onClick={() => onScanChild(childUri)}
                        disabled={isChildScanning}
                        sx={{ minWidth: 0, padding: '2px 4px' }}
                      >
                        {isChildScanning ? <CircularProgress size={14} /> : <FaSync size={12} />}
                      </Button>
                    </span>
                  </Tooltip>
                )}
              </td>
              {canAct && (
                <td className="col-action" onClick={e => e.stopPropagation()}>
                  <Tooltip title={`${actionVerb} ${row.kind === 'dir' ? 'directory' : 'file'}`}>
                    <span>
                      <Button
                        size="small"
                        onClick={() => onDelete(childUri)}
                        disabled={deletingPaths.has(childUri)}
                        sx={{ minWidth: 0, padding: '2px 4px', color: actionColor }}
                      >
                        {deletingPaths.has(childUri) ? <CircularProgress size={14} /> : <FaTrash size={12} />}
                      </Button>
                    </span>
                  </Tooltip>
                </td>
              )}
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

/**
 * Hierarchical shape fed to `<DTTreemap>`. Built from `rows` (flat, parented
 * by relative path) so the widget can walk it via `getChildren`.
 * A synthetic `…` child is inserted per parent whose direct children's sizes
 * don't sum to its own — that "unaccounted" area is what the row-limit or
 * depth-limit dropped, and it deserves visible surface, not silent absorption.
 */
function useDebounced<T>(value: T, ms: number): T {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

interface DTNode {
  path: string
  label: string
  size: number
  /** Absolute URI — what `loadChildren` asks the server about. */
  uri?: string
  /** Newest descendant mtime (epoch s) — age-lens fallback when no mean is available. */
  mtime?: number | null
  /** Size-weighted mean mtime (epoch s) — preferred age signal (`--mean-mtime` scans). */
  mtimeMean?: number | null
  children?: DTNode[]
  /** Directory with children the server knows about, loaded here or not. */
  expandable?: boolean
  /** True for synthetic "…" placeholders — kept clickless. */
  isPlaceholder?: boolean
  /** Re-aggregated filter view: this node itself is an outermost match. */
  matched?: boolean
  /** Re-aggregated filter view: outermost matches at-or-under this node. */
  nMatches?: number
}

/**
 * Flat rows (relative paths, parented by relative path) → the nested shape
 * `<DTTreemap>` walks.
 *
 * A dir with *no* children in these rows is left `children: undefined` rather
 * than filled with a full-size "…": the rows stop at the response's depth, and
 * "we haven't asked yet" is what `loadChildren` exists to resolve. The
 * placeholder is still inserted for *partial* coverage, where children came
 * back but don't sum to the parent — that gap is real, dropped by the row
 * limit, and deserves visible surface.
 */
function buildDTNodes(rows: Row[], parentPath: string, parentSize: number | null): DTNode[] {
  const childrenByParent = new Map<string, Row[]>()
  for (const row of rows) {
    const parent = row.parent || '.'
    let arr = childrenByParent.get(parent)
    if (!arr) {
      arr = []
      childrenByParent.set(parent, arr)
    }
    arr.push(row)
  }

  const build = (path: string, size: number | null): DTNode[] => {
    const kids = childrenByParent.get(path) ?? []
    const nodes: DTNode[] = kids.map(r => {
      const sub = r.kind === 'dir' ? build(r.path, r.size ?? 0) : undefined
      return {
        path: r.path,
        label: r.path.split('/').pop() || r.path,
        size: r.size ?? 0,
        uri: r.uri,
        mtime: r.mtime,
        mtimeMean: r.mtime_mean,
        expandable: r.kind === 'dir' && (r.n_children ?? 0) > 0,
        children: sub && sub.length > 0 ? sub : undefined,
      }
    })
    const shown = nodes.reduce((s, n) => s + n.size, 0)
    const unaccounted = (size ?? 0) - shown
    if (nodes.length > 0 && size != null && unaccounted > 1_000_000) {
      nodes.push({
        path: `${path}/__other__`,
        label: '…',
        size: unaccounted,
        isPlaceholder: true,
      })
    }
    nodes.sort((a, b) => {
      if (a.isPlaceholder && !b.isPlaceholder) return 1
      if (!a.isPlaceholder && b.isPlaceholder) return -1
      return b.size - a.size
    })
    return nodes
  }

  return build(parentPath, parentSize)
}

/**
 * `/api/filter` rows (a depth-bounded rollup slice, sorted (depth, path) so
 * parents precede children) → the nested shape `<DTTreemap>` walks. Sizes are
 * *matched bytes only* — true re-aggregation. A matched dir is a frontier
 * leaf here: its contents are wholly matched, so lazily drilling it through
 * the normal scan endpoint shows correct sizes. An unmatched ancestor whose
 * matches sit *below* the slice depth stays unexpandable — a normal drill
 * would mix unfiltered children into filtered sizes.
 */
function buildFilterTree(result: FilterResult, rootLabel: string, rootUri?: string): DTNode {
  const rootNode: DTNode = {
    path: '.',
    label: rootLabel,
    size: result.total_size,
    uri: rootUri,
    nMatches: result.n_matches,
    children: [],
  }
  const byPath = new Map<string, DTNode>([['.', rootNode]])
  for (const r of result.rows) {
    const node: DTNode = {
      path: r.path,
      label: r.path.split('/').pop() || r.path,
      size: r.size,
      uri: r.uri,
      matched: r.matched,
      nMatches: r.n_matches,
      expandable: r.matched && r.kind === 'dir',
    }
    byPath.set(r.path, node)
    const i = r.path.lastIndexOf('/')
    const parent = byPath.get(i < 0 ? '.' : r.path.slice(0, i)) ?? rootNode
    ;(parent.children ??= []).push(node)
  }
  return rootNode
}

function Treemap({
  root,
  rootLabel,
  rows,
  ageLens,
  query,
  scanId,
  filterResult,
}: {
  root: Row
  /** The root cell's label — the location's basename (`gbfs`), not `.`. */
  rootLabel: string
  rows: Row[]
  ageLens: boolean
  query: string
  scanId?: number
  /** When set, render the re-aggregated filter slice instead of the scan rows. */
  filterResult?: FilterResult
}) {
  const navigate = useNavigate()
  const [tiling, setTiling] = useTiling()
  const [renderer, setRenderer] = useRenderer()
  const tree = useMemo(
    () => ({
      path: '.',
      label: rootLabel,
      size: root.size ?? 0,
      uri: root.uri,
      children: buildDTNodes(rows, '.', root.size ?? null),
    } satisfies DTNode),
    [root, rootLabel, rows],
  )

  // Drilling past the depth the response carried fetches that subtree, so the
  // treemap recurses as far as the scan does instead of stopping at depth 2.
  const loadChildren = useCallback(
    async (n: DTNode) => {
      if (!n.uri) return []
      const details = await fetchScanDetails(n.uri, scanId)
      return buildDTNodes(details.rows, '.', details.root?.size ?? n.size)
    },
    [scanId],
  )

  // Age-lens domain over all rows (not just the drill level), so a cell's
  // fade is stable as you drill instead of renormalizing per level. Prefer
  // the size-weighted mean mtime (`--mean-mtime` scans) — a stale 10 TB dir
  // with one fresh file reads stale, not fresh; fall back to max-mtime.
  const rowAge = (r: Row) => r.mtime_mean ?? r.mtime
  const mtimeDomain = useMemo(() => ageDomain(rows, rowAge), [rows])
  const nodeAge = (n: DTNode) => n.mtimeMean ?? n.mtime
  // Filter is a *display* lens at this level: it dims non-matching cells and
  // stacks on whatever the palette/age lens resolved. Sizes keep counting
  // hidden children — this is highlight, not re-aggregation (spec §5 v0).
  // With `filterResult` set (re-aggregate mode), the tree below IS the
  // matched slice, so no dimming applies.
  const matches = useMemo(() => parseQuery(query), [query])

  const filterTree = useMemo(
    () => (filterResult ? buildFilterTree(filterResult, rootLabel, root.uri) : null),
    [filterResult, root, rootLabel],
  )

  return (
    <Box sx={{ height: 400 }}>
      <DTTreemap<DTNode>
        root={filterTree ?? tree}
        tiling={tiling}
        renderer={renderer}
        exportable={{ title: true }}
        renderTip={(label, btn) => <Tooltip title={label}>{btn as ReactElement}</Tooltip>}
        nestedHues
        renderLegend={() => (
          <span style={{ display: 'inline-flex', gap: 8, fontSize: '0.8rem' }}>
            {/* One control, not a gaps|shared pair: cells abut and share one
                stroke by default (areas exact); check `gaps` for the 2px-gutter
                look (which under-paints dense leaf fields by ~perimeter/area). */}
            <Tooltip title="Off (default): cells abut, one stroke per boundary — areas exact. On: 2px gutters and rounded corners, but a dense leaf field under-paints by ~perimeter/area (a 6×6px cell with 2px gutters paints only 4×4).">
              <label
                onClick={e => e.stopPropagation()}
                style={{ display: 'inline-flex', alignItems: 'center', gap: 3, cursor: 'pointer', fontSize: '0.75rem' }}
              >
                <input
                  type="checkbox"
                  checked={tiling === 'gaps'}
                  onChange={e => setTiling(e.target.checked ? 'gaps' : 'shared')}
                  style={{ margin: 0, verticalAlign: 'middle' }}
                />
                gaps
              </label>
            </Tooltip>
            <span style={{ display: 'inline-flex', gap: 2 }}>
              {(['dom', 'canvas'] as const).map(r => (
                <button
                  key={r}
                  onClick={e => { e.stopPropagation(); setRenderer(r) }}
                  title={r === 'dom'
                    ? 'DOM renderer: one <div> per cell (mature, full feature set)'
                    : 'Canvas renderer: the whole map on one <canvas> — no DOM node per cell, progressive paint (faster on large maps)'}
                  style={{
                    cursor: 'pointer', fontSize: '0.75rem', padding: '1px 7px', borderRadius: 3,
                    border: '1px solid var(--dt-border, #444)',
                    background: renderer === r ? 'var(--dt-accent-bg, #30363d)' : 'transparent',
                    color: 'inherit', fontWeight: renderer === r ? 600 : 400,
                  }}
                >
                  {r}
                </button>
              ))}
            </span>
          </span>
        )}
        getSize={n => n.size}
        getChildren={n => n.children}
        hasChildren={n => !!n.expandable && !n.isPlaceholder}
        loadChildren={loadChildren}
        // Option A: a cell click re-roots the whole page (URL + breadcrumb +
        // table + treemap) to that node, SPA-style — same navigation the
        // staleness/voronoi views already do — rather than drilling the map
        // in place and diverging from the table above. `uri` is the absolute
        // URI, so this is correct through chunk/collapse boundaries; `true`
        // suppresses the widget's built-in in-place drill. ⌥-click falls
        // through (returns nothing) so the widget pins the branch instead —
        // inspect/act on a deep cell's tip without re-rooting the page.
        onCellClick={(n, _path, e) => {
          if (e.altKey) return
          if (!n.uri || n.isPlaceholder) return
          navigate(uriToPath(n.uri))
          return true
        }}
        cellHref={n => (n.uri && !n.isPlaceholder ? uriToPath(n.uri) : undefined)}
        renderLoading={n => `Loading ${n.label}…`}
        getLabel={n => n.label}
        formatSize={formatSize}
        colorForCell={n =>
          // Only override the synthetic "…" placeholders; the widget's
          // default categorical palette handles real nodes.
          n.isPlaceholder ? { bg: '#4a4a52', ink: '#d0d0d8' } : null
        }
        lens={
          !filterTree && (ageLens || query.trim())
            ? (n, _path, _depth, _ctx, style) => {
                let out = style
                const age = nodeAge(n)
                if (ageLens && mtimeDomain && age != null && !n.isPlaceholder) {
                  out = ageFade(out, age01(age, mtimeDomain))
                }
                if (query.trim()) {
                  // Placeholders can't match a path query, so they dim too —
                  // leaving them bright would read as "these matched".
                  out = dimUnmatched(out, !n.isPlaceholder && matches(n.path)) ?? out
                }
                return out
              }
            : undefined
        }
        renderTooltip={n => (
          <>
            <div style={{ fontWeight: 500 }}>{n.label}</div>
            <div style={{ opacity: 0.75, fontSize: '0.85em' }}>{formatSize(n.size)}</div>
            {filterTree && n.nMatches != null && (
              <div style={{ opacity: 0.6, fontSize: '0.75em', marginTop: 2 }}>
                {n.matched
                  ? 'matched — size is its full aggregate'
                  : `${n.nMatches.toLocaleString()} match${n.nMatches === 1 ? '' : 'es'} below; size counts matched bytes only`}
              </div>
            )}
            {!filterTree && ageLens && nodeAge(n) != null && (
              <div style={{ opacity: 0.6, fontSize: '0.75em', marginTop: 2 }}>
                {n.mtimeMean != null ? `mean mtime ${timeAgo(n.mtimeMean)}` : `modified ${timeAgo(n.mtime!)}`}
              </div>
            )}
            {n.isPlaceholder && (
              <div style={{ opacity: 0.6, fontSize: '0.75em', marginTop: 2 }}>
                unaccounted (dropped by row-limit or depth-limit)
              </div>
            )}
          </>
        )}
      />
    </Box>
  )
}

/**
 * Staleness scatter over the drill level's direct children: x = age, y =
 * bytes, marker area ∝ descendant count, log-log with exact iso-sum-TB·year
 * diagonals. Upper-right of a diagonal is the delete-candidate frontier.
 *
 * Sizes here are SI (1 TB = 1e12 B) rather than the table's IEC GiB/TiB, so
 * the axis agrees with the TB·years score the diagonals are denominated in.
 */
function StalenessPanel({
  nodes,
  uri,
  collapsedRows,
}: {
  nodes: Row[]
  uri: string
  collapsedRows?: CollapsedRow[] | null
}) {
  const navigate = useNavigate()
  const prefix = childLinkPrefix(uri)
  const collapsedPrefix = collapsedRows?.length
    ? collapsedRows[collapsedRows.length - 1].original_path
    : ''
  const now = Date.now() / 1000
  return (
    <Box sx={{ height: 400 }}>
      <StalenessScatter<Row>
        nodes={nodes}
        getAge={r => {
          const t = r.mtime_mean ?? r.mtime
          return t == null ? null : now - t
        }}
        getSize={r => r.size}
        getLabel={r => r.path}
        getWeight={r => r.n_desc}
        onNodeClick={r => {
          if (r.kind === 'dir') {
            navigate(`${prefix}/${collapsedPrefix ? collapsedPrefix + '/' : ''}${r.path}`)
          }
        }}
        renderTooltip={r => (
          <>
            <div style={{ fontWeight: 500 }}>{r.path}</div>
            <div style={{ opacity: 0.75, fontSize: '0.85em' }}>
              {formatSize(r.size)} · {formatCount(r.n_desc)} items
            </div>
            <div style={{ opacity: 0.6, fontSize: '0.75em' }}>
              {r.mtime_mean != null ? 'mean mtime ' : 'modified '}
              {timeAgo(r.mtime_mean ?? r.mtime)}
            </div>
          </>
        )}
      />
    </Box>
  )
}

/**
 * Byte-weighted age histograms for the drill dir's children (spec §4). Fetched
 * lazily — unlike `/api/scan`, the histogram needs every descendant file row,
 * so it only runs when this view is open.
 */
function HistogramPanel({ uri, scanId, query }: { uri: string; scanId?: number; query: string }) {
  const { data, isLoading, error } = useQuery({
    queryKey: ['histogram', uri, scanId],
    queryFn: () => fetchHistogram(uri, 24, 10, scanId),
    staleTime: 60 * 1000,
  })
  const [threshold, setThreshold] = useState<number | null>(null)
  const [reclaimable, setReclaimable] = useState(0)
  const [normalize, setNormalize] = useState(false)

  if (isLoading) return <Box sx={{ height: 400, display: 'flex', alignItems: 'center', justifyContent: 'center' }}><CircularProgress size={24} /></Box>
  if (error) return <Alert severity="error">{(error as Error).message}</Alert>
  if (!data || data.children.length === 0) return <Box sx={{ opacity: 0.6, fontSize: '0.85rem' }}>No file mtimes to bin here.</Box>

  // Same display-level filter semantics as the other views: hide non-matching
  // columns; each column's own bins are unchanged.
  const matches = parseQuery(query)
  const shown = data.children.filter(c => matches(c.path))
  if (shown.length === 0) return <Box sx={{ opacity: 0.6, fontSize: '0.85rem' }}>No children match the filter.</Box>

  return (
    <>
      <Box sx={{ height: 400 }}>
        <AgeHistograms<HistogramChild>
          items={shown}
          edges={data.edges}
          getBins={c => c.bytes}
          getLabel={c => c.path}
          formatSize={formatSize}
          formatTime={t => new Date(t * 1000).toLocaleDateString()}
          threshold={threshold}
          onThresholdChange={(t, bytes) => { setThreshold(t); setReclaimable(bytes) }}
          normalize={normalize}
          renderTooltip={c => (
            <>
              <div style={{ fontWeight: 500 }}>{c.path}</div>
              <div style={{ opacity: 0.75, fontSize: '0.85em' }}>
                {formatSize(c.total_bytes)} · {formatCount(c.n_files)} files
              </div>
            </>
          )}
        />
      </Box>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, fontSize: '0.8rem', opacity: 0.7, mt: 0.5 }}>
        <Tooltip title="Scale each column to its own largest bin. Shapes become legible when children differ by orders of magnitude, but bar widths stop being comparable between columns.">
          <label style={{ cursor: 'pointer', whiteSpace: 'nowrap' }}>
            <input
              type="checkbox"
              checked={normalize}
              onChange={e => setNormalize(e.target.checked)}
              style={{ marginRight: 4, verticalAlign: 'middle' }}
            />
            Shape only
          </label>
        </Tooltip>
        <span>
        {threshold == null
          ? 'Drag in the plot to set an age threshold; the shaded area is what deleting everything older would reclaim.'
          : `${formatSize(reclaimable)} older than ${new Date(threshold * 1000).toLocaleDateString()}`}
        {data.omitted > 0 && ` · ${formatCount(data.omitted)} smaller children omitted (${formatSize(data.omitted_bytes)})`}
        </span>
      </Box>
    </>
  )
}

/**
 * Voronoi alt view (spec §6). Not a replacement for the treemap — rectangles
 * label far better — but it fills a circle, which rectangles can't, and reads
 * as one organic whole rather than a grid.
 */
function VoronoiPanel({
  nodes,
  uri,
  collapsedRows,
}: {
  nodes: Row[]
  uri: string
  collapsedRows?: CollapsedRow[] | null
}) {
  const navigate = useNavigate()
  const prefix = childLinkPrefix(uri)
  const collapsedPrefix = collapsedRows?.length
    ? collapsedRows[collapsedRows.length - 1].original_path
    : ''
  return (
    <Box sx={{ height: 460 }}>
      <VoronoiTreemap<Row>
        items={nodes}
        getValue={r => r.size ?? 0}
        getLabel={r => r.path}
        formatValue={formatSize}
        // Seed off the directory so a given dir always lays out the same way.
        seed={uri}
        onCellClick={r => {
          if (r.kind === 'dir') {
            navigate(`${prefix}/${collapsedPrefix ? collapsedPrefix + '/' : ''}${r.path}`)
          }
        }}
        renderTooltip={r => (
          <>
            <div style={{ fontWeight: 500 }}>{r.path}</div>
            <div style={{ opacity: 0.75, fontSize: '0.85em' }}>{formatSize(r.size)}</div>
          </>
        )}
      />
    </Box>
  )
}

function FilePreviewSection({ path }: { path: string }) {
  const caps = useCapabilities()
  const { data: preview, isLoading, error } = useQuery({
    queryKey: ['file-preview', path],
    queryFn: () => fetchFilePreview(path),
    staleTime: 60 * 1000,
    enabled: caps?.preview === true,
  })
  if (!caps?.preview) return null

  if (isLoading) {
    return (
      <Box sx={{ mt: 2, display: 'flex', alignItems: 'center', gap: 1 }}>
        <CircularProgress size={16} />
        <span>Loading preview...</span>
      </Box>
    )
  }

  if (error) {
    return (
      <Alert severity="error" sx={{ mt: 2 }}>
        Failed to load preview: {error instanceof Error ? error.message : 'Unknown error'}
      </Alert>
    )
  }

  if (!preview) {
    return null
  }

  if (!preview.is_text) {
    return (
      <Box sx={{ mt: 2 }}>
        <Box sx={{ mb: 1, fontSize: '0.85rem', opacity: 0.7 }}>
          Binary file ({formatSize(preview.size)})
          {preview.hex_truncated && ` — showing first ${formatSize(preview.preview_bytes)}`}
        </Box>
        {preview.hex && (
          <Box
            component="pre"
            sx={{
              p: 2,
              bgcolor: 'rgba(0,0,0,0.3)',
              borderRadius: 1,
              overflow: 'auto',
              maxHeight: '500px',
              fontSize: '0.75rem',
              lineHeight: 1.3,
              fontFamily: 'monospace',
              m: 0,
              whiteSpace: 'pre',
            }}
          >
            {preview.hex}
          </Box>
        )}
      </Box>
    )
  }

  return (
    <Box sx={{ mt: 2 }}>
      <Box sx={{ mb: 1, fontSize: '0.85rem', opacity: 0.7 }}>
        Preview {preview.truncated && `(first ${formatSize(preview.preview_bytes)} of ${formatSize(preview.size)})`}
      </Box>
      <Box
        component="pre"
        sx={{
          p: 2,
          bgcolor: 'rgba(0,0,0,0.3)',
          borderRadius: 1,
          overflow: 'auto',
          maxHeight: '500px',
          fontSize: '0.8rem',
          lineHeight: 1.4,
          fontFamily: 'monospace',
          m: 0,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-word',
        }}
      >
        {preview.content}
      </Box>
    </Box>
  )
}

export function ScanDetails() {
  const params = useParams()
  const pathSegments = params['*'] || ''
  const pathname = window.location.pathname
  const routeType: RouteType = detectRouteType(pathname)
  const uri = segmentsToUri(routeType, pathSegments)
  const navigate = useNavigate()

  // The current location's own name, for the treemap's root cell and the table's
  // root row (the full navigable path already sits in the breadcrumb above, so
  // `.` there was redundant *and* uninformative). Basename of the uri: `gbfs`
  // for `r2://ctbk/gbfs`; the bare scheme/`/` root falls back to its label.
  const rootLabel = isSchemeRoot(uri)
    ? (routeType === 'file' ? '/' : `${routeType}://`)
    : (uri.replace(/\/+$/, '').split('/').pop() || uri)

  // Selected scan ID for time-travel (undefined = latest)
  const [selectedScanId, setSelectedScanId] = useState<number | undefined>(undefined)

  // Fetch scan history for the dropdown
  const { data: scanHistory } = useQuery({
    queryKey: ['scan-history', uri],
    queryFn: () => fetchScanHistory(uri),
    staleTime: 60 * 1000,
  })

  const [treemapMaxRows, setTreemapMaxRows] = useState(DEFAULT_MAX_ROWS)
  const [ageLens, setAgeLens] = useState(false)
  const [viz, setViz] = useState<Viz>('treemap')
  const caps = useCapabilities()
  // A static deployment has no live scanner, so `/file/` (uri `/`) can never
  // resolve — don't fire the request (it would 404 and retry, spinning on
  // "Loading…"); render a friendly note instead.
  const localBrowseUnavailable = routeType === 'file' && isSchemeRoot(uri) && caps?.filesystem === false
  const { data: details, isLoading, error: queryError, refetch } = useQuery({
    queryKey: ['scan-details', uri, selectedScanId, treemapMaxRows],
    queryFn: () => fetchScanDetails(uri, selectedScanId, 2, treemapMaxRows),
    staleTime: 60 * 1000, // 1 minute - scan details don't change frequently
    enabled: !localBrowseUnavailable,
  })
  const [mutationError, setMutationError] = useState<string | null>(null)
  const [stageNotice, setStageNotice] = useState<string | null>(null)
  const error = queryError?.message || mutationError
  const [scanning, setScanning] = useState(false)
  const [scanJob, setScanJob] = useState<ScanJob | null>(null)
  const [childJobs, setChildJobs] = useState<Map<string, ScanJob>>(new Map())
  const [sorts, setSorts] = useState<SortSpec[]>([{ key: 'size', dir: 'desc' }])
  const [deletingPaths, setDeletingPaths] = useState<Set<string>>(new Set())
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(50)
  const [filter, setFilter] = useState('')
  // Re-aggregate mode: the treemap shows the recursive `/api/filter` slice —
  // sizes count matched bytes only — instead of dimming the current level.
  const [reagg, setReagg] = useState(false)
  const debouncedFilter = useDebounced(filter, 300)
  const filterActive = reagg && !!debouncedFilter.trim()
  const { data: filterResult, isFetching: filterFetching } = useQuery({
    queryKey: ['filter', uri, debouncedFilter, selectedScanId],
    queryFn: () => fetchFilter(uri, debouncedFilter, selectedScanId),
    enabled: filterActive,
    placeholderData: prev => prev, // keep the last slice on screen while typing
    staleTime: 60 * 1000,
  })
  const wrapperRef = useRef<HTMLDivElement>(null)
  const tableRef = useRef<HTMLTableElement>(null)

  // Live scan progress from SSE
  const scanProgress = useScanProgress()
  // Sync vs stage is the deployment's `deleteApproval` policy (CP6), not the scheme.
  const { method: deleteMethod } = useDeleteMethod()
  const deletable = supportsDelete(routeType)
  const canDelete = deletable && caps?.delete === true && deleteMethod === 'sync'
  const staging = deletable && caps?.stageDelete === true && deleteMethod === 'staged'
  const canAct = canDelete || staging
  const actionColor = staging ? '#ed6c02' : '#d32f2f'

  // Auto-refetch when a scan relevant to this view finishes. A completed scan
  // is *deleted* from `scan_progress` (see ScanProgress.finish), so completion
  // is a path that was streaming last tick and is now gone. Relevance mirrors
  // the banner: exact / ancestor / descendant of the current uri — a fresher
  // ancestor or descendant scan both change what this view should show
  // (descendants via fresher-child patching). Invalidate rather than refetch
  // so background/unmounted queries (history, sibling depths) refresh too.
  const queryClient = useQueryClient()
  const prevScanPaths = useRef<Set<string>>(new Set())
  useEffect(() => {
    const relevant = (p: string) =>
      p === uri || uri.startsWith(p + '/') || p.startsWith(uri + '/')
    const active = new Set(scanProgress.map(s => s.path).filter(relevant))
    let finished = false
    for (const p of prevScanPaths.current) {
      if (!active.has(p)) { finished = true; break }
    }
    prevScanPaths.current = active
    if (finished) {
      queryClient.invalidateQueries({ queryKey: ['scan-details', uri] })
      queryClient.invalidateQueries({ queryKey: ['scan-history', uri] })
    }
  }, [scanProgress, uri, queryClient])

  // Record visit to recent paths
  const { recordVisit } = useRecentPaths()
  useEffect(() => {
    if (uri && !isSchemeRoot(uri)) {
      recordVisit(uri, 'tree')
    }
  }, [uri, recordVisit])

  const handleSort = (key: SortKey) => {
    setSorts(prev => {
      const existingIdx = prev.findIndex(s => s.key === key)
      if (existingIdx === 0) {
        // Toggle direction if clicking current primary sort
        return [{ key, dir: prev[0].dir === 'asc' ? 'desc' : 'asc' }, ...prev.slice(1)]
      }
      // Make this the new primary sort, keep previous as secondary
      const newSorts: SortSpec[] = [{ key, dir: 'desc' }]
      // Add previous primary as secondary (if different)
      if (prev.length > 0 && prev[0].key !== key) {
        newSorts.push(prev[0])
      }
      return newSorts
    })
  }

  const filteredChildren = useMemo(() => {
    if (!details) return []
    const { children } = details
    if (!filter.trim()) return children
    const lowerFilter = filter.toLowerCase()
    return children.filter(r => r.path.toLowerCase().includes(lowerFilter))
  }, [details, filter])

  const sortedChildren = useMemo(() => {
    return [...filteredChildren].sort((a, b) => {
      for (const { key, dir } of sorts) {
        let cmp = 0
        switch (key) {
          case 'kind':
            cmp = (a.kind === 'dir' ? 0 : 1) - (b.kind === 'dir' ? 0 : 1)
            break
          case 'path':
            cmp = a.path.localeCompare(b.path)
            break
          case 'size':
            cmp = (a.size ?? 0) - (b.size ?? 0)
            break
          case 'mtime':
            cmp = (a.mtime ?? 0) - (b.mtime ?? 0)
            break
          case 'n_children':
            cmp = (a.n_children ?? 0) - (b.n_children ?? 0)
            break
          case 'n_desc':
            cmp = (a.n_desc ?? 0) - (b.n_desc ?? 0)
            break
          case 'scanned':
            // Sort by: has own scan_time > scanned true > scanned partial > not scanned
            const scanOrder = (r: Row) => {
              if (r.scan_time) return 3
              if (r.scanned === true) return 2
              if (r.scanned === 'partial') return 1
              return 0
            }
            cmp = scanOrder(a) - scanOrder(b)
            break
        }
        if (cmp !== 0) {
          return dir === 'asc' ? cmp : -cmp
        }
      }
      return 0
    })
  }, [filteredChildren, sorts])

  const totalPages = Math.ceil(sortedChildren.length / pageSize)
  const paginatedChildren = useMemo(() => {
    const start = page * pageSize
    return sortedChildren.slice(start, start + pageSize)
  }, [sortedChildren, page, pageSize])

  // Multi-row selection (shift-range, ⌘-click, j/k) — use-kbd's `useRowSelection`
  // via the shared wrapper, keyed by `uri` so it survives paging and sort.
  const sel = useRowSelection(paginatedChildren, r => r.uri)
  useRowSelectionKeys(sel)

  // Reset page when sort/filter changes or data reloads
  useEffect(() => {
    setPage(0)
  }, [sorts, filter, details])

  // Clear selection on a new listing (scan reload); a page/sort/filter change
  // keeps it — use-kbd freezes the active range into pins by key.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => { sel.clear(); setStageNotice(null) }, [details])

  // Click outside the wrapper (table + toolbar) deselects.
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (wrapperRef.current && !wrapperRef.current.contains(e.target as Node)) sel.clear()
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Selection summary (over the current page's selected rows).
  const selectedRows = sel.selectedRows()
  const selectedSize = selectedRows.reduce((sum, r) => sum + (r.size ?? 0), 0)
  const selectedDirs = selectedRows.filter(r => r.kind === 'dir')

  // Bulk actions
  const handleBulkScan = async () => {
    for (const row of selectedDirs) {
      try {
        const job = await startScan(row.uri)
        setChildJobs(prev => new Map(prev).set(row.uri, job))
      } catch (e) {
        setMutationError(e instanceof Error ? e.message : 'Failed to start scan')
      }
    }
  }

  const handleBulkDelete = async () => {
    if (selectedRows.length === 0) return

    // Cloud buckets stage into a plan (reversible, no deadline) rather than
    // deleting immediately; an admin dispatches from `/staged`.
    if (staging) {
      const uris = selectedRows.map(r => r.uri)
      try {
        const { added } = await stageUris(uris)
        const dup = uris.length - added.length
        setStageNotice(`Staged ${added.length} for deletion${dup ? ` (${dup} already staged)` : ''} — review in Staged.`)
        setMutationError(null)
      } catch (e) {
        setMutationError(e instanceof Error ? e.message : 'Failed to stage')
      }
      sel.clear()
      return
    }

    const msg = selectedRows.length === 1
      ? `Delete "${selectedRows[0].path}"?`
      : `Delete ${selectedRows.length} items (${formatSize(selectedSize)})?`

    if (!confirm(`${msg} This cannot be undone.`)) {
      return
    }

    const pathsToDelete = selectedRows.map(r => r.uri)
    setDeletingPaths(prev => {
      const next = new Set(prev)
      pathsToDelete.forEach(p => next.add(p))
      return next
    })

    for (const path of pathsToDelete) {
      try {
        await deletePath(path)
      } catch (e) {
        setMutationError(e instanceof Error ? e.message : 'Failed to delete')
      }
    }

    setDeletingPaths(prev => {
      const next = new Set(prev)
      pathsToDelete.forEach(p => next.delete(p))
      return next
    })

    sel.clear()
    refetch()
  }

  // Poll scan job status (main scan)
  useEffect(() => {
    if (!scanJob || (scanJob.status !== 'pending' && scanJob.status !== 'running')) return

    const interval = setInterval(async () => {
      const status = await fetchScanStatus(scanJob.job_id)
      setScanJob(status)
      if (status.status === 'completed') {
        setScanning(false)
        refetch()
      } else if (status.status === 'failed') {
        setScanning(false)
        setMutationError(status.error || 'Scan failed')
      }
    }, 2000)

    return () => clearInterval(interval)
  }, [scanJob])

  // Poll child scan jobs
  useEffect(() => {
    const activeJobs = Array.from(childJobs.entries()).filter(
      ([, job]) => job.status === 'pending' || job.status === 'running'
    )
    if (activeJobs.length === 0) return

    const interval = setInterval(async () => {
      const updates = new Map(childJobs)
      let anyCompleted = false

      for (const [path, job] of activeJobs) {
        const status = await fetchScanStatus(job.job_id)
        updates.set(path, status)
        if (status.status === 'completed') {
          anyCompleted = true
        }
      }

      setChildJobs(updates)
      if (anyCompleted) {
        refetch()
      }
    }, 2000)

    return () => clearInterval(interval)
  }, [childJobs])

  const handleRescan = async () => {
    setScanning(true)
    try {
      const job = await startScan(uri)
      setScanJob(job)
    } catch (e) {
      setScanning(false)
      setMutationError(e instanceof Error ? e.message : 'Failed to start scan')
    }
  }

  const handleScanChild = async (childPath: string) => {
    try {
      const job = await startScan(childPath)
      setChildJobs(prev => new Map(prev).set(childPath, job))
    } catch (e) {
      setMutationError(e instanceof Error ? e.message : 'Failed to start scan')
    }
  }

  const handleDelete = async (path: string) => {
    // Cloud buckets stage into a plan; an admin dispatches from `/staged`.
    if (staging) {
      try {
        const { added } = await stageUris([path])
        setStageNotice(added.length ? 'Staged for deletion — review in Staged.' : 'Already staged.')
        setMutationError(null)
      } catch (e) {
        setMutationError(e instanceof Error ? e.message : 'Failed to stage')
      }
      return
    }

    const name = path.split('/').pop() || path
    if (!confirm(`Delete "${name}"? This cannot be undone.`)) {
      return
    }

    setDeletingPaths(prev => new Set(prev).add(path))
    try {
      await deletePath(path)
      if (path === uri) {
        // The folder on screen is gone: show its parent instead
        navigate(uriToPath(path.replace(/\/[^/]+\/?$/, '')))
        return
      }
      refetch()
    } catch (e) {
      setMutationError(e instanceof Error ? e.message : 'Failed to delete')
    } finally {
      setDeletingPaths(prev => {
        const next = new Set(prev)
        next.delete(path)
        return next
      })
    }
  }

  const scanningPaths = new Set(
    Array.from(childJobs.entries())
      .filter(([, job]) => job.status === 'pending' || job.status === 'running')
      .map(([path]) => path)
  )

  if (localBrowseUnavailable) {
    return (
      <div style={{ padding: '1.5rem 0' }}>
        <p>Local filesystem browsing isn't available in this deployment.</p>
        <p style={{ opacity: 0.75 }}>
          Head to <Link to="/">Scans</Link> to browse the published scans.
        </p>
      </div>
    )
  }
  if (isLoading) return <div>Loading...</div>
  if (error && !details) {
    return (
      <div>
        <p>Error: {error}</p>
        {error.includes('No scan found') && caps?.scan && (
          <Button
            variant="contained"
            onClick={handleRescan}
            disabled={scanning}
            startIcon={scanning ? <CircularProgress size={16} /> : <FaSync />}
          >
            {scanning ? 'Scanning...' : 'Scan This Path'}
          </Button>
        )}
      </div>
    )
  }
  if (!details) return <div>No data</div>

  const { root, rows, scan_status, time, error_count, error_paths, collapsed_rows } = details

  // Parse error_paths JSON string to array (inline, no hook needed)
  const parsedErrorPaths = error_paths ? (() => {
    try {
      return JSON.parse(error_paths) as string[]
    } catch {
      return null
    }
  })() : null

  // Format scan time for dropdown display
  const formatScanTime = (timeStr: string) => {
    const date = new Date(timeStr)
    const now = new Date()
    const diffMs = now.getTime() - date.getTime()
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24))
    const timeFormatted = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    if (diffDays === 0) return `Today ${timeFormatted}`
    if (diffDays === 1) return `Yesterday ${timeFormatted}`
    if (diffDays < 7) return `${diffDays}d ago ${timeFormatted}`
    return date.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ` ${timeFormatted}`
  }

  // The viz panel (treemap / scatter / histograms / voronoi) + its controls,
  // rendered *above* the table so the map is visible without scrolling past a
  // long directory listing (a bucket root can be dozens of rows).
  const vizPanel = rows.length > 0 ? (
    <Box sx={{ mt: 2, mb: 2 }}>
      <VizBoundary label={VIZ_LABELS[viz]}>
        {viz === 'treemap' ? (
          <Treemap
            root={root}
            rootLabel={rootLabel}
            rows={rows}
            ageLens={ageLens}
            query={filter}
            scanId={selectedScanId}
            filterResult={filterActive ? filterResult : undefined}
          />
        ) : viz === 'scatter' ? (
          <StalenessPanel nodes={filteredChildren} uri={uri} collapsedRows={collapsed_rows} />
        ) : viz === 'histograms' ? (
          <HistogramPanel uri={uri} scanId={selectedScanId} query={filter} />
        ) : (
          <VoronoiPanel nodes={filteredChildren} uri={uri} collapsedRows={collapsed_rows} />
        )}
      </VizBoundary>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mt: 1, fontSize: '0.85rem', opacity: 0.7 }}>
        <span>{viz === 'treemap' ? `${rows.length} items` : `${filteredChildren.length} children`}</span>
        {filter.trim() && caps?.filter && (
          <Tooltip
            title={reagg
              ? 'Recursive server-side filter: sizes count matched bytes only, and a match inside a matched dir never double-counts. Click to switch back to display-only dimming.'
              : "The filter highlights and re-lays-out what's shown at this level; directory sizes still include children the filter hides. Click to re-aggregate: sizes count only what matches, recursively."}
          >
            <span
              onClick={() => setReagg(r => !r)}
              style={{ fontStyle: 'italic', cursor: 'pointer', textDecoration: 'underline dotted' }}
            >
              {reagg
                ? filterResult
                  ? `filtered (re-aggregated): ${formatSize(filterResult.total_size)} in ${filterResult.n_matches.toLocaleString()} match${filterResult.n_matches === 1 ? '' : 'es'}${filterFetching ? ' …' : ''}`
                  : 'filtered (re-aggregating…)'
                : 'filtered (display only)'}
            </span>
          </Tooltip>
        )}
        <label>
          View:
          <select value={viz} onChange={e => setViz(e.target.value as Viz)} style={{ marginLeft: 4 }}>
            <option value="treemap">Treemap</option>
            <option value="scatter">Staleness</option>
            {caps?.histogram && <option value="histograms">Age histograms</option>}
            <option value="voronoi">Voronoi</option>
          </select>
        </label>
        {viz === 'treemap' && (
          <Tooltip title="Fade cells by age (size-weighted mean mtime when the scan has it, else newest descendant) — older fades toward the background">
            <label style={{ cursor: 'pointer' }}>
              <input
                type="checkbox"
                checked={ageLens}
                onChange={e => setAgeLens(e.target.checked)}
                style={{ marginRight: 4, verticalAlign: 'middle' }}
              />
              Age lens
            </label>
          </Tooltip>
        )}
        <label>
          Max:
          <select
            value={treemapMaxRows}
            onChange={e => setTreemapMaxRows(Number(e.target.value))}
            style={{ marginLeft: 4 }}
          >
            <option value={500}>500</option>
            <option value={1000}>1,000</option>
            <option value={2000}>2,000</option>
            <option value={5000}>5,000</option>
            <option value={0}>All</option>
          </select>
        </label>
      </Box>
    </Box>
  ) : null

  return (
    <div ref={wrapperRef} tabIndex={0} style={{ outline: 'none' }}>
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 1, flexWrap: 'wrap' }}>
        <h1 style={{ margin: 0 }}>
          <Breadcrumbs uri={uri} routeType={routeType} />
          {collapsed_rows && collapsed_rows.map((row, idx) => {
            const segment = row.original_path.split('/').pop() || row.original_path
            const fullPath = `${uri}/${row.original_path}`
            return (
              <span key={idx}>
                <span className="breadcrumb-sep">/</span>
                <Link to={uriToPath(fullPath)}>
                  {segment}
                </Link>
              </span>
            )
          })}
        </h1>
        {scanHistory && scanHistory.length > 1 && (
          <Tooltip title="View historical scans">
            <select
              value={selectedScanId ?? ''}
              onChange={e => setSelectedScanId(e.target.value ? Number(e.target.value) : undefined)}
              style={{ padding: '4px 8px', fontSize: '0.85rem', background: '#2d2d2d', color: '#e6edf3', border: '1px solid #444', borderRadius: 4, maxWidth: '100%' }}
            >
              <option value="">
                Latest — {formatScanTime(scanHistory[0].time)} · {formatSize(scanHistory[0].size ?? 0)}
              </option>
              {scanHistory.map(scan => (
                <option key={scan.id} value={scan.id}>
                  {formatScanTime(scan.time)} - {formatSize(scan.size ?? 0)}
                  {scan.scan_path !== uri && ` (from ${scan.scan_path})`}
                </option>
              ))}
            </select>
          </Tooltip>
        )}
        {caps?.compare && scanHistory && scanHistory.length > 1 && (
          <Button
            component={Link}
            to={`/compare${uriToPath(uri)}`}
            size="small"
            variant="outlined"
            startIcon={<FaExchangeAlt size={12} />}
            sx={{ textTransform: 'none' }}
          >
            Compare
          </Button>
        )}
      </Box>
      <ScanProgressBanner progress={scanProgress} currentUri={uri} />
      {error && <p style={{ color: 'red' }}>{error}</p>}
      {stageNotice && (
        <Alert severity="success" onClose={() => setStageNotice(null)} sx={{ mb: 1 }}>
          {stageNotice} <Link to="/staged">Staged →</Link>
        </Alert>
      )}
      {error_count && error_count > 0 && (
        <PermissionErrorWarning
          errorCount={error_count}
          errorPaths={parsedErrorPaths}
          scanPath={details.scan_path}
        />
      )}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 1 }}>
        <TextField
          size="small"
          placeholder="Filter by name..."
          value={filter}
          onChange={e => setFilter(e.target.value)}
          slotProps={{ input: { startAdornment: <FaSearch style={{ marginRight: 8, opacity: 0.5 }} /> } }}
          sx={{ width: 250 }}
        />
        {selectedRows.length > 0 && (
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, fontSize: '0.85rem' }}>
            <span style={{ opacity: 0.8 }}>
              {selectedRows.length} selected ({formatSize(selectedSize)})
            </span>
            {selectedDirs.length > 0 && (
              <Tooltip title={`Scan ${selectedDirs.length} director${selectedDirs.length === 1 ? 'y' : 'ies'}`}>
                <Button
                  size="small"
                  onClick={handleBulkScan}
                  startIcon={<FaSync size={12} />}
                  sx={{ minWidth: 0 }}
                >
                  Scan
                </Button>
              </Tooltip>
            )}
            {canAct && (
              <Tooltip title={`${staging ? 'Stage' : 'Delete'} ${selectedRows.length} item${selectedRows.length === 1 ? '' : 's'}${staging ? ' for deletion' : ''}`}>
                <Button
                  size="small"
                  onClick={handleBulkDelete}
                  startIcon={<FaTrash size={12} />}
                  sx={{ minWidth: 0, color: actionColor }}
                >
                  {staging ? 'Stage' : 'Delete'}
                </Button>
              </Tooltip>
            )}
            <Button
              size="small"
              onClick={() => sel.clear()}
              sx={{ minWidth: 0, opacity: 0.7 }}
            >
              Clear
            </Button>
          </Box>
        )}
      </Box>
      {vizPanel}
      {/* The table x-scrolls inside its own box on narrow screens; the page
          itself never scrolls sideways. */}
      <div style={{ overflowX: 'auto' }}>
      <DetailsTable
        root={root}
        rootLabel={rootLabel}
        children={paginatedChildren}
        uri={uri}
        routeType={routeType}
        onScanChild={handleScanChild}
        scanningPaths={scanningPaths}
        scanStatus={scan_status}
        scanTime={time}
        onRescan={handleRescan}
        isScanning={scanning}
        sorts={sorts}
        onSort={handleSort}
        onDelete={handleDelete}
        deletingPaths={deletingPaths}
        sel={sel}
        collapsedRows={collapsed_rows}
        tableRef={tableRef}
      />
      </div>
      {sortedChildren.length > pageSize && (
        <div style={{ display: 'flex', alignItems: 'center', gap: '1rem', marginTop: '0.5rem', fontSize: '0.85rem' }}>
          <span style={{ opacity: 0.7 }}>
            {page * pageSize + 1}-{Math.min((page + 1) * pageSize, sortedChildren.length)} of {sortedChildren.length}
          </span>
          <div style={{ display: 'flex', gap: '0.25rem' }}>
            <Button size="small" disabled={page === 0} onClick={() => setPage(0)} sx={{ minWidth: 0, padding: '2px 6px' }}>
              &laquo;
            </Button>
            <Button size="small" disabled={page === 0} onClick={() => setPage(p => p - 1)} sx={{ minWidth: 0, padding: '2px 6px' }}>
              &lsaquo;
            </Button>
            <Button size="small" disabled={page >= totalPages - 1} onClick={() => setPage(p => p + 1)} sx={{ minWidth: 0, padding: '2px 6px' }}>
              &rsaquo;
            </Button>
            <Button size="small" disabled={page >= totalPages - 1} onClick={() => setPage(totalPages - 1)} sx={{ minWidth: 0, padding: '2px 6px' }}>
              &raquo;
            </Button>
          </div>
          <select
            value={pageSize}
            onChange={e => { setPageSize(Number(e.target.value)); setPage(0) }}
            style={{ padding: '2px 4px', fontSize: '0.85rem' }}
          >
            <option value={25}>25</option>
            <option value={50}>50</option>
            <option value={100}>100</option>
            <option value={250}>250</option>
          </select>
        </div>
      )}
      {scanHistory && scanHistory.length > 1 && (
        <Box sx={{ mt: 2, height: 180, border: '1px solid rgba(255,255,255,0.08)', borderRadius: 1, padding: '4px 8px' }}>
          <Box sx={{ fontSize: '0.75rem', opacity: 0.7, mb: 0.5 }}>
            Size over time · {scanHistory.length} scans
          </Box>
          <Box sx={{ height: 'calc(100% - 20px)' }}>
            <BytesOverTime
              points={scanHistory.map(h => ({ time: h.time, bytes: h.size ?? null }))}
              formatBytes={formatSize}
            />
          </Box>
        </Box>
      )}
      {root.kind === 'file' && routeType === 'file' && <FilePreviewSection path={uri} />}
    </div>
  )
}
