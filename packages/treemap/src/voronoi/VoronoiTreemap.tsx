import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { DEFAULT_PALETTE } from '../colors'
import { useHoverPin } from '../useHoverPin'
import { circlePolygon, rectPolygon, toPointsAttr } from './geometry'
import type { Polygon } from './geometry'
import { voronoiLayout } from './layout'
import type { VoronoiLayoutOpts } from './layout'

/**
 * Voronoi treemap for one level of children (spec: viz-widgets.md §6).
 *
 * Deliberately **not** a replacement for `<Treemap>`: rectangles give labels
 * far better real estate, and the rect layout is exact where this one is
 * iterative. Where this wins is a *circular* clip — a glyph inside a scatter
 * marker, or an aesthetic full-panel alt view — which rectangles can't fill.
 *
 * Ships from the `@open-athena/treemap/voronoi` subpath so the core package stays
 * dependency-free; `d3-voronoi-treemap` and `d3-hierarchy` are optional peers
 * that only consumers of this subpath need to install.
 */

export interface VoronoiTreemapProps<T> extends VoronoiLayoutOpts {
  items: T[]
  getValue: (n: T) => number
  getLabel?: (n: T) => string
  getColor?: (n: T, i: number) => string | null | undefined
  /** `circle` (default) fills the panel's inscribed circle; `rect` fills it. */
  shape?: 'circle' | 'rect'
  /** Explicit clip polygon; overrides `shape`. */
  clip?: Polygon
  /** Draw labels in cells with room for them. Default: true. */
  showLabels?: boolean
  /** Format the excluded-items total in the footer note (e.g. bytes). */
  formatValue?: (v: number) => string
  renderTooltip?: (n: T) => ReactNode
  onCellClick?: (n: T, event: React.MouseEvent) => void
  className?: string
  style?: CSSProperties
  height?: number | string
}

const FALLBACK_DIMS = { w: 420, h: 420 }
/** Below this cell area (px²) a label is illegible slivers, so we drop it. */
const LABEL_MIN_AREA = 900

export function VoronoiTreemap<T>({
  items,
  getValue,
  getLabel,
  getColor,
  shape = 'circle',
  clip,
  showLabels = true,
  formatValue,
  renderTooltip,
  onCellClick,
  convergenceRatio,
  maxIterationCount,
  minShare,
  minWeightRatio,
  seed,
  tolerance,
  className,
  style,
  height,
}: VoronoiTreemapProps<T>) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [dims, setDims] = useState({ w: 0, h: 0 })

  useLayoutEffect(() => {
    const el = wrapRef.current
    if (!el) return
    setDims({ w: el.clientWidth, h: el.clientHeight })
    const ro = new ResizeObserver(() => setDims({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const pin = useHoverPin<number>({ excludeRefs: [wrapRef] })

  const w = dims.w || FALLBACK_DIMS.w
  const h = dims.h || FALLBACK_DIMS.h

  const clipPolygon = useMemo<Polygon>(() => {
    if (clip) return clip
    if (shape === 'rect') return rectPolygon(1, 1, Math.max(0, w - 2), Math.max(0, h - 2))
    const r = Math.max(0, Math.min(w, h) / 2 - 1)
    return circlePolygon(w / 2, h / 2, r)
  }, [clip, shape, w, h])

  const { cells, error, converged, excluded, excludedValue } = useMemo(
    () =>
      voronoiLayout(items, getValue, clipPolygon, {
        convergenceRatio,
        maxIterationCount,
        minShare,
        minWeightRatio,
        seed,
        tolerance,
      }),
    [items, getValue, clipPolygon, convergenceRatio, maxIterationCount, minShare, minWeightRatio, seed, tolerance],
  )

  const active = pin.active
  const activeCell = active == null ? null : cells[active] ?? null

  return (
    <div
      ref={wrapRef}
      className={'dt-voronoi' + (className ? ` ${className}` : '')}
      style={{ position: 'relative', width: '100%', height: height ?? '100%', ...style }}
    >
      {cells.length > 0 && (
        <svg width={w} height={h} style={{ display: 'block' }} role="img" aria-label="voronoi treemap">
          {cells.map((cell, i) => {
            const color = getColor?.(cell.node, i) ?? DEFAULT_PALETTE[i % DEFAULT_PALETTE.length]
            const label = getLabel?.(cell.node)
            const isActive = active === i
            return (
              <g
                key={i}
                className="dt-voronoi-cell"
                onMouseEnter={() => pin.hover(i)}
                onMouseLeave={() => pin.hover(null)}
                onClick={e => {
                  pin.togglePin(i)
                  onCellClick?.(cell.node, e)
                }}
                style={{ cursor: onCellClick ? 'pointer' : undefined }}
              >
                <polygon
                  points={toPointsAttr(cell.polygon)}
                  fill={color}
                  fillOpacity={active == null || isActive ? 0.82 : 0.3}
                  stroke="var(--dt-voronoi-edge, rgba(0,0,0,0.45))"
                  strokeWidth={1}
                />
                {showLabels && label && cell.area >= LABEL_MIN_AREA && (
                  <text
                    x={cell.centroid[0]}
                    y={cell.centroid[1]}
                    textAnchor="middle"
                    dominantBaseline="middle"
                    fontSize={11}
                    fill="var(--dt-voronoi-ink, #10101a)"
                    pointerEvents="none"
                  >
                    {label}
                  </text>
                )}
              </g>
            )
          })}
        </svg>
      )}
      {activeCell && (
        <div
          className="dt-voronoi-tip"
          style={{
            position: 'absolute',
            left: Math.min(activeCell.centroid[0] + 8, Math.max(0, w - 180)),
            top: Math.max(0, activeCell.centroid[1] - 10),
            background: 'var(--dt-voronoi-tip-bg, rgba(20,20,24,0.94))',
            color: 'var(--dt-voronoi-tip-ink, #e6e6ea)',
            border: '1px solid var(--dt-voronoi-tip-border, #333)',
            borderRadius: 4,
            padding: '4px 8px',
            fontSize: 11,
            pointerEvents: 'none',
            whiteSpace: 'nowrap',
            zIndex: 1,
          }}
        >
          {renderTooltip?.(activeCell.node) ?? getLabel?.(activeCell.node) ?? String(activeCell.value)}
        </div>
      )}
      {(excluded > 0 || (!converged && cells.length > 0)) && (
        // Say what was left out and how far off the areas are — an
        // unconverged tessellation must never read as exact.
        <div className="dt-voronoi-note" style={{ fontSize: 11, opacity: 0.6, marginTop: 2 }}>
          {excluded > 0 && `${excluded} too small to tessellate${formatValue ? ` (${formatValue(excludedValue)})` : ''}`}
          {excluded > 0 && !converged && ' · '}
          {!converged && cells.length > 0 && `areas approximate (max ${(error * 100).toFixed(1)}% off target)`}
        </div>
      )}
    </div>
  )
}
