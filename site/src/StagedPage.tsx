// /staged — the opt-in deletion console (specs/staged-delete.md; the OA build
// plan `sweep-plan-union.md` checkpoint 4). Trash gestures on the map's table
// stage prefixes into one shared open plan; this page shows that plan — a
// treemap of everything staged (and of the selection), then each gesture's
// batch with who/when/memo, its items sized at a scan — lets a stager take
// their own back, and lets an admin dry-run or really dispatch it to the
// deployment's executor (`EXEC_API`, `Store.executor`: cw's plan-first Batch
// bridge, or gcs's sweep bridge). Non-admins see everything read-only.
// Nothing is deleted by inaction: no deadline, no auto-sweep.
//
// Below the plan, its runs (`StagedRuns.tsx`, specs/staged-runs.md): live
// progress, totals, undo windows, files and logs, and the run controls the
// executor offers (`CAPS`). `?plan=<id>` shows another plan — a closed one
// keeps its runs (and their undo windows) reachable; an admin closes the open
// plan from here. A gesture whose prefixes all went (absorbed by a later
// ancestor, taken back) folds to one line.
import { Fragment, type ReactNode, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { boolParam, optIntParam, stringParam, useUrlState } from 'use-prms'
import { SiteNav } from './SiteNav'
import { SiteKbd } from './SiteKbd'
import { Tooltip } from './Tooltip'
import { Treemap } from './Treemap'
import { UserChip, canonId, shortName } from './UserChip'
import { ownerShares } from './OwnerBar'
import { encodeSort, filterStaged, parseSort } from './stagedFilter'
import { PrefixTable, TimeCell } from './PrefixTable'
import { useUnits } from './units'
import { fmtN, type Meta, type TreeNode } from './types'
import { DEFAULT_STORE } from './stores'
import { buildUserIndex } from './colors'
import { useCanStage, useIdent } from './auth'
import { applyLedger } from './ledgerOverlay'
import { useOwnerIndex, useOwners } from './owners'
import { useRowSelection, useRowSelectionKeys } from './rowSelection'
import { CAPS, useClosePlan, useDispatch, useExecJobs, usePlanList, useRunAction, useStagedPlan, useUnstage } from './plans'
import type { EmptiedBatch, StagedItem } from './plans'
import { joinRuns, LIVE_STATES, viewLive } from './runs'
import { RunsSection } from './StagedRuns'
import { type PrefixSortKey, type PrefixStat, relAgo, sortPrefixRows, usePrefixes } from './prefixes'
import { stagedTree } from './stagedTree'
import { machineFor } from '../functions/_lib/sweepMachines'

const iso = (ts: number): string => new Date(ts * 1000).toISOString()
const store = DEFAULT_STORE

// `<scheme><bucket>/<path>/` → the treemap's URL path (below the store root).
const prefixToPath = (prefix: string): string => {
  const m = /^[a-z0-9]+:\/\/(.*?)\/?$/.exec(prefix)
  return m ? m[1] : prefix
}

/** Rows per batch page; the flat (filtered or ungrouped) table pages longer. */
const PAGE = 20
const FLAT_PAGE = 50

/** A user id or email as its search text: the canonical id and the display name. */
const searchName = (who: string) => `${canonId(who)} ${shortName(who)}`

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

type Row = StagedItem & { name: string; to: string; stat?: PrefixStat }
type Group = { id: number | null; batch?: { created_by: string; created_ts: number; note: string | null }; rows: Row[]; emptied?: EmptiedBatch }

/** An emptied gesture's fate, as one line: what absorbed it, what was taken
 * back, what was already covered. */
function EmptiedLine({ e, batches }: { e: EmptiedBatch; batches: Map<number, { created_by: string; created_ts: number }> }) {
  const n = (k: number) => `${k} ${k === 1 ? 'prefix' : 'prefixes'}`
  const parts: ReactNode[] = e.absorbed.map(a => {
    const into = batches.get(a.into)
    return (
      <span key={`a${a.into}`}>
        {n(a.n)} absorbed into {into ? <><UserChip who={into.created_by} size={16} />'s batch <Tooltip content={iso(into.created_ts)}><span>{relAgo(into.created_ts)}</span></Tooltip></> : <>batch #{a.into}</>}
      </span>
    )
  })
  if (e.unstaged) parts.push(<span key="u">{n(e.unstaged)} unstaged</span>)
  if (e.covered) parts.push(<span key="c">{n(e.covered)} already covered by a staged ancestor</span>)
  if (!parts.length) parts.push(<span key="none">nothing new (already staged)</span>)
  return <span className="emptied-fate">{parts.map((p, i) => <Fragment key={i}>{i > 0 && ' · '}{p}</Fragment>)}</span>
}

export function StagedPage() {
  const { fmtBytes } = useUnits()
  const ident = useIdent()
  const admin = useIsAdmin()
  const canStage = useCanStage()

  // `?plan=<id>`: a plan other than the shared open one (a closed plan keeps
  // its runs, and their undo windows, reachable here).
  const [planP, setPlanP] = useUrlState('plan', optIntParam)
  const [live, setLive] = useState(false)
  const staged = useStagedPlan(live, planP)
  const plans = usePlanList()
  const jobs = useExecJobs(live)
  const jobList = useMemo(() => jobs.data?.jobs ?? [], [jobs.data])
  const plan = staged.data?.plan ?? null
  const closed = plan?.state === 'closed'
  const items = useMemo(() => staged.data?.items ?? [], [staged.data])
  const batches = useMemo(() => staged.data?.batches ?? [], [staged.data])
  const emptied = useMemo(() => staged.data?.emptied ?? [], [staged.data])
  const batchById = useMemo(() => new Map(batches.map(b => [b.id, b])), [batches])
  const runs = useMemo(() => staged.data?.runs ?? [], [staged.data])
  const unstage = useUnstage(plan?.id ?? null)
  const dispatch = useDispatch(plan?.id ?? null)
  const closePlan = useClosePlan()
  const views = useMemo(() => joinRuns(runs, jobList, plan?.id ?? null), [runs, jobList, plan?.id])
  // A just-dispatched job before the jobs list shows it (or its executor
  // records its run row — gcs's does that from inside Batch, once the VM is
  // up): shown under the buttons, and it keeps the page polling meanwhile.
  // A job that ended without recording one stops counting.
  const dispatched = dispatch.data?.job_id
  const dispatchedState = dispatched ? jobList.find(j => j.job_id === dispatched)?.state : undefined
  const pendingJob = dispatched && !views.some(v => v.job?.job_id === dispatched && v.run) && !['SUCCEEDED', 'FAILED', 'CANCELLED'].includes(dispatchedState ?? '') ? dispatched : null
  const pendingState = pendingJob ? dispatchedState : undefined
  const anyLive = !!pendingJob
    || views.some(v => viewLive(v) || v.ops.some(o => LIVE_STATES.has(o.state)) || (v.run && !v.run.finished_ts && !v.job))
  useEffect(() => setLive(anyLive), [anyLive])

  const runAction = useRunAction()
  const busy = unstage.isPending || dispatch.isPending || runAction.isPending || closePlan.isPending

  // The scan everything on the page is sized at — and the one a dispatch reads.
  const [scans, setScans] = useState<string[]>([])
  const [date, setDate] = useState('')
  useEffect(() => {
    void fetch(`${store.base}/scans.json`, { credentials: 'include' }).then(r => r.json()).then((s: string[]) => { setScans(s); setDate(s[0] ?? '') }).catch(() => {})
  }, [])
  const prefixes = useMemo(() => items.map(it => it.prefix), [items])
  const statsQ = usePrefixes(date, prefixes)
  const stats = statsQ.data
  const metaQ = useQuery<Meta>({
    queryKey: ['meta', store.key, date],
    queryFn: () => fetch(`${store.base}/${date}/meta.json`).then(r => r.json() as Promise<Meta>),
    enabled: !!date,
    staleTime: Infinity,
  })
  const userIdx = useMemo(() => buildUserIndex(metaQ.data?.users ?? []), [metaQ.data])
  const ownerIdx = useOwnerIndex(useOwners(!!store.owners).data)
  const error = unstage.error ?? dispatch.error ?? runAction.error ?? closePlan.error ?? staged.error ?? statsQ.error

  const rows: Row[] = useMemo(() => items.map(it => ({ ...it, name: it.prefix, to: `/${prefixToPath(it.prefix)}`, stat: stats?.[it.prefix] })), [items, stats])
  // The view lives in the URL, so a link carries it: `?q=hedy|grace&s=-b`
  // (filter, sort; `-` = descending, `-b` default) and `flat=1` (one table,
  // not one per batch — implied by a filter).
  const [qP, setQ] = useUrlState('q', stringParam())
  const [sP, setS] = useUrlState('s', stringParam())
  const [flatP, setFlat] = useUrlState('flat', boolParam)
  const q = qP ?? ''
  const sort = parseSort(sP) as { k: PrefixSortKey; asc: boolean }
  const onSort = (k: PrefixSortKey) => setS(encodeSort({ k, asc: sort.k === k ? !sort.asc : k === 'name' }))
  // What a row is found by: its prefix, its owners (the ledger's assignee, else
  // the scan's attribution — what the owner(s) column shows) and its stager.
  const filtered = useMemo(() => filterStaged(rows, q, r => {
    const cl = ownerIdx.count ? ownerIdx.claimOf(r.prefix) : null
    const owners = cl ? [cl.who] : r.stat ? ownerShares({ n: r.name, b: r.stat.b, o: r.stat.o, ...(r.stat.us ? { us: r.stat.us } : {}) }).map(([u]) => u) : []
    return { prefix: r.prefix, owners, stagedBy: r.added_by }
  }, searchName), [rows, q, ownerIdx])
  const shownRows = filtered.rows
  const flat = !!flatP || !!q.trim()

  // Items grouped by gesture (newest first); items staged before batches
  // existed fall into one "earlier" group. Each group sorts by the table's key.
  const groups: Group[] = useMemo(() => {
    if (flat) return [{ id: -1, rows: sortPrefixRows(shownRows, sort.k, sort.asc, r => r.added_ts) }]
    const byBatch = new Map<number | null, Row[]>()
    for (const r of shownRows) {
      if (!byBatch.has(r.batch_id)) byBatch.set(r.batch_id, [])
      byBatch.get(r.batch_id)!.push(r)
    }
    const known = new Map(batches.map(b => [b.id, b]))
    // Gestures whose prefixes all went (absorbed by a later ancestor, taken
    // back, or covered from the start): one folded line each, in time order.
    const gone: Group[] = emptied.map(e => ({ id: e.id, batch: e, rows: [], emptied: e }))
    return [...[...byBatch.entries()]
      .map(([id, rs]): Group => ({ id, batch: id != null ? known.get(id) : undefined, rows: sortPrefixRows(rs, sort.k, sort.asc, r => r.added_ts) })), ...gone]
      .sort((a, b) => (b.batch?.created_ts ?? 0) - (a.batch?.created_ts ?? 0))
  }, [shownRows, batches, emptied, sP, flat]) // eslint-disable-line react-hooks/exhaustive-deps

  // Collapsed batches and each batch's page.
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set())
  const [pages, setPages] = useState<Record<string, number>>({})
  const gkey = (g: Group) => String(g.id ?? 'none')
  const pageSize = flat ? FLAT_PAGE : PAGE
  const pageOf = (g: Group) => Math.min(pages[gkey(g)] ?? 0, Math.max(0, Math.ceil(g.rows.length / pageSize) - 1))
  // What's on screen, in order: the rows selection and j/k walk.
  const visible = useMemo(
    () => groups.flatMap(g => collapsed.has(gkey(g)) ? [] : g.rows.slice(pageOf(g) * pageSize, (pageOf(g) + 1) * pageSize)),
    [groups, collapsed, pages, pageSize], // eslint-disable-line react-hooks/exhaustive-deps
  )
  const sel = useRowSelection(visible, r => r.prefix)
  useRowSelectionKeys(sel, 'staged', 'Staged')
  const selected = items.filter(it => sel.selected.has(it.prefix)).map(it => it.prefix)
  const mine = (it: StagedItem) => !!ident && it.added_by === ident.email
  const canRemove = (it: StagedItem) => !closed && (admin || (canStage && mine(it)))
  const removable = selected.filter(p => { const it = items.find(i => i.prefix === p); return it ? canRemove(it) : false })

  const [armed, setArmed] = useState(false)
  useEffect(() => setArmed(false), [plan?.id, items.length])
  const [closeArmed, setCloseArmed] = useState(false)
  useEffect(() => setCloseArmed(false), [plan?.id])

  // A dispatch may be cut to some of the plan's buckets (`CAPS.bucketCut`):
  // unchecked buckets stay out of the run. The cut's prefixes are what its
  // digest names, so a real run needs a dry run of the same cut.
  const [bucketsOff, setBucketsOff] = useState<ReadonlySet<string>>(new Set())
  const perBucket = useMemo(() => {
    const m = new Map<string, { n: number; b: number; o: number }>()
    for (const r of rows) {
      const bkt = prefixToPath(r.prefix).split('/')[0]
      const e = m.get(bkt) ?? { n: 0, b: 0, o: 0 }
      e.n++
      e.b += r.stat?.b ?? 0
      e.o += r.stat?.o ?? 0
      m.set(bkt, e)
    }
    return m
  }, [rows])
  const planBuckets = [...perBucket.keys()].sort()
  const onBuckets = planBuckets.filter(b => !bucketsOff.has(b))
  const cut = CAPS.bucketCut && onBuckets.length < planBuckets.length ? onBuckets : undefined
  const cutItems = cut ? onBuckets.reduce((n, b) => n + perBucket.get(b)!.n, 0) : items.length
  // The executor holds a bucket's manifest in memory: a bucket with tens of
  // millions of planned objects needs the big machine (gcs only).
  const machine = CAPS.bucketCut ? machineFor(Math.max(0, ...onBuckets.map(b => perBucket.get(b)!.o))) : undefined

  const total = (rs: Row[]) => rs.reduce((t, r) => ({ b: t.b + (r.stat?.b ?? 0), o: t.o + (r.stat?.o ?? 0), gone: t.gone + (stats && !r.stat ? 1 : 0) }), { b: 0, o: 0, gone: 0 })
  const all = total(shownRows)
  const everything = total(rows)
  const selRows = rows.filter(r => sel.selected.has(r.prefix))
  const selTotal = total(selRows)

  const overlay = (t: TreeNode) => (ownerIdx.count ? applyLedger(t, ownerIdx, store.scheme) : t)
  const shownPrefixes = useMemo(() => shownRows.map(r => r.prefix), [shownRows])
  const tree = useMemo(() => (stats && shownPrefixes.length ? overlay(stagedTree(shownPrefixes, stats, 'staged')) : null), [shownPrefixes, stats, ownerIdx]) // eslint-disable-line react-hooks/exhaustive-deps
  const selKey = selected.join('\n')
  const selTree = useMemo(() => (stats && selected.length ? overlay(stagedTree(selected, stats, 'selected')) : null), [selKey, stats, ownerIdx]) // eslint-disable-line react-hooks/exhaustive-deps

  const sizesNote = !date ? null : statsQ.isLoading ? 'sizing…' : stats ? null : 'sizes unavailable'

  return (
    <main className="staged-page">
      <SiteNav />
      <div className="staged-head">
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
      {(planP != null || (plans.data?.length ?? 0) > 1) && (
        <label className="plan-pick">plan{' '}
          <select value={planP ?? ''} onChange={e => setPlanP(e.target.value ? Number(e.target.value) : null)} aria-label="plan">
            <option value="">the open plan (where staging lands)</option>
            {(plans.data ?? []).map(p => <option key={p.id} value={p.id}>#{p.id}{p.name !== 'Staged' ? ` “${p.name}”` : ''} · {p.state} · {p.items} {p.items === 1 ? 'item' : 'items'} · {p.runs} {p.runs === 1 ? 'run' : 'runs'}</option>)}
          </select>
          {closed && <span className="dim"> · closed: read-only, its runs (and their undo windows) stay here</span>}
        </label>
      )}
      {plan?.note && <p className="plan-note">{plan.note}</p>}

      {staged.isLoading ? <p className="loading">loading…</p> : !plan || !items.length ? (
        <p className="staged-empty">{plan && closed ? `Plan #${plan.id} is closed, with nothing left in it.` : 'Nothing is staged.'}</p>
      ) : (
        <>
          <div className="pp-head">
            <h2>
              {shownRows.length !== items.length && <>{shownRows.length} of </>}{items.length} {items.length === 1 ? 'prefix' : 'prefixes'}
              {stats && <> · {fmtBytes(all.b)}{shownRows.length !== items.length && <span className="dim"> of {fmtBytes(everything.b)}</span>} · {fmtN(all.o)} objects</>}
              <span className="dim"> · plan #{plan.id}{plan.name !== 'Staged' && <> “{plan.name}”</>} · {closed && plan.closed_ts ? <>closed {relAgo(plan.closed_ts)}</> : <>open since {relAgo(plan.created_ts).replace(/ ago$/, '')}</>}</span>
            </h2>
            <label className="scan-pick">sized at scan <select value={date} onChange={e => setDate(e.target.value)} aria-label="scan">{scans.map(s => <option key={s}>{s}</option>)}</select>
              {sizesNote && <span className="dim"> {sizesNote}</span>}
              {all.gone > 0 && <Tooltip content="Staged prefixes with nothing under them at this scan (already deleted, or never there)."><span className="dim"> · {all.gone} empty</span></Tooltip>}
            </label>
          </div>

          <div className={`staged-maps${selTree ? ' two' : ''}`}>
            <section className="staged-map">
              <h3>{q.trim() ? <>staged, matching “{q.trim()}”</> : 'everything staged'}</h3>
              <div className="map-box">
                {tree ? <Treemap key={`all:${date}`} root={tree} mode="user" userIdx={userIdx} dateRange={null} scheme={store.scheme} /> : <p className="dim">{sizesNote ?? 'nothing to draw'}</p>}
              </div>
            </section>
            {selTree && (
              <section className="staged-map">
                <h3>selected · {selected.length} · {fmtBytes(selTotal.b)}</h3>
                <div className="map-box">
                  <Treemap key={`sel:${date}:${selKey}`} root={selTree} mode="user" userIdx={userIdx} dateRange={null} scheme={store.scheme} />
                </div>
              </section>
            )}
          </div>

          <div className="staged-filter">
            <input type="search" value={q} onChange={e => setQ(e.target.value || undefined)} placeholder="filter: hedy|grace, isoflop -nemotron, owner:will, staged-by:david"
              aria-label="filter staged prefixes" className={filtered.error ? 'bad' : undefined} />
            {filtered.error && <span className="err">{filtered.error}</span>}
            <Tooltip content={q.trim() ? 'A filter shows one table across batches.' : 'One table per staging gesture (who, when, note), or one table of everything.'}>
              <label className="group-by"><input type="checkbox" checked={!flat} disabled={!!q.trim()} onChange={e => setFlat(!e.target.checked)} /> group by batch</label>
            </Tooltip>
          </div>

          <div className="staged-actions">
            <label className="sel-all"><input type="checkbox" checked={sel.pageAll} onChange={sel.togglePage} aria-label="select all shown" /> {sel.selected.size ? `${sel.selected.size} selected · ${fmtBytes(selTotal.b)}` : 'select'}</label>
            {sel.selected.size > 0 && <button type="button" onClick={sel.clear}>deselect</button>}
            {removable.length > 0 && (
              <button type="button" disabled={busy} onClick={() => unstage.mutate(removable, { onSuccess: () => sel.clear() })}>unstage {removable.length}</button>
            )}
            {!flat && <span className="fold-all">
              <button type="button" disabled={collapsed.size === 0} onClick={() => setCollapsed(new Set())} aria-label="expand all batches">▾ all</button>
              <button type="button" disabled={collapsed.size === groups.length} onClick={() => setCollapsed(new Set(groups.map(gkey)))} aria-label="collapse all batches">▸ all</button>
            </span>}
          </div>

          {groups.map(g => {
            const k = gkey(g)
            const open = !collapsed.has(k)
            const t = total(g.rows)
            const pg = pageOf(g)
            const np = Math.max(1, Math.ceil(g.rows.length / pageSize))
            const setPg = (p: number) => setPages(ps => ({ ...ps, [k]: p }))
            const shown = g.rows.slice(pg * pageSize, (pg + 1) * pageSize)
            if (g.emptied) return (
              <section key={k} className="stage-batch folded emptied">
                <div className="batch-head">
                  <span className="fold dim" aria-hidden>·</span>
                  <UserChip who={g.emptied.created_by} size={18} /> staged <Tooltip content={iso(g.emptied.created_ts)}><span>{relAgo(g.emptied.created_ts)}</span></Tooltip>
                  <span className="dim">· <EmptiedLine e={g.emptied} batches={batchById} /></span>
                  {g.emptied.note && <i className="memo">{g.emptied.note}</i>}
                </div>
              </section>
            )
            return (
              <section key={k} className={`stage-batch${open ? '' : ' folded'}`}>
                {g.id !== -1 && <div className="batch-head">
                  <button type="button" className="fold" aria-expanded={open} aria-label={open ? 'collapse batch' : 'expand batch'}
                    onClick={() => setCollapsed(c => { const n = new Set(c); if (open) n.add(k); else n.delete(k); return n })}>{open ? '▾' : '▸'}</button>
                  {g.batch
                    ? <><UserChip who={g.batch.created_by} size={18} /> staged <Tooltip content={iso(g.batch.created_ts)}><span>{relAgo(g.batch.created_ts)}</span></Tooltip></>
                    : <span className="dim">staged earlier</span>}
                  <span className="dim">· {g.rows.length} {g.rows.length === 1 ? 'prefix' : 'prefixes'}{stats && <> · {fmtBytes(t.b)}</>}</span>
                  {g.batch?.note && <i className="memo">{g.batch.note}</i>}
                </div>}
                {(open || g.id === -1) && (
                  <div className="staged-wrap">
                    <PrefixTable
                      rows={shown}
                      sort={sort}
                      onSort={onSort}
                      shareOf={all.b}
                      userIdx={userIdx}
                      ownerIdx={ownerIdx}
                      loading={!stats}
                      extra={[{
                        key: 'staged', label: 'staged', className: 'nb staged-by', sort: r => r.added_ts,
                        cell: r => <>{r.added_by !== g.batch?.created_by && <UserChip who={r.added_by} size={16} />}<TimeCell ts={r.added_ts} /></>,
                      }]}
                      lead={{
                        header: null,
                        cell: r => <input type="checkbox" checked={sel.selected.has(r.prefix)} onChange={() => { const i = visible.indexOf(r); sel.toggle(i); sel.commit() }} aria-label={`select ${r.prefix}`} />,
                      }}
                      rowProps={r => { const i = visible.indexOf(r); return { ref: sel.rowRef(i), ...sel.rowProps(i) } }}
                      trail={r => canRemove(r) && <button type="button" className="rm" title="unstage" disabled={busy} onClick={() => unstage.mutate([r.prefix])}>×</button>}
                    />
                    {np > 1 && (
                      <div className="pg">
                        <button type="button" disabled={pg === 0} onClick={() => setPg(0)} aria-label="first page">«</button>
                        <button type="button" disabled={pg === 0} onClick={() => setPg(pg - 1)} aria-label="previous page">‹</button>
                        <span>{pg * pageSize + 1}–{Math.min(g.rows.length, (pg + 1) * pageSize)} of {g.rows.length.toLocaleString('en-US')}</span>
                        <button type="button" disabled={pg >= np - 1} onClick={() => setPg(pg + 1)} aria-label="next page">›</button>
                        <button type="button" disabled={pg >= np - 1} onClick={() => setPg(np - 1)} aria-label="last page">»</button>
                      </div>
                    )}
                  </div>
                )}
              </section>
            )
          })}

          {admin && !closed && (
            <div className="dispatch" id="dispatch">
              <h3>Dispatch</h3>
              <label>scan <select value={date} onChange={e => setDate(e.target.value)}>{scans.map(s => <option key={s}>{s}</option>)}</select></label>
              {CAPS.bucketCut && planBuckets.length > 1 && (
                <details className="dispatch-buckets" open={!!cut}>
                  <summary className="dim">limit this run to some buckets{cut ? <> — <b>{onBuckets.length} of {planBuckets.length}</b> checked</> : ''}</summary>
                  {planBuckets.map(b => {
                    const e = perBucket.get(b)!
                    return (
                      <label key={b} className={bucketsOff.has(b) ? 'off' : undefined}>
                        <input type="checkbox" checked={!bucketsOff.has(b)} onChange={ev => { setArmed(false); setBucketsOff(s => { const n = new Set(s); if (ev.target.checked) n.delete(b); else n.add(b); return n }) }} />
                        {' '}<code>{b}</code> <span className="dim">{e.n} {e.n === 1 ? 'prefix' : 'prefixes'}{stats && <> · {fmtBytes(e.b)}</>}</span>
                      </label>
                    )
                  })}
                  {cut && <p className="dim">Only the checked buckets are listed and swept; a real run needs a dry run of the same cut.</p>}
                </details>
              )}
              <div className="dispatch-btns">
                <button type="button" className="dry" disabled={busy || !date || !onBuckets.length} onClick={() => dispatch.mutate({ mode: 'dry', date, buckets: cut, machine })}>dispatch dry-run</button>
                {!armed
                  ? <button type="button" className="danger" disabled={busy || !date || !onBuckets.length} onClick={() => setArmed(true)}>real delete…</button>
                  : <>
                      <button type="button" className="danger armed" disabled={busy} onClick={() => dispatch.mutate({ mode: 'real', date, buckets: cut, machine }, { onSuccess: () => setArmed(false) })}>
                        confirm REAL delete of {cutItems} {cutItems === 1 ? 'prefix' : 'prefixes'}{cut && <> ({onBuckets.length} of {planBuckets.length} buckets)</>}
                      </button>
                      <button type="button" onClick={() => setArmed(false)}>cancel</button>
                    </>}
              </div>
              {pendingJob && (
                <p className="dispatch-pending">
                  Dispatched <code>{pendingJob}</code> · {(pendingState ?? 'submitted').toLowerCase()}. Its run appears below once the job starts (a few minutes while Batch brings up the VM).
                </p>
              )}
              <p className="dispatch-note dim">A run reads the scan you pick and deletes only what it listed; new objects since are left alone.{machine && <> Runs on <code>{machine}</code>.</>}</p>
            </div>
          )}
        </>
      )}

      {admin && plan && !closed && (
        <div className="close-plan">
          {!closeArmed
            ? <Tooltip content="Close this plan: its items stay with it (read-only, under the plan picker, with its runs), and the next trash gesture opens a fresh plan.">
                <button type="button" disabled={busy} onClick={() => setCloseArmed(true)}>close plan…</button>
              </Tooltip>
            : <>
                <button type="button" className="danger armed" disabled={busy} onClick={() => closePlan.mutate(plan.id, { onSuccess: () => { setCloseArmed(false); setPlanP(plan.id) } })}>confirm: close plan #{plan.id}</button>
                <button type="button" onClick={() => setCloseArmed(false)}>cancel</button>
              </>}
        </div>
      )}

      {plan && (
        <RunsSection
          planId={plan.id}
          runs={runs}
          jobs={jobList}
          configured={jobs.data?.configured ?? true}
          jobsError={jobs.error}
          admin={admin}
          busy={busy}
          act={a => runAction.mutate(a)}
          fmtBytes={fmtBytes}
          refreshing={staged.isFetching || jobs.isFetching}
        />
      )}
      <SiteKbd />
    </main>
  )
}
