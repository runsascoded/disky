import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { enumParam, useUrlState } from 'use-prms'
import { SiteNav } from './SiteNav'
import { Tooltip } from './Tooltip'
import { useDocTitle } from './title'
import {
  type AttrKey, type AttrRow, attribution, coverage, fmtCount, fmtDollars, lastDays, PRODUCTS, productCosts,
  type UsageSnapshot, type UsageSums, windowTotals,
} from './usageModel'
import './usage.scss'

// /admin/usage (specs/usage-metrics.md), admin only: the deployment's Cloudflare bill and who drives it, from the
// daily `snapshots/usage/<date>.json` documents the job's `cf-usage` step writes (`/api/usage`). The window's units
// against the plan's included amounts, the list $ per product per day, then the usage points by caller or route.

const WINDOWS = ['7', '30'] as const
const RANKS: [keyof UsageSums, string][] = [
  ['requests', 'requests'], ['r2_reads', 'R2 reads'], ['d1_rows_read', 'D1 rows read'], ['d1_rows_written', 'D1 rows written'],
]
const TOP = 12

async function getUsage(): Promise<UsageSnapshot[]> {
  const r = await fetch('/api/usage?days=30', { credentials: 'same-origin' })
  if (!r.ok) throw new Error(r.status === 401 ? 'unauthenticated' : r.status === 403 ? 'admins only' : `HTTP ${r.status}`)
  return ((await r.json()) as { days: UsageSnapshot[] }).days
}

