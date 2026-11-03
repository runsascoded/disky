import { useQuery } from '@tanstack/react-query'
import { Fragment } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import type { StoredBurst } from '../functions/_lib/activity'
import { KIND_DEFS, MAX_SESSION_LINKS, type ServerEvent } from '../functions/_lib/activityShape'
import { countsText, getJson, Line } from './ActivityKinds'
import { NO_AUTOFILL } from './noAutofill'
import { relAgo } from './prefixes'
import { SiteNav } from './SiteNav'
import { useDocTitle } from './title'

// /admin/activity — the activity feed (specs/done/activity-feed.md), admin only: each person's bursts of activity
// (server actions and, while the session log is on, their page loads, URL changes and clicks), newest first; a
// burst opens to its actions. `/admin`'s "Activity" table (`ActivityKinds.tsx`) sets what is recorded and reported.

const fmtTime = (s: number): string => new Date(s * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
const fmtClock = (s: number): string => new Date(s * 1000).toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit', second: '2-digit' })
const via = (v: string): string => v === 'grant' ? 'token / link' : v === 'public' ? 'signed out' : 'browser'

export function ActivityPage() {
  useDocTitle('Activity', 'Admin')
  const [sp, setSp] = useSearchParams()
  const actor = sp.get('u') ?? ''
  const kind = sp.get('k') ?? ''
  const since = sp.get('since') ?? ''
  const open = Number(sp.get('burst')) || null
  const set = (k: string, v: string | null) => setSp(p => { const n = new URLSearchParams(p); if (v) n.set(k, v); else n.delete(k); return n }, { replace: true })
  const qs = new URLSearchParams({ ...(actor ? { actor } : {}), ...(kind ? { kind } : {}), ...(since ? { since } : {}) }).toString()
  const q = useQuery({
    queryKey: ['activity', 'bursts', qs],
    queryFn: () => getJson<{ bursts: StoredBurst[] }>(`/api/activity/bursts${qs ? `?${qs}` : ''}`),
  })
  return (
    <div className="admin-page sessions-page activity-page">
      <SiteNav />
      <p className="crumbs"><Link to="/admin">admin</Link></p>
      <h1>Activity</h1>
      <p className="dim">
        What people did, one row per person per burst (no gap over 15 minutes): their changes, and while the session
        log is on, their page loads, URL changes and clicks. Open a row for its changes. What is recorded and posted to
        the admin channel is set at <Link to="/admin">/admin</Link> → Activity.
      </p>
      <div className="sessions-filters">
        <label>who <input type="search" name="activity-actor" value={actor} onChange={e => set('u', e.target.value || null)} placeholder="contains…" size={18} {...NO_AUTOFILL} /></label>
        <label>kind <select value={kind} onChange={e => set('k', e.target.value || null)}>
          <option value="">any</option>
          {KIND_DEFS.map(d => <option key={d.kind} value={d.kind}>{d.kind}</option>)}
        </select></label>
        <label>since <input type="date" value={since} onChange={e => set('since', e.target.value || null)} /></label>
      </div>
      {q.error && <p className="err">{q.error.message}</p>}
      <div className="table-scroll">
        <table className="sessions">
          <thead><tr><th>who</th><th>last active</th><th>started</th><th>via</th><th>activity</th></tr></thead>
          <tbody>
            {(q.data?.bursts ?? []).map(b => (
              <Fragment key={b.id}>
                <tr className={open === b.id ? 'focused' : ''}>
                  <td>{b.actor}</td>
                  <td><Link className="when" to={`?${withBurst(sp, open === b.id ? null : b.id)}`} replace>{relAgo(b.last_ts)}</Link></td>
                  <td>{fmtTime(b.first_ts)}</td>
                  <td>{via(b.via)}</td>
                  <td>{countsText(b.counts) || <span className="dim">—</span>}</td>
                </tr>
                {open === b.id && <tr><td colSpan={5}><BurstDetail id={b.id} /></td></tr>}
              </Fragment>
            ))}
            {q.data && !q.data.bursts.length && <tr><td colSpan={5}><em>no activity matches</em></td></tr>}
            {q.isPending && <tr><td colSpan={5}><em>loading…</em></td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  )
}

const withBurst = (sp: URLSearchParams, id: number | null): string => {
  const n = new URLSearchParams(sp)
  if (id) n.set('burst', String(id))
  else n.delete('burst')
  return n.toString()
}

function BurstDetail({ id }: { id: number }) {
  const q = useQuery({
    queryKey: ['activity', 'burst', id],
    queryFn: () => getJson<{ burst: StoredBurst; events: ServerEvent[]; slack: string | null }>(`/api/activity/bursts/${id}`),
  })
  if (q.error) return <p className="err">{q.error.message}</p>
  if (!q.data) return <p className="dim">loading…</p>
  const { burst, events, slack } = q.data
  const pages = Object.entries(burst.pages)
  return (
    <div className="burst-detail">
      <p className="session-meta">
        {burst.sids.length > MAX_SESSION_LINKS
          ? <><Link to={`/admin/sessions?u=${encodeURIComponent(burst.actor)}`}>{burst.sids.length} sessions</Link> · </>
          : burst.sids.map(sid => <Fragment key={sid}><Link to={`/admin/sessions/${sid}`}>session <code>{sid.slice(0, 8)}</code></Link> · </Fragment>)}
        {slack ? <a href={slack} target="_blank" rel="noreferrer">Slack thread</a> : <span className="dim">not posted to Slack</span>}
      </p>
      {burst.trail.n > 0
        ? <p className="session-meta">
          {burst.trail.n > burst.trail.last.length && <>{burst.trail.n} stops, the last {burst.trail.last.length}: </>}
          {burst.trail.last.map(([p, n], i) => <Fragment key={i}>{i > 0 && ' → '}<code>{p}</code>{n > 1 ? ` ×${n}` : ''}</Fragment>)}
        </p>
        : pages.length > 0 && <p className="session-meta">pages: {pages.slice(0, 8).map(([p, n]) => <Fragment key={p}><code>{p}</code>{n > 1 ? ` ×${n}` : ''} </Fragment>)}</p>}
      {events.length
        ? (
          <table className="sessions timeline">
            <thead><tr><th className="num">time</th><th>kind</th><th>what</th></tr></thead>
            <tbody>
              {events.map((e, i) => (
                <tr key={i} className={e.kind === 'error' ? 'has-err' : ''}>
                  <td className="num"><code className="dim">{fmtClock(e.ts)}</code></td><td>{e.kind}</td>
                  <td className="ev">
                    {e.kind === 'error' ? <>{e.line}{e.route && <> on <code>{e.route}</code></>}</> : <Line text={e.line} />}
                    {typeof e.detail?.sid === 'string' && <> · <Link to={`/admin/sessions/${e.detail.sid}?show=errors`}>its session</Link></>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )
        : <p className="dim">No changes: only browsing.</p>}
    </div>
  )
}
