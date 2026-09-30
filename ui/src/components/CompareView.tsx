import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import {
  Box,
  Button,
  CircularProgress,
  FormControl,
  InputLabel,
  MenuItem,
  Paper,
  Select,
  Tooltip,
  Typography,
} from '@mui/material'
import { FaArrowRight, FaFolder, FaFile, FaSync, FaList } from 'react-icons/fa'
import { DiffTable, DiffTreemap, deltaTextColor, mapMinFrac } from '@disk-tree/react'
import type { DiffInput, DiffMetric, DiffRecRow, DiffSubtree, DiffTableRow } from '@disk-tree/react'
import '@rdub/treemap/styles.css'
import { compareScans, compareScansRecursive, fetchDiffIndexStatus, fetchScanHistory, startScan } from '../api'
import type { CompareRecResult, CompareRecRow, CompareResult, ScanHistoryItem } from '../api'
import { useScanProgress } from '../hooks/useScanProgress'
import { useRecentPaths } from '../hooks/useRecentPaths'
import { formatSize, formatCount, timeAgo } from '../utils/format'
import { useTiling } from '../utils/tiling'
import { comparePathToUri, isSchemeRoot, uriToPath, type RouteType } from '../schemes'

function formatDelta(bytes: number): string {
  const sign = bytes < 0 ? '−' : '+'  // U+2212: a hyphen reads as a dash at small sizes
  return sign + formatSize(Math.abs(bytes)).replace(' ', '')
}

function formatDeltaNumber(n: number): string {
  const sign = n > 0 ? '+' : ''
  return sign + formatCount(Math.abs(n))
}

/** The two disk metrics the diff table (and treemap area) are built on: bytes
 *  and descendant count. `desc` is `dirOnly` (files render `-`). */
const DIFF_METRICS: DiffMetric[] = [
  { id: 'size', label: 'Size', fmt: formatSize, fmtDelta: formatDelta },
  { id: 'desc', label: 'Descendants', dirOnly: true, fmt: formatCount, fmtDelta: formatDeltaNumber },
]

// --- Adapters: the server's compare payload → the core widgets' shapes. -----

const toRecRow = (r: CompareRecRow): DiffRecRow => ({
  path: r.path,
  uri: r.uri,
  depth: r.depth,
  kind: r.kind,
  status: r.status,
  oldSize: r.size_a,
  newSize: r.size_b,
  delta: r.size_delta,
  countDelta: r.n_desc_delta,
  pruned: r.pruned,
})

const toDiffInput = (result: CompareResult, rec: CompareRecResult | undefined): DiffInput => ({
  uri: result.uri,
  flatRows: result.rows.map(r => ({
    path: r.path,
    uri: r.uri,
    kind: r.kind,
    status: r.status,
    oldSize: r.size_old ?? r.size ?? 0,
    newSize: r.size ?? 0,
  })),
  recRows: (rec?.rows ?? []).map(toRecRow),
  unchangedTop: (rec?.unchanged?.top ?? []).map(toRecRow),
  unchangedRest: rec?.unchanged?.rest ?? {},
  totalDelta: result.summary.total_delta,
  oldRootSize: result.scan1.size ?? 0,
  newRootSize: result.scan2.size ?? 0,
  oldRootCount: result.scan1.n_desc ?? 0,
  newRootCount: result.scan2.n_desc ?? 0,
  index: rec?.index ? { status: rec.index.status, error: rec.index.error } : undefined,
})

const toTableRows = (result: CompareResult): DiffTableRow[] => result.rows.map(r => ({
  key: r.path,
  path: r.path,
  uri: r.uri,
  kind: r.kind,
  status: r.status,
  values: {
    size: { old: r.size_old ?? r.size ?? null, new: r.size ?? null, delta: r.size_delta },
    desc: { old: r.n_desc_old ?? r.n_desc ?? null, new: r.n_desc ?? null, delta: r.n_desc_delta ?? 0 },
  },
}))

const toParent = (result: CompareResult) => ({
  uri: result.uri,
  values: {
    size: {
      old: result.scan1.size ?? null,
      new: result.scan2.size ?? null,
      delta: (result.scan2.size ?? 0) - (result.scan1.size ?? 0),
    },
    desc: {
      old: result.scan1.n_desc ?? null,
      new: result.scan2.n_desc ?? null,
      delta: (result.scan2.n_desc ?? 0) - (result.scan1.n_desc ?? 0),
    },
  },
})

