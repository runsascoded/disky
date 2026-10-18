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
  /** A band callout: the label hugs the upper trace inside `[y0, y]`, and is
   *  skipped when the trace is off screen or the band is too thin to hold it. */
  y0?: number
  label: string
  below?: boolean
}

/** Annotation font size, and the box estimate collision culling uses: a
 *  semibold digit run is ~0.62 em wide per char, a line ~1.15 em tall. */
const ANNO_PT = 10.5
const ANNO_CHAR_W = ANNO_PT * 0.62
const ANNO_LINE_H = ANNO_PT * 1.15
const BAND_INSET = 2
const MIN_BAND_PX = ANNO_LINE_H + 2 * BAND_INSET

interface PlacedAnnotation { a: Annotation; px: number; py: number; anchor: 'start' | 'middle' | 'end'; dx: number }

/** Where each annotation's label goes, in order, dropping a band callout whose
 *  band is too thin on screen and any label whose (estimated) box overlaps a
 *  label already placed — so earlier annotations win. */
function placeAnnotations(
  annotations: Annotation[],
  xToPx: (x: number) => number,
  yToPx: (y: number) => number,
  left: number,
  top: number,
  plotW: number,
  plotH: number,
): PlacedAnnotation[] {
  const placed: PlacedAnnotation[] = []
  const boxes: [number, number, number, number][] = []
  for (const a of annotations) {
    const px = xToPx(a.x)
    const anchor = px < left + plotW * 0.15 ? 'start' : px > left + plotW * 0.85 ? 'end' : 'middle'
    const dx = anchor === 'start' ? 5 : anchor === 'end' ? -5 : 0
    let py: number
    let y0: number
    if (a.y0 != null) {
      const t = Math.min(yToPx(a.y), yToPx(a.y0))
      const b = Math.min(top + plotH, Math.max(yToPx(a.y), yToPx(a.y0)))
      if (t < top || b - t < MIN_BAND_PX) continue
      y0 = t + BAND_INSET
      py = y0 + ANNO_LINE_H / 2
    } else {
      py = yToPx(a.y) + (a.below ? 14 : -7)
      y0 = py - ANNO_PT // `py` is the baseline
    }
    const w = a.label.length * ANNO_CHAR_W
    const x0 = px + dx - (anchor === 'start' ? 0 : anchor === 'end' ? w : w / 2)
    const box: [number, number, number, number] = [x0, y0, x0 + w, y0 + ANNO_LINE_H]
    if (boxes.some(o => box[0] < o[2] && o[0] < box[2] && box[1] < o[3] && o[1] < box[3])) continue
    boxes.push(box)
    placed.push({ a, px, py, anchor, dx })
  }
  return placed
}

/** A vertical marker at an x (e.g. a hand-written note pinned to a date): a thin line through the plot, its
 *  label in a lane above it — clear of the point callouts, which stay inside the plot. */
export interface Marker {
  key: string
  x: number
  /** Short text for the lane; the caller's `renderMarker` can wrap it (a tooltip with the full note). */
  label: string
}

/** Marker lane height (px), added above the plot when any marker is in view. */
export const MARKER_LANE = 16
/** The lane labels' box: at the very top of the chart, clear of the point callouts above the plot's top edge
 *  (a callout over the top point reaches ~9 px above it, i.e. into the bottom of the lane). */
const LANE_TOP = 2
const LANE_LABEL_H = 14
const MARKER_PT = 10.5
const MARKER_CHAR_W = MARKER_PT * 0.6
/** A compact marker's handle width (px): what's left when its label would overlap a neighbour's. */
const MARKER_DOT_W = 9

export interface PlacedMarker {
  m: Marker
  px: number
  /** `start`: the label runs right from the line; `end`: it ends at the line (near the right edge). */
  anchor: 'start' | 'end'
  /** Its label would overlap one already placed: drawn as a small handle instead. */
  compact: boolean
}

/** Where each marker's lane label goes, by x: markers off the plot's x-span are dropped; a label that would run
 *  past the right edge (`right`, px) ends at its line instead; one whose (estimated) box overlaps a label already
 *  placed, left to right, is compact. */
