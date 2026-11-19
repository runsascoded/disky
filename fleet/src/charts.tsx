/** The fleet page's history: the card's four panels as interactive SVG (no chart library) over the last 7 or 30 UTC days,
 *  one trace per deployment in its colour. Hovering (or, on a touch screen, tapping) reads out the nearest point; clicking
 *  it (a second tap) opens its run's `/scans/<run>` page.
 *  - `/meta` store size and the primary store's objects: the newest scan per day, log y.
 *  - Scan duration: each run (hollow: a step issue left open, or it failed).
 *  - Cost: a row per deployment on its own scale, a bar per UTC day stacked from its runs, scheduled under hand-started. */
import { fmtSI, interWidth, niceTicks, spread } from '@disk-tree/pages-kit/og/text'
import { type ReactNode, type PointerEvent as RPointerEvent, useCallback, useRef, useState, useSyncExternalStore } from 'react'
import { fmtCount, fmtTick, fmtUsd, mix } from '../functions/_lib/card'
import type { FleetDeployment, FleetDoc, FleetRun } from '../functions/_lib/fleetTypes'
import { runOutcome } from '../functions/_lib/runOutcome'
import { DAY, Delta, RunInfo, runHref } from './runs'
import { PointTip } from './Tip'

const { abs, ceil, floor, log10, max, min, round } = Math

const LEFT = 40
const TOP = 6
const BOTTOM = 18
const LINE_H = 128
const COST_ROW = 36
const COST_EMPTY = 16
const COST_GAP = 8
/** How far (px) from a point the pointer still reads it out. */
const REACH = 40
/** The panel colour under a dark-mode line (`styles.css`' `--panel`). */
const DARK_PANEL = '#232322'

// ── hooks ──────────────────────────────────────────────────────────────────

/** An element's width: measured when it mounts, then followed. */
function useWidth(): [(el: HTMLDivElement | null) => void, number] {
  const [w, setW] = useState(0)
  const ro = useRef<ResizeObserver | null>(null)
  const ref = useCallback((el: HTMLDivElement | null) => {
    ro.current?.disconnect()
    if (!el) return
    setW(el.clientWidth)
    ro.current = new ResizeObserver(([e]) => setW(round(e.contentRect.width)))
    ro.current.observe(el)
  }, [])
  return [ref, w]
}

const DARK = typeof matchMedia === 'undefined' ? null : matchMedia('(prefers-color-scheme: dark)')
const useDark = () => useSyncExternalStore(
  cb => { DARK?.addEventListener('change', cb); return () => DARK?.removeEventListener('change', cb) },
  () => DARK?.matches ?? false,
)

/** The range in days, kept in the URL (`?range=30d`) so a link holds it. */
function useRange(options: number[]): [number, (n: number) => void] {
  const [n, setN] = useState(() => Number(new URLSearchParams(location.search).get('range')?.replace(/d$/, '')))
  const set = (k: number) => {
    setN(k)
    const u = new URL(location.href)
    if (k === options[0]) u.searchParams.delete('range')
    else u.searchParams.set('range', `${k}d`)
    history.replaceState(history.state, '', u)
  }
  return [options.includes(n) ? n : options[0], set]
}

/** A pointer's position in an SVG's own pixels. */
const local = (svg: SVGSVGElement, e: { clientX: number; clientY: number }) => {
  const r = svg.getBoundingClientRect()
  return { x: e.clientX - r.left, y: e.clientY - r.top }
}

/** What a hovered mark opens, and the touch rule: on a touch screen the first tap only reads it out. */
function useOpen() {
  const touch = useRef(false)
  const tapped = useRef<string | null>(null)
  return {
    down: (e: RPointerEvent) => { touch.current = e.pointerType === 'touch' },
    open: (e: React.MouseEvent, key: string, href: string | undefined) => {
      if (!href) return
      if (touch.current && tapped.current !== key) { tapped.current = key; return }
      if (e.metaKey || e.ctrlKey) window.open(href, '_blank')
      else location.href = href
    },
    isTouch: () => touch.current,
  }
}

