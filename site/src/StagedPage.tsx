// /staged — the opt-in deletion console (specs/staged-delete.md; the OA build
// plan `sweep-plan-union.md` checkpoint 4). Trash gestures on the map's table
// stage prefixes into one shared open plan; this page shows that plan — each
// gesture's batch with who/when/memo — lets a stager take their own back, and
// lets an admin dry-run or really dispatch it to the deployment's executor
// (`EXEC_API`, `Store.executor`: cw's plan-first Batch bridge, or gcs's
// sweep bridge). Non-admins see everything read-only. Nothing is deleted by
// inaction: no deadline, no auto-sweep.
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { Tooltip } from './Tooltip'
import { UserChip } from './UserChip'
import { useUnits } from './units'
import { fmtN } from './types'
import { DEFAULT_STORE } from './stores'
import { ago } from './colors'
import { useCanStage, useIdent } from './auth'
import { useRowSelection, useRowSelectionKeys } from './rowSelection'
import { useDispatch, useExecJobs, useRunAction, useStagedPlan, useUnstage, LIVE_STATES } from './plans'
import type { DeletionRun, ExecJob, StagedItem } from './plans'

const iso = (ts: number): string => new Date(ts * 1000).toISOString()

// `<scheme><bucket>/<path>/` → the treemap's URL path (below the store root).
const prefixToPath = (prefix: string): string => {
  const m = /^[a-z0-9]+:\/\/(.*?)\/?$/.exec(prefix)
  return m ? m[1] : prefix
}

/** Whoami's admin flag: the plan-first console keys on it server-side too. */
function useIsAdmin(): boolean {
  const [admin, setAdmin] = useState(false)
  useEffect(() => {
    let live = true
    void fetch('/api/whoami', { credentials: 'include' }).then(r => r.ok ? r.json() : null).then((w: { admin?: boolean } | null) => { if (live) setAdmin(!!w?.admin) }).catch(() => {})
    return () => { live = false }
  }, [])
  return admin
}

