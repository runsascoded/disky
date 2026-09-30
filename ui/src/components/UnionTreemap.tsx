import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { Box } from '@mui/material'
import { Treemap as DTTreemap } from '@disk-tree/react'
import '@rdub/treemap/styles.css'
import { formatSize } from '../utils/format'
import { useTiling } from '../utils/tiling'
import { uriToPath } from '../schemes'

/**
 * A top-level treemap over a *set* of scans/buckets — a synthetic root whose
 * children are each item, so several separate scans read as one union map
 * (the Scans landing page, the S3 bucket list). Each cell is a real anchor to
 * that scan's browse route (`uriToPath(path)`); a plain click drills via the
 * SPA router, cmd/middle-click opens a new tab natively.
 */
export interface UnionItem {
  /** Cell label. */
  name: string
  /** Size in bytes; items with size ≤ 0 (unscanned) are dropped. */
  size: number
  /** Canonical URI (`r2://bucket`, `/local/path`) the cell links to. */
  path?: string
}

interface UnionNode {
  name: string
  size: number
  path?: string
  children?: UnionNode[]
}

export function UnionTreemap({
  items,
  rootName,
  height = 400,
}: {
  items: UnionItem[]
  rootName: string
  height?: number
}) {
  const navigate = useNavigate()
  const [tiling] = useTiling()

  const root = useMemo<UnionNode | null>(() => {
    const scanned = items.filter(i => i.size > 0)
    if (scanned.length === 0) return null
    return {
      name: rootName,
      size: scanned.reduce((sum, i) => sum + i.size, 0),
      children: scanned.map(i => ({ name: i.name, size: i.size, path: i.path })),
    }
  }, [items, rootName])

  if (!root) return null

  return (
    <Box sx={{ height }}>
      <DTTreemap<UnionNode>
        root={root}
        tiling={tiling}
        getSize={n => n.size}
        getChildren={n => n.children}
        getLabel={n => n.name}
        formatSize={formatSize}
        cellHref={n => (n.path ? uriToPath(n.path) : undefined)}
        onCellClick={(n, _path, e) => {
          if (!n.path) return
          // Let the anchor's native behavior handle modified / middle clicks
          // (new tab); intercept only the plain click for the SPA router.
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button === 1) return true
          navigate(uriToPath(n.path))
          return true
        }}
        renderTooltip={n => (
          <>
            <div style={{ fontWeight: 500 }}>{n.name}</div>
            <div style={{ opacity: 0.75, fontSize: '0.85em' }}>{formatSize(n.size)}</div>
          </>
        )}
      />
    </Box>
  )
}
