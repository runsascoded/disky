// /scans and /scans/<run id> (specs/done/scan-runs-ui.md): what each scan job did —
// when it ran, its phases, what it wrote and how that compares to the
// previous scan — from the D1 rows `dt-cloud scan-run record | backfill`
// writes. DIY SVG: a duration strip over all runs, a phase bar per row, and a
// phase timeline per run.
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent, type ReactNode, type RefObject } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useLocation, useNavigate, useParams } from 'react-router-dom'
import { SiteNav } from './SiteNav'
import { useDocTitle } from './title'
import { SiteKbd } from './SiteKbd'
import { Tooltip } from './Tooltip'
import { axisTicks } from './runChart'
import { fmtBytesPrecise, fmtN } from './types'
import { useUnits } from './units'
import { STORES, type Store } from './stores'
import { useMinSlug } from './scan'
import {
  batchHref, logsHref, mapHref, metaHref, type OutputDelta, phaseOrder, type PhaseSpan, type RunSummary, type ScanRun,
  type ScanRunPhase, type PhaseIssue, type PhaseRecovery, overlappingFailures, phaseFill, phaseOutcome, runOutcome, runTasks, type RunTask, taskLogsHref,
} from './scanRunsModel'
import { colorer, fmtDur, fmtRunTime, getJson, type Links, PhaseBar, runDay, RunWhen, scanRunsApi, scansHref, scansStore, Status, useScanRuns } from './scanRunsUi'
import { brushRole, dagNodes, processOf, type DagNode, type Hl, type RunDagRow } from './runDag'
import './scans.scss'

type DetailResponse = Links & {
  run: ScanRun; secs: number | null; failed_at: string | null; phases: ScanRunPhase[]; spans: PhaseSpan[]
  outputs: OutputDelta[]; downstream: ScanRun[]; siblings: ScanRun[]; prev_scan: string | null; next_scan: string | null
  phase_issues: PhaseIssue[]; recoveries: PhaseRecovery[]; dag?: RunDagRow | null
}

const useScanRun = (id: string, store: Store) => useQuery<DetailResponse, Error>({
  queryKey: ['scan-run', store.key, id], queryFn: () => getJson(scanRunsApi(store, id)),
  refetchInterval: q => (q.state.data?.run.status === 'running' || q.state.data?.phase_issues?.some(i => !i.recovery) ? 30_000 : false), staleTime: 15_000,
})

/** The store this page shows (`?store=<key>`; the primary without one) and its run links. */
function useScansStore(): { store: Store; scanHref: (id?: string) => string } {
  const { search } = useLocation()
  const store = useMemo(() => scansStore(search), [search])
  return { store, scanHref: (id?: string) => scansHref(store, id) }
}

function useBytes() {
  const { units, suffixB } = useUnits()
  return {
    bytes: (b: number | null | undefined) => (b == null ? '—' : fmtBytesPrecise(b, units, suffixB)),
    delta: (b: number | null | undefined) => (b == null ? '—' : b === 0 ? '±0' : `${b > 0 ? '+' : '−'}${fmtBytesPrecise(Math.abs(b), units, suffixB)}`),
  }
}

/** A tip for SVG marks (the HTML `Tooltip` wraps its reference in a span,
 * which SVG can't hold): marks set it on pointer move, the wrapper renders it. */
function useSvgTip() {
  const [tip, setTip] = useState<{ x: number; y: number; content: ReactNode } | null>(null)
  const on = (content: ReactNode) => ({
    onPointerMove: (e: ReactPointerEvent<SVGElement>) => {
      const box = (e.currentTarget.ownerSVGElement ?? e.currentTarget).parentElement!.getBoundingClientRect()
      setTip({ x: e.clientX - box.left, y: e.clientY - box.top, content })
    },
    onPointerLeave: () => setTip(null),
  })
  const el = tip && <div className="sr-svgtip tooltip-content" style={{ left: tip.x, top: tip.y }}>{tip.content}</div>
  return { on, el }
}

/** The wrapper's width in CSS px, so a chart draws at 1:1 (text stays legible
 * on a phone instead of scaling down with a fixed viewBox). Measured before
 * observing: a hidden tab never gets the first ResizeObserver callback. */
function useWidth<T extends HTMLElement>(fallback = 720): [RefObject<T | null>, number] {
  const ref = useRef<T>(null)
  const [w, setW] = useState(fallback)
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    setW(el.clientWidth || fallback)
    const ro = new ResizeObserver(() => setW(el.clientWidth || fallback))
    ro.observe(el)
    return () => ro.disconnect()
  }, [fallback])
  return [ref, w]
}

const usd = (x: number | null | undefined) => (x == null ? '—' : `$${x < 10 ? x.toFixed(2) : Math.round(x).toLocaleString('en-US')}`)

const deltaClass = (d: number | null | undefined) => (d == null || d === 0 ? 'dim' : d > 0 ? 'grew' : 'shrank')