export function placeMarkers(markers: readonly Marker[], xToPx: (x: number) => number, left: number, plotW: number, right = 0): PlacedMarker[] {
  const out: PlacedMarker[] = []
  let lastEnd = -Infinity
  for (const m of [...markers].sort((a, b) => a.x - b.x)) {
    const px = xToPx(m.x)
    if (px < left - 0.5 || px > left + plotW + 0.5) continue
    const w = m.label.length * MARKER_CHAR_W + 6
    const anchor = px + w > left + plotW + right ? 'end' : 'start'
    const x0 = anchor === 'start' ? px : px - w
    const compact = x0 < lastEnd
    lastEnd = Math.max(lastEnd, compact ? px + MARKER_DOT_W / 2 : x0 + w)
    out.push({ m, px, anchor, compact })
  }
  return out
}

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
  /** x's with no data (e.g. scans an index doesn't cover): the x-range spans
   *  them, each stretch of them is shaded, and every line and area breaks
   *  across them instead of joining its neighbours — a missing value is never
   *  drawn as a value. */
  gaps?: number[]
  /** x's whose dots are drawn hollow (e.g. scans taken off the schedule). */
  hollowXs?: number[]
  /** x's marked by a small tick under the x-axis (the same scans, styled apart from the dots). */
  tickXs?: number[]
  /** Vertical markers (`placeMarkers`): those inside the x-span get a line and a lane label above the plot. */
  markers?: Marker[]
  /** Wrap a marker's lane label (e.g. in a tooltip); `node` is the label (or a compact handle) as drawn. */
  renderMarker?: (m: Marker, node: ReactNode) => ReactNode
  /** Extra CSS on the outer wrapper. */
  className?: string
  style?: CSSProperties
  /** Chart height in px (default: fill parent via 100%). */
  height?: number | string
}

const DEFAULT_COLORS = DEFAULT_PALETTE

/** A series' sorted x's cut at the gaps: index ranges `[i, j]` (inclusive) with no gap strictly between
 *  consecutive points — each drawn as its own line. */
export function splitAtGaps(xs: readonly number[], gaps: readonly number[]): [number, number][] {
  if (!xs.length) return []
  const g = [...gaps].sort((a, b) => a - b)
  const out: [number, number][] = []
  let start = 0
  for (let i = 1; i < xs.length; i++) if (g.some(x => x > xs[i - 1] && x < xs[i])) { out.push([start, i - 1]); start = i }
  out.push([start, xs.length - 1])
  return out
}

/** The gaps' stretches `[from, to]`: consecutive gap x's with no data x between them. */
export function gapStretches(gaps: readonly number[], xs: readonly number[]): [number, number][] {
  const g = [...new Set(gaps)].sort((a, b) => a - b)
  const out: [number, number][] = []
  for (const x of g) {
    const last = out[out.length - 1]
    if (last && !xs.some(d => d > last[1] && d < x)) last[1] = x
    else out.push([x, x])
  }
  return out
}

