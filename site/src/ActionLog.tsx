import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useUserEmails } from './owners'
import { Tooltip } from './Tooltip'
import { UserChip } from './UserChip'
import { Skeleton } from './Busy'
import type { ActionLogRow, ActionStatus } from '../functions/_lib/actionLogShape'

const PAGE = 50

const STATUS_TIP: Record<ActionStatus, string> = {
  live: 'In force: the newest action on this prefix and on every ancestor.',
  superseded: 'Replaced by a newer action on the same prefix.',
  overridden: 'A newer action on an ancestor prefix decides this one now (the most recent action wins across nesting).',
  retracted: 'Withdrawn: this action no longer counts.',
}

/** `gs://bucket/a/b/` → the map at `bucket/a/b`. */
const mapLink = (prefix: string) => '/' + prefix.replace(/^gs:\/\//, '').replace(/\/$/, '')

// The ownership ledger as an audit log (`GET /api/actions?log=1`): every
// assignment, newest first, with who, when, the note, and what became of it.
export function ActionLog() {
  const [page, setPage] = useState(0)
  const emails = useUserEmails(true)
  const q = useQuery<{ total: number; rows: ActionLogRow[] }>({
    queryKey: ['action-log', page],
    queryFn: async () => {
      const r = await fetch(`/api/actions?log=1&limit=${PAGE}&offset=${page * PAGE}`, { credentials: 'include' })
      if (!r.ok) throw new Error(`${r.status}`)
      return r.json()
    },
  })
  const total = q.data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / PAGE))
  const when = (ts: number) => new Date(ts * 1000).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
  return (
    <section className="action-log">
      <div className="hrow">
        <h2>actions</h2>
        <span className="dim">{total.toLocaleString()} assignments, newest first</span>
        {pages > 1 && (
          <span className="pg">
            <button type="button" disabled={page === 0} onClick={() => setPage(0)} aria-label="first page">«</button>
            <button type="button" disabled={page === 0} onClick={() => setPage(page - 1)} aria-label="previous page">‹</button>
            <span>{page * PAGE + 1}–{Math.min(total, (page + 1) * PAGE)} of {total.toLocaleString()}</span>
            <button type="button" disabled={page >= pages - 1} onClick={() => setPage(page + 1)} aria-label="next page">›</button>
            <button type="button" disabled={page >= pages - 1} onClick={() => setPage(pages - 1)} aria-label="last page">»</button>
          </span>
        )}
      </div>
      {q.isLoading && <Skeleton height={240} label="loading actions…" />}
      {q.error && <p className="err">{(q.error as Error).message}</p>}
      {q.data && (
        <div className="table-scroll">
          <table className="log">
            <thead>
              <tr><th>when</th><th>by</th><th>prefix</th><th>owner</th><th>status</th><th>note</th></tr>
            </thead>
            <tbody>
              {q.data.rows.map((r, i) => (
                <tr key={r.id} className={r.status}>
                  <td className="when">{when(r.ts)}</td>
                  <td><UserChip who={emails?.[r.who.toLowerCase()] ?? r.who} size={15} /></td>
                  <td className="prefix"><Link to={mapLink(r.prefix)}>{r.prefix}</Link></td>
                  <td>{r.owner ? <UserChip who={r.owner} size={15} /> : <span className="dim">cleared</span>}</td>
                  <td>
                    <Tooltip content={<>{STATUS_TIP[r.status]}{r.retracted && <div className="how">{r.retracted}</div>}</>}>
                      <span className={`status ${r.status}`}>{r.status}</span>
                    </Tooltip>
                  </td>
                  <td className="memo">
                    {/* A batch's rows share one note: show it once, ditto after. */}
                    {r.memo && (i > 0 && q.data.rows[i - 1].memo === r.memo
                      ? <span className="dim">〃</span>
                      : <Tooltip content={r.memo}><span className="clip">{r.memo}</span></Tooltip>)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
