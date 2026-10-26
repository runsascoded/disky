import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { enumParam, useUrlState } from 'use-prms'
import {
  bucketNotes, bucketsByCost, COST_CLOUDS, type CostSnapshot, componentSeries, components, type GcpCostSnapshot,
  lastSnapshots, LIVE, projections, SOFT_DELETED, trend,
} from './costModel'
import { SiteNav } from './SiteNav'
import { Tooltip } from './Tooltip'
import { useDocTitle } from './title'
import { useUnits } from './units'
import { fmtCount, fmtDollars } from './usageModel'
import './usage.scss'

// /admin/cost/<cloud>, admin only: a cloud's cost now, projected, and over the snapshot window, from the daily
// `snapshots/cost/<cloud>/<date>.json` documents the job writes (`/api/cost/<cloud>`). GCP's (`gcp-cost` step,
// `dt-cloud cost -o`): $/month by component (storage, each Batch `component` label), storage by bucket with its
// projection, and the trend. Cloudflare's page is `/admin/usage`.

const WINDOWS = ['7', '30'] as const

/** A byte delta with its sign (`fmtBytes` takes magnitudes). */
const signed = (fmt: (b: number) => string, b: number): string => `${b < 0 ? '−' : '+'}${fmt(Math.abs(b))}`

async function getCost(cloud: string): Promise<CostSnapshot[]> {
  const r = await fetch(`/api/cost/${encodeURIComponent(cloud)}?days=30`, { credentials: 'same-origin' })
  if (!r.ok) throw new Error(r.status === 401 ? 'unauthenticated' : r.status === 403 ? 'admins only' : `HTTP ${r.status}`)
  return ((await r.json()) as { days: CostSnapshot[] }).days
}