export function UsagePage() {
  useDocTitle('Usage', 'Admin')
  const [win, setWin] = useUrlState('days', enumParam<(typeof WINDOWS)[number]>('30', WINDOWS))
  const [attr, setAttr] = useUrlState('attr', enumParam<AttrKey>('caller', ['caller', 'route']))
  const [rank, setRank] = useUrlState('um', enumParam<keyof UsageSums>('requests', RANKS.map(([k]) => k)))
  const q = useQuery({ queryKey: ['usage', 30], queryFn: getUsage })
  const snaps = lastDays(q.data ?? [], Number(win))
  return (
    <div className="admin-page usage-page">
      <SiteNav />
      <p className="crumbs"><Link to="/admin">admin</Link> · <Link to="/admin/cost/gcp">GCP cost</Link></p>
      <h1>Cloudflare usage</h1>
      <p className="dim">
        This deployment's billed Cloudflare units per day (GraphQL Analytics) at list price, and which callers and
        routes drive them (the site's usage points). The job writes one file a day; the newest is yesterday (UTC).
      </p>
      <div className="usage-filters">
        <label>window <select value={win} onChange={e => setWin(e.target.value as (typeof WINDOWS)[number])}>
          {WINDOWS.map(w => <option key={w} value={w}>last {w} days</option>)}
        </select></label>
      </div>
      {q.error && <p className="err">{q.error.message}</p>}
      {q.isLoading && <p className="loading">Loading…</p>}
      {q.data && !snaps.length && <p>No usage snapshots yet: the job's <code>cf-usage</code> step writes them.</p>}
      {snaps.length > 0 && <>
        <Totals snaps={snaps} />
        <DailyBars snaps={snaps} />
        <Attribution snaps={snaps} attr={attr} setAttr={setAttr} rank={rank} setRank={setRank} />
        {snaps.some(s => s.notes.length) && (
          <p className="dim usage-notes">Notes: {[...new Set(snaps.flatMap(s => s.notes))].join('; ')}</p>
        )}
      </>}
    </div>
  )
}

const fmtAmount = (x: number, storage: boolean) => storage ? `${fmtCount(x)} GB` : fmtCount(x)

function Totals({ snaps }: { snaps: UsageSnapshot[] }) {
  const t = windowTotals(snaps)
  const list = t.reduce((a, x) => a + x.listUsd, 0)
  const over = t.reduce((a, x) => a + x.overUsd, 0)
  const d1w = t.find(x => x.unit === 'd1_rows_written')
  const plan = snaps[snaps.length - 1].plan
  const shown = t.filter(x => x.total > 0).sort((a, b) => b.overUsd - a.overUsd || b.listUsd - a.listUsd)
  return (
    <section>
      <div className="usage-tiles">
        <div className="usage-tile">
          <div className="k">Over the plan</div>
          <div className="v">{fmtDollars(over)}</div>
          <div className="dim">{snaps.length} days, past the included amounts pro rata</div>
        </div>
        <div className="usage-tile">
          <div className="k">List price</div>
          <div className="v">{fmtDollars(list)}</div>
          <div className="dim">before the included amounts</div>
        </div>
        {d1w && over > 0 && (
          <div className="usage-tile usage-tile-d1w">
            <div className="k">D1 rows written</div>
            <div className="v">{Math.round(100 * d1w.overUsd / over)}%</div>
            <div className="dim">of the overage: {fmtCount(d1w.total)} rows, {fmtCount(d1w.included)} included</div>
          </div>
        )}
      </div>
      <div className="table-scroll">
        <table className="usage-table">
          <thead><tr><th>unit</th><th>used</th><th>included</th><th>over</th><th>over $</th><th>list $</th></tr></thead>
          <tbody>
            {shown.map(x => {
              const storage = plan[x.unit]?.storage ?? false
              const p = plan[x.unit]
              return (
                <tr key={x.unit} className={x.unit === 'd1_rows_written' ? 'hot' : ''}>
                  <td>
                    <Tooltip cell content={<>
                      {fmtDollars(p.usd)} per {fmtCount(p.per)}{storage ? ' GB-month' : ''}; {fmtAmount(p.included, storage)} a month included.
                      {storage && ' Used is the window\'s mean level.'}
                    </>}>{x.label}</Tooltip>
                  </td>
                  <td className="num">{fmtAmount(x.total, storage)}</td>
                  <td className="num dim">{fmtAmount(x.included, storage)}</td>
                  <td className="num">{x.over ? fmtAmount(x.over, storage) : '–'}</td>
                  <td className="num">{x.overUsd ? fmtDollars(x.overUsd) : '–'}</td>
                  <td className="num dim">{fmtDollars(x.listUsd)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      <p className="dim">The Workers Paid base fee is not included. Functions CPU is an estimate (the analytics carry
        CPU-time quantiles, not a sum).</p>
    </section>
  )
}

function DailyBars({ snaps }: { snaps: UsageSnapshot[] }) {
  const days = snaps.map(s => ({ date: s.date, costs: productCosts(s) }))
  const totals = days.map(d => Object.values(d.costs).reduce((a, b) => a + b, 0))
  const max = Math.max(...totals, 1e-9)
  // a date under every `step`th bar (the newest always), at most ~10 across
  const step = Math.ceil(days.length / 10)
  return (
    <section>
      <h2>List $ per day, by product</h2>
      <ul className="usage-legend">
        {PRODUCTS.map(p => <li key={p.key}><span className="sw" style={{ background: p.color }} />{p.label}</li>)}
      </ul>
      <div className="usage-bars" style={{ ['--n' as string]: days.length }}>
        <div className="axis-label dim">{fmtDollars(max)}</div>
        {days.map((d, i) => (
          <Tooltip key={d.date} placement="top" content={
            <div className="usage-tip">
              <div><strong>{d.date}</strong> · {fmtDollars(totals[i])} list</div>
              {PRODUCTS.filter(p => d.costs[p.key] > 0).map(p => (
                <div key={p.key}><span className="sw" style={{ background: p.color }} />{p.label}: {fmtDollars(d.costs[p.key])}</div>
              ))}
            </div>
          }>
            <span className="bar" aria-label={`${d.date}: ${fmtDollars(totals[i])}`}>
              <span className="stack" style={{ height: `${100 * totals[i] / max}%` }}>
                {PRODUCTS.map(p => d.costs[p.key] > 0 && (
                  <span key={p.key} className="seg" style={{ background: p.color, flexGrow: d.costs[p.key] }} />
                ))}
              </span>
              {(days.length - 1 - i) % step === 0 && <span className="day dim">{d.date.slice(5)}</span>}
            </span>
          </Tooltip>
        ))}
      </div>
    </section>
  )
}

function Attribution({ snaps, attr, setAttr, rank, setRank }: {
  snaps: UsageSnapshot[]
  attr: AttrKey
  setAttr: (a: AttrKey) => void
  rank: keyof UsageSums
  setRank: (r: keyof UsageSums) => void
}) {
  const rows = attribution(snaps, attr, TOP, rank)
  const cov = coverage(snaps)
  const max = Math.max(...rows.map(r => r[rank]), 1)
  const cell = (r: AttrRow, k: keyof UsageSums) => <td className={`num${k === rank ? ' ranked' : ''}`}>{fmtCount(r[k])}</td>
  return (
    <section>
      <h2>Who drives it</h2>
      <div className="usage-filters">
        <label>by <select value={attr} onChange={e => setAttr(e.target.value as AttrKey)}>
          <option value="caller">caller</option><option value="route">route</option>
        </select></label>
        <label>top {TOP} by <select value={rank} onChange={e => setRank(e.target.value as keyof UsageSums)}>
          {RANKS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select></label>
      </div>
      <p className="dim">
        {fmtCount(cov.attributed)} of {fmtCount(cov.billed)} billed Functions requests carry a usage point (points began
        with the meter's deploy; the dev stack's count too). D1 rows written here are the site's own; the job's
        footer writes go through D1's HTTP API and appear only in the totals above.
      </p>
      {rows.length === 0 ? <p>No usage points in this window.</p> : (
        <div className="table-scroll">
          <table className="usage-table">
            <thead><tr>
              <th>{attr}</th>{attr === 'caller' && <th>via</th>}<th>requests</th><th>R2 reads</th><th>R2 bytes</th>
              <th>D1 queries</th><th>D1 rows read</th><th>D1 rows written</th><th>KV reads</th><th>ms/req</th>
            </tr></thead>
            <tbody>
              {rows.map(r => (
                <tr key={r.name}>
                  <td className="name">
                    <span className="share" style={{ width: `${100 * r[rank] / max}%` }} />
                    <code>{r.name}</code>
                  </td>
                  {attr === 'caller' && <td className="dim">{r.via}</td>}
                  {cell(r, 'requests')}{cell(r, 'r2_reads')}
                  <td className="num">{fmtCount(r.r2_bytes)}B</td>
                  {cell(r, 'd1_queries')}{cell(r, 'd1_rows_read')}{cell(r, 'd1_rows_written')}{cell(r, 'kv_reads')}
                  <td className="num dim">{r.requests ? Math.round(r.wall_ms / r.requests) : '–'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
