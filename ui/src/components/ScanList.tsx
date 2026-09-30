import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Box,
  Button,
  CircularProgress,
  LinearProgress,
  MenuItem,
  Paper,
  Select,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material'
import { FaPlay, FaExchangeAlt } from 'react-icons/fa'
import { fetchScans, startScan } from '../api'
import type { Scan, ScanJob, ScanProgress } from '../api'
import { useCapabilities } from '../hooks/useCapabilities'
import { useScanProgress } from '../hooks/useScanProgress'
import { DataTable } from './DataTable'
import type { Column } from './DataTable'
import { UnionTreemap } from './UnionTreemap'
import { elapsed, formatCount } from '../utils/format'
import { uriToPath, type RouteType } from '../schemes'

const scanColumns: Column<Scan>[] = [
  {
    key: 'path',
    label: 'Path',
    render: scan => (
      <Link to={uriToPath(scan.path)}>
        <code>{scan.path}</code>
      </Link>
    ),
  },
  { key: 'size', label: 'Size', type: 'size' },
  { key: 'n_desc', label: 'Items', type: 'count' },
  { key: 'n_children', label: 'Children', type: 'count' },
  { key: 'time', label: 'Scanned', type: 'time' },
]

/** Compare-action column — a jump straight from the landing table into a path's
 *  diff view (`/compare/<path>`, CompareView auto-selects the latest two scans).
 *  Appended only where the deployment serves compare (`caps?.compare`); the
 *  target handles the "only one scan yet" case itself. */
const compareColumn: Column<Scan> = {
  key: 'compare',
  label: '',
  align: 'center',
  shrink: true,
  render: scan => (
    <Tooltip title="Compare scans of this path">
      <Link to={`/compare${uriToPath(scan.path)}`}>
        <Button size="small" sx={{ minWidth: 0, padding: '2px 6px' }}>
          <FaExchangeAlt size={12} />
        </Button>
      </Link>
    </Tooltip>
  ),
}

function LiveScanProgress({ progress }: { progress: ScanProgress[] }) {
  const activeScans = progress.filter(p => p.status === 'running')
  const [, setTick] = useState(0)

  // Tick every second so elapsed times update live
  useEffect(() => {
    if (activeScans.length === 0) return
    const id = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(id)
  }, [activeScans.length])

  if (activeScans.length === 0) return null

  return (
    <Paper sx={{ p: 2, mb: 2 }}>
      <Typography variant="subtitle2" sx={{ mb: 2, display: 'flex', alignItems: 'center', gap: 1 }}>
        <CircularProgress size={16} />
        Scans in Progress (Live)
      </Typography>
      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {activeScans.map(scan => (
          <Box key={scan.id} sx={{ borderLeft: '3px solid', borderColor: 'primary.main', pl: 2 }}>
            <Typography variant="body2" sx={{ fontFamily: 'monospace', mb: 0.5 }}>
              {scan.path}
            </Typography>
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 2, mb: 0.5 }}>
              <LinearProgress
                variant="indeterminate"
                sx={{ flexGrow: 1, height: 6, borderRadius: 1 }}
              />
            </Box>
            <Box sx={{ display: 'flex', gap: 3, color: 'text.secondary', fontSize: '0.85rem' }}>
              <span><strong>{formatCount(scan.items_found)}</strong> items</span>
              {scan.items_per_sec && (
                <span>{formatCount(Math.round(scan.items_per_sec))} items/sec</span>
              )}
              {scan.error_count > 0 && (
                <span style={{ color: '#ed6c02' }}>{scan.error_count} errors</span>
              )}
              <span>{elapsed(scan.started)}</span>
            </Box>
          </Box>
        ))}
      </Box>
    </Paper>
  )
}


function NewScanForm({ onStarted }: { onStarted: (job: ScanJob) => void }) {
  const [path, setPath] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!path.trim()) return

    setLoading(true)
    setError(null)
    try {
      const job = await startScan(path.trim())
      onStarted(job)
      setPath('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start scan')
    } finally {
      setLoading(false)
    }
  }

  const caps = useCapabilities()
  if (!caps?.scan) return null

  return (
    <Paper sx={{ p: 2, mb: 2 }}>
      <Typography variant="subtitle2" sx={{ mb: 1 }}>Start New Scan</Typography>
      <form onSubmit={handleSubmit}>
        <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
          <Tooltip title="Local path (/Users/ryan), S3/GCS/R2 URI (s3://bucket/prefix, gcs://bucket/prefix, r2://bucket/prefix), or SSH (ssh://m1/Users/ryan or m1:/Users/ryan). Live scanning is currently implemented for local, s3, and ssh; gcs/r2 URIs are only reachable via imported scans (see `disk-tree import`).">
            <TextField
              size="small"
              placeholder="/path or s3://bucket or gcs://bucket or ssh://host/path"
              value={path}
              onChange={e => setPath(e.target.value)}
              error={!!error}
              helperText={error}
              sx={{ flexGrow: 1 }}
            />
          </Tooltip>
          <Button
            type="submit"
            variant="contained"
            disabled={loading || !path.trim()}
            startIcon={loading ? <CircularProgress size={16} /> : <FaPlay />}
          >
            Scan
          </Button>
        </Box>
      </form>
    </Paper>
  )
}

