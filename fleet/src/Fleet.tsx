import { useQuery } from '@tanstack/react-query'
import type { FleetDeployment, FleetDoc, FleetResponse, FleetRun } from '../functions/_lib/fleetTypes'
import { runOutcome } from '../functions/_lib/runOutcome'
import { useWhoami } from './App'
import { Charts } from './charts'
import { age, bytes, DAY, Delta, hours, MARK, RunInfo, runHref, runWords, TRIGGER, usd, when } from './runs'
import { Tip } from './Tip'

class Unauthenticated extends Error {}

/** Runs in a deployment's strip: the Slack post's (`slack.ts` `STRIP_RUNS`). */
const STRIP_RUNS = 10
/** A newest scan older than this is called out, as the Slack line does. */
const STALE = 36 * 3600
/** Rows in a deployment's run table; the rest are on its own `/scans`. */
const RUNS_SHOWN = 30

const host = (d: FleetDeployment) => new URL(d.url).host

/** The newest `STRIP_RUNS` runs as marks, oldest first, each linked to its run page with its summary on hover; padded on
 *  the left so every deployment's newest mark lines up. */
function Strip({ d }: { d: FleetDeployment }) {
  const runs = d.runs.slice(0, STRIP_RUNS).reverse()
  return (
    <span className="strip">
      {Array.from({ length: STRIP_RUNS - runs.length }, (_, i) => <span key={`pad${i}`} className="mark pad" aria-hidden>·</span>)}
      {runs.map(r => (
        <Tip key={r.run_id} content={<RunInfo d={d} r={r} />}>
          <a className="mark" href={runHref(d, r)} aria-label={`${r.scan}: ${runWords(r)}`}>{MARK[runOutcome(r)]}</a>
        </Tip>
      ))}
    </span>
  )
}

/** The runs' summed cost over the 7 days to `now`: scheduled and hand-started, and how many runs recorded none. */
function weekCost(runs: FleetRun[], now: number) {
  const week = runs.filter(r => r.started !== null && r.started >= now - 7 * DAY && runOutcome(r) !== 'running')
  const sum = (rs: FleetRun[]) => rs.reduce((s, r) => s + (r.cost_usd ?? 0), 0)
  const priced = week.filter(r => r.cost_usd !== null)
  return {
    total: priced.length ? sum(priced) : null,
    manual: sum(priced.filter(r => r.trigger === 'manual')),
    runs: week.length,
    unpriced: week.length - priced.length,
  }
}

function SummaryRow({ d, now }: { d: FleetDeployment; now: number }) {
  const r = d.last
  const scanAge = d.health?.newest_scan_age ?? (r?.started != null ? now - r.started : null)
  const cost = weekCost(d.runs, now)
  const errors = Object.entries(d.errors)
  const m = d.meta
  return (
    <>
      <tr className="sum">
        <td className="dep">
          <Tip content={<div className="run-info">
            <div><b>{host(d)}</b></div>
            <div>newest scan {d.health?.newest_scan ?? '—'}{scanAge !== null ? `, ${age(scanAge)} old` : ''}</div>
            <div>coverage gaps: {d.health ? d.health.gaps : '—'}</div>
          </div>}>
            <a href={d.url}><i className="dot" style={{ background: d.color }} />{d.name}</a>
          </Tip>
        </td>
        <td className="runs"><Strip d={d} /></td>
        <td className="newest">
          {r ? (
            <>
              {r.trigger && <span className="trigger" role="img" aria-label={TRIGGER[r.trigger].label}>{TRIGGER[r.trigger].emoji}</span>}
              <a href={runHref(d, r)}>{r.scan}</a>
              {' '}<span className={scanAge !== null && scanAge > STALE ? 'stale' : 'muted'}>{age(scanAge)} ago</span>
              {' · '}{runOutcome(r) === 'running' ? <span className="o-running">running</span> : hours(r.secs)}
              {' · '}{usd(r.cost_usd)}
            </>
          ) : <span className="muted">{d.errors.runs ? 'unreachable' : 'no scans'}</span>}
        </td>
        <td className="num cost" data-label="7d">
          <Tip content={<div className="run-info">
            <div>{cost.runs} run{cost.runs === 1 ? '' : 's'} in the last 7 days{cost.unpriced ? `, ${cost.unpriced} with no cost recorded` : ''}</div>
            {cost.total !== null && <div>scheduled {usd(cost.total - cost.manual)} · by hand {usd(cost.manual)}</div>}
          </div>}>
            <span tabIndex={0}>{usd(cost.total)}</span>
          </Tip>
        </td>
        <td className="num meta" data-label="/meta">
          <Tip content={<div className="run-info">
            <div>/meta store {bytes(m?.now)}</div>
            {m ? <div>1d <Delta now={m.now} then={m.d1} /> · 7d <Delta now={m.now} then={m.d7} /></div> : <div className="muted">{d.errors.meta ?? 'n/a'}</div>}
          </div>}>
            <span tabIndex={0}>{bytes(m?.now)} <Delta now={m?.now} then={m?.d7} /></span>
          </Tip>
        </td>
        <td className="links">
          <a href={`${d.url}/scans`}>scans</a>
          <a href={d.url}>map</a>
          {r?.thread_url && <a href={r.thread_url}>thread</a>}
        </td>
      </tr>
      {errors.length > 0 && (
        <tr className="sum-err"><td colSpan={6}>{d.name}: {errors.map(([k, v]) => `${k} ${v}`).join(' · ')}</td></tr>
      )}
    </>
  )
}

