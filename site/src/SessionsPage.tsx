import { autoUpdate, flip, FloatingPortal, offset, shift, useFloating } from '@floating-ui/react'
import { useQuery } from '@tanstack/react-query'
import { useMemo, useRef, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import type { SessionRow, StoredEvent } from '../functions/_lib/sessionLogShape'
import { fmtDuration, fmtOffset, LANES, type Line, ticks, timeline, uaLabel } from './sessionsModel'
import { SiteNav } from './SiteNav'
import { Tooltip } from './Tooltip'
import { useDocTitle } from './title'

// /admin/sessions[/:sid] — the session log's viewer (specs/session-log.md),
// admin-only: the APIs are `requireAdmin`; this page renders their 401/403.

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url, { credentials: 'same-origin' })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error((j as { error?: string }).error ?? `HTTP ${r.status}`)
  return j as T
}

const fmtTime = (ms: number): string => new Date(ms).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit' })

export function SessionsPage() {
  const { sid } = useParams()
  useDocTitle(sid ? `session ${sid.slice(0, 8)}` : 'Sessions', 'Admin')
  return (
    <div className="admin-page sessions-page">
      <SiteNav />
      <p className="crumbs">
        <Link to="/admin">admin</Link>
        {sid && <> · <Link to="/admin/sessions">sessions</Link></>}
      </p>
      {sid ? <SessionView sid={sid} /> : <SessionList />}
    </div>
  )
}

function SessionList() {
  const [sp, setSp] = useSearchParams()
  const email = sp.get('u') ?? ''
  const since = sp.get('since') ?? ''
  const errors = sp.get('err') === '1'
  const set = (k: string, v: string | null) => setSp(p => { const n = new URLSearchParams(p); if (v) n.set(k, v); else n.delete(k); return n }, { replace: true })
  const qs = new URLSearchParams({ ...(email ? { email } : {}), ...(since ? { since } : {}), ...(errors ? { errors: '1' } : {}) }).toString()
  const q = useQuery({
    queryKey: ['session-log', 'sessions', qs],
    queryFn: () => getJson<{ sessions: SessionRow[] }>(`/api/session-log/sessions${qs ? `?${qs}` : ''}`),
  })
  return (
    <>
      <h1>Sessions</h1>
      <p className="dim">What signed-in viewers did while the session log was on: newest first. Open one for its timeline.</p>
      <div className="sessions-filters">
        <label>user <input value={email} onChange={e => set('u', e.target.value || null)} placeholder="email contains…" size={22} /></label>
        <label>active since <input type="date" value={since} onChange={e => set('since', e.target.value || null)} /></label>
        <label><input type="checkbox" checked={errors} onChange={e => set('err', e.target.checked ? '1' : null)} /> with errors</label>
      </div>
      {q.error && <p className="err">{q.error.message}</p>}
      <div className="table-scroll">
        <table className="sessions">
          <thead>
            <tr><th>user</th><th>started</th><th className="num">duration</th><th className="num">events</th><th className="num">errors</th><th>browser</th><th>build</th></tr>
          </thead>
          <tbody>
            {(q.data?.sessions ?? []).map(s => (
              <tr key={s.sid} className={s.n_errors ? 'has-err' : ''}>
                <td><Link to={`/admin/sessions/${s.sid}`}>{s.email ?? <em>anonymous</em>}</Link>{s.via && s.via !== 'session' && <span className="dim"> · {s.via}</span>}</td>
                <td>{fmtTime(s.started_ms)}</td>
                <td className="num">{fmtDuration(s.last_ms - s.started_ms)}</td>
                <td className="num">{s.n_events.toLocaleString()}</td>
                <td className="num">{s.n_errors ? <b className="err">{s.n_errors}</b> : <span className="dim">0</span>}</td>
                <td>{uaLabel(s.ua)}</td>
                <td><code>{s.build ?? '—'}</code></td>
              </tr>
            ))}
            {q.data && !q.data.sessions.length && <tr><td colSpan={7}><em>no sessions match</em></td></tr>}
            {q.isPending && <tr><td colSpan={7}><em>loading…</em></td></tr>}
          </tbody>
        </table>
      </div>
    </>
  )
}

const KIND_FILTERS = [
  { id: 'all', label: 'all' },
  { id: 'errors', label: 'errors' },
  { id: 'actions', label: 'actions' },
  { id: 'api', label: 'requests' },
] as const
type KindFilter = typeof KIND_FILTERS[number]['id']
const shown = (l: Line, f: KindFilter): boolean =>
  f === 'all' || (f === 'errors' ? l.error : f === 'actions' ? l.lane === 'input' || l.lane === 'nav' : l.k === 'api')

