import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties } from 'react'
import { TS_PAD } from './TimeSeries'

/**
 * A slim overview strip under a `<TimeSeries>`: the whole series as a filled sparkline, the chart's displayed
 * x-range (`domain`) lit and the rest dimmed, and the highlighted window (`window`, e.g. a diff range) as a band.
 * Drag across it to pick a new displayed range (`onDomain`, both ends in data x); double-click it to show
 * everything (`onReset`). It aligns with the chart above it (same side padding), so a range brushed here sits
 * under the same dates. The chart's own drag stays free for its window brush.
 */
export interface XOverviewProps {
  /** The full series, any order. */
  points: { x: number; y: number }[]
  /** The displayed range, or null = everything. */
  domain: readonly [number, number] | null
  /** The highlighted window (drawn as a band). */
  window?: readonly [number, number]
  onDomain: (x0: number, x1: number) => void
  onReset?: () => void
  height?: number
  className?: string
  style?: CSSProperties
  /** Accessible name for the strip. */
  label?: string
}

export function XOverview({ points, domain, window: win, onDomain, onReset, height = 26, className, style, label = 'Displayed range: drag to zoom, double-click for every scan' }: XOverviewProps) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const [w, setW] = useState(0)
  useLayoutEffect(() => {
    const el = wrapRef.current
    if (!el) return
    setW(el.clientWidth)
    const ro = new ResizeObserver(() => setW(el.clientWidth))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const pts = useMemo(() => [...points].sort((a, b) => a.x - b.x), [points])
  const xMin = pts[0]?.x ?? 0
  const xMax = pts[pts.length - 1]?.x ?? 1
  const yMax = Math.max(0, ...pts.map(p => p.y)) || 1
  const plotW = Math.max(0, w - TS_PAD.left - TS_PAD.right)
  const xToPx = (x: number) => TS_PAD.left + ((x - xMin) / Math.max(1, xMax - xMin)) * plotW
  const pxToX = (px: number) => xMin + ((px - TS_PAD.left) / Math.max(1, plotW)) * (xMax - xMin)
  const yToPx = (y: number) => height - 2 - (Math.max(0, y) / yMax) * (height - 4)

  const dragRef = useRef<{ x0: number; x1: number } | null>(null)
  const [drag, setDragState] = useState<{ x0: number; x1: number } | null>(null)
  const setDrag = (d: { x0: number; x1: number } | null) => { dragRef.current = d; setDragState(d) }
  const svgRef = useRef<SVGSVGElement>(null)
  const xAt = (clientX: number) => {
    const el = svgRef.current
    const x = el ? pxToX(clientX - el.getBoundingClientRect().left) : xMin
    return Math.max(xMin, Math.min(xMax, x))
  }
  const onDown = (e: React.PointerEvent<SVGSVGElement>) => {
    if (!e.isPrimary || e.button !== 0 || pts.length < 2) return
    e.preventDefault()
    e.currentTarget.setPointerCapture?.(e.pointerId)
    const x = xAt(e.clientX)
    setDrag({ x0: x, x1: x })
    const up = (ev: PointerEvent) => {
      off()
      const d = dragRef.current
      setDrag(null)
      if (!d) return
      const x1 = xAt(ev.clientX)
      // a press without a drag is a click (a double-click resets, below)
      if (Math.abs(xToPx(x1) - xToPx(d.x0)) >= 3) onDomain(Math.min(d.x0, x1), Math.max(d.x0, x1))
    }
    const cancel = () => { off(); setDrag(null) }
    const off = () => { window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', cancel) }
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', cancel)
  }
  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const d = dragRef.current
    if (d) setDrag({ x0: d.x0, x1: xAt(e.clientX) })
  }

  const path = pts.length > 1
    ? `${pts.map((p, i) => `${i ? 'L' : 'M'} ${xToPx(p.x)} ${yToPx(p.y)}`).join(' ')} L ${xToPx(xMax)} ${height} L ${xToPx(xMin)} ${height} Z`
    : null
  const lit = drag ? [Math.min(drag.x0, drag.x1), Math.max(drag.x0, drag.x1)] as const : domain
  return (
    <div ref={wrapRef} className={'dt-ts-overview' + (className ? ` ${className}` : '')} style={{ position: 'relative', width: '100%', height, ...style }}>
      {w > 0 && (
        <svg
          ref={svgRef}
          width={w}
          height={height}
          role="group"
          aria-label={label}
          onPointerDown={onDown}
          onPointerMove={onMove}
          onDoubleClick={onReset}
          style={{ display: 'block', cursor: 'col-resize', userSelect: 'none', touchAction: 'pan-y' }}
        >
          <rect x={TS_PAD.left} y={0} width={plotW} height={height} rx={3} fill="var(--dt-ts-overview-bg, rgba(139,148,158,0.08))" />
          {path && <path d={path} fill="var(--dt-ts-overview-fill, rgba(139,148,158,0.35))" stroke="var(--dt-ts-overview-line, rgba(139,148,158,0.8))" strokeWidth={1} />}
          {win && win[1] > win[0] && (
            <rect className="dt-ts-overview-window" x={xToPx(win[0])} y={0} width={Math.max(2, xToPx(win[1]) - xToPx(win[0]))} height={height} fill="var(--dt-ts-overview-window, rgba(122,162,247,0.35))" pointerEvents="none" />
          )}
          {lit && (
            <g pointerEvents="none" className="dt-ts-overview-domain">
              {/* outside the displayed range: dimmed */}
              <rect x={TS_PAD.left} y={0} width={Math.max(0, xToPx(lit[0]) - TS_PAD.left)} height={height} fill="var(--dt-ts-overview-dim, rgba(0,0,0,0.35))" />
              <rect x={xToPx(lit[1])} y={0} width={Math.max(0, TS_PAD.left + plotW - xToPx(lit[1]))} height={height} fill="var(--dt-ts-overview-dim, rgba(0,0,0,0.35))" />
              <rect x={xToPx(lit[0])} y={0.5} width={Math.max(1, xToPx(lit[1]) - xToPx(lit[0]))} height={height - 1} rx={2} fill="none" stroke="var(--dt-ts-overview-edge, rgba(139,148,158,0.9))" strokeWidth={1} />
            </g>
          )}
        </svg>
      )}
    </div>
  )
}