/** Duration per run over time, stacked by phase; a failed run in the danger ink. */
function DurationStrip({ runs, color, onPick }: { runs: RunSummary[]; color: (p: string) => string; onPick: (id: string) => void }) {
  const tip = useSvgTip()
  const [ref, W] = useWidth<HTMLDivElement>()
  const pts = runs.filter(r => r.secs != null && r.run.started_ts != null)
  if (pts.length < 2) return null
  const H = W < 500 ? 150 : 190, L = 36, R = 6, T = 8, B = 20
  const t0 = Math.min(...pts.map(p => p.run.started_ts!)), t1 = Math.max(...pts.map(p => p.run.started_ts!))
  const span = Math.max(3600, t1 - t0)
  const hours = Math.max(...pts.map(p => p.secs!)) / 3600
  const ticks = axisTicks(hours)
  const top = ticks[ticks.length - 1] || 1
  const x = (t: number) => L + ((t - t0) / span) * (W - L - R)
  const y = (h: number) => H - B - (h / top) * (H - T - B)
  const bw = Math.max(1.5, Math.min(14, ((W - L - R) / pts.length) * 0.7))
  const days = [...new Set(pts.map(p => runDay(p.run.started_ts!)))]
  const labelEvery = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(W / 90))))
  return (
    <div className="sr-svgwrap" ref={ref}>
      <svg className="sr-strip" width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Run duration over time, by phase">
        {ticks.map(h => <g key={h}><line className="grid" x1={L} x2={W - R} y1={y(h)} y2={y(h)} /><text className="tick" x={L - 6} y={y(h) + 4} textAnchor="end">{h}h</text></g>)}
        {days.filter((_, i) => i % labelEvery === 0).map(d => {
          const t = Date.parse(`${d}T00:00:00`) / 1000
          return <text key={d} className="tick" x={Math.min(W - R - 16, Math.max(L + 16, x(t)))} y={H - 6} textAnchor="middle">{d.slice(5)}</text>
        })}
        {pts.map(p => {
          const cx = x(p.run.started_ts!) - bw / 2
          const failed = p.run.status === 'failed'
          const main = p.spans.filter(s => s.lane === 0)
          const where = p.failed_at ? ` · failed ${p.failed_at.startsWith('after') ? p.failed_at : `in ${p.failed_at}`}` : ''
          return (
            <g key={p.run.run_id} className={`sr-col${failed ? ' failed' : ''}`} onClick={() => onPick(p.run.run_id)} role="link" aria-label={`${p.run.scan} ${p.run.kind} run`}
              {...tip.on(<><div><b>{p.run.scan}</b> · {p.run.kind} · {runOutcome(p.run, p.phase_issues)}</div><div>{fmtDur(p.secs!)}{where}</div>{p.spans.map(s => <div key={s.phase}><i className="sw" style={{ background: phaseFill(s, color) }} />{s.phase} {fmtDur(s.end - s.start)}{s.lane ? ' (beside)' : ''}{s.status === 'failed' ? ` · ${phaseOutcome(s)}` : ''}</div>)}</>)}>
              <rect className="sr-col-hit" x={cx - 2} y={T} width={bw + 4} height={H - T - B} />
              <rect className="sr-col-total" x={cx} y={y(p.secs! / 3600)} width={bw} height={Math.max(1, y(0) - y(p.secs! / 3600))} />
              {!failed && main.map(s => (
                <rect key={s.phase} x={cx} y={y(s.end / 3600)} width={bw} height={Math.max(0.5, y(s.start / 3600) - y(s.end / 3600))} style={{ fill: phaseFill(s, color) }} />
              ))}
              {!failed && overlappingFailures(p.spans).map(s => <rect key={`failed-${s.phase}`} x={cx + bw * 0.6} y={y(s.end / 3600)} width={Math.max(1, bw * 0.4)} height={Math.max(0.5, y(s.start / 3600) - y(s.end / 3600))} style={{ fill: 'var(--danger)' }} />)}
            </g>
          )
        })}
        <line className="axis" x1={L} x2={W - R} y1={y(0)} y2={y(0)} />
      </svg>
      {tip.el}
    </div>
  )
}

function Legend({ order, color }: { order: string[]; color: (p: string) => string }) {
  if (!order.length) return null
  return <div className="sr-legend">{order.map(p => <span key={p}><i className="sw" style={{ background: color(p) }} />{p}</span>)}<span><i className="sw" style={{ background: 'var(--other)' }} />unrecorded work (not necessarily idle)</span><span><i className="sw failed" />failed</span></div>
}

function Links({ run, links }: { run: ScanRun; links: Links }) {
  return <>
    {links.project && run.job_name && run.region && <a href={batchHref(links.project, run.region, run.job_name)} target="_blank" rel="noreferrer">Batch ↗</a>}
    {links.project && <> · <a href={logsHref(links.project, run)} target="_blank" rel="noreferrer">logs ↗</a></>}
  </>
}