// ── axes ───────────────────────────────────────────────────────────────────

const md = (t: number) => { const d = new Date(t * 1000); return `${d.getUTCMonth() + 1}/${d.getUTCDate()}` }

/** The days: a rule and a date under the labelled ones (each day at 7 d; every few, ending today, when they're narrow). */
function XAxis({ from, to, X, y0, h }: { from: number; to: number; X: (t: number) => number; y0: number; h: number }) {
  const n = round((to - from) / DAY)
  const dayPx = (X(to) - X(from)) / n
  const step = [1, 2, 3, 5, 7, 10, 14].find(s => s * dayPx >= 34) ?? 14
  const out: ReactNode[] = []
  for (let i = 0; i < n; i++) {
    if ((n - 1 - i) % step) continue
    const t = from + i * DAY
    out.push(<line key={`g${i}`} className="grid" x1={X(t)} x2={X(t)} y1={y0} y2={y0 + h} />)
    out.push(<text key={`l${i}`} className="tick" x={X(t + DAY / 2)} y={y0 + h + 12} textAnchor="middle">{md(t)}</text>)
  }
  return <>{out}</>
}

interface YScale { ticks: [number, string][]; Y: (v: number) => number }

/** A panel's y scale over `vals`: log (1-2-5 ticks, decades only when crowded) or linear from 0. */
function yScale(vals: number[], log: boolean, y0: number, h: number, fmt: (v: number) => string): YScale {
  if (log) {
    const a = log10(min(...vals) / 1.3)
    const b = log10(max(...vals) * 1.3)
    let ticks: { v: number; m: number }[] = []
    for (let k = floor(a); k <= ceil(b); k++) {
      for (const m of [1, 2, 5]) {
        const v = m * Number(`1e${k}`)
        if (log10(v) >= a && log10(v) <= b) ticks.push({ v, m })
      }
    }
    const decades = ticks.filter(t => t.m === 1)
    if (ticks.length > 6 && decades.length >= 2) ticks = decades
    return { ticks: ticks.map(t => [t.v, fmt(t.v)]), Y: v => y0 + h - ((log10(v) - a) / (b - a)) * h }
  }
  const top = max(...vals) * 1.1 || 1
  const nt = niceTicks(0, top, 4)
  const hi = max(top, nt[nt.length - 1])
  return { ticks: nt.map(v => [v, fmt(v)]), Y: v => y0 + h - (v / hi) * h }
}

/** A line's under-stroke, so a light colour (hf's yellow) holds on a light panel; its gutter label's colour. */
const halo = (color: string, dark: boolean) => (dark ? DARK_PANEL : mix(color, '#000000', 0.35))
const labelColor = (color: string, dark: boolean) => (dark ? mix(color, '#ffffff', 0.2) : mix(color, '#000000', 0.4))

// ── line panels ────────────────────────────────────────────────────────────

interface LinePt { key: string; t: number; v: number; hollow?: boolean; href?: string; tip: ReactNode }
interface LineSeries { name: string; color: string; pts: LinePt[]; label: string }

function Panel({ title, keys, note, children }: { title: string; keys?: ReactNode; note?: string; children: ReactNode }) {
  return (
    <div className="panel">
      <div className="panel-title"><b>{title}</b>{keys}{note && <span className="muted note">{note}</span>}</div>
      {children}
    </div>
  )
}

