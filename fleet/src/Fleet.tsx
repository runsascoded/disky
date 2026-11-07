import { useQuery } from '@tanstack/react-query'
import { fmtSI } from '@disk-tree/pages-kit/og/text'
import { fmtHours, fmtUsd } from '../functions/_lib/card'
import type { FleetDeployment, FleetResponse, FleetRun } from '../functions/_lib/fleetTypes'
import { useWhoami } from './App'

class Unauthenticated extends Error {}

const when = (t: number | null) => (t === null ? '—' : new Date(t * 1000).toISOString().slice(5, 16).replace('T', ' '))
const age = (s: number | null) => (s === null ? '—' : s < 3600 ? `${Math.round(s / 60)}m` : s < 172800 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`)
const bytes = (b: number | null | undefined) => (b == null ? '—' : fmtSI(b))
const delta = (now: number | null | undefined, then: number | null | undefined) =>
  now == null || then == null ? '' : ` (${now >= then ? '+' : '−'}${fmtSI(Math.abs(now - then))})`

function runState(r: FleetRun): { label: string; cls: string } {
  if (r.status === 'running') return { label: 'running', cls: 'running' }
  if (r.status === 'failed') return { label: r.failed_at ? `failed at ${r.failed_at}` : 'failed', cls: 'bad' }
  if (r.open_issues.length) return { label: `open: ${r.open_issues.join(', ')}`, cls: 'warn' }
  return { label: r.status === 'succeeded' ? 'ok' : r.status, cls: 'ok' }
}

function Deployment({ d }: { d: FleetDeployment }) {
  const errors = Object.entries(d.errors)
  return (
    <section className="deployment" style={{ '--c': d.color } as React.CSSProperties}>
      <h2><span className="dot" /> <a href={d.url}>{d.name}</a> <span className="host">{new URL(d.url).host}</span></h2>
      <dl>
        <dt>newest scan</dt><dd>{d.health?.newest_scan ?? '—'} <span className="muted">{d.health ? `${age(d.health.newest_scan_age)} old` : ''}</span></dd>
        <dt>coverage gaps</dt><dd>{d.health ? d.health.gaps : '—'}</dd>
        <dt>/meta size</dt><dd>{bytes(d.meta?.now)}<span className="muted">{delta(d.meta?.now, d.meta?.d1)} 1d{delta(d.meta?.now, d.meta?.d7)} 7d</span></dd>
      </dl>
      {errors.length > 0 && <p className="error">{errors.map(([k, v]) => `${k}: ${v}`).join(' · ')}</p>}
      {d.runs.length > 0 && (
        <div className="scroll">
          <table>
            <thead><tr><th>scan</th><th>status</th><th>started (UTC)</th><th className="num">duration</th><th className="num">cost</th></tr></thead>
            <tbody>
              {d.runs.map(r => {
                const s = runState(r)
                return (
                  <tr key={r.run_id}>
                    <td><a href={`${d.url}/scans/${encodeURIComponent(r.run_id)}`}>{r.scan}</a></td>
                    <td className={s.cls}>{s.label}</td>
                    <td>{when(r.started)}</td>
                    <td className="num">{r.secs === null ? '—' : fmtHours(r.secs)}</td>
                    <td className="num">{r.cost_usd === null ? '—' : fmtUsd(r.cost_usd)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function Wall({ signIn }: { signIn: boolean }) {
  return (
    <main>
      <h1>Fleet</h1>
      <p>Each deployment’s newest scans and its <code>/meta</code> store’s size, for staff.</p>
      {signIn ? <p><a className="button" href="/auth/oa?next=%2Ffleet">Sign in with OA</a></p> : <p className="error">Sign-in isn’t configured here.</p>}
    </main>
  )
}

export function Fleet() {
  const who = useWhoami()
  const q = useQuery({
    queryKey: ['fleet'],
    queryFn: async (): Promise<FleetResponse> => {
      const r = await fetch('/api/fleet')
      if (r.status === 401) throw new Unauthenticated()
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return r.json()
    },
    refetchInterval: 5 * 60_000,
  })
  if (q.error instanceof Unauthenticated) return <Wall signIn={who.data?.signIn ?? false} />
  if (q.error) return <main><h1>Fleet</h1><p className="error">{q.error.message}</p></main>
  if (!q.data) return <main><h1>Fleet</h1><p className="muted">Reading each deployment…</p></main>
  const doc = q.data
  return (
    <main className="fleet">
      <h1>Fleet <span className="muted">read {when(doc.now)} UTC</span></h1>
      {doc.card && (
        <picture className="card">
          <source media="(prefers-color-scheme: light)" srcSet={doc.card.light} />
          <img src={doc.card.dark} alt="The fleet card: /meta store size, scan duration and cost per scan, per deployment, over the last week" />
        </picture>
      )}
      {doc.deployments.map(d => <Deployment key={d.name} d={d} />)}
    </main>
  )
}