export function ScansPage() {
  const { store, scanHref } = useScansStore()
  const q = useScanRuns(store)
  const slug = useMinSlug(store)
  const nav = useNavigate()
  const { bytes, delta } = useBytes()
  const [kind, setKind] = useState('')
  const [status, setStatus] = useState('')
  useDocTitle('Scans')
  const runs = q.data?.runs ?? []
  const order = useMemo(() => phaseOrder(runs.flatMap(r => r.spans.map((s, i) => ({ phase: s.phase, seq: i })))), [runs])
  const color = colorer(order)
  const kinds = [...new Set(runs.map(r => r.run.kind))].sort()
  const shown = runs.filter(r => (!kind || r.run.kind === kind) && (!status || runOutcome(r.run, r.phase_issues) === status))
  return (
    <main className="staged-page scans-page">
      <SiteNav />
      <h1>Scan runs</h1>
      {STORES.length > 1 && <div className="run-filters">
        <select aria-label="Store" value={store.key} onChange={e => nav(scansHref(STORES.find(s => s.key === e.target.value) ?? STORES[0]))}>{STORES.map(s => <option key={s.key} value={s.key}>{s.label}</option>)}</select>
      </div>}
      <p className="sub">Each scan job's run: when it ran, how long each phase took, what it wrote, and how each structure changed against the previous scan. The stores they append to are on <Link to="/health">Health</Link>.</p>
      {q.isLoading ? <p>Loading runs…</p> : q.error ? <p className="err">{q.error.message}</p> : !q.data?.configured ? (
        <p className="dim">Scan runs aren't recorded on this deployment yet (its D1 has no <code>scan_runs</code> table: migration <code>scan_runs</code>, then <code>dt-cloud scan-run backfill</code>).</p>
      ) : (<>
        <section className="sr-chart">
          <h3>Duration by run</h3>
          <DurationStrip runs={shown} color={color} onPick={id => nav(scanHref(id))} />
          <Legend order={order} color={color} />
        </section>
        <div className="run-filters">
          {kinds.length > 1 && <select aria-label="Filter by kind" value={kind} onChange={e => setKind(e.target.value)}><option value="">all kinds</option>{kinds.map(k => <option key={k}>{k}</option>)}</select>}
          <select aria-label="Filter by status" value={status} onChange={e => setStatus(e.target.value)}><option value="">all statuses</option>{['succeeded', 'partial', 'recovered', 'failed', 'running', 'no-op'].map(s => <option key={s} value={s}>{s}</option>)}</select>
          <span className="dim">{shown.length} / {runs.length} runs</span>
        </div>
        <div className="runs-wrap">
          <table className="runs sr-runs">
            <thead><tr>
              <th><Tooltip content="When the scan started, in your time zone; hover for its id.">scan</Tooltip></th><th>status</th>
              <th className="num">duration</th><th className="hide-md">phases</th>
              <th className="num"><Tooltip content="Bytes of the run's top-level outputs (listing, index, snapshot, …).">wrote</Tooltip></th>
              <th className="num"><Tooltip content="Against the previous scan's same structures. Green grew, red shrank.">Δ prev</Tooltip></th>
              <th className="num"><Tooltip content="Compute estimate: the machine's list $/h × run hours × tasks, including the run's downstream jobs (listing fan-out).">cost</Tooltip></th>
              <th className="hide-md">links</th>
            </tr></thead>
            <tbody>
              {shown.map(s => {
                const r = s.run
                const map = r.status === 'succeeded' ? mapHref(r.scan, store.path, slug) : null
                return (
                  <tr key={r.run_id} className={r.status === 'failed' ? 'failed' : undefined}>
                    <td className="nb"><RunWhen ts={r.started_ts} scan={r.scan} href={scanHref(r.run_id)} />
                      {r.trigger === 'manual' && <> <Tooltip content="Started by hand (a submit or a resume), not by the schedule."><span className="dim">manual</span></Tooltip></>}
                      {(kinds.length > 1 || s.downstream > 0) && <div className="sr-kind">{kinds.length > 1 && r.kind}{s.downstream > 0 && <Tooltip content={`${s.downstream} downstream job${s.downstream > 1 ? 's' : ''} (listing fan-out, …)`}><span>{kinds.length > 1 ? ' + ' : '+ '}{s.downstream} job{s.downstream > 1 ? 's' : ''}</span></Tooltip>}</div>}</td>
                    <td><Status cell run={r} failedAt={s.failed_at} issues={s.phase_issues} /></td>
                    <td className="num nb">{s.secs == null ? <span className="dim">—</span> : fmtDur(s.secs)}</td>
                    <td className="hide-md"><PhaseBar cell spans={s.spans} secs={s.secs} color={color} width={130} /></td>
                    <td className="num nb">{bytes(s.out_bytes)}</td>
                    <td className={`num nb ${deltaClass(s.delta_bytes)}`}>{delta(s.delta_bytes)}</td>
                    <td className="num nb">{s.cost_usd == null ? <span className="dim">—</span> : usd(s.cost_usd)}</td>
                    <td className="nb links hide-md">{map && <><Link to={map}>map</Link> · </>}<Links run={r} links={q.data!} /></td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </>)}
      <SiteKbd />
    </main>
  )
}

/** How long a highlight outlives the pointer leaving its mark: crossing from one phase row to the next, or the gap
 * between two DAG nodes, keeps it instead of flashing everything back on. */
const LINGER_MS = 160

/** Brushing between the pipeline DAG and the phase timeline: `hl` is the hovered step or arrow, else the pinned
 * step (clicked: its drawer is open under the DAG; Esc or a second click closes it). */
interface Brush { hl: Hl | null; pinned: string | null; hover: (h: Hl) => void; leave: () => void; toggle: (node: string) => void }

function useBrush(): Brush {
  const [hov, setHov] = useState<Hl | null>(null)
  const [pinned, setPinned] = useState<string | null>(null)
  const timer = useRef<number | undefined>(undefined)
  const cancel = () => { window.clearTimeout(timer.current); timer.current = undefined }
  useEffect(() => cancel, [])
  useEffect(() => {
    if (!pinned) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setPinned(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [pinned])
  return {
    hl: hov ?? (pinned ? { from: pinned } : null),
    pinned,
    hover: h => { cancel(); setHov(p => (p && p.from === h.from && p.to === h.to ? p : h)) },
    leave: () => { cancel(); timer.current = window.setTimeout(() => { timer.current = undefined; setHov(null) }, LINGER_MS) },
    toggle: node => { cancel(); setHov(null); setPinned(p => (p === node ? null : node)) },
  }
}

/** A tooltip's pointer handlers, plus the brush: entering an element highlights `h`, leaving clears it (after
 * `LINGER_MS`), and clicking a step pins it. */
function brushed(tipOn: ReturnType<ReturnType<typeof useSvgTip>['on']>, brush: Brush | undefined, h: Hl | null) {
  if (!brush || !h) return tipOn
  return {
    onPointerMove: (e: ReactPointerEvent<SVGElement>) => { tipOn.onPointerMove(e); brush.hover(h) },
    onPointerLeave: () => { tipOn.onPointerLeave(); brush.leave() },
    ...(h.to ? {} : { onClick: () => brush.toggle(h.from) }),
  }
}
const brushClass = (brush: Brush | undefined, node: string | null): string => {
  const role = brushRole(brush?.hl ?? null, node)
  return role === 'dimmed' ? ' dimmed' : role ? ` brushed ${role}` : ''
}

/** The run's phases on its clock: one row per phase. */
function PhaseTimeline({ spans, secs, color, brush }: { spans: PhaseSpan[]; secs: number | null; color: (p: string) => string; brush?: Brush }) {
  const tip = useSvgTip()
  const [ref, W] = useWidth<HTMLDivElement>()
  if (!spans.length || !secs) return <p className="dim">No phase markers for this run (the job records its phases; the backfill recovers them from the last 30 days of logs).</p>
  const last = Math.max(...spans.map(s => s.end))
  const tail = secs - last > 30 ? [{ phase: 'unrecorded work', start: last, end: secs, status: 'done' as const, note: 'No phase timing recorded for this interval. Work may be running here; this is not evidence of idle time.', lane: 0, tail: true }] : []
  const rows: (PhaseSpan & { tail?: boolean })[] = [...spans, ...tail]
  const narrow = W < 500
  const rowH = 24, L = narrow ? 112 : 160, R = rows.some(s => s.status !== 'done') ? (narrow ? 116 : 145) : (narrow ? 40 : 56)
  const H = rows.length * rowH + 24
  const x = (t: number) => L + (t / secs) * (W - L - R)
  const maxTicks = Math.max(3, Math.floor((W - L - R) / 60))
  const step = [60, 300, 600, 900, 1800, 3600, 7200, 14400].find(s => secs / s <= maxTicks) ?? 28800
  const cut = narrow ? 15 : 22
  const ticks = Array.from({ length: Math.floor(secs / step) + 1 }, (_, i) => i * step)
  return (
    <div className="sr-svgwrap" ref={ref}>
      <svg className="sr-gantt" width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Phase timeline">
        {ticks.map(t => <g key={t}><line className="grid" x1={x(t)} x2={x(t)} y1={0} y2={H - 20} /><text className="tick" x={x(t)} y={H - 5} textAnchor="middle">{t ? fmtDur(t).replace(' 00m', '') : '0'}</text></g>)}
        {rows.map((s, i) => (
          <g key={s.phase} className={`${s.tail ? 'tail' : s.status === 'running' ? 'active' : ''}${brushClass(brush, s.task ? processOf(s.task) : null)}` || undefined} {...brushed(tip.on(<><div><b>{s.phase}</b>{s.lane ? ' — beside the main sequence' : ''}</div><div>{fmtDur(s.start)} → {s.status === 'running' ? 'now' : fmtDur(s.end)} · {fmtDur(s.end - s.start)}{s.status === 'running' ? ' elapsed · running' : ''}</div>{s.task ? <div className="dim">in pipeline step <b>{s.task}</b>{s.machine ? ` · ${s.machine}` : ''}</div> : s.machine && <div className="dim">its own Batch task · {s.machine}</div>}{s.note && <div className="dim">{s.note}</div>}{s.task && brush && <div className="dim">{brush.pinned === processOf(s.task) ? 'Click to close its step\u2019s tasks.' : 'Click for its step\u2019s tasks and logs.'}</div>}</>), brush, s.task ? { from: processOf(s.task) } : null)}>
            <rect className="sr-row-hit" x={0} y={i * rowH} width={W} height={rowH} />
            <text className={`lbl${s.status === 'failed' ? ' failed-label' : ''}`} x={L - 8} y={i * rowH + 16} textAnchor="end">{s.phase.length > cut ? `${s.phase.slice(0, cut - 1)}…` : s.phase}</text>
            <rect x={x(s.start)} y={i * rowH + 5} width={Math.max(2, x(s.end) - x(s.start))} height={rowH - 10} rx={2} style={{ fill: s.tail ? 'var(--other)' : phaseFill(s, color), stroke: s.status === 'running' ? color(s.phase) : undefined }} className={s.status === 'failed' ? 'failed' : undefined} />
            <text className="dur" x={x(s.end) + 5} y={i * rowH + 16}>{s.status === 'failed' ? `${phaseOutcome(s)} · ` : s.status === 'running' ? 'running · ' : ''}{fmtDur(s.end - s.start)}</text>
          </g>
        ))}
      </svg>
      {tip.el}
    </div>
  )
}

/** A DAG node's second line: its task count, duration and state. */
function nodeSub(n: DagNode, now: number): string {
  if (n.status === 'pending' || n.status === 'unrecorded') return n.status === 'pending' ? 'waiting' : 'no phases recorded'
  const k = n.tasks.length > 1 ? `${n.tasks.length} tasks · ` : ''
  return `${k}${n.start != null ? fmtDur((n.end ?? now) - n.start) : ''}${n.status === 'running' ? ' · running' : n.status === 'failed' ? ' · failed' : ''}`
}

/** The run's pipeline DAG: its processes in dependency columns, each coloured by its tasks' phases. Hovering a step
 * or an arrow brushes the phase timeline; clicking a step opens its drawer (tasks, machines, logs). */
function RunDag({ nodes, edges, brush }: { nodes: DagNode[]; edges: [string, string][]; brush?: Brush }) {
  const tip = useSvgTip()
  if (!nodes.length) return null
  const w = 168, h = 46, gx = 44, gy = 14, pad = 4
  const cols = Math.max(...nodes.map(n => n.layer)) + 1, rows = Math.max(...nodes.map(n => n.row)) + 1
  const W = cols * w + (cols - 1) * gx + 2 * pad, H = rows * h + (rows - 1) * gy + 2 * pad
  const at = new Map(nodes.map(n => [n.id, { x: pad + n.layer * (w + gx), y: pad + n.row * (h + gy) }]))
  const now = Math.floor(Date.now() / 1000)
  const label = new Map(nodes.map(n => [n.id, n.label]))
  const hl = brush?.hl ?? null
  const edgeClass = (f: string, t: string) =>
    !hl ? '' : (hl.to ? hl.from === f && hl.to === t : f === hl.from || t === hl.from) ? ' brushed' : ' dimmed'
  const tipOf = (n: DagNode) => <>
    <div><b>{n.label}</b></div>
    <div>{nodeSub(n, now)}{n.tasks.length === 1 && n.tasks[0].machine ? ` · ${n.tasks[0].machine}` : ''}</div>
    {n.tasks.length
      ? <div className="dim">{brush?.pinned === n.id ? 'Click to close its tasks.' : 'Click for its tasks, machines and logs.'}</div>
      : <div className="dim">{n.status === 'pending' ? 'Not started yet.' : 'This process recorded no phases (a run before phases named their task, or it ran nothing).'}</div>}
  </>
  return (
    <div className="sr-svgwrap sr-dagwrap"><div className="runs-wrap">
      <svg className="sr-dag" width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Pipeline DAG">
        <defs><marker id="sr-dag-arrow" viewBox="0 0 8 8" refX="8" refY="4" markerWidth="7" markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" className="arrow" /></marker></defs>
        {edges.map(([f, t]) => {
          const a = at.get(f), b = at.get(t)
          if (!a || !b) return null
          const x1 = a.x + w, y1 = a.y + h / 2, x2 = b.x, y2 = b.y + h / 2, mx = (x1 + x2) / 2
          const d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`
          return <g key={`${f}>${t}`} className={`edge-g${edgeClass(f, t)}`} {...brushed(tip.on(<><b>{label.get(f)}</b> → <b>{label.get(t)}</b></>), brush, { from: f, to: t })}>
            <path className="edge-hit" d={d} />
            <path className="edge" d={d} markerEnd="url(#sr-dag-arrow)" />
          </g>
        })}
        {nodes.map(n => {
          const { x, y } = at.get(n.id)!
          return <g key={n.id} className={`node ${n.status}${brushClass(brush, n.id)}${brush?.pinned === n.id ? ' pinned' : ''}`} {...brushed(tip.on(tipOf(n)), brush, n.tasks.length ? { from: n.id } : null)}>
            <rect x={x} y={y} width={w} height={h} rx={6} />
            <text className="name" x={x + 10} y={y + 19}>{n.label}</text>
            <text className="sub" x={x + 10} y={y + 36}>{nodeSub(n, now)}</text>
          </g>
        })}
      </svg>
    </div>{tip.el}</div>
  )
}

/** The pinned step's drawer: each of its tasks (a fan-out's member by its tag), the machine it ran on, its phases,
 * timing and logs. */
function StepDrawer({ node, run, project, onClose }: { node: DagNode; run: ScanRun; project: string | null; onClose: () => void }) {
  const t0 = run.started_ts
  const now = Math.floor(Date.now() / 1000)
  return (
    <div className="sr-drawer">
      <div className="sr-drawer-head">
        <b><code>{node.label}</code></b><span className="dim">{nodeSub(node, now)}</span>
        <button type="button" className="sr-drawer-close" onClick={onClose} aria-label="Close the step's tasks">close</button>
      </div>
      <div className="runs-wrap">
        <table className="runs sr-tasks">
          <thead><tr>
            {node.tasks.some(t => t.tag) && <th>task</th>}<th>phases</th><th>machine</th><th className="num">start</th><th className="num">duration</th><th>logs</th>
          </tr></thead>
          <tbody>{node.tasks.map(t => {
            const end = t.end ?? (t.status === 'running' ? now : null)
            return (
              <tr key={t.task}>
                {node.tasks.some(t => t.tag) && <td className="nb"><code>{t.tag}</code></td>}
                <td className={t.status === 'failed' ? 'failed-label' : undefined}>{t.phases.join(', ')}{t.status === 'running' && <span className="dim"> · running</span>}</td>
                <td className="nb">{t.machine ?? <span className="dim">—</span>}</td>
                <td className="num nb">{t.start != null && t0 != null ? `+${fmtDur(t.start - t0)}` : '—'}</td>
                <td className="num nb">{t.start != null && end != null ? fmtDur(end - t.start) : '—'}</td>
                <td className="nb">{project && t.job_name ? <a href={taskLogsHref(project, t.job_name, t.start)} target="_blank" rel="noreferrer">logs ↗</a> : <span className="dim">—</span>}</td>
              </tr>
            )
          })}</tbody>
        </table>
      </div>
    </div>
  )
}

/** A Nextflow run's step tasks: each its own Batch job, with its machine, phases and logs. */
function TasksTable({ tasks, run, project }: { tasks: RunTask[]; run: ScanRun; project: string | null }) {
  const t0 = run.started_ts
  return (
    <div className="runs-wrap">
      <table className="runs sr-tasks">
        <thead><tr>
          <th>phases</th><th>machine</th><th className="num">start</th><th className="num">duration</th>
          <th><Tooltip content="Nextflow deletes a task's Batch job when it finishes (its logs stay), so the job links only while it runs.">Batch job</Tooltip></th><th>logs</th>
        </tr></thead>
        <tbody>{tasks.map(t => {
          const live = t.status === 'running' && project && run.region ? batchHref(project, run.region, t.job_name) : null
          const end = t.end ?? (t.status === 'running' ? Math.floor(Date.now() / 1000) : null)
          return (
            <tr key={t.job_name}>
              <td className={t.status === 'failed' ? 'failed-label' : undefined}>{t.phases.join(', ')}{t.status === 'running' && <span className="dim"> · running</span>}</td>
              <td className="nb">{t.machine ?? <span className="dim">—</span>}</td>
              <td className="num nb">{t.start != null && t0 != null ? `+${fmtDur(t.start - t0)}` : '—'}</td>
              <td className="num nb">{t.start != null && end != null ? fmtDur(end - t.start) : '—'}</td>
              <td className="nb">{live ? <a href={live} target="_blank" rel="noreferrer"><code>{t.job_name}</code></a> : <code className="dim">{t.job_name}</code>}</td>
              <td className="nb">{project ? <a href={taskLogsHref(project, t.job_name, t.start)} target="_blank" rel="noreferrer">logs ↗</a> : <span className="dim">—</span>}</td>
            </tr>
          )
        })}</tbody>
      </table>
    </div>
  )
}

function OutputsTable({ outputs, meta }: { outputs: OutputDelta[]; meta: string | null }) {
  const { bytes, delta } = useBytes()
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set())
  if (!outputs.length) return <p className="dim">No outputs recorded for this run.</p>
  const tops = outputs.filter(o => !o.key.includes('/'))
  const kids = (k: string) => outputs.filter(o => o.key.startsWith(`${k}/`))
  const row = (o: OutputDelta, child: boolean) => {
    const href = metaHref(meta, o.uri)
    const n = child ? 0 : kids(o.key).length
    const isOpen = open.has(o.key)
    return (
      <tr key={o.key} className={child ? 'child' : undefined}>
        <td className="nb">
          {n > 0 ? <button type="button" className="fold" aria-expanded={isOpen} onClick={() => setOpen(s => { const t = new Set(s); if (isOpen) t.delete(o.key); else t.add(o.key); return t })}>{isOpen ? '▾' : '▸'}</button> : <span className="fold-pad" />}
          <Tooltip content={<code>{o.uri}</code>}><span>{child ? o.key.split('/').slice(1).join('/') : o.key}</span></Tooltip>
          {n > 0 && <span className="dim"> ({n})</span>}
        </td>
        <td className="num nb">{bytes(o.bytes)}</td>
        <td className={`num nb ${deltaClass(o.delta_bytes)}`}>{delta(o.delta_bytes)}</td>
        <td className="num nb">{o.objects == null ? '—' : fmtN(o.objects)}</td>
        <td className="num nb">{o.rows == null ? <span className="dim">—</span> : fmtN(o.rows)}</td>
        <td className={`num nb ${deltaClass(o.delta_rows)}`}>{o.delta_rows == null ? '—' : `${o.delta_rows > 0 ? '+' : o.delta_rows < 0 ? '−' : '±'}${fmtN(Math.abs(o.delta_rows))}`}</td>
        <td className="nb hide-md">{o.gen ? <code className="dim">{o.gen}</code> : ''}</td>
        <td className="nb">{href ? <a href={href} target="_blank" rel="noreferrer">meta ↗</a> : <span className="dim">—</span>}</td>
      </tr>
    )
  }
  return (
    <div className="runs-wrap">
      <table className="runs sr-outputs">
        <thead><tr>
          <th>structure</th><th className="num">bytes</th>
          <th className="num"><Tooltip content={`Against the previous scan${tops[0]?.prev_scan ? ` (${tops[0].prev_scan})` : ''}: the same structure's bytes.`}>Δ prev</Tooltip></th>
          <th className="num">objects</th><th className="num">rows</th><th className="num">Δ rows</th><th className="hide-md">gen</th>
          <th><Tooltip content="The output's directory in the /meta tree (the deployments' own data bucket, scanned periodically: a just-written output appears after its next meta scan).">tree</Tooltip></th>
        </tr></thead>
        <tbody>{tops.flatMap(o => [row(o, false), ...(open.has(o.key) ? kids(o.key).map(k => row(k, true)) : [])])}</tbody>
      </table>
    </div>
  )
}

function RunList({ runs, links, title, scanHref }: { runs: ScanRun[]; links: Links; title: string; scanHref: (id: string) => string }) {
  if (!runs.length) return null
  return <>
    <h3>{title}</h3>
    <div className="runs-wrap">
      <table className="runs">
        <thead><tr><th>run</th><th>kind</th><th>Batch status</th><th>started</th><th className="num">duration</th><th>machine</th><th className="num">cost</th><th>links</th></tr></thead>
        <tbody>{runs.map(r => (
          <tr key={r.run_id} className={r.status === 'failed' ? 'failed' : undefined}>
            <td className="nb">{r.parent && r.kind !== 'phase-recovery' ? <code>{r.job_name ?? r.run_id}</code> : <Link to={scanHref(r.run_id)}><code>{r.job_name ?? r.run_id}</code></Link>}</td>
            <td>{r.kind}</td><td><Status cell run={r} /></td><td className="nb">{r.started_ts == null ? '—' : <RunWhen ts={r.started_ts} scan={r.job_name ?? r.run_id} />}</td>
            <td className="num nb">{r.started_ts != null && r.finished_ts != null ? fmtDur(r.finished_ts - r.started_ts) : '—'}</td>
            <td className="nb">{r.machine ?? '—'}{r.tasks && r.tasks > 1 ? ` × ${r.tasks}` : ''}{r.spot ? ' (spot)' : ''}</td>
            <td className="num nb">{r.cost_usd == null ? '—' : usd(r.cost_usd)}</td>
            <td className="nb"><Links run={r} links={links} /></td>
          </tr>
        ))}</tbody>
      </table>
    </div>
  </>
}

export function ScanRunPage() {
  const id = useParams()['*'] ?? ''
  const { store, scanHref } = useScansStore()
  const q = useScanRun(id, store)
  const list = useScanRuns(store)
  const slug = useMinSlug(store)
  useDocTitle(q.data ? q.data.run.scan : 'Scan', 'Scans')
  const order = useMemo(() => phaseOrder((list.data?.runs ?? []).flatMap(r => r.spans.map((s, i) => ({ phase: s.phase, seq: i })))), [list.data])
  const color = colorer(order.length ? order : (q.data?.spans ?? []).map(s => s.phase))
  const d = q.data
  const downstream = d?.downstream.filter(r => r.kind !== 'phase-recovery') ?? []
  const scanRun = (scan: string | null) => scan ? list.data?.runs.find(s => s.run.scan === scan && s.run.kind === 'scan' && s.run.status === 'succeeded') ?? list.data?.runs.find(s => s.run.scan === scan) : undefined
  const prev = scanRun(d?.prev_scan ?? null), next = scanRun(d?.next_scan ?? null)
  const r = d?.run
  const kinds = new Set(list.data?.runs.map(s => s.run.kind))
  const map = r && r.status === 'succeeded' ? mapHref(r.scan, store.path, slug) : null
  const tasks = useMemo(() => runTasks(d?.phases ?? []), [d])
  const brush = useBrush()
  const nodes = useMemo(() => (d?.dag ? dagNodes(d.dag.dag, d.phases, d.run.status === 'running') : []), [d])
  const pinnedNode = nodes.find(n => n.id === brush.pinned)
  return (
    <main className="staged-page scans-page">
      <SiteNav />
      <p className="sr-crumbs"><Link to={scanHref()}>Scan runs</Link>{prev && <> · <Link to={scanHref(prev.run.run_id)}>‹ {prev.run.scan}</Link></>}{next && <> · <Link to={scanHref(next.run.run_id)}>{next.run.scan} ›</Link></>}</p>
      {q.isLoading ? <p>Loading run…</p> : q.error ? <p className="err">{q.error.message}</p> : d && r && <>
        <h1><code>{r.scan}</code>{kinds.size > 1 && <> <span className="dim">{r.kind}</span></>}</h1>
        <p className="sub">
          <Status run={r} failedAt={d.failed_at} issues={d.phase_issues} /> · {fmtRunTime(r.started_ts)} → {r.finished_ts ? fmtRunTime(r.finished_ts) : '…'}
          {d.secs != null && <> · {fmtDur(d.secs)}</>}
          {tasks.length
            ? <> · <Tooltip content="A Nextflow run: a head job on this machine runs the pipeline, and each step runs as its own Batch job on a machine sized for it (Tasks below)."><span>Nextflow · {tasks.length} tasks{r.machine && <span className="dim"> + {r.machine} head</span>}</span></Tooltip></>
            : r.machine && <> · {r.machine}{r.spot ? ' (spot)' : ''}</>}
          {r.cost_usd != null && <> · ~{usd(r.cost_usd)}</>}
          {map && <> · <Link to={map}>map at this scan</Link></>}
          {' · '}<Links run={r} links={d} />
        </p>
        {r.status === 'failed' && <div className="sr-failure"><b>Failed {d.failed_at ? (d.failed_at.startsWith('after') ? d.failed_at : `in ${d.failed_at}`) : ''}</b>{r.error && <div><code>{r.error}</code></div>}</div>}
        {d.phase_issues?.map(i => <div className={`sr-failure${i.recovery ? ' recovered' : ''}`} key={i.phase}>
          <b>{i.phase}: {i.outcome} in the original attempt.</b>{' '}
          {i.recovery ? <>Recovered by <Link to={scanHref(i.recovery.run_id)}>a subsequent attempt</Link> at {fmtRunTime(i.recovery.finished_ts)}.</> : 'The Batch job completed, but this phase did not. No successful recovery is recorded.'}
        </div>)}
        {r.kind === 'phase-recovery' && r.parent && <p className="sub">Phase recovery for <Link to={scanHref(r.parent)}>the original scan run</Link>. This attempt has its own clock; it does not replace the original timings.</p>}
        <dl className="sr-facts">
          <div><dt>run id</dt><dd>{d.project ? <a href={logsHref(d.project, r)} target="_blank" rel="noreferrer"><code>{r.run_id}</code></a> : <code>{r.run_id}</code>}</dd></div>
          {r.job_name && <div><dt>{tasks.length ? 'head Batch job' : 'Batch job'}</dt><dd>{d.project && r.region ? <a href={batchHref(d.project, r.region, r.job_name)} target="_blank" rel="noreferrer"><code>{r.job_name}</code></a> : <code>{r.job_name}</code>}{r.region && <span className="dim"> · {r.region}</span>}</dd></div>}
          {r.gen && <div><dt>index generation</dt><dd><code>{r.gen}</code></dd></div>}
          {r.image && <div><dt>image</dt><dd><code>{r.image}</code></dd></div>}
          {r.trigger && <div><dt>started by</dt><dd>{r.trigger === 'schedule' ? 'the schedule (cron)' : 'hand (a submit or a resume)'}</dd></div>}
          <div><dt>recorded by</dt><dd>{r.source === 'job' ? 'the job' : 'backfill (Batch, logs, the bucket)'}</dd></div>
        </dl>
        {d.dag && <>
          <h3>Pipeline</h3>
          <RunDag nodes={nodes} edges={d.dag.dag.edges} brush={brush} />
          {pinnedNode && <StepDrawer node={pinnedNode} run={r} project={d.project} onClose={() => brush.toggle(pinnedNode.id)} />}
        </>}
        <h3>Phases</h3>
        {!!d.phase_issues?.length && <p className="dim">Original attempt: failed bars include time spent waiting until failure or timeout, not successful build time. Later recoveries are separate below.</p>}
        <PhaseTimeline spans={d.spans} secs={d.secs} color={color} brush={d.dag ? brush : undefined} />
        {!!tasks.length && <>
          <h3>Tasks</h3>
          <TasksTable tasks={tasks} run={r} project={d.project} />
        </>}
        {!!d.recoveries?.length && <>
          <h3>Phase recovery attempts</h3>
          <p className="dim">Each attempt is linked to its original phase. Durations exclude the gap between attempts and are not added to the original job duration.</p>
          {d.recoveries.map(a => <div className="sr-recovery" key={a.run.run_id}>
            <div><b>{a.phases.map(p => p.phase).join(', ')}</b>{' · '}<Status run={a.run} />{' · '}{a.secs == null ? '—' : fmtDur(a.secs)}{' · '}{fmtRunTime(a.run.started_ts)} → {fmtRunTime(a.run.finished_ts)}{' · '}<Link to={scanHref(a.run.run_id)}>attempt details</Link>{' · '}<Links run={a.run} links={d} /></div>
            {a.phases.map(p => p.note && <p className="dim" key={p.phase}>{p.note}</p>)}
          </div>)}
        </>}
        <h3>Outputs</h3>
        <OutputsTable outputs={d.outputs} meta={d.meta} />
        <RunList runs={downstream} links={d} title={`Downstream jobs (${downstream.length})`} scanHref={scanHref} />
        <RunList runs={d.siblings} links={d} title="This scan's other runs" scanHref={scanHref} />
      </>}
      <SiteKbd />
    </main>
  )
}