function LinePanel({ title, keys, note, series, log, fmtTick, from, to }: {
  title: string
  keys?: ReactNode
  note?: string
  series: LineSeries[]
  log: boolean
  fmtTick: (v: number) => string
  from: number
  to: number
}) {
  const [ref, w] = useWidth()
  const dark = useDark()
  const svg = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<{ s: number; i: number } | null>(null)
  const act = useOpen()
  const live = series.filter(s => s.pts.length)
  const gutter = max(32, ...live.map(s => interWidth(s.label, 10.5, 600))) + 10
  const right = max(LEFT + 40, w - gutter)
  const X = (t: number) => LEFT + ((t - from) / (to - from)) * (right - LEFT)
  const { ticks, Y } = live.length ? yScale(live.flatMap(s => s.pts.map(p => p.v)), log, TOP, LINE_H, fmtTick) : { ticks: [], Y: () => 0 }
  const at = live.map(s => s.pts.map(p => ({ x: X(p.t), y: Y(p.v) })))
  const r = (to - from) / DAY > 10 ? 2.4 : 3.2

  const nearest = (e: { clientX: number; clientY: number }): { s: number; i: number } | null => {
    if (!svg.current) return null
    const { x, y } = local(svg.current, e)
    let best: { s: number; i: number } | null = null
    let bd = REACH * REACH
    at.forEach((ps, s) => ps.forEach((p, i) => {
      const d = (p.x - x) ** 2 + (p.y - y) ** 2
      if (d < bd) { bd = d; best = { s, i } }
    }))
    return best
  }
  const h = hover && live[hover.s]?.pts[hover.i] ? hover : null
  const hp = h ? live[h.s].pts[h.i] : null

  const ends = live.map((s, k) => ({ s, ...at[k][at[k].length - 1] }))
  const ys = spread(ends.map(e => e.y + 3.5), 12, TOP + 8, TOP + LINE_H + 3)

  return (
    <Panel title={title} keys={keys} note={note}>
      <div ref={ref} className="plot">
        {w > 0 && (
          <svg
            ref={svg} width={w} height={TOP + LINE_H + BOTTOM} role="img" aria-label={title}
            style={{ cursor: hp?.href ? 'pointer' : 'default' }}
            onPointerMove={e => setHover(nearest(e))}
            onPointerDown={e => { act.down(e); setHover(nearest(e)) }}
            onPointerLeave={() => { if (!act.isTouch()) setHover(null) }}
            onClick={e => { const n = nearest(e); if (n) act.open(e, live[n.s].pts[n.i].key, live[n.s].pts[n.i].href) }}
          >
            <rect className="bg" x={LEFT} y={TOP} width={right - LEFT} height={LINE_H} />
            <XAxis from={from} to={to} X={X} y0={TOP} h={LINE_H} />
            {ticks.map(([v, label]) => {
              const ty = Y(v)
              return (
                <g key={v}>
                  <line className="grid" x1={LEFT} x2={right} y1={ty} y2={ty} />
                  <text className="tick" x={LEFT - 4} y={ty + 3.5} textAnchor="end">{label}</text>
                </g>
              )
            })}
            {!live.length && <text className="tick" x={(LEFT + right) / 2} y={TOP + LINE_H / 2} textAnchor="middle">no data in this range</text>}
            {hp && <line className="guide" x1={at[h!.s][h!.i].x} x2={at[h!.s][h!.i].x} y1={TOP} y2={TOP + LINE_H} />}
            {live.map((s, k) => {
              const pts = at[k].map(p => `${p.x.toFixed(1)},${p.y.toFixed(1)}`).join(' ')
              return (
                <g key={s.name} opacity={h && h.s !== k ? 0.45 : 1}>
                  {s.pts.length > 1 && <polyline points={pts} fill="none" stroke={halo(s.color, dark)} strokeWidth={4} strokeLinejoin="round" strokeLinecap="round" />}
                  {s.pts.length > 1 && <polyline points={pts} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />}
                  {s.pts.map((p, i) => p.hollow
                    ? <circle key={p.key} cx={at[k][i].x} cy={at[k][i].y} r={r} className="hollow" stroke={s.color} strokeWidth={1.6} />
                    : <circle key={p.key} cx={at[k][i].x} cy={at[k][i].y} r={r} fill={s.color} stroke={halo(s.color, dark)} strokeWidth={0.8} />)}
                </g>
              )
            })}
            {hp && <circle className="ring" cx={at[h!.s][h!.i].x} cy={at[h!.s][h!.i].y} r={r + 3.5} />}
            {ends.map((e, k) => (
              <g key={e.s.name}>
                {(abs(ys[k] - 3.5 - e.y) > 2 || right + 6 - e.x > 10) && (
                  <line x1={e.x + 5} y1={e.y} x2={right + 4} y2={ys[k] - 3.5} stroke={e.s.color} strokeOpacity={0.7} strokeDasharray="0.1 3" strokeLinecap="round" strokeWidth={1.2} />
                )}
                <text x={right + 7} y={ys[k]} fontSize={10.5} fontWeight={600} fill={labelColor(e.s.color, dark)}>{e.s.label}</text>
              </g>
            ))}
          </svg>
        )}
      </div>
      <PointTip svg={svg.current} at={h ? at[h.s][h.i] : null}>{hp?.tip}</PointTip>
    </Panel>
  )
}

