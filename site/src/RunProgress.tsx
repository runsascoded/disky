import { useState } from 'react'
import { useActions } from 'use-kbd'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { intParam, stringParam, useUrlState } from 'use-prms'
import { historyPoints, type HistoryPoint, type ProgressSample, runHref } from './runHistory'
import { alignedTicks, axisTicks, chartRange, durationLabel, parseDuration, rollingRates, runWindowParam, shiftWindow, zoomWindow } from './runChart'
import { fmtN } from './types'
import { Tooltip } from './Tooltip'
import { useUnits } from './units'
import type { PlanSummaryFile } from './runs'

type Metric = 'rate' | 'objects' | 'bytes' | 'byteRate'
type Props = { id: string; live: boolean; compact?: boolean; fmtBytes: (b: number) => string; lifetime?: { objects: number; seconds: number }; planned?: PlanSummaryFile | null }
const metrics: Metric[] = ['rate', 'objects', 'bytes', 'byteRate']
const metricName = (m: Metric) => m === 'rate' ? 'objects/s' : m === 'byteRate' ? 'bytes/s' : m
const metricKey = { rate: 'r', objects: 'o', bytes: 'b', byteRate: 'v' }
const isRate = (m: Metric | null) => m === 'rate' || m === 'byteRate'
const validMetric = (s: string | undefined): Metric => metrics.includes(s as Metric) ? s as Metric : 'rate'

function useHistory(id: string, live: boolean) {
  return useQuery<{ samples: ProgressSample[] }>({
    queryKey: ['run-history', id],
    queryFn: async () => {
      const r = await fetch(`/api/plans/progress?id=${encodeURIComponent(id)}`, { credentials: 'include' })
      if (!r.ok) throw new Error(`Progress history: HTTP ${r.status}`)
      return r.json()
    },
    refetchInterval: live ? 30_000 : false,
    staleTime: 20_000,
  })
}

function paths(points: HistoryPoint[], value: (p: HistoryPoint) => number | null, x: (ts: number) => number, y: (v: number) => number): string[] {
  const out: string[] = []
  let path = '', previous = 0
  for (const p of points) {
    const v = value(p)
    if (v == null || previous && p.ts - previous > 180) { if (path) out.push(path); path = '' }
    if (v != null) path += `${path ? ' L' : 'M'}${x(p.ts).toFixed(2)},${y(v).toFixed(2)}`
    previous = p.ts
  }
  if (path) out.push(path)
  return out
}

function Sparkline({ id, live, fmtBytes, lifetime }: Props) {
  const q = useHistory(id, live)
  const all = historyPoints(q.data?.samples ?? [])
  const end = live ? Date.now() / 1000 : all.at(-1)?.ts ?? 0
  const points = all.filter(p => p.ts >= end - 3600)
  const peak = Math.max(1, ...points.map(p => p.rate ?? 0))
  const rate = (v: number) => `${fmtN(Math.round(v))}/s`
  const recent = rollingRates(all, 3600).at(-1)
  const ps = paths(points, p => p.rate, ts => 4 + (ts - end + 3600) / 3600 * 132, v => 28 - v / peak * 24)
  return <Tooltip content={<>
    <div>{id}</div>
    {lifetime && lifetime.seconds > 0 && <div>Lifetime average: {rate(lifetime.objects / lifetime.seconds)} · includes startup and pauses</div>}
    {recent != null && <div>Last recorded hour average: {rate(recent)} · peak {rate(peak)}</div>}
    {q.error ? <div>{q.error.message}</div> : all.length < 2 ? <div>No recorded history; open details for totals.</div> : <div>Open rate history. Gaps indicate missing samples.{live && end - (all.at(-1)?.ts ?? 0) > 120 && ' Latest sample is stale.'}</div>}
  </>}>
    <Link className="run-sparkline" to={runHref(id)} aria-label="Open deletion-rate history">
      {points.length < 2 ? <span className="dim">—</span> : <svg viewBox="0 0 140 32" role="img" aria-label="Last hour deletion rate">{ps.map((d, i) => <path key={i} d={d} />)}</svg>}
      {lifetime && lifetime.seconds > 0 && <span className="spark-lifetime dim">{rate(lifetime.objects / lifetime.seconds)} lifetime avg</span>}
    </Link>
  </Tooltip>
}