const PAD = { top: 12, right: 16, bottom: 24, left: 56 }
/** Height of the window's grip: a strip along the band's top edge that slides the window (anywhere else brushes). */
const GRIP = 12

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
  gaps,
  hollowXs,
  tickXs,
  markers,
  renderMarker,
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
    xs.push(...(gaps ?? []))
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
  }, [series, getX, getY, yScale, yFrom, gaps])

  // A marker in view reserves its lane above the plot.
  const lane = markers?.some(m => m.x >= xMin && m.x <= xMax) ? MARKER_LANE : 0
  const pad = lane ? { ...PAD, top: PAD.top + lane } : PAD
  const plotW = Math.max(0, dims.w - pad.left - pad.right)
  const plotH = Math.max(0, dims.h - pad.top - pad.bottom)

  const xToPx = (x: number) => pad.left + ((x - xMin) / Math.max(1, xMax - xMin)) * plotW
  const yToPx = (y: number) => {
    if (yScale === 'log') {
      const lmin = Math.log10(Math.max(1, yMin))
      const lmax = Math.log10(Math.max(10, yMax))
      const ly = Math.log10(Math.max(1, y))
      return pad.top + plotH - ((ly - lmin) / Math.max(0.001, lmax - lmin)) * plotH
    }
    return pad.top + plotH - ((y - yMin) / Math.max(0.001, yMax - yMin)) * plotH
  }

  const placedMarkers = lane && dims.w > 0 ? placeMarkers(markers ?? [], xToPx, pad.left, plotW, pad.right) : []

  const xTickVals = niceTicks(xMin, xMax, xTicks)
  const yTickVals = yTickValues
    ? yTickValues.filter(v => v >= yMin - 1e-9 && v <= yMax + 1e-9)
    : niceTicks(yMin, yMax, yTicks, yScale === 'log')

  // Collect all distinct x's across series for hover snapping.
  const hollow = useMemo(() => new Set(hollowXs ?? []), [hollowXs])
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
  // A drag that starts on the window's grip (the strip along its top edge)
  // SLIDES it (same width in points, clamped to the data); a drag anywhere
  // else, inside the window included, brushes a new one. Held in point indices
  // so the window keeps its point count while the x spacing varies.
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
  /** Whether a pointer at (x, clientY) is on the window's grip. */
  const onGrip = (x: number | null, clientY: number): boolean => {
    const el = svgRef.current
    if (!el || !inWindow(x)) return false
    const y = clientY - el.getBoundingClientRect().top
    return y >= pad.top && y <= pad.top + GRIP
  }
  const [hoverGrip, setHoverGrip] = useState(false)
  const onMove = (e: React.PointerEvent<SVGSVGElement>) => {
    const x = xAt(e.clientX)
    setHoverX(x)
    setHoverGrip(onGrip(x, e.clientY))
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
  // Pointer events, so a finger brushes like a mouse: `touch-action: pan-y`
  // leaves vertical swipes to the page, and a horizontal drag reaches here. A
  // cancelled gesture (the browser took it over as a scroll) drops the drag.
  const onEnd = (commit: (up: PointerEvent) => void) => {
    const up = (ev: PointerEvent) => { off(); commit(ev) }
    const cancel = () => { off(); slideRef.current = null; setDrag(null) }
    const off = () => { window.removeEventListener('pointerup', up); window.removeEventListener('pointercancel', cancel) }
    window.addEventListener('pointerup', up)
    window.addEventListener('pointercancel', cancel)
  }
  const onDown = (e: React.PointerEvent<SVGSVGElement>) => {
    if (!onBrush || !e.isPrimary || e.button !== 0) return
    e.preventDefault() // no text selection while dragging
    const x = xAt(e.clientX)
    if (x == null) return
    setHoverX(x)
    e.currentTarget.setPointerCapture?.(e.pointerId)
    // A press on the window's grip slides it (keeps its point span); any other
    // press brushes a fresh window.
    if (onGrip(x, e.clientY) && xWindow && xsSorted.length > 1) {
      const i0 = idxOf(xWindow[0])
      const span = Math.max(1, idxOf(xWindow[1]) - i0)
      slideRef.current = { i0, span, start: idxOf(x) }
      setDrag({ x0: xsSorted[i0], x1: xsSorted[i0 + span] })
      onEnd(up => {
        const s = slideRef.current
        const d = dragRef.current
        slideRef.current = null
        setDrag(null)
        if (!s || !d) return
        const xi = idxOf(xAt(up.clientX) ?? x)
        if (xi === s.start) onPickX?.(x) // a click inside the window is still a pick
        else onBrush(d.x0, d.x1)
      })
      return
    }
    setDrag({ x0: x, x1: x })
    // Commit on the release wherever it lands (a drag often ends past the
    // plot's edge), from the pointer's own x rather than the last move.
    onEnd(up => {
      const d = dragRef.current
      if (!d) return
      setDrag(null)
      const x1 = xAt(up.clientX) ?? d.x1
      if (x1 === d.x0) onPickX?.(d.x0)
      else onBrush(Math.min(d.x0, x1), Math.max(d.x0, x1))
    })
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

  // The tooltip keeps clear of the trace it describes: on the far side of the
  // crosshair from the plot's middle, and in the half of the plot the hovered
  // points don't reach. Hidden while dragging (the band shows the range).
  const tip = (() => {
    if (hoverX == null || drag || !hoverPoints.some(p => p.y != null)) return null
    const px = xToPx(hoverX)
    const tops = series.flatMap(s => {
      const pt = s.points.find(p => getX(p) === hoverX)
      return pt && s.plot !== false ? [yToPx(getY(pt))] : []
    })
    const high = tops.length > 0 && Math.min(...tops) < pad.top + plotH / 2
    return {
      ...(px > pad.left + plotW / 2 ? { right: dims.w - px + 8 } : { left: px + 8 }),
      ...(high ? { bottom: pad.bottom + 4 } : { top: pad.top + 4 }),
    }
  })()

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
          onPointerMove={onMove}
          onPointerLeave={() => { if (!dragRef.current) { setHoverX(null); setHoverGrip(false) } }}
          onPointerDown={onDown}
          // With a brush, clicks resolve in mouseup (a zero-width drag) — a
          // separate click handler would fire the pick twice.
          onClick={onPickX && !onBrush ? () => { if (hoverX != null) onPickX(hoverX) } : undefined}
          style={{ display: 'block', cursor: drag ? (slideRef.current ? 'grabbing' : 'col-resize') : onBrush ? (hoverGrip ? 'grab' : 'crosshair') : onPickX ? 'pointer' : undefined, userSelect: 'none', touchAction: onBrush ? 'pan-y' : undefined }}
        >
          {/* Window band (the highlighted x-range, or the drag in progress) */}
          {band && band[1] > band[0] && (
            <g pointerEvents="none">
              <rect
                x={xToPx(band[0])}
                y={pad.top}
                width={Math.max(0, xToPx(band[1]) - xToPx(band[0]))}
                height={plotH}
                fill={drag ? 'var(--dt-ts-brush, rgba(255,255,255,0.14))' : 'var(--dt-ts-window, rgba(255,255,255,0.07))'}
              />
              {[band[0], band[1]].map((x, i) => (
                <line
                  key={`w${i}`}
                  x1={xToPx(x)}
                  x2={xToPx(x)}
                  y1={pad.top}
                  y2={pad.top + plotH}
                  stroke="var(--dt-ts-window-edge, rgba(255,255,255,0.35))"
                  strokeDasharray={drag ? undefined : '2 3'}
                />
              ))}
              {/* The grip: drag it to slide the window (brushing starts anywhere else). */}
              {onBrush && (!drag || slideRef.current) && (
                <rect
                  className="dt-ts-grip"
                  x={xToPx(band[0])}
                  y={pad.top}
                  width={Math.max(0, xToPx(band[1]) - xToPx(band[0]))}
                  height={GRIP}
                  fill={hoverGrip || drag ? 'var(--dt-ts-grip-hover, rgba(255,255,255,0.28))' : 'var(--dt-ts-grip, rgba(255,255,255,0.14))'}
                />
              )}
            </g>
          )}
          {/* Gaps: each stretch of x's without data, shaded. */}
          {gaps && gaps.length > 0 && (
            <g pointerEvents="none" className="dt-ts-gaps">
              {gapStretches(gaps, allXs).map(([a, b]) => (
                <rect
                  key={`g${a}`}
                  className="dt-ts-gap"
                  x={xToPx(a) - (a === b ? 1 : 0)}
                  y={pad.top}
                  width={Math.max(2, xToPx(b) - xToPx(a))}
                  height={plotH}
                  fill="var(--dt-ts-gap, rgba(139,148,158,0.12))"
                />
              ))}
            </g>
          )}
          {/* Series + callouts stay inside the plot: a fitted y-range clips
              whatever falls below it (a stack's lower bands) instead of
              painting over the axis. */}
          <defs>
            <clipPath id={clipId}>
              <rect x={pad.left} y={pad.top} width={plotW} height={plotH} />
            </clipPath>
          </defs>
          {/* Y grid + ticks */}
          {yTickVals.map((y, i) => (
            <g key={`y${i}`}>
              <line
                x1={pad.left}
                x2={pad.left + plotW}
                y1={yToPx(y)}
                y2={yToPx(y)}
                stroke={y === 0 && yMin < 0 ? 'var(--dt-ts-axis, rgba(255,255,255,0.2))' : 'var(--dt-ts-grid, rgba(255,255,255,0.08))'}
              />
              <text
                x={pad.left - 6}
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
                y1={pad.top + plotH}
                y2={pad.top + plotH + 4}
                stroke="var(--dt-ts-axis, rgba(255,255,255,0.2))"
              />
              <text
                x={xToPx(x)}
                y={pad.top + plotH + 16}
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
            x1={pad.left}
            x2={pad.left + plotW}
            y1={pad.top + plotH}
            y2={pad.top + plotH}
            stroke="var(--dt-ts-axis, rgba(255,255,255,0.2))"
          />
          <line
            x1={pad.left}
            x2={pad.left}
            y1={pad.top}
            y2={pad.top + plotH}
            stroke="var(--dt-ts-axis, rgba(255,255,255,0.2))"
          />
          {yLabel && (
            <text
              x={12}
              y={pad.top + plotH / 2}
              textAnchor="middle"
              fontSize={10}
              fill="var(--dt-ts-axis-ink, #8b949e)"
              transform={`rotate(-90, 12, ${pad.top + plotH / 2})`}
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
            // One run per stretch between gaps (`splitAtGaps`): never a line across missing data.
            const runs = splitAtGaps(sortedPts.map(getX), gaps ?? []).map(([i, j]) => sortedPts.slice(i, j + 1))
            const drawn = runs.map(run => {
              const linePath = seg(run)
              // A band's lower edge is its baseline, walked back; a plain area
              // drops to the axis.
              const base = getY0
                ? [...run].reverse().map(p => `L ${xToPx(getX(p))} ${yToPx(getY0(p))}`).join(' ')
                : `L ${xToPx(getX(run[run.length - 1]))} ${pad.top + plotH} L ${xToPx(getX(run[0]))} ${pad.top + plotH}`
              const areaPath = (s.area ?? area) ? `${linePath} ${base} Z` : null
              // `dashBeforeX`: the line up to (and joining) the first point at or
              // past it is dashed; the rest solid.
              const cut = s.dashBeforeX != null ? run.findIndex(p => getX(p) >= s.dashBeforeX!) : -1
              const dashed = cut > 0 ? seg(run.slice(0, cut + 1)) : null
              const solid = cut > 0 ? seg(run.slice(cut)) : linePath
              return { areaPath, dashed, solid }
            })
            return (
              <g key={s.key}>
                {drawn.map((d, i) => d.areaPath && <path key={`a${i}`} d={d.areaPath} fill={color} fillOpacity={getY0 ? 0.35 : 0.15} />)}
                {drawn.map((d, i) => d.dashed && <path key={`d${i}`} d={d.dashed} fill="none" stroke={color} strokeWidth={s.strokeWidth ?? 1.75} strokeDasharray="4 4" />)}
                {drawn.map((d, i) => <path key={`s${i}`} className="dt-ts-line" d={d.solid} fill="none" stroke={color} strokeWidth={s.strokeWidth ?? 1.75} />)}
                {(s.dots ?? true) && sortedPts.map((p, i) => hollow.has(getX(p))
                  ? <circle key={i} className="dt-ts-dot-hollow" cx={xToPx(getX(p))} cy={yToPx(getY(p))} r={3.5} fill="var(--dt-ts-bg, #0d1117)" stroke={color} strokeWidth={2} />
                  : <circle key={i} cx={xToPx(getX(p))} cy={yToPx(getY(p))} r={2.5} fill={color} />)}
              </g>
            )
          })}
          </g>
          {/* Ticks: a small triangle under the x-axis at each `tickXs` x. */}
          {tickXs && tickXs.length > 0 && (
            <g pointerEvents="none" className="dt-ts-ticks">
              {tickXs.filter(x => x >= xMin && x <= xMax).map(x => {
                const px = xToPx(x), y = pad.top + plotH + 1
                return <path key={`t${x}`} className="dt-ts-tick" d={`M${px},${y}l3.5,6h-7z`} fill="var(--dt-ts-tick, rgba(139,148,158,0.9))" />
              })}
            </g>
          )}
          {/* Markers: a thin dashed line from the lane down through the plot. */}
          {placedMarkers.length > 0 && (
            <g pointerEvents="none" className="dt-ts-markers">
              {placedMarkers.map(({ m, px }) => (
                <line
                  key={m.key}
                  className="dt-ts-marker"
                  x1={px}
                  x2={px}
                  y1={LANE_TOP}
                  y2={pad.top + plotH}
                  stroke="var(--dt-ts-marker, #d29922)"
                  strokeWidth={1}
                  strokeDasharray="3 2"
                />
              ))}
            </g>
          )}
          {/* Annotations: a haloed label beside the point, leaning away from
              the nearest side edge; a band callout (`y0`) sits inside the
              band, centred, and only where the band is tall enough. A label
              whose box would overlap one already placed is dropped, so the
              caller's order is the priority. */}
          {placeAnnotations(annotations ?? [], xToPx, yToPx, pad.left, pad.top, plotW, plotH).map(({ a, px, py, anchor, dx }, i) => (
            <text
              key={`a${i}`}
              x={px + dx}
              y={py}
              textAnchor={anchor}
              dominantBaseline={a.y0 != null ? 'middle' : undefined}
              clipPath={a.y0 != null ? `url(#${clipId})` : undefined}
              fontSize={ANNO_PT}
              fontWeight={600}
              fill="var(--dt-ts-anno-ink, #e6e6ea)"
              stroke="var(--dt-ts-anno-halo, rgba(20,20,24,0.85))"
              strokeWidth={3}
              paintOrder="stroke"
              pointerEvents="none"
            >
              {a.label}
            </text>
          ))}
          {/* Hover crosshair */}
          {hoverX != null && (
            <line
              x1={xToPx(hoverX)}
              x2={xToPx(hoverX)}
              y1={pad.top}
              y2={pad.top + plotH}
              stroke="var(--dt-ts-cross, rgba(255,255,255,0.4))"
              strokeDasharray="3 3"
              pointerEvents="none"
            />
          )}
        </svg>
      )}
      {/* Marker lane labels (HTML, so a caller's tooltip can anchor to them). */}
      {placedMarkers.map(({ m, px, anchor, compact }) => {
        const node = compact
          ? <span className="dt-ts-marker-dot" style={{ display: 'inline-block', width: MARKER_DOT_W - 2, height: MARKER_DOT_W - 2, borderRadius: '50%', background: 'var(--dt-ts-marker, #d29922)' }} />
          : <span className="dt-ts-marker-label">{m.label}</span>
        return (
          <div
            key={m.key}
            className={'dt-ts-marker-lane' + (compact ? ' compact' : '')}
            style={{
              position: 'absolute',
              top: LANE_TOP,
              height: LANE_LABEL_H,
              display: 'flex',
              alignItems: 'center',
              ...(compact ? { left: px - MARKER_DOT_W / 2 + 1 } : anchor === 'start' ? { left: px + 3 } : { right: dims.w - px + 3 }),
              fontSize: MARKER_PT,
              lineHeight: 1,
              whiteSpace: 'nowrap',
              color: 'var(--dt-ts-marker-ink, var(--dt-ts-marker, #d29922))',
              cursor: 'default',
            }}
          >
            {renderMarker ? renderMarker(m, node) : node}
          </div>
        )
      })}
      {/* Tooltip */}
      {tip && hoverX != null && (
        <div
          className="dt-ts-tip"
          style={{
            position: 'absolute',
            ...tip,
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