function formatDateTime(dateStr: string): string {
  const date = new Date(dateStr)
  return date.toLocaleString()
}

// Check if a path is covered by a scan (path is at or below scan_path)
function isPathCoveredByScan(path: string, scanPath: string): boolean {
  if (!scanPath) return false
  // Normalize: ensure both have consistent trailing slash handling
  const normPath = path.endsWith('/') ? path.slice(0, -1) : path
  const normScan = scanPath.endsWith('/') ? scanPath.slice(0, -1) : scanPath
  return normPath === normScan || normPath.startsWith(normScan + '/')
}

// Breadcrumb component for compare view
function CompareBreadcrumbs({
  uri,
  routeType,
  scan1Path,
  scan2Path,
  scan1,
  scan2,
}: {
  uri: string
  routeType: RouteType
  scan1Path?: string
  scan2Path?: string
  scan1: number | ''
  scan2: number | ''
}) {
  // Split path into segments
  const isFile = routeType === 'file'
  const scheme = routeType // 's3' | 'gcs' | 'r2' | 'ssh' (file handled by isFile)
  const segments: { name: string; path: string }[] = []

  if (isFile) {
    // /Users/ryan/Library/...
    const parts = uri.split('/').filter(Boolean)
    let currentPath = ''
    for (const part of parts) {
      currentPath += '/' + part
      segments.push({ name: part, path: currentPath })
    }
  } else {
    // <scheme>://bucket/path/to/dir
    const withoutScheme = uri.slice(scheme.length + 3) // strip '<scheme>://'
    const parts = withoutScheme.split('/').filter(Boolean)
    let currentPath = `${scheme}:/`
    for (const part of parts) {
      currentPath += '/' + part
      segments.push({ name: part, path: currentPath })
    }
  }

  return (
    <Typography
      variant="body2"
      sx={{ mb: 3, fontFamily: 'monospace', display: 'flex', flexWrap: 'wrap', alignItems: 'center' }}
    >
      {isFile && <span style={{ color: '#8b949e' }}>/</span>}
      {!isFile && <span style={{ color: '#8b949e' }}>{scheme}://</span>}
      {segments.map((seg, i) => {
        const basePath = `/compare${uriToPath(seg.path)}`
        const params = new URLSearchParams()
        if (scan1 !== '') params.set('scan1', String(scan1))
        if (scan2 !== '') params.set('scan2', String(scan2))
        const compareUrl = params.toString() ? `${basePath}?${params}` : basePath

        // Check if this segment is covered by both scans
        const coveredBy1 = scan1Path ? isPathCoveredByScan(seg.path, scan1Path) : true
        const coveredBy2 = scan2Path ? isPathCoveredByScan(seg.path, scan2Path) : true
        const fullyCovered = coveredBy1 && coveredBy2
        const partiallyCovered = coveredBy1 || coveredBy2

        // Style based on coverage - brighter colors for better visibility
        const color = fullyCovered ? '#e6edf3' : partiallyCovered ? '#b0b8c1' : '#8b949e'
        const opacity = 1

        return (
          <span key={seg.path} style={{ display: 'inline-flex', alignItems: 'center' }}>
            <Link
              to={compareUrl}
              style={{
                color,
                opacity,
                textDecoration: 'none',
              }}
              title={
                fullyCovered
                  ? 'Both scans cover this path'
                  : partiallyCovered
                    ? 'Only one scan covers this path'
                    : 'Neither scan covers this path'
              }
              onMouseEnter={(e) => (e.currentTarget.style.textDecoration = 'underline')}
              onMouseLeave={(e) => (e.currentTarget.style.textDecoration = 'none')}
            >
              {seg.name}
            </Link>
            {i < segments.length - 1 && (
              <span style={{ color: '#6e7681', margin: '0 2px' }}>/</span>
            )}
          </span>
        )
      })}
    </Typography>
  )
}

function Summary({ result }: { result: CompareResult }) {
  const { summary, scan1, scan2 } = result

  return (
    <Paper sx={{ p: 2, mb: 3 }}>
      <Box sx={{ display: 'flex', gap: 4, flexWrap: 'wrap', alignItems: 'center' }}>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 2 }}>
          <Box>
            <Typography variant="caption" color="text.secondary">From</Typography>
            <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
              {formatDateTime(scan1.time)}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {formatSize(scan1.size)}
            </Typography>
          </Box>
          <FaArrowRight color="#8b949e" />
          <Box>
            <Typography variant="caption" color="text.secondary">To</Typography>
            <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
              {formatDateTime(scan2.time)}
            </Typography>
            <Typography variant="caption" color="text.secondary">
              {formatSize(scan2.size)}
            </Typography>
          </Box>
        </Box>
        <Box sx={{ borderLeft: '1px solid rgba(255,255,255,0.1)', pl: 3, display: 'flex', gap: 3 }}>
          <Box>
            <Typography variant="caption" color="text.secondary">Added</Typography>
            <Typography variant="body1" sx={{ color: '#3fb950', fontWeight: 'bold' }}>
              {summary.added}
            </Typography>
          </Box>
          <Box>
            <Typography variant="caption" color="text.secondary">Removed</Typography>
            <Typography variant="body1" sx={{ color: '#f85149', fontWeight: 'bold' }}>
              {summary.removed}
            </Typography>
          </Box>
          <Box>
            <Typography variant="caption" color="text.secondary">Changed</Typography>
            <Typography variant="body1" sx={{ color: '#d29922', fontWeight: 'bold' }}>
              {summary.changed}
            </Typography>
          </Box>
        </Box>
        <Box sx={{ borderLeft: '1px solid rgba(255,255,255,0.1)', pl: 3 }}>
          <Typography variant="caption" color="text.secondary">Total Delta</Typography>
          <Typography
            variant="body1"
            sx={{
              fontWeight: 'bold',
              fontFamily: 'monospace',
              color: deltaTextColor(summary.total_delta),
            }}
          >
            {formatDelta(summary.total_delta)}
          </Typography>
        </Box>
      </Box>
    </Paper>
  )
}

