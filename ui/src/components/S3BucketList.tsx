import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Box, Button, Checkbox, CircularProgress, Paper, TextField, Tooltip, Typography } from '@mui/material'
import { FaCloud, FaFolder, FaPlay, FaSync } from 'react-icons/fa'
import { useQuery } from '@tanstack/react-query'
import { fetchS3Buckets, startScan, fetchScanStatus } from '../api'
import type { S3Bucket, ScanJob } from '../api'
import { useCapabilities } from '../hooks/useCapabilities'
import { timeAgo, formatSize } from '../utils/format'
import { UnionTreemap } from './UnionTreemap'

function NewS3ScanForm({ onStarted }: { onStarted: (path: string, job: ScanJob) => void }) {
  const [path, setPath] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    let scanPath = path.trim()
    if (!scanPath) return

    // Ensure it starts with s3://
    if (!scanPath.startsWith('s3://')) {
      scanPath = `s3://${scanPath}`
    }

    setLoading(true)
    setError(null)
    try {
      const job = await startScan(scanPath)
      onStarted(scanPath, job)
      setPath('')
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start scan')
    } finally {
      setLoading(false)
    }
  }

  return (
    <Paper sx={{ p: 2, mb: 2 }}>
      <Typography variant="subtitle2" sx={{ mb: 1 }}>Scan S3 Bucket</Typography>
      <form onSubmit={handleSubmit}>
        <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-start' }}>
          <Tooltip title="Enter an S3 URI (e.g., bucket-name or bucket-name/prefix)">
            <TextField
              size="small"
              placeholder="bucket-name/prefix"
              value={path}
              onChange={e => setPath(e.target.value)}
              error={!!error}
              helperText={error}
              sx={{ flexGrow: 1 }}
              slotProps={{
                input: {
                  startAdornment: <span style={{ opacity: 0.5, marginRight: 4 }}>s3://</span>,
                },
              }}
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

const EMPTY_BUCKETS: S3Bucket[] = []

export function S3BucketList() {
  const caps = useCapabilities()
  const s3Available = caps?.s3 !== false
  const { data: rawBuckets = EMPTY_BUCKETS, isLoading, error, refetch } = useQuery({
    queryKey: ['s3-buckets'],
    queryFn: fetchS3Buckets,
    staleTime: 5 * 60 * 1000, // 5 minutes - bucket list is expensive and rarely changes
    enabled: s3Available, // no live bucket lister on a static deployment
  })

  // Sort: scanned buckets first by size desc, then unscanned by name
  const buckets = useMemo(() => {
    return [...rawBuckets].sort((a, b) => {
      const aScanned = a.size != null
      const bScanned = b.size != null
      if (aScanned && !bScanned) return -1
      if (!aScanned && bScanned) return 1
      if (aScanned && bScanned) return (b.size ?? 0) - (a.size ?? 0)
      return a.name.localeCompare(b.name)
    })
  }, [rawBuckets])

  const [scanJobs, setScanJobs] = useState<Map<string, ScanJob>>(new Map())
  const [mutationError, setMutationError] = useState<string | null>(null)

  // Selection state
  const [selectedBuckets, setSelectedBuckets] = useState<Set<string>>(new Set())
  const [anchorIndex, setAnchorIndex] = useState<number | null>(null)
  const [expandDirection, setExpandDirection] = useState<'up' | 'down' | null>(null)
  const [hoveredIndex, setHoveredIndex] = useState<number | null>(null)
  const tableRef = useRef<HTMLDivElement>(null)

  // Clear selection when data changes
  useEffect(() => {
    setSelectedBuckets(new Set())
    setAnchorIndex(null)
    setExpandDirection(null)
  }, [buckets])

  const handleScanBucket = async (bucketName: string) => {
    try {
      const job = await startScan(`s3://${bucketName}`)
      setScanJobs(prev => new Map(prev).set(bucketName, job))
    } catch (e) {
      setMutationError(e instanceof Error ? e.message : 'Failed to start scan')
    }
  }

  // Poll scan job status
  useEffect(() => {
    const activeJobs = Array.from(scanJobs.entries()).filter(
      ([, job]) => job.status === 'pending' || job.status === 'running'
    )
    if (activeJobs.length === 0) return

    const interval = setInterval(async () => {
      const updates = new Map(scanJobs)
      let anyCompleted = false

      for (const [bucketName, job] of activeJobs) {
        try {
          const status = await fetchScanStatus(job.job_id)
          updates.set(bucketName, status)
          if (status.status === 'completed' || status.status === 'failed') {
            anyCompleted = true
          }
        } catch {
          // Ignore errors, keep polling
        }
      }

      setScanJobs(updates)
      if (anyCompleted) {
        refetch()
      }
    }, 2000)

    return () => clearInterval(interval)
  }, [scanJobs, refetch])

  const handleNewScan = (scanPath: string, job: ScanJob) => {
    // Extract bucket name from s3://bucket-name or s3://bucket-name/prefix
    const bucketMatch = scanPath.match(/^s3:\/\/([^/]+)/)
    const key = bucketMatch ? bucketMatch[1] : scanPath
    setScanJobs(prev => new Map(prev).set(key, job))
  }

  // Handle row click with shift/meta modifiers
  const handleRowClick = useCallback((bucketName: string, index: number, event: React.MouseEvent | React.KeyboardEvent) => {
    const shiftKey = event.shiftKey
    const metaKey = 'metaKey' in event ? event.metaKey || event.ctrlKey : false

    if (metaKey) {
      // Meta-click: toggle this row in selection
      setSelectedBuckets(prev => {
        const next = new Set(prev)
        if (next.has(bucketName)) {
          next.delete(bucketName)
        } else {
          next.add(bucketName)
        }
        return next
      })
      setAnchorIndex(index)
      setExpandDirection(null)
    } else if (shiftKey && anchorIndex !== null) {
      // Shift-click: select range from anchor to clicked
      const start = Math.min(anchorIndex, index)
      const end = Math.max(anchorIndex, index)
      const newSelection = new Set<string>()
      for (let i = start; i <= end; i++) {
        if (buckets[i]) {
          newSelection.add(buckets[i].name)
        }
      }
      setSelectedBuckets(newSelection)
    } else {
      // Regular click: toggle if only this row selected, otherwise select just this row
      const isOnlySelected = selectedBuckets.size === 1 && selectedBuckets.has(bucketName)
      if (isOnlySelected) {
        setSelectedBuckets(new Set())
        setAnchorIndex(null)
        setExpandDirection(null)
      } else {
        setSelectedBuckets(new Set([bucketName]))
        setAnchorIndex(index)
        setExpandDirection(null)
      }
    }
  }, [anchorIndex, buckets, selectedBuckets])

  // Click outside table to deselect
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (tableRef.current && !tableRef.current.contains(e.target as Node)) {
        setSelectedBuckets(new Set())
        setAnchorIndex(null)
        setExpandDirection(null)
      }
    }

    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  // Keyboard navigation (Superhuman-style)
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      const tableHasFocus = tableRef.current?.contains(document.activeElement)
      const nothingFocused = document.activeElement === document.body
      const isHoveringTable = hoveredIndex !== null

      if (!tableHasFocus && !nothingFocused && !isHoveringTable) {
        return
      }

      if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        e.preventDefault()
        const keyDirection = e.key === 'ArrowUp' ? 'up' : 'down'

        // Without shift: just move hover
        if (!e.shiftKey) {
          if (hoveredIndex === null) {
            setHoveredIndex(keyDirection === 'up' ? buckets.length - 1 : 0)
          } else {
            const newIndex = keyDirection === 'up'
              ? Math.max(0, hoveredIndex - 1)
              : Math.min(buckets.length - 1, hoveredIndex + 1)
            setHoveredIndex(newIndex)
          }
          return
        }

        // With shift: selection logic
        if (anchorIndex === null) {
          const startIndex = hoveredIndex ?? (keyDirection === 'up' ? buckets.length - 1 : 0)
          if (startIndex >= 0 && startIndex < buckets.length) {
            const nextIndex = keyDirection === 'up'
              ? Math.max(0, startIndex - 1)
              : Math.min(buckets.length - 1, startIndex + 1)

            setAnchorIndex(startIndex)
            if (nextIndex !== startIndex) {
              setSelectedBuckets(new Set([
                buckets[startIndex].name,
                buckets[nextIndex].name,
              ]))
            } else {
              setSelectedBuckets(new Set([buckets[startIndex].name]))
            }
            setExpandDirection(keyDirection)
          }
          return
        }

        const selectedIndices = buckets
          .map((b, i) => selectedBuckets.has(b.name) ? i : -1)
          .filter(i => i >= 0)

        if (selectedIndices.length === 0) return

        const minSelected = Math.min(...selectedIndices)
        const maxSelected = Math.max(...selectedIndices)

        if (selectedIndices.length === 1 && expandDirection === null) {
          setExpandDirection(keyDirection)
        }

        const currentDirection = expandDirection ?? keyDirection

        if (keyDirection === currentDirection) {
          const newIndex = currentDirection === 'up'
            ? Math.max(0, minSelected - 1)
            : Math.min(buckets.length - 1, maxSelected + 1)

          if (newIndex >= 0 && newIndex < buckets.length) {
            setSelectedBuckets(prev => {
              const next = new Set(prev)
              next.add(buckets[newIndex].name)
              return next
            })
          }
        } else {
          if (selectedIndices.length > 1) {
            const indexToRemove = currentDirection === 'up' ? minSelected : maxSelected
            setSelectedBuckets(prev => {
              const next = new Set(prev)
              next.delete(buckets[indexToRemove].name)
              if (next.size === 1) {
                setExpandDirection(null)
              }
              return next
            })
          }
        }
      } else if (e.key === 'Escape') {
        setSelectedBuckets(new Set())
        setAnchorIndex(null)
        setExpandDirection(null)
      } else if (e.key === 'a' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault()
        setSelectedBuckets(new Set(buckets.map(b => b.name)))
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [anchorIndex, expandDirection, hoveredIndex, buckets, selectedBuckets])

  // Selection summary
  const selectedCount = selectedBuckets.size
  const selectedBucketsList = buckets.filter(b => selectedBuckets.has(b.name))
  const selectedSize = selectedBucketsList.reduce((sum, b) => sum + (b.size ?? 0), 0)
  const unscannedCount = selectedBucketsList.filter(b => b.size == null).length

  // Bulk scan
  const handleBulkScan = async () => {
    for (const bucketName of selectedBuckets) {
      handleScanBucket(bucketName)
    }
  }

  // Select all toggle
  const allSelected = buckets.length > 0 && buckets.every(b => selectedBuckets.has(b.name))
  const someSelected = buckets.some(b => selectedBuckets.has(b.name))

  const handleSelectAll = () => {
    if (allSelected) {
      setSelectedBuckets(new Set())
    } else {
      setSelectedBuckets(new Set(buckets.map(b => b.name)))
    }
  }

  if (!s3Available) {
    return (
      <Box sx={{ py: 4 }}>
        <Typography variant="h6" sx={{ mb: 1, display: 'flex', alignItems: 'center', gap: 1 }}>
          <FaCloud /> S3 Buckets
        </Typography>
        <Typography color="text.secondary">
          Live S3 bucket browsing isn't available in this deployment. Head to{' '}
          <Link to="/">Scans</Link> to browse the published scans.
        </Typography>
      </Box>
    )
  }
  if (isLoading) return <div>Loading S3 buckets...</div>
  if (error) return <div>Error: {error.message}</div>

  return (
    <div ref={tableRef} tabIndex={0} style={{ outline: 'none' }}>
      <h1 style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
        <FaCloud /> S3 Buckets
      </h1>
      <NewS3ScanForm onStarted={handleNewScan} />
      {mutationError && <div style={{ color: 'red', marginBottom: '0.5rem' }}>{mutationError}</div>}
      {selectedCount > 0 && (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1, fontSize: '0.85rem' }}>
          <span style={{ opacity: 0.8 }}>
            {selectedCount} selected
            {unscannedCount === selectedCount
              ? ` (${unscannedCount} unscanned)`
              : unscannedCount > 0
                ? ` (${formatSize(selectedSize)}, ${unscannedCount} unscanned)`
                : ` (${formatSize(selectedSize)})`}
          </span>
          <Tooltip title={`Scan ${selectedCount} bucket${selectedCount === 1 ? '' : 's'}`}>
            <Button
              size="small"
              onClick={handleBulkScan}
              startIcon={<FaSync size={12} />}
              sx={{ minWidth: 0 }}
            >
              Scan
            </Button>
          </Tooltip>
          <Button
            size="small"
            onClick={() => setSelectedBuckets(new Set())}
            sx={{ minWidth: 0, opacity: 0.7 }}
          >
            Clear
          </Button>
        </Box>
      )}
      {buckets.length === 0 ? (
        <Typography color="text.secondary">
          No S3 buckets found. Check your AWS credentials.
        </Typography>
      ) : (
        <>
          <table className="s3-bucket-table">
            <thead>
              <tr>
                <th style={{ width: '1.5rem', padding: '0.25rem 0.1rem' }}>
                  <Checkbox
                    size="small"
                    checked={allSelected}
                    indeterminate={someSelected && !allSelected}
                    onChange={handleSelectAll}
                    sx={{ padding: 0 }}
                  />
                </th>
                <th></th>
                <th>Bucket</th>
                <th>Size</th>
                <th>Children</th>
                <th>Desc.</th>
                <th>Last Scanned</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {buckets.map((bucket, idx) => {
                const job = scanJobs.get(bucket.name)
                const isScanning = job && (job.status === 'pending' || job.status === 'running')
                const hasStats = bucket.last_scanned !== null
                const isSelected = selectedBuckets.has(bucket.name)
                const isHovered = hoveredIndex === idx
                return (
                  <tr
                    key={bucket.name}
                    style={{
                      opacity: hasStats ? 1 : 0.6,
                      background: isSelected
                        ? 'var(--selected-bg, rgba(25, 118, 210, 0.12))'
                        : isHovered
                          ? 'var(--hover-bg, #f5f5f5)'
                          : undefined,
                    }}
                    onClick={e => handleRowClick(bucket.name, idx, e)}
                    onMouseEnter={() => setHoveredIndex(idx)}
                    onMouseLeave={() => setHoveredIndex(null)}
                  >
                    <td onClick={e => e.stopPropagation()}>
                      <Checkbox
                        size="small"
                        checked={isSelected}
                        onChange={e => handleRowClick(bucket.name, idx, e as unknown as React.MouseEvent)}
                        sx={{ padding: 0 }}
                      />
                    </td>
                    <td><FaFolder style={{ color: hasStats ? '#4caf50' : undefined, opacity: hasStats ? 1 : 0.5 }} /></td>
                    <td>
                      <Link to={`/s3/${bucket.name}`} onClick={e => e.stopPropagation()}>
                        <code>{bucket.name}</code>
                      </Link>
                    </td>
                    <td>{formatSize(bucket.size)}</td>
                    <td>{bucket.n_children?.toLocaleString() ?? '-'}</td>
                    <td>{bucket.n_desc?.toLocaleString() ?? '-'}</td>
                    <td>
                      {bucket.last_scanned ? (
                        timeAgo(bucket.last_scanned)
                      ) : (
                        <span style={{ opacity: 0.5 }}>-</span>
                      )}
                    </td>
                    <td onClick={e => e.stopPropagation()}>
                      <Tooltip title={bucket.last_scanned ? 'Rescan bucket' : 'Scan bucket'}>
                        <span>
                          <Button
                            size="small"
                            onClick={() => handleScanBucket(bucket.name)}
                            disabled={isScanning}
                            sx={{ minWidth: 0, padding: '2px 4px' }}
                          >
                            {isScanning ? <CircularProgress size={14} /> : <FaSync size={12} />}
                          </Button>
                        </span>
                      </Tooltip>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
          <UnionTreemap
            items={buckets.map(b => ({ name: b.name, size: b.size ?? 0, path: `s3://${b.name}` }))}
            rootName="S3"
          />
        </>
      )}
    </div>
  )
}
