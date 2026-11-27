/** The over-time chart's change strip (specs/change-metrics.md §4.2): under the objects line, one signed bar per UTC
 *  day (per ISO week over a long span): objects written above the zero line (added, green, with modified stacked on it,
 *  neutral), deleted below (red), the net change a dot. Aligned with the chart above it (`TS_PAD`, the same x span);
 *  hover a bar for its numbers. */
import { TS_PAD } from '@disk-tree/react'
import { useLayoutEffect, useMemo, useRef, useState } from 'react'
import { barExtent, bucketFor, changeBars, type ChangeBar, type FlowPoint } from './changeBars'
import { DAY } from './scan'
import { fmtCount } from './usageModel'

const H = 84
const n = (v: number) => v.toLocaleString('en-US')
const fmtDay = (t: number) => new Date(t).toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' })

/** A bar's tooltip line: `Nov 18 · +12,410 added · −88,201 deleted · ~3,120 modified · net −75,791 (2 scans)`. */
export function barText(b: ChangeBar, week: boolean, label: (date: string) => string = d => d): string {
  const when = week ? `wk of ${fmtDay(b.t)}` : fmtDay(b.t)
  const net = b.net === 0 ? '0' : b.net > 0 ? `+${n(b.net)}` : `−${n(-b.net)}`
  const scans = b.scans.length === 1 ? '' : ` (${b.scans.length} scans)`
  const since = b.since ? ` · since ${label(b.since)}` : ''
  return `${when} · +${n(b.added)} added · −${n(b.deleted)} deleted · ~${n(b.modified)} modified · net ${net}${scans}${since}`
}

export function ChangeStrip({ points, xMin, xMax, xOf, label }: {
  /** The series' points in the displayed range (their flows, where counted). */
  points: readonly FlowPoint[]
  /** The chart's x span (its points' first and last x). */
  xMin: number
  xMax: number
  /** A scan id's x (`xOfScan`). */
  xOf: (date: string) => number
  /** A scan's label for the tooltip's `since`. */
  label?: (date: string) => string
}) {
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
  const week = bucketFor(xMax - xMin) === 'week'
  const bars = useMemo(() => changeBars(points, week ? 'week' : 'day'), [points, week])
  const [hover, setHover] = useState<number | null>(null)
  if (!bars.length) return null
  const { up, down } = barExtent(bars)
  const plotW = Math.max(0, w - TS_PAD.left - TS_PAD.right)
  const xToPx = (x: number) => TS_PAD.left + ((x - xMin) / Math.max(1, xMax - xMin)) * plotW
  const span = up + down || 1
  const zero = 4 + ((H - 8) * up) / span
  const yPx = (v: number) => ((H - 8) * v) / span
  // A bar centred on its scans (a day's: its last scan's x; a week's: the week's middle), 80% of its bucket wide
  const bucketPx = Math.max(2, (plotW * (week ? 7 : 1) * DAY) / Math.max(1, xMax - xMin) * 0.8)
  const placed = bars.map(b => {
    const cx = week ? xToPx(b.t + 3.5 * DAY) : xToPx(xOf(b.scans[b.scans.length - 1]))
    const bw = Math.min(bucketPx, 28)
    const left = Math.max(TS_PAD.left, cx - bw / 2)
    return { b, left, width: Math.max(1.5, Math.min(cx + bw / 2, TS_PAD.left + plotW) - left) }
  })
  const hb = hover == null ? null : placed[hover]
  return (
    <div className="change-strip" ref={wrapRef} style={{ position: 'relative', height: H, margin: '2px 0 4px' }} onPointerLeave={() => setHover(null)}>
      <div style={{ position: 'absolute', left: TS_PAD.left, width: plotW, top: zero, borderTop: '1px solid var(--ink-3)', opacity: 0.6 }} />
      <span className="cs-axis" style={{ position: 'absolute', right: w - TS_PAD.left + 6, top: 0 }}>+{fmtCount(up)}</span>
      {down > 0 && <span className="cs-axis" style={{ position: 'absolute', right: w - TS_PAD.left + 6, bottom: 0 }}>−{fmtCount(down)}</span>}
      {placed.map(({ b, left, width }, i) => (
        <div
          key={b.t}
          className={`cs-bar${hover === i ? ' on' : ''}`}
          style={{ position: 'absolute', left, width, top: 0, height: H }}
          onPointerEnter={() => setHover(i)}
          onFocus={() => setHover(i)}
          tabIndex={0}
          aria-label={barText(b, week, label)}
        >
          <div className="cs-mod" style={{ position: 'absolute', left: 0, right: 0, top: zero - yPx(b.added + b.modified), height: yPx(b.modified) }} />
          <div className="cs-add" style={{ position: 'absolute', left: 0, right: 0, top: zero - yPx(b.added), height: yPx(b.added) }} />
          <div className="cs-del" style={{ position: 'absolute', left: 0, right: 0, top: zero, height: yPx(b.deleted) }} />
          <div className="cs-net" style={{ position: 'absolute', left: '50%', top: zero - yPx(b.net) - 2, width: 4, height: 4, marginLeft: -2 }} />
        </div>
      ))}
      {hb && (
        <div className="cs-tip" style={{ position: 'absolute', top: -8, transform: 'translateY(-100%)', ...(hb.left > TS_PAD.left + plotW / 2 ? { right: w - hb.left + 6 } : { left: hb.left + hb.width + 6 }) }}>
          {barText(hb.b, week, label)}
        </div>
      )}
    </div>
  )
}

/** The strip's legend line. */
export function ChangeLegend() {
  return (
    <div className="legend change-legend">
      <span className="li"><span className="sw cs-add" />added</span>
      <span className="li"><span className="sw cs-mod" />modified</span>
      <span className="li"><span className="sw cs-del" />deleted</span>
      <span className="li"><span className="sw cs-net-sw" />net</span>
    </div>
  )
}