/**
 * The scan landing. Without `scheme` it is the `/` union root over *every*
 * scan ("all scans"); with a `scheme` it is that scheme's root (`/r2` → the R2
 * buckets), filtered to scans under `<scheme>://`. Same component, so a
 * multi-cloud deployment gets a per-cloud page for free (spec `union-of-roots.md`).
 */
export function ScanList({ scheme }: { scheme?: Exclude<RouteType, 'file'> } = {}) {
  const [scans, setScans] = useState<Scan[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(50)

  const caps = useCapabilities()

  // On a scheme-scoped landing (`/` = R2 here) the `<scheme>://` prefix is
  // implied by the page/root label above, so strip it from every bucket cell
  // and table row — `r2://ctbk` → `ctbk`. Links still target the full URI.
  const stripPrefix = useCallback(
    (p: string) => {
      if (!scheme) return p
      const prefix = `${scheme}://`
      return p.startsWith(prefix) ? p.slice(prefix.length) : p
    },
    [scheme],
  )

  const columns = useMemo(() => {
    const base: Column<Scan>[] = scheme
      ? [
          {
            key: 'path',
            label: 'Path',
            render: scan => (
              <Link to={uriToPath(scan.path)}>
                <code>{stripPrefix(scan.path)}</code>
              </Link>
            ),
          },
          ...scanColumns.slice(1),
        ]
      : scanColumns
    return caps?.compare ? [...base, compareColumn] : base
  }, [caps?.compare, scheme, stripPrefix])

  // Live progress from SSE
  const scanProgress = useScanProgress()

  // Scope to one scheme's roots when this is a per-scheme landing (`/r2`).
  const visibleScans = useMemo(
    () => (scheme ? scans.filter(s => s.path.startsWith(`${scheme}://`)) : scans),
    [scans, scheme],
  )

  // Pagination
  const totalPages = Math.ceil(visibleScans.length / pageSize)
  const paginatedScans = useMemo(() => {
    const start = page * pageSize
    return visibleScans.slice(start, start + pageSize)
  }, [visibleScans, page, pageSize])

  const loadData = async () => {
    try {
      const scansData = await fetchScans()
      setScans(scansData)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load data')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    loadData()
  }, [])

  // Refresh scans list when a live scan completes (SSE shows empty but we had scans)
  const [prevProgressCount, setPrevProgressCount] = useState(0)
  useEffect(() => {
    if (prevProgressCount > 0 && scanProgress.length === 0) {
      // A scan just finished - refresh the list
      fetchScans().then(setScans)
    }
    setPrevProgressCount(scanProgress.length)
  }, [scanProgress.length, prevProgressCount])

  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  const handleNewScan = (_job: ScanJob) => {
    // Scan progress is tracked via SSE, no need to track here
  }

  if (loading) return <div>Loading scans...</div>
  if (error) return <div>Error: {error}</div>

  return (
    <div>
      <NewScanForm onStarted={handleNewScan} />
      <LiveScanProgress progress={scanProgress} />
      {/* Union of the (scheme-scoped) scans as one treemap — each cell drills
          into that scan, so the separate rows below read as a single top map.
          The treemap's own root row (`r2:// — 873 G`) is the page's one title,
          so there's no separate `<h1>` repeating it. */}
      <UnionTreemap
        items={visibleScans.map(s => ({ name: stripPrefix(s.path), size: s.size ?? 0, path: s.path }))}
        rootName={scheme ? `${scheme}://` : 'all scans'}
      />
      <Tooltip title="Previously completed scans. Click a path to browse its contents.">
        <Typography variant="subtitle2" sx={{ mb: 1, mt: 2 }}>Completed Scans</Typography>
      </Tooltip>
      <DataTable<Scan>
        columns={columns}
        data={paginatedScans}
        rowKey={scan => scan.id}
      />
      {visibleScans.length > pageSize && (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 1, fontSize: '0.85rem' }}>
          <span style={{ opacity: 0.7 }}>
            {page * pageSize + 1}-{Math.min((page + 1) * pageSize, visibleScans.length)} of {visibleScans.length}
          </span>
          <Box sx={{ display: 'flex', gap: 0.5 }}>
            <Button size="small" disabled={page === 0} onClick={() => setPage(0)} sx={{ minWidth: 0, padding: '2px 6px' }}>
              ⏮
            </Button>
            <Button size="small" disabled={page === 0} onClick={() => setPage(p => p - 1)} sx={{ minWidth: 0, padding: '2px 6px' }}>
              ◀
            </Button>
            <Button size="small" disabled={page >= totalPages - 1} onClick={() => setPage(p => p + 1)} sx={{ minWidth: 0, padding: '2px 6px' }}>
              ▶
            </Button>
            <Button size="small" disabled={page >= totalPages - 1} onClick={() => setPage(totalPages - 1)} sx={{ minWidth: 0, padding: '2px 6px' }}>
              ⏭
            </Button>
          </Box>
          <Select
            size="small"
            value={pageSize}
            onChange={e => { setPageSize(Number(e.target.value)); setPage(0) }}
            sx={{ fontSize: '0.85rem', height: '28px' }}
          >
            <MenuItem value={25}>25</MenuItem>
            <MenuItem value={50}>50</MenuItem>
            <MenuItem value={100}>100</MenuItem>
            <MenuItem value={250}>250</MenuItem>
          </Select>
        </Box>
      )}
    </div>
  )
}