function Summary({ doc }: { doc: FleetDoc }) {
  return (
    <table className="summary">
      <thead>
        <tr>
          <th>deployment</th>
          <th>last {STRIP_RUNS} runs</th>
          <th>newest scan</th>
          <th className="num">7d cost</th>
          <th className="num">/meta · 7d</th>
          <th />
        </tr>
      </thead>
      <tbody>{doc.deployments.map(d => <SummaryRow key={d.name} d={d} now={doc.now} />)}</tbody>
    </table>
  )
}

/** A deployment's runs, collapsed: its health and its newest runs, each linked to its run page and its thread. */
function Runs({ d, days }: { d: FleetDeployment; days: number }) {
  const errors = Object.entries(d.errors)
  return (
    <details className="deployment">
      <summary>
        <i className="dot" style={{ background: d.color }} /> <b>{d.name}</b> <span className="host">{host(d)}</span>
        <span className="muted"> {d.runs.length} run{d.runs.length === 1 ? '' : 's'} over {days}d</span>
      </summary>
      <dl>
        <dt>newest scan</dt><dd>{d.health?.newest_scan ?? '—'} <span className="muted">{d.health ? `${age(d.health.newest_scan_age)} old` : ''}</span></dd>
        <dt>coverage gaps</dt><dd>{d.health ? d.health.gaps : '—'}</dd>
        <dt>/meta size</dt><dd>{bytes(d.meta?.now)} <span className="muted">1d</span> <Delta now={d.meta?.now} then={d.meta?.d1} /> <span className="muted">7d</span> <Delta now={d.meta?.now} then={d.meta?.d7} /></dd>
      </dl>
      {errors.length > 0 && <p className="error">{errors.map(([k, v]) => `${k}: ${v}`).join(' · ')}</p>}
      {d.runs.length > 0 && (
        <div className="scroll">
          <table className="runs-table">
            <thead><tr><th>scan</th><th>status</th><th>started (UTC)</th><th className="num">duration</th><th className="num">cost</th><th>thread</th></tr></thead>
            <tbody>
              {d.runs.slice(0, RUNS_SHOWN).map(r => {
                const o = runOutcome(r)
                return (
                  <tr key={r.run_id}>
                    <td>
                      {r.trigger && <span className="trigger" role="img" aria-label={TRIGGER[r.trigger].label}>{TRIGGER[r.trigger].emoji}</span>}
                      <a href={runHref(d, r)}>{r.scan}</a>
                    </td>
                    <td className={`o-${o}`}>{MARK[o]} {runWords(r)}</td>
                    <td>{when(r.started)}</td>
                    <td className="num">{hours(r.secs)}</td>
                    <td className="num">{usd(r.cost_usd)}</td>
                    <td>{r.thread_url ? <a href={r.thread_url}>thread</a> : <span className="muted">—</span>}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
      <p className="more"><a href={`${d.url}/scans`}>All scans on {host(d)} →</a></p>
    </details>
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
      <h1>
        Fleet <span className="muted">read {when(doc.now)} UTC</span>
        {doc.card && <a className="card-link" href={doc.card.dark}>Slack card</a>}
      </h1>
      <div className="scroll"><Summary doc={doc} /></div>
      <Charts doc={doc} />
      <h2 className="runs-head">Runs</h2>
      {doc.deployments.map(d => <Runs key={d.name} d={d} days={doc.days ?? 7} />)}
    </main>
  )
}
