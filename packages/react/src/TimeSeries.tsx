import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import { DEFAULT_PALETTE } from '@rdub/treemap'
import { pow10 } from './stats'

/**
 * DIY SVG line/area chart. No Plotly, no recharts, no d3 — the whole widget
 * is under ~180 LOC. Designed for `/api/scans/history` (bytes vs. snapshot
 * date) but the accessor API keeps it usable for any `{x, y}` series.
 *
 * What ships:
 *   - Multi-series overlay (default: one series). Each series is an area
 *     under a line, colored from an 8-slot palette (mirrors the Treemap's).
 *   - Auto y-axis scaling from the data; optional log scale via `yScale`.
 *   - Hover-follow crosshair with a per-x tooltip listing each series's
 *     value at that x.
 *   - ResizeObserver-driven layout (no fixed pixel size in the component;
 *     wrap in a container with a height and it fills).
 *   - Zero external dep besides React.
 */

export interface Series<T> {
  key: string
  label?: string
  color?: string
  points: T[]
  /** Per-series area fill; overrides the chart-wide `area`. */
  area?: boolean
  /** The part of the line at x < this is dashed — e.g. a total drawn before
   *  every component existed (specs/done/root-geneses.md). */
  dashBeforeX?: number
  /** Draw per-point dots (default true). */
  dots?: boolean
  /** Line stroke width (default 1.75). */
  strokeWidth?: number
  /** With `yFrom: 'data'`, only series marked `fit` set the y-range (when any
   *  is) — e.g. a stack's total, so "fit" zooms to the stack's top edge and the
   *  bands below are clipped rather than the whole stack shrunk to fit. */
  fit?: boolean
  /** `false`: listed in the tooltip but not drawn — a total beside per-part
   *  lines it would dwarf. */
  plot?: boolean
}

export interface Annotation {
  x: number
  y: number
  /** A band callout: the label sits centred between `y0` and `y` (inside the
   *  band) instead of beside the point, and is skipped when the band is too
   *  thin on screen to hold it. */
  y0?: number
  label: string
  below?: boolean
}

/** A band callout needs this many px of band to sit inside. */
const MIN_BAND_PX = 13

export interface TimeSeriesProps<T> {
  series: Series<T>[]
  getX: (p: T) => number
  getY: (p: T) => number
  /** A point's baseline: with it, a series is a BAND from `getY0` up to
   *  `getY` (stacked areas: each series' y0 = the running sum below it), the
   *  tooltip shows the band's height, and the y-range still spans `getY`. */
  getY0?: (p: T) => number
  /** Format an X tick (default: `new Date(x).toLocaleDateString()`). */
  formatX?: (x: number) => string
  /** Format the X value in the hover tooltip; defaults to `formatX`. Lets the
   *  tooltip show finer granularity (e.g. an intra-day scan's time) than the
   *  axis ticks, which stay coarse. */
  formatTipX?: (x: number) => string
  /** Format a Y tick / tooltip value. */
  formatY?: (y: number) => string
  /** `linear` (default) or `log`. */
  yScale?: 'linear' | 'log'
  /** Where the linear y-axis starts: `zero` (default) or `data` — fit the
   *  y-range to the series (5% pad each side) so small movements in a large
   *  total stay visible. Ignored for `log`. */
  yFrom?: 'zero' | 'data'
  /** Y-axis label (rendered vertically). */
  yLabel?: string
  /** Approximate tick counts. */
  xTicks?: number
  yTicks?: number
  /** Explicit y-tick values (bypasses auto nice-ticks) — e.g. to align to a
   *  binary display unit (1000 TiB, not a base-10-nice 909 TiB). */
  yTickValues?: number[]
  /** Show area fill under each line (default true). */
  area?: boolean
  /** Point callouts (e.g. first/last/min/max): a short text beside the point
   *  at (x, y), above it by default (`below` flips it). Anchored away from
   *  the nearest plot edge so labels never run off the chart. */
  annotations?: Annotation[]
  /** Click handler for the snapped hover x (the crosshair position) — e.g.
   *  jump the page to that scan date. Sets a pointer cursor on the plot. */
  onPickX?: (x: number) => void
  /** Drag across the plot to pick an x-range (both ends snapped to data x's);
   *  a drag that never leaves its start x counts as a click (`onPickX`). */
  onBrush?: (x0: number, x1: number) => void
  /** A highlighted x-window (e.g. the diff range the page is showing), drawn
   *  as a shaded band behind the series. */
  window?: [number, number]
  /** Extra CSS on the outer wrapper. */
  className?: string
  style?: CSSProperties
  /** Chart height in px (default: fill parent via 100%). */
  height?: number | string
}