// ── the cost panel ─────────────────────────────────────────────────────────

/** The smallest round number (a `niceTicks` step multiple) at or above `v`; 0 for 0. */
function niceCeil(v: number): number {
  if (!v) return 0
  const nt = niceTicks(0, v, 2)
  const step = nt.length > 1 ? nt[1] - nt[0] : v
  return ceil(v / step - 1e-9) * step
}

interface Seg { r: FleetRun; lo: number; hi: number }
interface CostDay { t: number; total: number; segs: Seg[] }

/** Each UTC day's runs with a cost, stacked: scheduled (and unrecorded triggers) first, then hand-started, each by start. */
function costDays(runs: FleetRun[], from: number, to: number): CostDay[] {
  const days = new Map<number, FleetRun[]>()
  for (const r of runs) {
    if (r.started === null || r.started < from || r.started >= to || r.cost_usd === null || runOutcome(r) === 'running') continue
    const t = floor(r.started / DAY) * DAY
    days.set(t, [...(days.get(t) ?? []), r])
  }
  return [...days].sort(([a], [b]) => a - b).map(([t, rs]) => {
    const order = rs.sort((a, b) => Number(a.trigger === 'manual') - Number(b.trigger === 'manual') || a.started! - b.started!)
    let lo = 0
    const segs = order.map(r => { const s = { r, lo, hi: lo + r.cost_usd! }; lo = s.hi; return s })
    return { t, total: lo, segs }
  })
}

