/**
 * Draws the grouped-outline overlay (`outlineGroups`, spec
 * `specs/treemap-mark-union-outlines.md`) on a `<canvas>` over the map area:
 * one stroked perimeter per group's union of cells, above the cell fills and
 * seam borders, below tooltips, and transparent to the pointer. Renderer-
 * agnostic — it reads the placed cells the DOM and canvas renderers share.
 */
import { useEffect, useRef } from 'react'
import { type PlacedCell } from './layout'
import { type OutlineGroups, groupOutlines } from './outlines'
import { colorResolver } from './cssColor'

export interface OutlineOverlayProps<T> {
  cells: PlacedCell<T>[]
  width: number
  height: number
  groups: OutlineGroups<T>
}

export function OutlineOverlay<T>({ cells, width, height, groups }: OutlineOverlayProps<T>) {
  const ref = useRef<HTMLCanvasElement | null>(null)

  useEffect(() => {
    const cv = ref.current
    if (!cv) return
    const dpr = (typeof window !== 'undefined' ? window.devicePixelRatio : 1) || 1
    cv.width = Math.round(width * dpr)
    cv.height = Math.round(height * dpr)
    const ctx = cv.getContext('2d')
    if (!ctx) return
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, width, height)

    const lw = groups.width ?? 2
    ctx.lineWidth = lw
    // Square caps extend each segment by lw/2 past its ends, so the inset
    // segments (offset inward by lw/2) meet cleanly at every corner without
    // extra geometry — and a centered stroke's corners fill too.
    ctx.lineCap = 'square'
    ctx.lineJoin = 'miter'
    // Default: stroke just inside the union, so the line sits on the group's
    // own cells rather than straddling into neighbors.
    const off = groups.inset === false ? 0 : lw / 2

    const resolve = colorResolver(cv)
    const drawn: string[] = []
    for (const { key, color, segs } of groupOutlines(cells, groups)) {
      if (segs.length === 0) continue
      drawn.push(key)
      ctx.strokeStyle = resolve(color)
      ctx.beginPath()
      for (const s of segs) {
        const dx = s.nx * off
        const dy = s.ny * off
        ctx.moveTo(s.x1 + dx, s.y1 + dy)
        ctx.lineTo(s.x2 + dx, s.y2 + dy)
      }
      ctx.stroke()
    }
    groups.onDrawn?.(drawn)
  }, [cells, width, height, groups])

  return (
    <canvas
      ref={ref}
      className="dt-treemap-outlines"
      aria-hidden
      style={{ position: 'absolute', left: 0, top: 0, width, height, pointerEvents: 'none' }}
    />
  )
}