function ProgressChart({ id, live, fmtBytes, planned }: Props) {
  const q = useHistory(id, live)
  const { units, fmtBytesLike } = useUnits()
  const [window, setWindow] = useUrlState('range', runWindowParam)
  const [metricP, setMetric] = useUrlState('metric', stringParam('rate'))
  const [secondP, setSecond] = useUrlState('axis', stringParam('none'))
  const [bucket, setBucket] = useUrlState('bucket', stringParam(''))
  const [smoothing, setSmoothing] = useUrlState('avg', intParam(0))
  const [hover, setHover] = useState<number | null>(null)
  const [drag, setDrag] = useState<number | null>(null)
  const [durationText, setDurationText] = useState('')
  const [durationError, setDurationError] = useState(false)
  const metric = validMetric(metricP)
  const secondary = metrics.includes(secondP as Metric) && secondP !== metric ? secondP as Metric : null
  useActions({ ...Object.fromEntries(metrics.flatMap(m => [
    [`run:primary:${m}`, { label: `Left axis: ${metricName(m)}`, group: 'Run progress metrics', defaultBindings: [metricKey[m]], handler: () => setMetric(m) }],
    [`run:secondary:${m}`, { label: `Right axis: ${metricName(m)}`, group: 'Run progress metrics', defaultBindings: [`shift+${metricKey[m]}`], handler: () => setSecond(m) }],
  ])), 'run:secondary:none': { label: 'Right axis: none', group: 'Run progress metrics', defaultBindings: ['shift+n'], handler: () => setSecond('none') } })
  const samples = q.data?.samples ?? []
  const buckets = [...new Set([...Object.keys(planned?.buckets ?? {}), ...samples.map(s => s.bucket)])].sort()
  const all = historyPoints(bucket ? samples.filter(s => s.bucket === bucket) : samples)
  const range = chartRange(all, window, live ? Date.now() / 1000 : all.at(-1)?.ts ?? Date.now() / 1000)
  const averageSeconds = Math.max(0, Math.min(86400, smoothing))
  const smoothed = rollingRates(all, averageSeconds), smoothedBytes = rollingRates(all, averageSeconds, 'byteRate')
  const averages = new Map(all.map((p, i) => [p.ts, { rate: smoothed[i], byteRate: smoothedBytes[i] }]))
  const points = all.filter(p => p.ts >= range[0] && p.ts <= range[1])
  const value = (p: HistoryPoint, m: Metric): number | null => (m === 'rate' || m === 'byteRate') && smoothing > 0 ? averages.get(p.ts)?.[m] ?? null : p[m]
  const tally = bucket ? planned?.buckets[bucket]?.eligible : Object.values(planned?.buckets ?? {}).reduce<{ bytes: number; objects: number }>((a, b) => ({ bytes: a.bytes + (b.eligible?.bytes ?? 0), objects: a.objects + (b.eligible?.objects ?? 0) }), { bytes: 0, objects: 0 })
  const target = (m: Metric) => m === 'bytes' ? tally?.bytes ?? 0 : m === 'objects' ? tally?.objects ?? 0 : 0
  const peak = (m: Metric) => Math.max(1, target(m), ...points.map(p => value(p, m) ?? 0))
  const scale = (m: Metric) => m === 'bytes' || m === 'byteRate' ? Math.pow(units === 'iec' ? 1024 : 1000, Math.max(0, Math.floor(Math.log(peak(m)) / Math.log(units === 'iec' ? 1024 : 1000)))) : 1
  const leftScale = scale(metric), rightScale = secondary ? scale(secondary) : 1
  const [lt, rt] = secondary ? alignedTicks(peak(metric) / leftScale, peak(secondary) / rightScale) : [axisTicks(peak(metric) / leftScale), []]
  const leftTicks = lt.map(v => v * leftScale), rightTicks = rt.map(v => v * rightScale)
  const left = 82, right = secondary ? 918 : 978, top = 28, bottom = 236
  const x = (ts: number) => left + (ts - range[0]) / (range[1] - range[0]) * (right - left)
  const y = (v: number, ts: number[]) => bottom - v / ts.at(-1)! * (bottom - top)
  const format = (v: number, m: Metric) => m === 'bytes' || m === 'byteRate' ? `${fmtBytesLike(v, peak(m))}${m === 'byteRate' ? '/s' : ''}` : `${fmtN(Math.round(v))}${m === 'rate' ? '/s' : ''}`
  const picked = hover === null ? null : points.reduce<HistoryPoint | null>((a, p) => !a || Math.abs(p.ts - hover) < Math.abs(a.ts - hover) ? p : a, null)
  const eventTime = (e: React.PointerEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect()
    return range[0] + Math.max(0, Math.min(1, ((e.clientX - box.left) / box.width * 1000 - left) / (right - left))) * (range[1] - range[0])
  }
  const clip = `progress-${id.replace(/[^A-Za-z0-9_-]/g, '-')}`
  return <section className="run-progress" id="progress">
    <div className="graph-controls">
      <h3>Progress</h3>
      {metrics.map(m => <Tooltip key={m} content={`Left: ${metricKey[m]} · right: Shift+${metricKey[m]}`}><button type="button" className={metric === m ? 'on' : ''} onClick={() => setMetric(m)}>{metricName(m)}</button></Tooltip>)}
      <label>right axis <select aria-label="Right axis" value={secondary ?? 'none'} onChange={e => setSecond(e.target.value)}><option value="none">none</option>{metrics.filter(m => m !== metric).map(m => <option value={m} key={m}>{metricName(m)}</option>)}</select></label>
      <select aria-label="Progress bucket" value={bucket} onChange={e => setBucket(e.target.value)}><option value="">all buckets</option>{buckets.map(b => <option key={b}>{b}</option>)}</select>
      {(isRate(metric) || isRate(secondary)) && <label>average <select aria-label="Rolling rate average" value={smoothing} onChange={e => setSmoothing(Number(e.target.value))}>{[0, 300, 900, 3600].map(n => <option key={n} value={n}>{n ? durationLabel(n) : 'raw'}</option>)}</select></label>}
    </div>
    <div className="graph-controls graph-window">
      <button type="button" className={window.end === null && window.duration === 3600 ? 'on' : ''} onClick={() => setWindow({ end: null, duration: 3600 })}>last hour</button>
      <button type="button" className={window.duration === null ? 'on' : ''} onClick={() => setWindow({ end: null, duration: null })}>all time</button>
      <form onSubmit={e => { e.preventDefault(); const duration = parseDuration(durationText); setDurationError(duration === null); if (duration !== null) setWindow({ ...window, duration }) }}>
        <label>duration <input aria-label="Chart duration" aria-invalid={durationError} placeholder={window.duration === null ? 'all' : durationLabel(window.duration)} value={durationText} onChange={e => { setDurationText(e.target.value); setDurationError(false) }} /></label><button type="submit">apply</button>
      </form>
      <label>end (UTC) <input type="datetime-local" aria-label="Chart end in UTC" value={new Date(range[1] * 1000).toISOString().slice(0, 16)} onChange={e => { const end = Date.parse(`${e.target.value}Z`) / 1000; if (Number.isFinite(end)) setWindow({ ...window, end }) }} /></label>
      <button type="button" className={window.end === null ? 'on' : ''} onClick={() => setWindow({ ...window, end: null })}>latest</button>
      <Tooltip content="Pan backward half a window"><button type="button" aria-label="Pan earlier" onClick={() => setWindow(shiftWindow(range, -1))}>←</button></Tooltip>
      <Tooltip content="Pan forward half a window"><button type="button" aria-label="Pan later" onClick={() => setWindow(shiftWindow(range, 1))}>→</button></Tooltip>
      <Tooltip content="Zoom in; drag across the plot to select a range"><button type="button" aria-label="Zoom in" onClick={() => setWindow(zoomWindow(range, 0.5))}>+</button></Tooltip>
      <Tooltip content="Zoom out"><button type="button" aria-label="Zoom out" onClick={() => setWindow(zoomWindow(range, 2))}>−</button></Tooltip>
      {durationError && <span className="err">Use 45m, 1.5h, 2h30m… (1m–365d).</span>}
    </div>
    {q.error ? <p className="err">{q.error.message}</p> : all.length < 2 ? <p className="dim">{q.isLoading ? 'Loading history…' : 'No recorded history yet.'}</p> : <>
      <Tooltip content={picked ? <><div>{new Date(picked.ts * 1000).toLocaleString()}</div><div>{fmtN(picked.objects)} objects · {fmtBytes(picked.bytes)}</div>{(['rate', 'byteRate'] as const).map(m => <div key={m}>{picked[m] === null ? `${metricName(m)} unavailable` : `${format(picked[m], m)} interval rate`}{smoothing > 0 && averages.get(picked.ts)?.[m] != null && <> · {format(averages.get(picked.ts)![m]!, m)} trailing {durationLabel(smoothing)}</>}</div>)}</> : 'Hover for exact values; drag to zoom.'}>
        <svg className="progress-chart" viewBox="0 0 1000 270" role="img" aria-label={`${metricName(metric)}${secondary ? ` and ${metricName(secondary)}` : ''} over time`} onPointerMove={e => setHover(eventTime(e))} onPointerLeave={() => { if (drag === null) setHover(null) }} onPointerDown={e => { setDrag(eventTime(e)); e.currentTarget.setPointerCapture(e.pointerId) }} onPointerUp={e => { const end = eventTime(e); if (drag !== null && Math.abs(end - drag) >= 60) setWindow({ end: Math.max(end, drag), duration: Math.round(Math.abs(end - drag)) }); setDrag(null) }} onPointerCancel={() => setDrag(null)}>
          <defs><clipPath id={clip}><rect x={left} y={top} width={right - left} height={bottom - top} /></clipPath></defs>
          <text x={left} y="15" className="primary-label">{metricName(metric)}</text>
          {secondary && <text x={right} y="15" textAnchor="end" className="secondary-label">{metricName(secondary)}</text>}
          {leftTicks.map(v => <g key={v}><line className="chart-grid" x1={left} x2={right} y1={y(v, leftTicks)} y2={y(v, leftTicks)} /><text x={left - 8} y={y(v, leftTicks) + 4} textAnchor="end">{format(v, metric)}</text></g>)}
          {secondary && rightTicks.map(v => <text key={v} className="secondary-label" x={right + 8} y={y(v, rightTicks) + 4}>{format(v, secondary)}</text>)}
          {[0, 0.25, 0.5, 0.75, 1].map(f => <text key={f} x={x(range[0] + f * (range[1] - range[0]))} y="257" textAnchor={f === 0 ? 'start' : f === 1 ? 'end' : 'middle'}>{new Date((range[0] + f * (range[1] - range[0])) * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</text>)}
          <g clipPath={`url(#${clip})`}>
            {[metric, secondary].map((m, i) => m && target(m) > 0 ? <g key={m} className={i ? 'chart-target secondary-target' : 'chart-target'}><line x1={left} x2={right} y1={y(target(m), i ? rightTicks : leftTicks)} y2={y(target(m), i ? rightTicks : leftTicks)} /><text x={i ? right - 5 : left + 5} textAnchor={i ? 'end' : 'start'} y={y(target(m), i ? rightTicks : leftTicks) - 5}>target {m === 'bytes' ? fmtBytes(target(m)) : format(target(m), m)}</text></g> : null)}
            {smoothing > 0 && isRate(metric) && paths(points, p => p[metric], x, v => y(v, leftTicks)).map((d, i) => <path className="chart-raw" d={d} key={`raw-${i}`} />)}
            {paths(points, p => value(p, metric), x, v => y(v, leftTicks)).map((d, i) => <path key={i} d={d} />)}
            {secondary && paths(points, p => value(p, secondary), x, v => y(v, rightTicks)).map((d, i) => <path className="chart-secondary" key={`second-${i}`} d={d} />)}
            {drag !== null && hover !== null && <rect className="chart-selection" x={x(Math.min(drag, hover))} y={top} width={Math.abs(x(drag) - x(hover))} height={bottom - top} />}
            {picked && <line className="chart-cursor" x1={x(picked.ts)} x2={x(picked.ts)} y1={top} y2={bottom} />}
          </g>
        </svg>
      </Tooltip>
      <div className="graph-times"><span>{new Date(range[0] * 1000).toLocaleString()}</span><span>{new Date(range[1] * 1000).toLocaleString()}</span></div>
      <p className="dim">Interval rate; gaps remain blank.{!points.length && ' No samples in the selected window.'}{samples.some(s => !s.bytes_exact) && ' Backfilled bytes are approximate (rounded task logs).'}{live && Date.now() / 1000 - (all.at(-1)?.ts ?? 0) > 120 && ' Latest sample is stale.'}</p>
    </>}
  </section>
}

export function RunProgress(props: Props) {
  return props.compact ? <Sparkline {...props} /> : <ProgressChart {...props} />
}