function CostPanel({ ds, from, to }: { ds: FleetDeployment[]; from: number; to: number }) {
  const [ref, w] = useWidth()
  const dark = useDark()
  const svg = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<{ row: number; day: number; seg: number } | null>(null)
  const act = useOpen()
  const rows = ds.map(d => {
    const days = costDays(d.runs, from, to)
    const total = days.reduce((s, x) => s + x.total, 0)
    const top = max(0, ...days.map(x => x.total))
    return { d, days, total, hi: niceCeil(top), label: days.length ? `${fmtUsd(total)}/${round((to - from) / DAY)}d` : 'not recorded' }
  })
  const gutter = max(32, ...rows.map(r => interWidth(`${r.d.name} ${r.label}`, 10.5, 600))) + 10
  const right = max(LEFT + 40, w - gutter)
  const X = (t: number) => LEFT + ((t - from) / (to - from)) * (right - LEFT)
  let y = TOP
  const bands = rows.map(r => {
    const h = r.days.length ? COST_ROW : COST_EMPTY
    const b = { y0: y, h }
    y += h + COST_GAP
    return b
  })
  const plotH = y - COST_GAP - TOP
  const dayW = X(from + DAY) - X(from)
  const barW = max(1.5, min(dayW * 0.72, 22))
  const Yr = (k: number, v: number) => bands[k].y0 + bands[k].h - (rows[k].hi ? (v / rows[k].hi) * bands[k].h : 0)

  const pick = (e: { clientX: number; clientY: number }) => {
    if (!svg.current) return null
    const { x, y: py } = local(svg.current, e)
    const row = bands.findIndex(b => py >= b.y0 - COST_GAP / 2 && py < b.y0 + b.h + COST_GAP / 2)
    if (row < 0 || x < LEFT || x > right) return null
    const t = from + floor((x - LEFT) / dayW) * DAY
    const day = rows[row].days.findIndex(dd => dd.t === t)
    if (day < 0) return null
    const segs = rows[row].days[day].segs
    let seg = segs.findIndex(s => py <= Yr(row, s.lo) && py >= Yr(row, s.hi))
    if (seg < 0) seg = py < Yr(row, segs[segs.length - 1].hi) ? segs.length - 1 : 0
    return { row, day, seg }
  }
  const h = hover && rows[hover.row]?.days[hover.day]?.segs[hover.seg] ? hover : null
  const hd = h ? rows[h.row].days[h.day] : null
  const hs = h ? hd!.segs[h.seg] : null

  return (
    <Panel
      title="cost per scan"
      keys={<span className="keys"><i className="sw" /> scheduled <i className="sw manual" /> by hand · own scale per row</span>}
    >
      <div ref={ref} className="plot">
        {w > 0 && (
          <svg
            ref={svg} width={w} height={TOP + plotH + BOTTOM} role="img" aria-label="cost per scan, stacked by UTC day"
            style={{ cursor: hs ? 'pointer' : 'default' }}
            onPointerMove={e => setHover(pick(e))}
            onPointerDown={e => { act.down(e); setHover(pick(e)) }}
            onPointerLeave={() => { if (!act.isTouch()) setHover(null) }}
            onClick={e => { const p = pick(e); if (p) { const r = rows[p.row]; const s = r.days[p.day].segs[p.seg]; act.open(e, s.r.run_id, runHref(r.d, s.r)) } }}
          >
            <XAxis from={from} to={to} X={X} y0={TOP} h={plotH} />
            {rows.map((r, k) => {
              const b = bands[k]
              return (
                <g key={r.d.name}>
                  <rect className="bg" x={LEFT} y={b.y0} width={right - LEFT} height={b.h} />
                  {r.days.length > 0 && <text className="tick" x={LEFT - 4} y={b.y0 + 8} textAnchor="end">{fmtUsd(r.hi).replace(/\.00$/, '')}</text>}
                  {r.days.map((dd, i) => dd.segs.map((s, j) => {
                    const manual = s.r.trigger === 'manual'
                    const on = h?.row === k && h.day === i && h.seg === j
                    return (
                      <rect
                        key={s.r.run_id}
                        x={X(dd.t) + (dayW - barW) / 2} width={barW}
                        y={Yr(k, s.hi)} height={max(0.8, Yr(k, s.lo) - Yr(k, s.hi))}
                        fill={r.d.color} fillOpacity={manual ? 0.4 : 1}
                        stroke={on ? 'var(--ink)' : manual ? r.d.color : halo(r.d.color, dark)} strokeWidth={on ? 1.5 : 0.7}
                      />
                    )
                  }))}
                  <text x={right + 7} y={b.y0 + min(b.h, 20) - 4} fontSize={10.5} fontWeight={600} fill={labelColor(r.d.color, dark)}>
                    {r.d.name} <tspan className="gutter-sub">{r.label}</tspan>
                  </text>
                </g>
              )
            })}
          </svg>
        )}
      </div>
      <PointTip svg={svg.current} at={h ? { x: X(hd!.t) + dayW / 2, y: Yr(h.row, hs!.hi) } : null}>
        {h && <RunInfo d={rows[h.row].d} r={hs!.r} extra={hd!.segs.length > 1 && <div className="muted">{md(hd!.t)}: {fmtUsd(hd!.total)} over {hd!.segs.length} runs</div>} />}
      </PointTip>
    </Panel>
  )
}

// ── the section ────────────────────────────────────────────────────────────

/** A store point's readout: its size, the change since the deployment's previous point, and its scan. */
function StoreInfo({ d, what, v, prev, fmt, scan, href }: { d: FleetDeployment; what: string; v: number; prev: number | null; fmt: (n: number) => string; scan: string; href?: string }) {
  return (
    <div className="run-info">
      <div className="tip-head"><span className="dot" style={{ background: d.color }} /> <b>{d.name}</b> {scan}</div>
      <div>{what} <b>{fmt(v)}</b> <Delta now={v} then={prev} fmt={fmt} suffix="vs prior scan" /></div>
      {href ? <div className="muted">click: its scan run</div> : <div className="muted">no run recorded for this scan</div>}
    </div>
  )
}