export function CompareView() {
  const location = useLocation()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()

  // Extract URI and routeType from path: /compare/file/Users/ryan/…, /compare/s3/bucket/…,
  // /compare/gcs/bucket/…, /compare/r2/bucket/…, /compare/ssh/host/…
  const pathAfterCompare = location.pathname.replace(/^\/compare/, '') || '/'
  const { uri, routeType } = comparePathToUri(pathAfterCompare)

  // Get scan selections from URL params
  const urlScan1 = searchParams.get('scan1')
  const urlScan2 = searchParams.get('scan2')
  const scan1: number | '' = urlScan1 ? parseInt(urlScan1, 10) : ''
  const scan2: number | '' = urlScan2 ? parseInt(urlScan2, 10) : ''

  // Update URL params when selections change
  const setScan1 = (id: number | '') => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (id === '') {
        next.delete('scan1')
      } else {
        next.set('scan1', String(id))
      }
      return next
    }, { replace: true })
  }
  const setScan2 = (id: number | '') => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (id === '') {
        next.delete('scan2')
      } else {
        next.set('scan2', String(id))
      }
      return next
    }, { replace: true })
  }
  const setScans = (id1: number | '', id2: number | '') => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (id1 === '') {
        next.delete('scan1')
      } else {
        next.set('scan1', String(id1))
      }
      if (id2 === '') {
        next.delete('scan2')
      } else {
        next.set('scan2', String(id2))
      }
      return next
    }, { replace: true })
  }

  const [history, setHistory] = useState<ScanHistoryItem[]>([])
  const [historyLoading, setHistoryLoading] = useState(true)
  const [result, setResult] = useState<CompareResult | null>(null)
  const [recResult, setRecResult] = useState<CompareRecResult | null>(null)
  const [recState, setRecState] = useState<'loading' | 'error' | 'ready'>('loading')
  const [recAttempt, setRecAttempt] = useState(0)
  // Measured at fetch time so the response carries only drawable cells (the
  // map may not be mounted for the very first request; the server's default
  // applies then).
  const mapRef = useRef<HTMLDivElement>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [tiling, setTiling] = useTiling()

  // Record visit to recent paths
  const { recordVisit } = useRecentPaths()
  useEffect(() => {
    if (uri && !isSchemeRoot(uri)) {
      recordVisit(uri, 'compare')
    }
  }, [uri, recordVisit])

  // Track previous URI to detect navigation
  const prevUriRef = useRef(uri)

  // Load scan history
  useEffect(() => {
    const isNavigation = prevUriRef.current !== uri
    prevUriRef.current = uri

    setHistory([])
    setHistoryLoading(true)
    setResult(null)
    setError(null)

    fetchScanHistory(uri)
      .then(h => {
        setHistory(h)

        // If we have URL scan params, keep them - they may be valid even if not in
        // this path's history (e.g., for a newly added directory that didn't exist
        // in the older scan, the older scan won't be in history, but it's still
        // a valid comparison showing the directory was added)
        if (scan1 !== '' && scan2 !== '') {
          // URL params present - keep them, let compare API handle the details
          // Just ensure correct order if both are in history
          const scan1Item = h.find(item => item.id === scan1)
          const scan2Item = h.find(item => item.id === scan2)
          if (scan1Item && scan2Item) {
            const time1 = new Date(scan1Item.time).getTime()
            const time2 = new Date(scan2Item.time).getTime()
            if (time1 > time2) {
              // Swap to ensure scan1 is older
              setScans(scan2, scan1)
            }
          }
          // If not both in history, keep them as-is - the compare API will handle it
        } else if (!isNavigation && h.length >= 2) {
          // Initial page load without URL params - auto-select most recent two
          setScans(h[1].id, h[0].id) // h[1] is older, h[0] is newer (sorted DESC)
        }
        // If navigating with no URL params and < 2 scans in history, leave empty
      })
      .catch(err => setError(err.message))
      .finally(() => setHistoryLoading(false))
  }, [uri])

  // Fetch comparison when both scans selected
  useEffect(() => {
    if (scan1 === '' || scan2 === '' || scan1 === scan2) {
      setResult(null)
      return
    }

    setLoading(true)
    setError(null)
    compareScans(uri, scan1 as number, scan2 as number)
      .then(setResult)
      .catch(err => setError(err.message))
      .finally(() => setLoading(false))
  }, [uri, scan1, scan2])

  // The recursive frontier feeds the nested treemap; the table stays on the
  // depth-1 rows. The flat view stands alone without it, but its absence is
  // surfaced (legend spinner / retry) — never a silently-grey map.
  useEffect(() => {
    if (scan1 === '' || scan2 === '' || scan1 === scan2) {
      setRecResult(null)
      return
    }
    let stale = false
    let timer: ReturnType<typeof setTimeout> | undefined
    setRecResult(null)
    setRecState('loading')
    const s1 = scan1 as number, s2 = scan2 as number
    // While the server builds the pair's full diff index, the walk's answer
    // stands in; poll, then refetch silently (no spinner — the map just gets
    // complete) once it lands.
    const pollIndex = () => {
      timer = setTimeout(() => {
        fetchDiffIndexStatus(s1, s2)
          .then(st => {
            if (stale) return
            if (st.status === 'done') {
              compareScansRecursive(uri, s1, s2, 200, mapMinFrac(mapRef))
                .then(r => { if (!stale) setRecResult(r) })
                .catch(() => { /* keep the walk result */ })
            } else if (st.status === 'building' || st.status === 'none') {
              pollIndex()
            }
          })
          .catch(() => { if (!stale) pollIndex() })
      }, 3000)
    }
    compareScansRecursive(uri, s1, s2, 200, mapMinFrac(mapRef))
      .then(r => {
        if (stale) return
        setRecResult(r)
        setRecState('ready')
        if (r.index?.status === 'building' || r.index?.status === 'none') pollIndex()
      })
      .catch(() => { if (!stale) setRecState('error') })
    return () => { stale = true; if (timer) clearTimeout(timer) }
  }, [uri, scan1, scan2, recAttempt])

  // Get scan_path for selected scans (for breadcrumb coverage highlighting)
  const scan1Item = history.find(h => h.id === scan1)
  const scan2Item = history.find(h => h.id === scan2)
  const scan1Path = scan1Item?.scan_path ?? scan1Item?.path
  const scan2Path = scan2Item?.scan_path ?? scan2Item?.path

  // Scan progress tracking
  const scanProgress = useScanProgress()
  const [scanningPath, setScanningPath] = useState<string | null>(null)

  const handleStartScan = async (path: string) => {
    try {
      setScanningPath(path)
      await startScan(path)
      // Refresh history after scan completes (SSE will update progress)
    } catch (err) {
      console.error('Failed to start scan:', err)
      setScanningPath(null)
    }
  }

  // Check if there's an active scan for a path
  const isScanning = (path: string) => {
    return scanProgress.some(s => s.path === path && s.status === 'running')
  }

  // Get progress for a path
  const getProgress = (path: string) => {
    return scanProgress.find(s => s.path === path && s.status === 'running')
  }

  // Clear scanningPath when scan completes
  useEffect(() => {
    if (scanningPath && !isScanning(scanningPath)) {
      setScanningPath(null)
      // Refresh history and set new scan as "after"
      fetchScanHistory(uri).then(h => {
        setHistory(h)
        if (h.length >= 1) {
          // New scan becomes the "after" (scan2)
          // Keep current scan1 if it's valid, otherwise promote current scan2
          const currentScan1Valid = scan1 !== '' && h.some(item => item.id === scan1)
          if (currentScan1Valid) {
            setScans(scan1, h[0].id)
          } else if (scan2 !== '') {
            // Promote current scan2 to scan1 (shift the window)
            setScans(scan2, h[0].id)
          } else {
            // No prior selections, just set the new scan
            setScans('', h[0].id)
          }
        }
      })
    }
  }, [scanProgress, scanningPath, uri, scan1, scan2])

  // Preserve scan1/scan2 query params on a link/drill so the sub-view shows
  // the same snapshot pair.
  const scanQuery = () => {
    const q = new URLSearchParams()
    if (scan1 !== '') q.set('scan1', String(scan1))
    if (scan2 !== '') q.set('scan2', String(scan2))
    return q.toString() ? `?${q}` : ''
  }

  return (
    <Box sx={{ p: { xs: 1, sm: 3 }, maxWidth: 1400, margin: '0 auto' }}>
      <Typography variant="h5" sx={{ mb: 1, display: 'flex', alignItems: 'center', gap: 1 }}>
        <FaFolder color="#54aeff" />
        Compare Scans
      </Typography>
      <CompareBreadcrumbs
        uri={uri}
        routeType={routeType}
        scan1Path={scan1Path}
        scan2Path={scan2Path}
        scan1={scan1}
        scan2={scan2}
      />

      {/* Show comparison UI if we have URL params (even with 0-1 scans in local history,
          because the path may be newly added and the scans come from an ancestor) */}
      {historyLoading ? (
        <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}>
          <CircularProgress />
        </Box>
      ) : history.length === 0 && (scan1 === '' || scan2 === '') ? (
        <Paper sx={{ p: 3, textAlign: 'center' }}>
          <Typography color="text.secondary" sx={{ mb: 2 }}>
            No scans found for this path.
          </Typography>
          <Button
            variant="outlined"
            onClick={() => handleStartScan(uri)}
            disabled={isScanning(uri)}
            startIcon={isScanning(uri) ? <CircularProgress size={14} /> : <FaSync />}
          >
            {isScanning(uri) ? 'Scanning...' : 'Scan Now'}
          </Button>
        </Paper>
      ) : history.length === 1 && (scan1 === '' || scan2 === '') ? (
        <Paper sx={{ p: 3 }}>
          <Typography color="text.secondary" sx={{ mb: 2 }}>
            Only one scan found for this path. This directory may have been added recently.
          </Typography>
          <Box sx={{ display: 'flex', gap: 3, alignItems: 'center', mb: 2 }}>
            <Box>
              <Typography variant="caption" color="text.secondary">Scanned</Typography>
              <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                {formatDateTime(history[0].time)} ({timeAgo(history[0].time)})
              </Typography>
            </Box>
            <Box>
              <Typography variant="caption" color="text.secondary">Size</Typography>
              <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                {formatSize(history[0].size)}
              </Typography>
            </Box>
            {history[0].n_desc != null && (
              <Box>
                <Typography variant="caption" color="text.secondary">Files</Typography>
                <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                  {formatCount(history[0].n_desc)}
                </Typography>
              </Box>
            )}
          </Box>
          <Button
            variant="outlined"
            onClick={() => handleStartScan(uri)}
            disabled={isScanning(uri)}
            startIcon={isScanning(uri) ? <CircularProgress size={14} /> : <FaSync />}
          >
            {isScanning(uri) ? 'Scanning...' : 'Rescan to Compare'}
          </Button>
        </Paper>
      ) : (
        <>
          {/* Show dropdowns only if history has enough scans; otherwise show simpler header */}
          {history.length >= 2 ? (
            <Box sx={{ display: 'flex', gap: 2, mb: 3, alignItems: 'center', flexWrap: 'wrap' }}>
              <FormControl sx={{ minWidth: 300 }}>
                <InputLabel>From (older)</InputLabel>
                <Select
                  value={scan1}
                  label="From (older)"
                  onChange={(e) => setScan1(e.target.value as number)}
                >
                  {history.map(h => (
                    <MenuItem key={h.id} value={h.id}>
                      {timeAgo(h.time)} — {formatSize(h.size)} — {formatDateTime(h.time)}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
              <FaArrowRight color="#8b949e" />
              <FormControl sx={{ minWidth: 300 }}>
                <InputLabel>To (newer)</InputLabel>
                <Select
                  value={scan2}
                  label="To (newer)"
                  onChange={(e) => setScan2(e.target.value as number)}
                >
                  {history.map(h => (
                    <MenuItem key={h.id} value={h.id}>
                      {timeAgo(h.time)} — {formatSize(h.size)} — {formatDateTime(h.time)}
                    </MenuItem>
                  ))}
                </Select>
              </FormControl>
              <Box sx={{ ml: 'auto', display: 'flex', gap: 1 }}>
                <Tooltip title="View directory tree">
                  <Button
                    component={Link}
                    to={`${uriToPath(uri)}${scan2 !== '' ? `?scan_id=${scan2}` : ''}`}
                    variant="outlined"
                    size="small"
                    startIcon={<FaList />}
                  >
                    Tree
                  </Button>
                </Tooltip>
                <Tooltip title={`Rescan ${uri}`}>
                  <span>
                    <Button
                      variant="outlined"
                      size="small"
                      onClick={() => handleStartScan(uri)}
                      disabled={isScanning(uri)}
                      startIcon={isScanning(uri) ? <CircularProgress size={14} /> : <FaSync />}
                    >
                      {isScanning(uri) ? (
                        getProgress(uri)?.items_found
                          ? `${getProgress(uri)!.items_found.toLocaleString()} items`
                          : 'Scanning...'
                      ) : 'Rescan'}
                    </Button>
                  </span>
                </Tooltip>
              </Box>
            </Box>
          ) : (
            /* History has < 2 scans but we have URL params - show simpler header
               (for newly added/removed directories where path doesn't have full history) */
            <Box sx={{ display: 'flex', gap: 2, mb: 3, alignItems: 'center', flexWrap: 'wrap' }}>
              {result ? (
                <>
                  <Typography variant="body2" sx={{ fontFamily: 'monospace' }}>
                    Comparing scans from {formatDateTime(result.scan1.time)} → {formatDateTime(result.scan2.time)}
                  </Typography>
                  <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                    {/* No scans of this exact path: usually a subtree of a
                        broader scan (sizes on both sides), else it really
                        was added/removed. */}
                    {result.scan1.size != null && result.scan2.size != null
                      ? `(within scans of ${result.scan1.scan_path ?? 'an ancestor'})`
                      : result.scan2.size != null
                      ? '(this path was added between scans)'
                      : result.scan1.size != null
                      ? '(this path was removed between scans)'
                      : '(this path is absent from both scans)'}
                  </Typography>
                </>
              ) : loading ? null : (
                <Typography variant="body2" color="text.secondary">
                  Loading comparison...
                </Typography>
              )}
              <Box sx={{ ml: 'auto', display: 'flex', gap: 1 }}>
                <Tooltip title="View directory tree">
                  <Button
                    component={Link}
                    to={`${uriToPath(uri)}${scan2 !== '' ? `?scan_id=${scan2}` : ''}`}
                    variant="outlined"
                    size="small"
                    startIcon={<FaList />}
                  >
                    Tree
                  </Button>
                </Tooltip>
                <Tooltip title={`Rescan ${uri}`}>
                  <span>
                    <Button
                      variant="outlined"
                      size="small"
                      onClick={() => handleStartScan(uri)}
                      disabled={isScanning(uri)}
                      startIcon={isScanning(uri) ? <CircularProgress size={14} /> : <FaSync />}
                    >
                      {isScanning(uri) ? (
                        getProgress(uri)?.items_found
                          ? `${getProgress(uri)!.items_found.toLocaleString()} items`
                          : 'Scanning...'
                      ) : 'Rescan'}
                    </Button>
                  </span>
                </Tooltip>
              </Box>
            </Box>
          )}

          {error && (
            <Paper sx={{ p: 2, mb: 3, backgroundColor: 'rgba(248, 81, 73, 0.1)' }}>
              <Typography color="error">{error}</Typography>
            </Paper>
          )}

          {loading && (
            <Box sx={{ display: 'flex', justifyContent: 'center', p: 4 }}>
              <CircularProgress />
            </Box>
          )}

          {!loading && !result && history.length >= 2 && (scan1 === '' || scan2 === '') && (
            <Paper sx={{ p: 3, textAlign: 'center' }}>
              <Typography color="text.secondary">
                Select two scans to compare.
              </Typography>
            </Paper>
          )}

          {result && !loading && (
            <>
              <Summary result={result} />
              <DiffTreemap
                input={toDiffInput(result, recResult ?? undefined)}
                recState={recState}
                onRecRetry={() => setRecAttempt(a => a + 1)}
                onDrill={childUri => navigate(`/compare${uriToPath(childUri)}${scanQuery()}`)}
                cellHref={childUri => `/compare${uriToPath(childUri)}?scan1=${scan1}&scan2=${scan2}`}
                fetchSubtree={async (n): Promise<DiffSubtree> => {
                  const sub = await compareScansRecursive(n.uri, scan1 as number, scan2 as number, 200, mapMinFrac(mapRef))
                  return {
                    recRows: sub.rows.map(toRecRow),
                    unchangedTop: (sub.unchanged?.top ?? []).map(toRecRow),
                    unchangedRest: sub.unchanged?.rest ?? {},
                  }
                }}
                formatSize={formatSize}
                formatCount={formatCount}
                tiling={tiling}
                setTiling={setTiling}
                mapRef={mapRef}
                renderContainer={children => <Paper sx={{ p: 0, mb: 3, overflow: 'hidden' }}>{children}</Paper>}
                renderOverlay={(rs, onRetry) => (
                  <Box
                    sx={{
                      position: 'absolute', inset: 0, zIndex: 2,
                      display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                      gap: 1.5, bgcolor: 'rgba(8, 10, 12, 0.55)',
                      pointerEvents: rs === 'error' ? 'auto' : 'none',
                    }}
                  >
                    {rs === 'loading' ? (
                      <>
                        <CircularProgress size={40} />
                        <Typography variant="body2" sx={{ opacity: 0.9 }}>computing Δ detail…</Typography>
                      </>
                    ) : (
                      <Button size="small" color="error" variant="outlined" onClick={onRetry}>
                        Δ detail failed — retry
                      </Button>
                    )}
                  </Box>
                )}
                renderEmpty={({ areaMode, showUnchanged, onShowUnchanged }) => (
                  <Paper sx={{ p: 3, textAlign: 'center' }}>
                    <Typography color="text.secondary" variant="body2">
                      {areaMode === 'max' && showUnchanged
                        ? 'Nothing to plot — no row has any bytes on either side.'
                        : 'No size deltas to plot — every row is unchanged.'}
                    </Typography>
                    {!showUnchanged && (
                      <Button size="small" sx={{ mt: 1 }} onClick={onShowUnchanged}>
                        show unchanged
                      </Button>
                    )}
                  </Paper>
                )}
              />
              <Paper sx={{ overflowX: 'auto' }}>
                <DiffTable
                  rows={toTableRows(result)}
                  parent={toParent(result)}
                  metrics={DIFF_METRICS}
                  unchangedCount={result.summary.unchanged}
                  renderIcon={({ kind }) => kind === 'dir'
                    ? <FaFolder size={14} color="#54aeff" style={{ flexShrink: 0 }} />
                    : <FaFile size={14} color="#8b949e" style={{ flexShrink: 0 }} />}
                  renderPathLink={(row, children) => (
                    <Link
                      to={`/compare${uriToPath(row.uri)}${scanQuery()}`}
                      style={{ fontFamily: 'monospace', fontSize: '0.9em', color: 'inherit', textDecoration: 'none' }}
                    >
                      {children}
                    </Link>
                  )}
                  rowAction={({ uri: rowUri, kind, path }) => kind === 'dir' ? (
                    <Tooltip title={`Scan ${path}`}>
                      <span>
                        <Button
                          size="small"
                          onClick={() => handleStartScan(rowUri)}
                          disabled={isScanning(rowUri)}
                          sx={{ minWidth: 'auto', padding: '2px 6px' }}
                        >
                          {isScanning(rowUri) ? <CircularProgress size={12} /> : <FaSync size={10} />}
                        </Button>
                      </span>
                    </Tooltip>
                  ) : null}
                />
              </Paper>
            </>
          )}
        </>
      )}
    </Box>
  )
}