function SessionView({ sid }: { sid: string }) {
  const q = useQuery({
    queryKey: ['session-log', 'session', sid],
    queryFn: () => getJson<{ session: SessionRow; events: StoredEvent[] }>(`/api/session-log/sessions/${encodeURIComponent(sid)}`),
  })
  const lines = useMemo(() => timeline(q.data?.events ?? []), [q.data])
  const [filter, setFilter] = useState<KindFilter>('all')
  const [focus, setFocus] = useState<number | null>(null)
  const rows = useRef(new Map<number, HTMLTableRowElement>())
  if (q.error) return <p className="err">{q.error.message}</p>
  if (!q.data) return <p className="dim">loading…</p>
  const s = q.data.session
  const jump = (i: number) => {
    setFilter('all')
    setFocus(i)
    requestAnimationFrame(() => rows.current.get(i)?.scrollIntoView({ block: 'center', behavior: 'smooth' }))
  }
  return (
    <>
      <h1>{s.email ?? 'anonymous'}</h1>
      <p className="session-meta">
        {fmtTime(s.started_ms)} · {fmtDuration(s.last_ms - s.started_ms)} · {s.n_events.toLocaleString()} events ·{' '}
        {s.n_errors ? <b className="err">{s.n_errors} errors</b> : '0 errors'} · {uaLabel(s.ua)} · build <code>{s.build ?? '—'}</code> · <code className="dim">{s.sid}</code>
      </p>
      <Strip lines={lines} onPick={jump} />
      <div className="sessions-filters" role="radiogroup" aria-label="Show">
        {KIND_FILTERS.map(f => (
          <label key={f.id}><input type="radio" name="kind" checked={filter === f.id} onChange={() => setFilter(f.id)} /> {f.label}</label>
        ))}
      </div>
      <div className="table-scroll">
        <table className="sessions timeline">
          <thead><tr><th className="num">at</th><th>kind</th><th>event</th><th></th></tr></thead>
          <tbody>
            {lines.filter(l => shown(l, filter)).map(l => (
              <tr key={l.i} ref={el => { if (el) rows.current.set(l.i, el); else rows.current.delete(l.i) }}
                className={`${l.error ? 'has-err' : ''}${focus === l.i ? ' focused' : ''}${l.k === 'start' ? ' load' : ''}`}>
                <td className="num"><code>{fmtOffset(l.off)}</code></td>
                <td><span className={`lane-dot lane-${l.lane}`} aria-hidden /> {l.k}</td>
                <td className="ev">{l.text}</td>
                <td>{l.url && <Tooltip content={<>The view as it was at this moment: <code>{l.url}</code></>}><a href={l.url} target="_blank" rel="noreferrer">open view ↗</a></Tooltip>}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

const STRIP_W = 1000
const LANE_H = 14

/** The session on a time axis: one lane per event family, errors red; hover names the event, click jumps to it. */
function Strip({ lines, onPick }: { lines: Line[]; onPick: (i: number) => void }) {
  const tks = useMemo(() => ticks(lines, STRIP_W), [lines])
  const [hover, setHover] = useState<number | null>(null)
  const { refs, floatingStyles } = useFloating({
    open: hover !== null, placement: 'top', middleware: [offset(8), flip(), shift({ padding: 8 })], whileElementsMounted: autoUpdate,
  })
  const svg = useRef<SVGSVGElement>(null)
  if (!lines.length) return null
  const nearest = (clientX: number): number | null => {
    const r = svg.current?.getBoundingClientRect()
    if (!r || !r.width) return null
    const x = ((clientX - r.left) / r.width) * STRIP_W
    let best: number | null = null, d = Infinity
    for (const t of tks) { const dd = Math.abs(t.x - x); if (dd < d) { d = dd; best = t.i } }
    return d <= 8 ? best : null
  }
  const h = LANES.length * LANE_H
  const span = lines[lines.length - 1].off
  const hovered = hover !== null ? lines[hover] : null
  return (
    <figure className="session-strip">
      <svg ref={svg} viewBox={`0 0 ${STRIP_W} ${h}`} preserveAspectRatio="none" role="img" aria-label="Events over the session"
        onMouseMove={e => {
          const i = nearest(e.clientX)
          if (i === null) { setHover(null); return }
          setHover(i)
          refs.setPositionReference({ getBoundingClientRect: () => ({ x: e.clientX, y: e.clientY, left: e.clientX, top: e.clientY, right: e.clientX, bottom: e.clientY, width: 0, height: 0 }) })
        }}
        onMouseLeave={() => setHover(null)}
        onClick={e => { const i = nearest(e.clientX); if (i !== null) onPick(i) }}>
        {LANES.map((lane, li) => <rect key={lane} x={0} y={li * LANE_H} width={STRIP_W} height={LANE_H} className={`lane-bg lane-${lane}`} />)}
        {tks.map(t => (
          <line key={t.i} x1={t.x} x2={t.x} y1={LANES.indexOf(t.lane) * LANE_H + 2} y2={(LANES.indexOf(t.lane) + 1) * LANE_H - 2}
            className={`tick lane-${t.lane}${t.error ? ' err' : ''}${hover === t.i ? ' hot' : ''}`} vectorEffect="non-scaling-stroke" />
        ))}
      </svg>
      <figcaption>
        <span className="lanes">{LANES.map(l => <span key={l}><span className={`lane-dot lane-${l}`} aria-hidden /> {l}</span>)}</span>
        <span className="dim">{fmtOffset(0)} → {fmtOffset(span)}</span>
      </figcaption>
      {hovered && (
        <FloatingPortal>
          <div className="tooltip-content" ref={refs.setFloating} style={floatingStyles}>
            <b>{fmtOffset(hovered.off)}</b> {hovered.k}: {hovered.text}
            <div className="pin-hint">click to jump to it</div>
          </div>
        </FloatingPortal>
      )}
    </figure>
  )
}
