import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import type { LatencySummary, Stat } from '../functions/_lib/sessionLatencyShape'
import { SiteNav } from './SiteNav'
import { useDocTitle } from './title'

// /admin/latency — request latency from the session log (`/api/session-log/latency`): what viewers' tabs measured for
// each request (route, status, duration, the response's cache tier), per route and tier, per user, and the slowest.
// Admin-only (the API is `requireAdmin`). `?days=`, `?email=`, `?sid=` narrow it.

const WINDOWS: [number, string][] = [[1 / 24, 'last hour'], [1, 'last day'], [7, 'last 7 days'], [30, 'last 30 days']]

async function getJson<T>(url: string): Promise<T> {
  const r = await fetch(url, { credentials: 'same-origin' })
  const j = await r.json().catch(() => ({}))
  if (!r.ok) throw new Error((j as { error?: string }).error ?? `HTTP ${r.status}`)
  return j as T
}

const fmtMs = (ms: number): string => (ms >= 10_000 ? `${(ms / 1000).toFixed(0)} s` : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`)
const fmtTime = (ms: number): string => new Date(ms).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit' })

function StatCells({ s }: { s: Stat }) {
  return (
    <>
      <td className="num">{s.n.toLocaleString()}</td>
      <td className="num">{fmtMs(s.p50)}</td>
      <td className="num">{fmtMs(s.p90)}</td>
      <td className="num">{fmtMs(s.p99)}</td>
      <td className="num">{fmtMs(s.max)}</td>
      <td className="num">{s.errors ? <b className="err">{s.errors}</b> : <span className="dim">0</span>}</td>
    </>
  )
}
const STAT_HEAD = <><th className="num">requests</th><th className="num">p50</th><th className="num">p90</th><th className="num">p99</th><th className="num">max</th><th className="num">errors</th></>

export function LatencyPage() {
  useDocTitle('Latency', 'Admin')
  const [params, setParams] = useSearchParams()
  const days = Number(params.get('days')) || 7
  const email = params.get('email') ?? ''
  const sid = params.get('sid') ?? ''
  const qs = new URLSearchParams({ days: String(days), ...(email ? { email } : {}), ...(sid ? { sid } : {}) })
  const q = useQuery({
    queryKey: ['latency', days, email, sid],
    queryFn: () => getJson<LatencySummary>(`/api/session-log/latency?${qs}`),
    placeholderData: keepPreviousData,
  })
  const set = (k: string, v: string) => setParams(p => { const n = new URLSearchParams(p); if (v) n.set(k, v); else n.delete(k); return n }, { replace: true })
  const d = q.data
  return (
    <div className="admin-page latency-page">
      <SiteNav />
      <p className="crumbs"><Link to="/admin">admin</Link> · <Link to="/admin/sessions">sessions</Link></p>
      <h1>Latency</h1>
      <p className="dim">
        What signed-in viewers' tabs measured for each request while the session log was on: time to the response's headers
        (the body's download and decode are not in it), per route and the response's cache tier (<code>x-cache</code>: <code>hit</code> /
        {' '}<code>kv</code> at the edge, <code>miss</code> computed, <code>-</code> none reported).
      </p>
      <div className="sessions-filters">
        <label>window <select value={days} onChange={e => set('days', e.target.value)}>
          {WINDOWS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
        </select></label>
        {email && <span>user <b>{email}</b> <button type="button" onClick={() => set('email', '')}>all users</button></span>}
        {sid && <span>session <Link to={`/admin/sessions/${sid}`}>{sid.slice(0, 8)}</Link> <button type="button" onClick={() => set('sid', '')}>all sessions</button></span>}
      </div>
      {q.error && <p className="err">{q.error.message}</p>}
      {d && (
        <>
          <p className="dim">
            {d.requests.toLocaleString()} requests in {d.batches.toLocaleString()} batches
            {d.capped && <> · <b>the oldest batches are left out</b> (newest {d.batches.toLocaleString()} read)</>}
          </p>
          <h2>By route and cache tier</h2>
          <div className="table-scroll">
            <table className="sessions">
              <thead><tr><th>route</th><th>cache</th>{STAT_HEAD}</tr></thead>
              <tbody>{d.routes.map(r => <tr key={`${r.route}|${r.cache}`}><td><code>{r.route}</code></td><td>{r.cache}</td><StatCells s={r} /></tr>)}</tbody>
            </table>
          </div>
          <h2>By user</h2>
          <div className="table-scroll">
            <table className="sessions">
              <thead><tr><th>user</th>{STAT_HEAD}</tr></thead>
              <tbody>{d.users.map(u => <tr key={u.email}><td><a href={`?${new URLSearchParams({ ...Object.fromEntries(params), email: u.email })}`} onClick={e => { e.preventDefault(); set('email', u.email) }}>{u.email}</a></td><StatCells s={u} /></tr>)}</tbody>
            </table>
          </div>
          <h2>Slowest requests</h2>
          <div className="table-scroll">
            <table className="sessions">
              <thead><tr><th>when</th><th>user</th><th>request</th><th className="num">status</th><th>cache</th><th className="num">time</th></tr></thead>
              <tbody>{d.slowest.map((r, i) => (
                <tr key={i}>
                  <td><Link to={`/admin/sessions/${r.sid}`}>{fmtTime(r.t)}</Link></td>
                  <td>{r.email ?? <span className="dim">signed out</span>}</td>
                  <td><code>{r.u}</code></td>
                  <td className="num">{r.s === 0 || r.s >= 400 ? <b className="err">{r.s || 'failed'}</b> : r.s}</td>
                  <td>{r.c}</td>
                  <td className="num">{fmtMs(r.ms)}</td>
                </tr>
              ))}</tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}