const DEFAULT_COLORS = DEFAULT_PALETTE

const PAD = { top: 12, right: 16, bottom: 24, left: 56 }

function niceTicks(min: number, max: number, count: number, log = false): number[] {
  if (log) {
    const lmin = Math.log10(Math.max(1, min))
    const lmax = Math.log10(Math.max(10, max))
    const step = Math.max(1, Math.ceil((lmax - lmin) / count))
    const out: number[] = []
    for (let e = Math.ceil(lmin); e <= Math.floor(lmax); e += step) out.push(pow10(e))
    return out
  }
  if (min === max) return [min]
  const range = max - min
  const rawStep = range / Math.max(1, count)
  const mag = pow10(Math.floor(Math.log10(rawStep)))
  const norm = rawStep / mag
  const stepMult = norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10
  const step = stepMult * mag
  const first = Math.ceil(min / step) * step
  const out: number[] = []
  for (let v = first; v <= max + step / 100; v += step) out.push(Number(v.toFixed(10)))
  return out
}

export function TimeSeries<T>({
  series,
  getX,
  getY,
  getY0,
  formatX = x => new Date(x).toLocaleDateString(),
  formatTipX,
  formatY = y => y.toLocaleString('en-US'),
  yScale = 'linear',
  yFrom = 'zero',
  yLabel,
  xTicks = 5,
  yTicks = 4,
  yTickValues,
  area = true,
  annotations,
  onPickX,
  onBrush,
  window: xWindow,
  className,
  style,
  height,
}: TimeSeriesProps<T>) {
  const wrapRef = useRef<HTMLDivElement>(null)
  // Per-instance clip id (several charts share a page); `useId`'s delimiters
  // aren't valid in a `url(#…)` reference.
  const clipId = 'dt-ts-clip-' + useId().replace(/\W/g, '')
  const [dims, setDims] = useState({ w: 0, h: 0 })

  // Measure synchronously first — a ResizeObserver's initial delivery can be
  // arbitrarily late (and never arrives in a hidden tab), which would leave
  // the chart blank at its correct size.
  useLayoutEffect(() => {
    const el = wrapRef.current
    if (!el) return
    setDims({ w: el.clientWidth, h: el.clientHeight })
    const ro = new ResizeObserver(() => setDims({ w: el.clientWidth, h: el.clientHeight }))
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  const [hoverX, setHoverX] = useState<number | null>(null)

  const { xMin, xMax, yMin, yMax } = useMemo(() => {
    const xs: number[] = []
    const ys: number[] = []
    const fit = yScale === 'linear' && yFrom === 'data'
    // Fitting: the `fit`-marked series set the y-range when any is marked.
    const fitTo = fit && series.some(s => s.fit) ? series.filter(s => s.fit) : series
    for (const s of series) for (const p of s.points) xs.push(getX(p))
    for (const s of fitTo) for (const p of s.points) ys.push(getY(p))
    if (xs.length === 0) return { xMin: 0, xMax: 1, yMin: 0, yMax: 1 }
    const yMinRaw = Math.min(...ys)
    const yMaxRaw = Math.max(...ys)
    const pad = fit ? Math.max(yMaxRaw - yMinRaw, Math.abs(yMaxRaw) * 0.01) * 0.05 : 0
    // Zero-anchored: the axis still spans below 0 when a series goes negative
    // (a Δ trace), padded like the top.
    const below = yMinRaw < 0 ? yMinRaw * 1.05 : 0
    return {
      xMin: Math.min(...xs),
      xMax: Math.max(...xs),
      yMin: yScale === 'log' ? Math.max(1, yMinRaw) : fit ? yMinRaw - pad : below,
      yMax: fit ? yMaxRaw + pad : yMaxRaw > 0 ? yMaxRaw * 1.05 : yMaxRaw < 0 ? 0 : 1,
    }
  }, [series, getX, getY, yScale, yFrom])

  const plotW = Math.max(0, dims.w - PAD.left - PAD.right)
  const plotH = Math.max(0, dims.h - PAD.top - PAD.bottom)

  const xToPx = (x: number) => PAD.left + ((x - xMin) / Math.max(1, xMax - xMin)) * plotW
  const yToPx = (y: number) => {
    if (yScale === 'log') {
      const lmin = Math.log10(Math.max(1, yMin))
      const lmax = Math.log10(Math.max(10, yMax))
      const ly = Math.log10(Math.max(1, y))
      return PAD.top + plotH - ((ly - lmin) / Math.max(0.001, lmax - lmin)) * plotH
    }
    return PAD.top + plotH - ((y - yMin) / Math.max(0.001, yMax - yMin)) * plotH
  }

  const xTickVals = niceTicks(xMin, xMax, xTicks)
  const yTickVals = yTickValues
    ? yTickValues.filter(v => v >= yMin - 1e-9 && v <= yMax + 1e-9)
    : niceTicks(yMin, yMax, yTicks, yScale === 'log')

  // Collect all distinct x's across series for hover snapping.
  const allXs = useMemo(() => {
    const s = new Set<number>()
    for (const ser of series) for (const p of ser.points) s.add(getX(p))
    return [...s].sort((a, b) => a - b)
  }, [series, getX])

  const snapX = (px: number): number | null => {
    if (allXs.length === 0) return null
    // find nearest x in plot coords
    let best = allXs[0]
    let bestD = Math.abs(xToPx(best) - px)
    for (const x of allXs) {
      const d = Math.abs(xToPx(x) - px)
      if (d < bestD) { bestD = d; best = x }
    }
    return best
  }

  // Brush: data-x endpoints of an in-progress drag (null = not dragging). The
  // ref is the source of truth for the handlers — state updates flush in a
  // microtask under createRoot, so down/move/up in quick succession would
  // otherwise read stale closures; the state copy only drives the render.
  const dragRef = useRef<{ x0: number; x1: number } | null>(null)
  const [drag, setDragState] = useState<{ x0: number; x1: number } | null>(null)
  const setDrag = (d: { x0: number; x1: number } | null) => { dragRef.current = d; setDragState(d) }
  // A drag that starts inside the shown window SLIDES it (same width in points,
  // clamped to the data) instead of brushing a new one; a drag that starts
  // outside brushes as before. Held in point indices so the window keeps its
  // point count while the x spacing varies. (Upstreamed from mgu `gcs`.)
  const slideRef = useRef<{ i0: number; span: number; start: number } | null>(null)
  const xsSorted = useMemo(() => [...allXs].sort((a, b) => a - b), [allXs])
  const idxOf = (x: number): number => {
    let best = 0
    for (let i = 1; i < xsSorted.length; i++) if (Math.abs(xsSorted[i] - x) < Math.abs(xsSorted[best] - x)) best = i
    return best
  }
  const inWindow = (x: number | null): boolean => x != null && !!xWindow && x >= xWindow[0] && x <= xWindow[1]
  const svgRef = useRef<SVGSVGElement>(null)
  const xAt = (clientX: number): number | null => {
    const el = svgRef.current
    return el ? snapX(clientX - el.getBoundingClientRect().left) : null
  }
  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const x = xAt(e.clientX)
    setHoverX(x)
    const d = dragRef.current
    if (!d || x == null) return
    const s = slideRef.current
    if (s) {
      // Sliding: shift the fixed-span window by the pointer's index delta.
      const i0 = Math.max(0, Math.min(xsSorted.length - 1 - s.span, s.i0 + (idxOf(x) - s.start)))
      const x0 = xsSorted[i0]
      if (x0 !== d.x0) setDrag({ x0, x1: xsSorted[i0 + s.span] })
    } else if (x !== d.x1) setDrag({ x0: d.x0, x1: x })
  }
  const onDown = (e: React.MouseEvent<SVGSVGElement>) => {
    if (!onBrush || e.button !== 0) return
    e.preventDefault() // no text selection while dragging
    const x = xAt(e.clientX)
    if (x == null) return
    // A press inside the current window slides it (keeps its point span); a
    // press outside brushes a fresh window.
    if (inWindow(x) && xWindow && xsSorted.length > 1) {
      const i0 = idxOf(xWindow[0])
      const span = Math.max(1, idxOf(xWindow[1]) - i0)
      slideRef.current = { i0, span, start: idxOf(x) }
      setDrag({ x0: xsSorted[i0], x1: xsSorted[i0 + span] })
      window.addEventListener('mouseup', (up: MouseEvent) => {
        const s = slideRef.current
        const d = dragRef.current
        slideRef.current = null
        setDrag(null)
        if (!s || !d) return
        const xi = idxOf(xAt(up.clientX) ?? x)
        if (xi === s.start) onPickX?.(x) // a click inside the window is still a pick
        else onBrush(d.x0, d.x1)
      }, { once: true })
      return
    }
    setDrag({ x0: x, x1: x })
    // Commit on the release wherever it lands (a drag often ends past the
    // plot's edge), from the pointer's own x rather than the last move.
    window.addEventListener('mouseup', (up: MouseEvent) => {
      const d = dragRef.current
      if (!d) return
      setDrag(null)
      const x1 = xAt(up.clientX) ?? d.x1
      if (x1 === d.x0) onPickX?.(d.x0)
      else onBrush(Math.min(d.x0, x1), Math.max(d.x0, x1))
    }, { once: true })
  }
  const band = drag ? [Math.min(drag.x0, drag.x1), Math.max(drag.x0, drag.x1)] as const : xWindow

  const hoverPoints: { color: string; label: string; y: number | null }[] = hoverX == null
    ? []
    : series.map((s, i) => {
      const pt = s.points.find(p => getX(p) === hoverX)
      return {
        color: s.color ?? DEFAULT_COLORS[i % DEFAULT_COLORS.length],
        label: s.label ?? s.key,
        y: pt ? getY(pt) - (getY0 ? getY0(pt) : 0) : null,
      }
    })

  return (
    <div
      ref={wrapRef}
      className={'dt-timeseries' + (className ? ` ${className}` : '')}
      style={{ position: 'relative', width: '100%', height: height ?? '100%', ...style }}
    >
      {dims.w > 0 && dims.h > 0 && (
        <svg
          ref={svgRef}
          width={dims.w}
          height={dims.h}
          onMouseMove={onMove}
          onMouseLeave={() => setHoverX(null)}
          onMouseDown={onDown}
          // With a brush, clicks resolve in mouseup (a zero-width drag) — a
          // separate click handler would fire the pick twice.
          onClick={onPickX && !onBrush ? () => { if (hoverX != null) onPickX(hoverX) } : undefined}
          style={{ display: 'block', cursor: drag ? (slideRef.current ? 'grabbing' : 'col-resize') : onBrush ? (inWindow(hoverX) ? 'grab' : 'crosshair') : onPickX ? 'pointer' : undefined, userSelect: 'none' }}
        >
          {/* Window band (the highlighted x-range, or the drag in progress) */}
          {band && band[1] > band[0] && (
            <g pointerEvents="none">
              <rect
                x={xToPx(band[0])}
                y={PAD.top}
                width={Math.max(0, xToPx(band[1]) - xToPx(band[0]))}
                height={plotH}
                fill={drag ? 'var(--dt-ts-brush, rgba(255,255,255,0.14))' : 'var(--dt-ts-window, rgba(255,255,255,0.07))'}
              />
              {[band[0], band[1]].map((x, i) => (
                <line
                  key={`w${i}`}
                  x1={xToPx(x)}
                  x2={xToPx(x)}
                  y1={PAD.top}
                  y2={PAD.top + plotH}
                  stroke="var(--dt-ts-window-edge, rgba(255,255,255,0.35))"
                  strokeDasharray={drag ? undefined : '2 3'}
                />
              ))}
            </g>
          )}
          {/* Series + callouts stay inside the plot: a fitted y-range clips
              whatever falls below it (a stack's lower bands) instead of
              painting over the axis. */}
          <defs>
            <clipPath id={clipId}>
              <rect x={PAD.left} y={PAD.top} width={plotW} height={plotH} />
            </clipPath>
          </defs>
          {/* Y grid + ticks */}
          {yTickVals.map((y, i) => (
            <g key={`y${i}`}>
              <line
                x1={PAD.left}
                x2={PAD.left + plotW}
                y1={yToPx(y)}
                y2={yToPx(y)}
                stroke={y === 0 && yMin < 0 ? 'var(--dt-ts-axis, rgba(255,255,255,0.2))' : 'var(--dt-ts-grid, rgba(255,255,255,0.08))'}
              />
              <text
                x={PAD.left - 6}
                y={yToPx(y)}
                textAnchor="end"
                dominantBaseline="middle"
                fontSize={10}
                fill="var(--dt-ts-axis-ink, #8b949e)"
              >
                {formatY(y)}
              </text>
            </g>
          ))}
          {/* X ticks */}
          {xTickVals.map((x, i) => (
            <g key={`x${i}`}>
              <line
                x1={xToPx(x)}
                x2={xToPx(x)}
                y1={PAD.top + plotH}
                y2={PAD.top + plotH + 4}
                stroke="var(--dt-ts-axis, rgba(255,255,255,0.2))"
              />
              <text
                x={xToPx(x)}
                y={PAD.top + plotH + 16}
                textAnchor="middle"
                fontSize={10}
                fill="var(--dt-ts-axis-ink, #8b949e)"
              >
                {formatX(x)}
              </text>
            </g>
          ))}
          {/* Axis lines */}
          <line
            x1={PAD.left}
            x2={PAD.left + plotW}
            y1={PAD.top + plotH}
            y2={PAD.top + plotH}
            stroke="var(--dt-ts-axis, rgba(255,255,255,0.2))"
          />
          <line
            x1={PAD.left}
            x2={PAD.left}
            y1={PAD.top}
            y2={PAD.top + plotH}
            stroke="var(--dt-ts-axis, rgba(255,255,255,0.2))"
          />
          {yLabel && (
            <text
              x={12}
              y={PAD.top + plotH / 2}
              textAnchor="middle"
              fontSize={10}
              fill="var(--dt-ts-axis-ink, #8b949e)"
              transform={`rotate(-90, 12, ${PAD.top + plotH / 2})`}
            >
              {yLabel}
            </text>
          )}
          {/* Series */}
          <g clipPath={`url(#${clipId})`}>
          {series.map((s, si) => {
            if (s.points.length === 0 || s.plot === false) return null
            const color = s.color ?? DEFAULT_COLORS[si % DEFAULT_COLORS.length]
            const sortedPts = [...s.points].sort((a, b) => getX(a) - getX(b))
            const seg = (pts: T[]) => pts.map((p, i) => `${i === 0 ? 'M' : 'L'} ${xToPx(getX(p))} ${yToPx(getY(p))}`).join(' ')
            const linePath = seg(sortedPts)
            // A band's lower edge is its baseline, walked back; a plain area
            // drops to the axis.
            const base = getY0
              ? [...sortedPts].reverse().map(p => `L ${xToPx(getX(p))} ${yToPx(getY0(p))}`).join(' ')
              : `L ${xToPx(getX(sortedPts[sortedPts.length - 1]))} ${PAD.top + plotH} L ${xToPx(getX(sortedPts[0]))} ${PAD.top + plotH}`
            const areaPath = (s.area ?? area) ? `${linePath} ${base} Z` : null
            // `dashBeforeX`: the line up to (and joining) the first point at or
            // past it is dashed; the rest solid.
            const cut = s.dashBeforeX != null ? sortedPts.findIndex(p => getX(p) >= s.dashBeforeX!) : -1
            const dashed = cut > 0 ? seg(sortedPts.slice(0, cut + 1)) : null
            const solid = cut > 0 ? seg(sortedPts.slice(cut)) : linePath
            return (
              <g key={s.key}>
                {areaPath && <path d={areaPath} fill={color} fillOpacity={getY0 ? 0.35 : 0.15} />}
                {dashed && <path d={dashed} fill="none" stroke={color} strokeWidth={s.strokeWidth ?? 1.75} strokeDasharray="4 4" />}
                <path d={solid} fill="none" stroke={color} strokeWidth={s.strokeWidth ?? 1.75} />
                {(s.dots ?? true) && sortedPts.map((p, i) => (
                  <circle
                    key={i}
                    cx={xToPx(getX(p))}
                    cy={yToPx(getY(p))}
                    r={2.5}
                    fill={color}
                  />
                ))}
              </g>
            )
          })}
          </g>
          {/* Annotations: a haloed label beside the point, leaning away from
              the nearest side edge; a band callout (`y0`) sits inside the
              band, centred, and only where the band is tall enough */}
          {annotations?.map((a, i) => {
            const px = xToPx(a.x)
            const anchor = px < PAD.left + plotW * 0.15 ? 'start' : px > PAD.left + plotW * 0.85 ? 'end' : 'middle'
            const dx = anchor === 'start' ? 5 : anchor === 'end' ? -5 : 0
            let py: number
            if (a.y0 != null) {
              const top = Math.max(PAD.top, Math.min(yToPx(a.y), yToPx(a.y0)))
              const bot = Math.min(PAD.top + plotH, Math.max(yToPx(a.y), yToPx(a.y0)))
              if (bot - top < MIN_BAND_PX) return null
              py = (top + bot) / 2
            } else py = yToPx(a.y) + (a.below ? 14 : -7)
            return (
              <text
                key={`a${i}`}
                x={px + dx}
                y={py}
                textAnchor={anchor}
                dominantBaseline={a.y0 != null ? 'middle' : undefined}
                clipPath={a.y0 != null ? `url(#${clipId})` : undefined}
                fontSize={10.5}
                fontWeight={600}
                fill="var(--dt-ts-anno-ink, #e6e6ea)"
                stroke="var(--dt-ts-anno-halo, rgba(20,20,24,0.85))"
                strokeWidth={3}
                paintOrder="stroke"
                pointerEvents="none"
              >
                {a.label}
              </text>
            )
          })}
          {/* Hover crosshair */}
          {hoverX != null && (
            <line
              x1={xToPx(hoverX)}
              x2={xToPx(hoverX)}
              y1={PAD.top}
              y2={PAD.top + plotH}
              stroke="var(--dt-ts-cross, rgba(255,255,255,0.4))"
              strokeDasharray="3 3"
              pointerEvents="none"
            />
          )}
        </svg>
      )}
      {/* Tooltip */}
      {hoverX != null && hoverPoints.some(p => p.y != null) && (
        <div
          style={{
            position: 'absolute',
            left: Math.min(xToPx(hoverX) + 8, dims.w - 180),
            top: PAD.top + 4,
            background: 'var(--dt-ts-tip-bg, rgba(20,20,24,0.94))',
            color: 'var(--dt-ts-tip-ink, #e6e6ea)',
            border: '1px solid var(--dt-ts-tip-border, #333)',
            borderRadius: 4,
            padding: '4px 8px',
            fontSize: 11,
            pointerEvents: 'none',
            whiteSpace: 'nowrap',
          }}
        >
          <div style={{ opacity: 0.7, marginBottom: 2 }}>{(formatTipX ?? formatX)(hoverX)}</div>
          {hoverPoints.map((p, i) => (
            <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{ display: 'inline-block', width: 8, height: 8, background: p.color, borderRadius: 2 }} />
              <span style={{ minWidth: 60 }}>{p.label}</span>
              <b>{p.y != null ? formatY(p.y) : '—'}</b>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

/** Convenience wrapper: single-series bytes-over-time. Used by disk-tree. */
export function BytesOverTime({
  points,
  formatBytes,
  height,
}: {
  points: { time: string | number; bytes: number | null }[]
  formatBytes: (n: number) => string
  height?: number | string
}): ReactNode {
  const cleaned = points
    .map(p => ({ t: typeof p.time === 'number' ? p.time : Date.parse(p.time), bytes: p.bytes }))
    .filter((p): p is { t: number; bytes: number } => Number.isFinite(p.t) && typeof p.bytes === 'number')
  return (
    <TimeSeries
      series={[{ key: 'bytes', label: 'size', points: cleaned }]}
      getX={p => p.t}
      getY={p => p.bytes}
      formatY={formatBytes}
      height={height}
    />
  )
}