export function StagedPage() {
  const { fmtBytes } = useUnits()
  const ident = useIdent()
  const admin = useIsAdmin()
  const canStage = useCanStage()
  // Plan-first executors (cw's Batch bridge, the laptop drainer) share the
  // `/api/plan-sweep/*` console routes; gcs's `sweep` has its own.
  const planFirst = DEFAULT_STORE.executor !== 'sweep'

  const [live, setLive] = useState(false)
  const staged = useStagedPlan(live)
  const jobs = useExecJobs(live)
  const plan = staged.data?.plan ?? null
  const items = staged.data?.items ?? []
  const batches = staged.data?.batches ?? []
  const runs = staged.data?.runs ?? []
  const anyLive = runs.some(r => LIVE_STATES.has(jobs.data?.[r.run_id]?.state ?? '') || (!r.finished_ts && !jobs.data?.[r.run_id]))
  useEffect(() => setLive(anyLive), [anyLive])

  const unstage = useUnstage(plan?.id ?? null)
  const dispatch = useDispatch(plan?.id ?? null)
  const runAction = useRunAction()
  const busy = unstage.isPending || dispatch.isPending || runAction.isPending
  const error = unstage.error ?? dispatch.error ?? runAction.error ?? staged.error

  const [scans, setScans] = useState<string[]>([])
  const [date, setDate] = useState('')
  useEffect(() => {
    void fetch(`${DEFAULT_STORE.base}/scans.json`, { credentials: 'include' }).then(r => r.json()).then((s: string[]) => { setScans(s); setDate(s[0] ?? '') }).catch(() => {})
  }, [])

  // Items grouped by gesture (newest first); items staged before batches
  // existed fall into one "earlier" group.
  const groups = useMemo(() => {
    const byBatch = new Map<number | null, StagedItem[]>()
    for (const it of items) {
      const k = it.batch_id
      if (!byBatch.has(k)) byBatch.set(k, [])
      byBatch.get(k)!.push(it)
    }
    const known = new Map(batches.map(b => [b.id, b]))
    return [...byBatch.entries()]
      .map(([id, its]) => ({ id, batch: id != null ? known.get(id) : undefined, items: its }))
      .sort((a, b) => (b.batch?.created_ts ?? 0) - (a.batch?.created_ts ?? 0))
  }, [items, batches])

  const sel = useRowSelection(items, it => it.prefix)
  useRowSelectionKeys(sel, 'staged', 'Staged')
  const selected = items.filter(sel.isSelected).map(it => it.prefix)
  const mine = (it: StagedItem) => !!ident && it.added_by === ident.email
  const canRemove = (it: StagedItem) => admin || (canStage && mine(it))
  const removable = selected.filter(p => { const it = items.find(i => i.prefix === p); return it ? canRemove(it) : false })

  const [armed, setArmed] = useState(false)
  useEffect(() => setArmed(false), [plan?.id, items.length])

  return (
    <div className="staged-page">
      <div className="staged-head">
        <Link to="/" className="back">← treemap</Link>
        <h1>Staged for deletion</h1>
        <span className="who">
          {ident ? <>{admin ? 'admin' : canStage ? 'stager' : 'viewer'} · <UserChip who={ident.email} size={20} /></> : 'not signed in'}
        </span>
      </div>
      <p className="sub">
        Nothing here is deleted until an admin dispatches it — staging is opt-in, with no deadline. Stage prefixes
        from the table under the map (the trash icon); a memo travels with each gesture.
        {admin ? ' Dry-run first to see what a real run would delete; a real run deletes recoverably.' : ' An admin reviews and dispatches from here.'}
      </p>
      {error && <p className="staged-err" role="alert">{error.message}</p>}

      {staged.isLoading ? <p className="loading">loading…</p> : !plan || !items.length ? (
        <p className="staged-empty">Nothing is staged.</p>
      ) : (
        <>
          <div className="pp-head">
            <h2>{items.length} {items.length === 1 ? 'prefix' : 'prefixes'} <span className="dim">· plan #{plan.id}{plan.name !== 'Staged' && <> “{plan.name}”</>} · open since {ago(plan.created_ts)} ago</span></h2>
          </div>
          <div className="staged-actions">
            <label className="sel-all"><input type="checkbox" checked={sel.pageAll} onChange={sel.togglePage} aria-label="select all" /> {sel.selected.size ? `${sel.selected.size} selected` : 'select'}</label>
            {removable.length > 0 && (
              <button type="button" disabled={busy} onClick={() => unstage.mutate(removable, { onSuccess: () => sel.clear() })}>unstage {removable.length}</button>
            )}
          </div>
          {groups.map(g => (
            <section key={g.id ?? 'none'} className="stage-batch">
              <div className="batch-head">
                {g.batch
                  ? <><UserChip who={g.batch.created_by} size={18} /> staged <Tooltip content={iso(g.batch.created_ts)}><span>{ago(g.batch.created_ts)} ago</span></Tooltip>{g.batch.note && <> — <i className="memo">{g.batch.note}</i></>}</>
                  : <span className="dim">staged earlier</span>}
              </div>
              <table className="staged-table">
                <tbody>
                  {g.items.map(it => {
                    const i = items.indexOf(it)
                    return (
                      <tr key={it.prefix} ref={sel.rowRef(i)} {...sel.rowProps(i)}>
                        <td className="col-sel"><input type="checkbox" checked={sel.isSelected(it)} onChange={() => { sel.toggle(i); sel.commit() }} aria-label={`select ${it.prefix}`} /></td>
                        <td><Link to={`/${prefixToPath(it.prefix)}`}><code>{it.prefix}</code></Link></td>
                        <td className="dim nb">{it.added_by !== g.batch?.created_by && <UserChip who={it.added_by} size={16} />}</td>
                        <td className="actions">
                          {canRemove(it) && <button type="button" className="rm" title="unstage" disabled={busy} onClick={() => unstage.mutate([it.prefix])}>×</button>}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </section>
          ))}

          {admin && (
            <div className="dispatch">
              <h3>Dispatch</h3>
              <label>scan <select value={date} onChange={e => setDate(e.target.value)}>{scans.map(s => <option key={s}>{s}</option>)}</select></label>
              <div className="dispatch-btns">
                <button type="button" className="dry" disabled={busy || !date} onClick={() => dispatch.mutate({ mode: 'dry', date })}>dispatch dry-run</button>
                {!armed
                  ? <button type="button" className="danger" disabled={busy || !date} onClick={() => setArmed(true)}>real delete…</button>
                  : <>
                      <button type="button" className="danger armed" disabled={busy} onClick={() => dispatch.mutate({ mode: 'real', date }, { onSuccess: () => setArmed(false) })}>confirm REAL delete of {items.length} {items.length === 1 ? 'prefix' : 'prefixes'}</button>
                      <button type="button" onClick={() => setArmed(false)}>cancel</button>
                    </>}
              </div>
              <p className="dispatch-note dim">A run reads the scan you pick and deletes only what it listed; new objects since are left alone.</p>
            </div>
          )}
        </>
      )}

      {plan && runs.length > 0 && (
        <>
          <h3>Runs ({runs.length})</h3>
          <div className="runs-wrap">
            <table className="runs">
              <thead><tr><th>run</th><th>mode</th><th>state</th><th>deleted</th><th>gone</th><th>started</th><th></th></tr></thead>
              <tbody>
                {runs.map(r => (
                  <RunRow key={r.run_id} r={r} job={jobs.data?.[r.run_id]} admin={admin} planFirst={planFirst} busy={busy} fmtBytes={fmtBytes}
                    act={(action, run_id) => runAction.mutate({ action, run_id })} />
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  )
}

function RunRow({ r, job, admin, planFirst, busy, fmtBytes, act }: {
  r: DeletionRun; job: ExecJob | undefined; admin: boolean; planFirst: boolean; busy: boolean
  fmtBytes: (b: number) => string
  act: (action: 'stop' | 'undo' | 'purge', run_id: string) => void
}) {
  const state = job?.state ?? (r.finished_ts ? 'done' : 'submitted')
  const live = LIVE_STATES.has(state)
  const now = Math.floor(Date.now() / 1000)
  const canUndo = planFirst && admin && r.mode === 'real' && r.undo_state !== 'full' && (!r.undo_deadline || now < r.undo_deadline) && !!r.finished_ts
  const canPurge = planFirst && admin && r.mode === 'real' && r.purge_state === 'pending' && r.undo_state !== 'full' && (!r.undo_deadline || now >= r.undo_deadline)
  return (
    <tr className={`run ${r.mode} ${state.toLowerCase()}`}>
      <td className="rid"><code>{r.run_id.replace(/^(cw|gcs)-sweep-(dry|real)-/, '')}</code>{job?.logs && <> <a href={job.logs} target="_blank" rel="noreferrer" className="logs">logs</a></>}</td>
      <td>{r.mode}</td>
      <td>
        <span className="rstate">{state}</span>
        {state === 'FAILED' && job?.last_event && <details className="why"><summary>why</summary><div>{job.last_event}</div></details>}
        {r.undo_state === 'full' && <span className="tag">undone</span>}
        {r.purge_state === 'done' && <span className="tag">purged</span>}
      </td>
      <td>
        {fmtBytes(r.deleted_bytes)} <span className="dim">/ {fmtN(r.deleted_objects)}</span>
        {r.freed_bytes != null && (
          <Tooltip content={<>What deleting this set would <b>actually</b> free, measured on the laptop: bytes it shares with a clone or hardlink outside the set (e.g. a <code>.venv</code>’s files cloned from <code>~/.cache/uv</code>) stay on disk. The size before it counts every path in full.</>}>
            <span className="frees"> · frees <b>{fmtBytes(r.freed_bytes)}</b></span>
          </Tooltip>
        )}
      </td>
      <td>{fmtN(r.skipped_gone)}</td>
      <td><Tooltip content={iso(r.started_ts)}><span>{ago(r.started_ts)} ago</span></Tooltip></td>
      <td className="actions">
        {planFirst && admin && live && <button type="button" disabled={busy} onClick={() => act('stop', r.run_id)}>stop</button>}
        {canUndo && <button type="button" disabled={busy} onClick={() => act('undo', r.run_id)}>undo</button>}
        {canPurge && <button type="button" className="danger" disabled={busy} onClick={() => act('purge', r.run_id)}>purge</button>}
      </td>
    </tr>
  )
}