/** A daily store series (`/meta` bytes, primary objects) as a line, each point linked to its scan's run when there is one. */
function storeSeries(
  d: FleetDeployment,
  pts: { scan: string; t: number; v: number }[],
  from: number,
  to: number,
  what: string,
  fmt: (n: number) => string,
): LineSeries {
  const shown = pts.map((p, i) => ({ p, prev: i ? pts[i - 1].v : null })).filter(({ p }) => p.t >= from && p.t < to && p.v > 0)
  return {
    name: d.name,
    color: d.color,
    label: shown.length ? `${d.name} ${fmt(shown[shown.length - 1].p.v)}` : d.name,
    pts: shown.map(({ p, prev }) => {
      const run = d.runs.find(r => r.scan === p.scan)
      const href = run ? runHref(d, run) : undefined
      return { key: p.scan, t: p.t, v: p.v, href, tip: <StoreInfo d={d} what={what} v={p.v} prev={prev} fmt={fmt} scan={p.scan} href={href} /> }
    }),
  }
}

const missing = (names: string[], what: string): string | undefined => (names.length ? `${names.join(', ')}: ${what}` : undefined)

export function Charts({ doc }: { doc: FleetDoc }) {
  const ranges = (doc.days ?? 7) >= 30 ? [7, 30] : [7]
  const [range, setRange] = useRange(ranges)
  const to = (floor(doc.now / DAY) + 1) * DAY
  const from = to - range * DAY
  const ds = doc.deployments
  const meta = ds.map(d => storeSeries(d, (d.meta?.daily ?? []).map(p => ({ scan: p.scan, t: p.t, v: p.bytes })), from, to, '/meta', fmtSI))
  const objs = ds.map(d => storeSeries(d, (d.objects ?? []).map(p => ({ scan: p.scan, t: p.t, v: p.objects })), from, to, 'objects', fmtCount))
  const dur: LineSeries[] = ds.map(d => {
    const runs = d.runs.filter(r => r.started !== null && r.started >= from && r.started < to && r.secs !== null && runOutcome(r) !== 'running')
      .sort((a, b) => a.started! - b.started!)
    const pts = runs.map(r => {
      const o = runOutcome(r)
      return { key: r.run_id, t: r.started!, v: r.secs! / 3600, hollow: o === 'failed' || o === 'issues', href: runHref(d, r), tip: <RunInfo d={d} r={r} extra={<div className="muted">click: its run page</div>} /> }
    })
    return { name: d.name, color: d.color, pts, label: pts.length ? `${d.name} ${pts[pts.length - 1].v.toFixed(1)}h` : d.name }
  })
  return (
    <section className="charts">
      <div className="charts-head">
        <h2>History</h2>
        <span className="legend">
          {ds.map(d => <span key={d.name}><i className="dot" style={{ background: d.color }} /> {d.name}</span>)}
        </span>
        {ranges.length > 1 && (
          <span className="seg" role="group" aria-label="range">
            {ranges.map(n => <button key={n} type="button" aria-pressed={range === n} onClick={() => setRange(n)}>{n}d</button>)}
          </span>
        )}
      </div>
      <div className="chart-grid">
        <LinePanel title="/meta store size" note={missing(ds.filter((_, k) => !meta[k].pts.length).map(d => d.name), 'n/a')} series={meta} log fmtTick={fmtTick} from={from} to={to} />
        <LinePanel title="objects (primary store)" note={missing(ds.filter((_, k) => !objs[k].pts.length).map(d => d.name), 'n/a')} series={objs} log fmtTick={fmtCount} from={from} to={to} />
        <LinePanel
          title="scan duration"
          keys={<span className="keys"><i className="pt" /> ok <i className="pt hollow" /> issue open / failed</span>}
          note={missing(ds.filter((_, k) => !dur[k].pts.length).map(d => d.name), 'no scans')}
          series={dur} log={false} fmtTick={v => `${v}h`} from={from} to={to}
        />
        <CostPanel ds={ds} from={from} to={to} />
      </div>
    </section>
  )
}