export function CostPage() {
  const { cloud = '' } = useParams()
  const known = Object.keys(COST_CLOUDS).includes(cloud) ? COST_CLOUDS[cloud] : null
  useDocTitle(`${known?.label ?? cloud} cost`, 'Admin')
  const [win, setWin] = useUrlState('days', enumParam<(typeof WINDOWS)[number]>('30', WINDOWS))
  const q = useQuery({ queryKey: ['cost', cloud, 30], queryFn: () => getCost(cloud), enabled: !!known })
  const snaps = lastSnapshots(q.data ?? [], Number(win))
  return (
    <div className="admin-page usage-page cost-page">
      <SiteNav />
      <p className="crumbs">
        <Link to="/admin">admin</Link>{' · '}
        {Object.entries(COST_CLOUDS).map(([k, c]) => <span key={k}><Link to={`/admin/cost/${k}`}>{c.label} cost</Link>{' · '}</span>)}
        <Link to="/admin/usage">Cloudflare usage</Link>
      </p>
      <h1>{known?.label ?? cloud} cost</h1>
      {!known ? <p>No cost page for <code>{cloud}</code> (known: {Object.keys(COST_CLOUDS).join(', ')}).</p> : <>
        <p className="dim">
          This deployment's {known.label} storage and Batch compute at list price, now and projected, from the daily
          snapshots the job's <code>{cloud}-cost</code> step writes. Estimates, not invoices: no discounts, disks,
          network or operations.
        </p>
        <div className="usage-filters">
          <label>window <select value={win} onChange={e => setWin(e.target.value as (typeof WINDOWS)[number])}>
            {WINDOWS.map(w => <option key={w} value={w}>last {w} days</option>)}
          </select></label>
        </div>
        {q.error && <p className="err">{q.error.message}</p>}
        {q.isLoading && <p className="loading">Loading…</p>}
        {q.data && !snaps.length && <p>No cost snapshots yet: the job's <code>{cloud}-cost</code> step writes them.</p>}
        {snaps.length > 0 && cloud === 'gcp' && <GcpCost snaps={snaps} />}
      </>}
    </div>
  )
}

function GcpCost({ snaps }: { snaps: GcpCostSnapshot[] }) {
  const s = snaps[snaps.length - 1]
  return <>
    <Now s={s} />
    <Buckets s={s} />
    <Trend snaps={snaps} />
    <p className="dim usage-notes">{s.summary} As of {s.as_of}, over a {s.window_days}-day window; {s.basis}.</p>
  </>
}

function Now({ s }: { s: GcpCostSnapshot }) {
  const { fmtBytes } = useUnits()
  const cs = components(s)
  const max = Math.max(...cs.map(c => c.usd), 1e-9)
  const c = s.compute
  const unlabeled = Object.values(c?.unlabeled ?? {})
  const unpriced = Object.entries(s.storage?.unpriced_bytes ?? {})
  const unpricedVm = Object.entries(c?.unpriced_vm_hours ?? {})
  return (
    <section>
      <div className="usage-tiles">
        <div className="usage-tile">
          <div className="k">Now</div>
          <div className="v">{fmtDollars(s.usd_per_month.total)}/mo</div>
          <div className="dim">{s.date}</div>
        </div>
        {s.storage && (
          <div className="usage-tile">
            <div className="k">Storage</div>
            <div className="v">{fmtDollars(s.usd_per_month.storage)}/mo</div>
            <div className="dim">{fmtBytes(s.storage.bytes)}, {signed(fmtBytes, s.storage.live_slope_bytes_per_day)}/day live</div>
          </div>
        )}
        {c && (
          <div className="usage-tile">
            <div className="k">Compute</div>
            <div className="v">{fmtDollars(s.usd_per_month.compute)}/mo</div>
            <div className="dim">{c.jobs} jobs, {fmtCount(c.vm_hours)} VM-h in {s.window_days} days</div>
          </div>
        )}
        {projections(s).map(p => (
          <div key={p.horizon} className="usage-tile">
            <div className="k">In {p.horizon}</div>
            <div className="v">{fmtDollars(p.totalUsd)}/mo</div>
            <div className="dim">{s.storage ? `${fmtBytes(p.storageBytes)} stored; ` : ''}compute flat</div>
          </div>
        ))}
      </div>
      <h2>$/month by component</h2>
      <div className="table-scroll">
        <table className="usage-table">
          <thead><tr><th>component</th><th>$/mo</th><th>jobs</th><th>VM-h</th></tr></thead>
          <tbody>
            {cs.map(x => {
              const t = x.key.startsWith('compute:') ? c?.components[x.key.slice('compute:'.length)] : undefined
              return (
                <tr key={x.key}>
                  <td className="name"><span className="share" style={{ width: `${100 * x.usd / max}%` }} /><code>{x.label}</code></td>
                  <td className="num">{fmtDollars(x.usd)}</td>
                  <td className="num dim">{t ? t.jobs : '–'}</td>
                  <td className="num dim">{t ? fmtCount(t.vm_hours) : '–'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
      {c && (
        <p className="dim">
          Batch jobs labeled <code>{Object.entries(c.selector).map(([k, v]) => `${k}=${v}`).join(',') || '(any)'}</code>,
          created in the window; VM-hours = run time × parallel tasks, SPOT at its discount; the rate is held flat.
          {unpricedVm.length > 0 && <> Unpriced: {unpricedVm.map(([m, h]) => `${fmtCount(h)} VM-h on ${m}`).join(', ')}.</>}
          {unlabeled.length > 0 && <> Not counted: {unlabeled.reduce((t, u) => t + u.jobs, 0)} jobs
            ({fmtDollars(unlabeled.reduce((t, u) => t + u.usd_per_month, 0))}/mo) carry none of the selector's labels.</>}
        </p>
      )}
      {unpriced.length > 0 && <p className="dim">Unpriced storage: {unpriced.map(([k, b]) => `${fmtBytes(b)} of ${k}`).join(', ')}.</p>}
    </section>
  )
}

function Buckets({ s }: { s: GcpCostSnapshot }) {
  const { fmtBytes } = useUnits()
  const buckets = bucketsByCost(s)
  if (!s.storage) return null
  const hs = Object.keys(s.horizons)
  const max = Math.max(...buckets.map(b => b.usd_per_month), 1e-9)
  return (
    <section>
      <h2>Storage by bucket</h2>
      <div className="table-scroll">
        <table className="usage-table">
          <thead><tr>
            <th>bucket</th><th>live</th><th>soft-deleted</th><th>live/day</th><th>$/mo</th>
            {hs.map(h => <th key={h}>in {h}</th>)}
          </tr></thead>
          <tbody>
            {buckets.map(b => (
              <tr key={b.bucket} className={b.short ? 'dim' : ''}>
                <td className="name">
                  <span className="share" style={{ width: `${100 * b.usd_per_month / max}%` }} />
                  <Tooltip cell content={<div className="usage-tip">
                    {Object.entries(b.bytes_by_class).map(([k, v]) => <div key={k}>{k}: {fmtBytes(v)}</div>)}
                    {b.unpriced_bytes > 0 && <div>unpriced: {fmtBytes(b.unpriced_bytes)}</div>}
                    {bucketNotes(b).map(n => <div key={n}>{n}</div>)}
                  </div>}><code>{b.bucket}</code></Tooltip>
                </td>
                <td className="num">{fmtBytes(b.bytes_by_type[LIVE] ?? 0)}</td>
                <td className="num dim">{fmtBytes(b.bytes_by_type[SOFT_DELETED] ?? 0)}</td>
                <td className="num dim">{b.ttl_days !== null ? 'TTL' : signed(fmtBytes, b.live_slope_bytes_per_day)}</td>
                <td className="num">{fmtDollars(b.usd_per_month)}</td>
                {hs.map(h => (
                  <td key={h} className="num">{fmtDollars(b.horizons[h]?.usd_per_month ?? 0)} <span className="dim">{fmtBytes(b.horizons[h]?.bytes ?? 0)}</span></td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="dim">Live bytes grow at the median of their daily deltas (held flat under a whole-bucket lifecycle
        Delete); soft-deleted bytes are held, or drain where soft delete is off. Dimmed: a short window, provisional.</p>
    </section>
  )
}

function Trend({ snaps }: { snaps: GcpCostSnapshot[] }) {
  const { fmtBytes } = useUnits()
  const series = componentSeries(snaps)
  const pts = trend(snaps)
  const max = Math.max(...pts.map(p => p.total), 1e-9)
  const first = pts[0]
  const last = pts[pts.length - 1]
  const delta = last.total - first.total
  const step = Math.ceil(pts.length / 10)
  return (
    <section>
      <h2>$/month over the window</h2>
      <p className="dim">
        {pts.length} snapshot{pts.length === 1 ? '' : 's'}, {first.date} … {last.date}
        {pts.length > 1 && <>: {delta >= 0 ? '+' : '−'}{fmtDollars(Math.abs(delta))}/mo, storage {fmtBytes(first.storageBytes)} → {fmtBytes(last.storageBytes)}</>}.
      </p>
      <ul className="usage-legend">
        {series.map(x => <li key={x.key}><span className="sw" style={{ background: x.color }} />{x.label}</li>)}
      </ul>
      <div className="usage-bars" style={{ ['--n' as string]: pts.length }}>
        <div className="axis-label dim">{fmtDollars(max)}/mo</div>
        {pts.map((p, i) => (
          <Tooltip key={p.date} placement="top" content={
            <div className="usage-tip">
              <div><strong>{p.date}</strong> · {fmtDollars(p.total)}/mo · {fmtBytes(p.storageBytes)} stored</div>
              {series.filter(x => (p.byKey[x.key] ?? 0) > 0).map(x => (
                <div key={x.key}><span className="sw" style={{ background: x.color }} />{x.label}: {fmtDollars(p.byKey[x.key])}</div>
              ))}
            </div>
          }>
            <span className="bar" aria-label={`${p.date}: ${fmtDollars(p.total)}/mo`}>
              <span className="stack" style={{ height: `${100 * p.total / max}%` }}>
                {series.map(x => (p.byKey[x.key] ?? 0) > 0 && (
                  <span key={x.key} className="seg" style={{ background: x.color, flexGrow: p.byKey[x.key] }} />
                ))}
              </span>
              {(pts.length - 1 - i) % step === 0 && <span className="day dim">{p.date.slice(5)}</span>}
            </span>
          </Tooltip>
        ))}
      </div>
    </section>
  )
}
