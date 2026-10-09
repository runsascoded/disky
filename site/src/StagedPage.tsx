// /staged — the opt-in deletion console (specs/staged-delete.md; the OA build
// plan `sweep-plan-union.md` checkpoint 4). Trash gestures on the map's table
// stage prefixes into one shared set (an open plan, internally — plans are
// bookkeeping, never shown); this page shows that set — a
// treemap of everything staged (and of the selection), then each gesture's
// batch with who/when/memo, its items sized at a scan — lets a stager take
// their own back, and lets an admin dry-run or really dispatch it to the
// deployment's executor (`EXEC_API`, `Store.executor`: cw's plan-first Batch
// bridge, or gcs's sweep bridge). Non-admins see everything read-only.
// Nothing is deleted by inaction: no deadline, no auto-sweep.
//
// Below it, every run (`StagedRuns.tsx`, specs/staged-runs.md), whatever it
// was dispatched against: live progress, totals, undo windows, files and
// logs, and the run controls the executor offers (`CAPS`); `?run=<id>` opens
// one (the Slack thread links there). A gesture whose prefixes all went (absorbed by a later
// ancestor, taken back) folds to one line.
import { Fragment, type ReactNode, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { boolParam, stringParam, useUrlState } from 'use-prms'
import { SiteNav } from './SiteNav'
import { SiteKbd } from './SiteKbd'
import { Tooltip } from './Tooltip'
import { Treemap } from './Treemap'
import { UserChip, canonId, shortName } from './UserChip'
import { ownerShares } from './OwnerBar'
import { encodeSort, filterStaged, parseSort } from './stagedFilter'
import { PrefixTable, TimeCell } from './PrefixTable'
import { useUnits } from './units'
import { fmtBytesPrecise, fmtN, type Meta, type TreeNode } from './types'
import { DEFAULT_STORE } from './stores'
import { buildUserIndex } from './colors'
import { useCanStage, useIdent } from './auth'
import { applyLedger } from './ledgerOverlay'
import { useOwnerIndex, useOwners } from './owners'
import { useRowSelection, useRowSelectionKeys } from './rowSelection'
import { CAPS, useDispatch, useExecJobs, usePrefixHistory, useRunAction, useRunFiles, useStagedPlan, useUnstage } from './plans'
import type { EmptiedBatch, StagedItem } from './plans'
import { joinRuns, LIVE_STATES, runFilesRel, viewBuckets, viewLive, type ProgressFile } from './runs'
import { PrefixRecovery } from './PrefixRecovery'
import { prefixExecution } from './prefixExecution'
import type { PrefixExecution } from './prefixExecution'
import { RunsSection } from './StagedRuns'
import { type PrefixSortKey, type PrefixStat, relAgo, sortPrefixRows, usePrefixes } from './prefixes'
import { batchCompletion, batchIsCollapsed, isCurrentlyStaged, stagedMapTree, stagedState, type StagedState } from './stagedCompletion'
import { batchAnchor } from './runHistory'
import { useLocation } from 'react-router-dom'
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

const stateLabel: Partial<Record<StagedState, string>> = { deleted: '✓ Deleted', empty: 'Empty at scan', recorded: 'Deletion recorded', running: 'Running' }

function PrefixStatus({ state, execution }: { state: StagedState; execution: PrefixExecution }) {
  const label = state === 'restored' && execution?.kind === 'recorded' ? execution.undone >= execution.objects ? 'Restored' : 'Partly restored' : stateLabel[state]
  return label ? <span className={`staged-status ${state}`}>{label}</span> : null
}

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
        {n(a.n)} absorbed into <a href={`#${batchAnchor(a.into)}`}>{into ? <>{shortName(into.created_by)}’s batch <Tooltip content={iso(into.created_ts)}><span>{relAgo(into.created_ts)}</span></Tooltip></> : <>batch #{a.into}</>}</a>
      </span>
    )
  })
  if (e.unstaged) parts.push(<span key="u">{n(e.unstaged)} unstaged</span>)
  if (e.covered) parts.push(<span key="c">{n(e.covered)} already covered by a staged ancestor</span>)
  if (!parts.length) parts.push(<span key="none">nothing new (already staged)</span>)
  return <span className="emptied-fate">{parts.map((p, i) => <Fragment key={i}>{i > 0 && ' · '}{p}</Fragment>)}</span>
}

export function StagedPage() {
  const { fmtBytes, units, suffixB } = useUnits()
  const ident = useIdent()
  const admin = useIsAdmin()
  const canStage = useCanStage()

  const [live, setLive] = useState(false)
  const staged = useStagedPlan(live, null)
  const jobs = useExecJobs(live)
  const jobList = useMemo(() => jobs.data?.jobs ?? [], [jobs.data])
  const plan = staged.data?.plan ?? null
  const items = useMemo(() => staged.data?.items ?? [], [staged.data])
  const batches = useMemo(() => staged.data?.batches ?? [], [staged.data])
  const emptied = useMemo(() => staged.data?.emptied ?? [], [staged.data])
  const batchById = useMemo(() => new Map(batches.map(b => [b.id, b])), [batches])
  const runs = useMemo(() => staged.data?.runs ?? [], [staged.data])
  const unstage = useUnstage(plan?.id ?? null)
  const dispatch = useDispatch(plan?.id ?? null)
  const views = useMemo(() => joinRuns(runs, jobList), [runs, jobList])
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
  const history = usePrefixHistory(plan?.id ?? null, anyLive)
  const progressTargets = views.filter(v => v.run?.mode === 'real' && v.run.plan_id === plan?.id && !v.run.finished_ts && (!v.job || viewLive(v)) && v.run.log_dir)
    .flatMap(v => viewBuckets(v).map(bucket => ({ bucket, run: v.run!, rel: `${runFilesRel(v.run!.log_dir!)}progress/${bucket}.json` })))
  const progressQs = useRunFiles<ProgressFile>(progressTargets.map(t => t.rel), anyLive)
  const rowExecution = (r: Row) => {
    const bucket = prefixToPath(r.prefix).split('/')[0]
    const ix = progressTargets.findIndex(t => t.bucket === bucket && t.run.started_ts >= r.added_ts)
    const progress = ix >= 0 ? progressQs[ix]?.data : null
    return prefixExecution(r.prefix, history.data?.bands ?? [], progress ? { runId: progressTargets[ix].run.run_id, progress } : undefined, Date.now() / 1000)
  }

  const runAction = useRunAction()
  const busy = unstage.isPending || dispatch.isPending || runAction.isPending

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
  const error = unstage.error ?? dispatch.error ?? runAction.error ?? staged.error ?? statsQ.error ?? history.error

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
    const cl = ownerIdx.count ? ownerIdx.assignmentOf(r.prefix) : null
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

  const outcomes = new Map(rows.map(r => {
    const execution = rowExecution(r)
    return [r.prefix, { execution, state: stagedState(r.stat, !!stats, execution) }]
  }))
  const completion = new Map(groups.map(g => [String(g.id ?? 'none'), batchCompletion(g.rows.map(r => outcomes.get(r.prefix)!.state))]))
  const activeRows = rows.filter(r => isCurrentlyStaged(outcomes.get(r.prefix)!.state))
  const activePrefixes = new Set(activeRows.map(r => r.prefix))
  // Defaults follow completion, while explicit open/closed choices survive polling.
  const [folds, setFolds] = useState<Record<string, boolean>>({})
  const [pages, setPages] = useState<Record<string, number>>({})
  const { hash } = useLocation()
  useEffect(() => {
    if (!/^#batch-\d+$/.test(hash)) return
    setFlat(false)
    setQ(undefined)
    const key = hash.slice('#batch-'.length)
    setFolds(f => ({ ...f, [key]: false }))
    requestAnimationFrame(() => document.getElementById(hash.slice(1))?.scrollIntoView({ block: 'start' }))
  }, [hash, batches.length, setFlat, setQ])
  const gkey = (g: Group) => String(g.id ?? 'none')
  const defaultFoldKey = groups.map(g => `${gkey(g)}:${completion.get(gkey(g))!.settled}`).join(',')
  const collapsed = useMemo(() => new Set(groups.filter(g => batchIsCollapsed(completion.get(gkey(g))!.settled, folds[gkey(g)], hash === `#${batchAnchor(g.id)}`, flat)).map(gkey)), [groups, defaultFoldKey, folds, hash, flat]) // eslint-disable-line react-hooks/exhaustive-deps
  const foldable = groups.filter(g => !g.emptied)
  const pageSize = flat ? FLAT_PAGE : PAGE
  const pageOf = (g: Group) => Math.min(pages[gkey(g)] ?? 0, Math.max(0, Math.ceil(g.rows.length / pageSize) - 1))
  // What's on screen, in order: the rows selection and j/k walk.
  const visible = useMemo(
    () => groups.flatMap(g => collapsed.has(gkey(g)) ? [] : g.rows.slice(pageOf(g) * pageSize, (pageOf(g) + 1) * pageSize).filter(r => activePrefixes.has(r.prefix))),
    [groups, collapsed, pages, pageSize, activePrefixes], // eslint-disable-line react-hooks/exhaustive-deps
  )
  const sel = useRowSelection(visible, r => r.prefix)
  useRowSelectionKeys(sel, 'staged', 'Staged')
  const selected = activeRows.filter(it => sel.selected.has(it.prefix)).map(it => it.prefix)
  const mine = (it: StagedItem) => !!ident && it.added_by === ident.email
  const canRemove = (it: StagedItem) => admin || (canStage && mine(it))
  const removable = selected.filter(p => { const it = items.find(i => i.prefix === p); return it ? canRemove(it) : false })

  const [armed, setArmed] = useState(false)
  useEffect(() => setArmed(false), [plan?.id, items.length])

  // A dispatch may be cut to some of the plan's buckets (`CAPS.bucketCut`):
  // unchecked buckets stay out of the run. The cut's prefixes are what its
  // digest names, so a real run needs a dry run of the same cut.
  const [bucketsOff, setBucketsOff] = useState<ReadonlySet<string>>(new Set())
  const perBucket = useMemo(() => {
    const m = new Map<string, { n: number; b: number; o: number }>()
    for (const r of activeRows) {
      const bkt = prefixToPath(r.prefix).split('/')[0]
      const e = m.get(bkt) ?? { n: 0, b: 0, o: 0 }
      e.n++
      e.b += r.stat?.b ?? 0
      e.o += r.stat?.o ?? 0
      m.set(bkt, e)
    }
    return m
  }, [activeRows])
  const planBuckets = [...perBucket.keys()].sort()
  const onBuckets = planBuckets.filter(b => !bucketsOff.has(b))
  const cut = CAPS.bucketCut && onBuckets.length < planBuckets.length ? onBuckets : undefined
  const cutItems = cut ? onBuckets.reduce((n, b) => n + perBucket.get(b)!.n, 0) : activeRows.length
  // The executor holds a bucket's manifest in memory: a bucket with tens of
  // millions of planned objects needs the big machine (gcs only).
  const machine = CAPS.bucketCut ? machineFor(Math.max(0, ...onBuckets.map(b => perBucket.get(b)!.o))) : undefined

  const total = (rs: Row[]) => rs.reduce((t, r) => ({ b: t.b + (r.stat?.b ?? 0), o: t.o + (r.stat?.o ?? 0), gone: t.gone + (stats && !r.stat ? 1 : 0) }), { b: 0, o: 0, gone: 0 })
  const activeShownRows = shownRows.filter(r => activePrefixes.has(r.prefix))
  const all = total(activeShownRows)
  const everything = total(activeRows)
  const selRows = rows.filter(r => sel.selected.has(r.prefix))
  const selTotal = total(selRows)

  const overlay = (t: TreeNode) => (ownerIdx.count ? applyLedger(t, ownerIdx, store.scheme) : t)
  const shownPrefixes = useMemo(() => activeShownRows.map(r => r.prefix), [activeShownRows])
  const tree = useMemo(() => {
    const t = stats && shownPrefixes.length ? stagedMapTree(shownPrefixes, stats, 'staged') : null
    return t ? overlay(t) : null
  }, [shownPrefixes, stats, ownerIdx]) // eslint-disable-line react-hooks/exhaustive-deps
  const selKey = selected.join('\n')
  const selTree = useMemo(() => {
    const t = stats && selected.length ? stagedMapTree(selected, stats, 'selected') : null
    return t ? overlay(t) : null
  }, [selKey, stats, ownerIdx]) // eslint-disable-line react-hooks/exhaustive-deps

  const sizesNote = !date ? null : statsQ.isLoading ? 'sizing…' : stats ? null : 'sizes unavailable'
  const loadingContents = items.length > 0 && (!date || statsQ.isLoading || history.isLoading || progressQs.some(query => query.isLoading))

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
        Files are not deleted until an admin dispatches a deletion job. Stage prefixes
        from a directory’s table (the trash icon); a memo travels with each gesture.
        {admin ? ' Dry-run first to see what a real run would delete; a real run deletes recoverably.' : ' An admin reviews and dispatches from here.'}
      </p>
      {error && <p className="staged-err" role="alert">{error.message}</p>}
      {plan?.note && <p className="plan-note">{plan.note}</p>}

      {staged.isLoading || loadingContents ? <>
        <p className="loading" role="status">Loading staged paths…</p>
      </> : !plan || (!items.length && !groups.length) ? (
        <p className="staged-empty">Nothing is staged.</p>
      ) : (
        <>
          <div className="pp-head">
            <h2>
              {activeShownRows.length !== activeRows.length && <>{activeShownRows.length} of </>}{activeRows.length} staged {activeRows.length === 1 ? 'path' : 'paths'}
              {stats && <> · {fmtBytes(everything.b)} · {fmtN(everything.o)} objects</>}
            </h2>
            <label className="scan-pick">sized at scan <select value={date} onChange={e => setDate(e.target.value)} aria-label="scan">{scans.map(s => <option key={s}>{s}</option>)}</select>
              {sizesNote && <span className="dim"> {sizesNote}</span>}
              {all.gone > 0 && <Tooltip content="Staged prefixes with nothing under them at this scan (already deleted, or never there)."><span className="dim"> · {all.gone} empty</span></Tooltip>}
            </label>
          </div>

          {tree && <p className="dim staged-sizing-note">Map and size columns are snapshots at the selected scan, not live remaining totals. Execution / recovery updates while a run is active; older jobs report bucket progress only, with exact prefix totals recorded when the run ends.</p>}

          {tree ? <div className={`staged-maps${selTree ? ' two' : ''}`}>
            <section className="staged-map">
              <h3>{q.trim() ? <>staged, matching “{q.trim()}”</> : 'everything staged'}</h3>
              <div className="map-box">
                <Treemap key={`all:${date}`} root={tree} mode="user" userIdx={userIdx} dateRange={null} scheme={store.scheme} />
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
          </div> : stats ? <section className="staged-no-map" aria-live="polite">
            <h3>{activeShownRows.length ? 'No sized rectangles to draw' : 'Nothing is staged.'}</h3>
            {activeShownRows.length > 0 && <p>{fmtN(all.o)} zero-byte objects at scan {date}.</p>}
          </section> : <p className="dim">{sizesNote ?? 'Sizing staged prefixes…'}</p>}

          <div className="staged-filter">
            <input type="search" value={q} onChange={e => setQ(e.target.value || undefined)} placeholder={activeRows.length ? 'filter: hedy|grace, isoflop -nemotron, owner:will, staged-by:david' : 'filter batch history…'}
              aria-label="filter staged prefixes" className={filtered.error ? 'bad' : undefined} />
            {filtered.error && <span className="err">{filtered.error}</span>}
            <Tooltip content={q.trim() ? 'A filter shows one table across batches.' : 'One table per staging gesture (who, when, note), or one table of everything.'}>
              <label className="group-by"><input type="checkbox" checked={!flat} disabled={!!q.trim()} onChange={e => setFlat(!e.target.checked)} /> group by batch</label>
            </Tooltip>
          </div>

          <div className="staged-actions">
            {activeShownRows.length > 0 && <label className="sel-all"><input type="checkbox" checked={sel.pageAll} onChange={sel.togglePage} aria-label="select all shown" /> {sel.selected.size ? `${sel.selected.size} selected · ${fmtBytes(selTotal.b)}` : 'select'}</label>}
            {sel.selected.size > 0 && <button type="button" onClick={sel.clear}>deselect</button>}
            {removable.length > 0 && (
              <button type="button" disabled={busy} onClick={() => unstage.mutate(removable, { onSuccess: () => sel.clear() })}>unstage {removable.length}</button>
            )}
            {!flat && <span className="fold-all">
              <button type="button" disabled={foldable.every(g => !collapsed.has(gkey(g)))} onClick={() => setFolds(Object.fromEntries(groups.map(g => [gkey(g), false])))} aria-label="expand all batches">▾ all</button>
              <button type="button" disabled={foldable.every(g => collapsed.has(gkey(g)))} onClick={() => setFolds(Object.fromEntries(groups.map(g => [gkey(g), true])))} aria-label="collapse all batches">▸ all</button>
            </span>}
          </div>

          {groups.map(g => {
            const k = gkey(g)
            const open = !collapsed.has(k)
            const status = completion.get(k)!
            const t = total(g.rows)
            const pg = pageOf(g)
            const np = Math.max(1, Math.ceil(g.rows.length / pageSize))
            const setPg = (p: number) => setPages(ps => ({ ...ps, [k]: p }))
            const shown = g.rows.slice(pg * pageSize, (pg + 1) * pageSize)
            if (g.emptied) return (
              <section key={k} id={batchAnchor(g.id)} className="stage-batch folded emptied">
                <div className="batch-head">
                  <span className="fold dim" aria-hidden>·</span>
                  <UserChip who={g.emptied.created_by} size={18} /> staged <Tooltip content={iso(g.emptied.created_ts)}><span>{relAgo(g.emptied.created_ts)}</span></Tooltip>
                  <span className="dim">· <EmptiedLine e={g.emptied} batches={batchById} /></span>
                  {g.emptied.note && <i className="memo">{g.emptied.note}</i>}
                </div>
              </section>
            )
            return (
              <section key={k} id={batchAnchor(g.id)} className={`stage-batch${open ? '' : ' folded'}${status.settled && status.deleted > 0 ? ' completed' : ''}`}>
                {g.id !== -1 && <div className="batch-head">
                  <button type="button" className="fold" aria-expanded={open} aria-label={open ? 'collapse batch' : 'expand batch'}
                    onClick={() => setFolds(f => ({ ...f, [k]: open }))}>{open ? '▾' : '▸'}</button>
                  {(status.deleted > 0 || status.empty === g.rows.length) && <span className={`staged-status ${status.deleted > 0 ? 'deleted' : 'empty'}`}>{status.settled && status.deleted > 0 ? '✓ Deleted' : status.deleted > 0 ? `${status.deleted} deleted` : 'Empty at scan'}</span>}
                  {g.batch
                    ? <><UserChip who={g.batch.created_by} size={18} /> staged <Tooltip content={iso(g.batch.created_ts)}><span>{relAgo(g.batch.created_ts)}</span></Tooltip></>
                    : <span className="dim">staged earlier</span>}
                  <span className="dim">· {g.rows.length} {g.rows.length === 1 ? 'prefix' : 'prefixes'}{status.deleted > 0 && <> · {status.deleted} with logged deletions</>}{status.empty > 0 && <> · {status.empty} empty at scan</>}{stats && t.b > 0 && <> · {fmtBytes(t.b)} at scan</>}</span>
                  {g.id != null && <a className="batch-permalink" href={`#${batchAnchor(g.id)}`} aria-label={`Link to batch ${g.id}`}>#{g.id}</a>}
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
                        key: 'execution', label: 'execution / recovery', className: 'nb',
                        cell: r => <PrefixRecovery execution={outcomes.get(r.prefix)!.execution} fmtBytes={fmtBytes} />,
                      }, {
                        key: 'staged', label: 'staged', className: 'nb staged-by', sort: r => r.added_ts,
                        cell: r => <>{r.added_by !== g.batch?.created_by && <UserChip who={r.added_by} size={16} />}<TimeCell ts={r.added_ts} /></>,
                      }]}
                      namePrefix={r => <PrefixStatus {...outcomes.get(r.prefix)!} />}
                      lead={{
                        header: null,
                        cell: r => activePrefixes.has(r.prefix) ? <input type="checkbox" checked={sel.selected.has(r.prefix)} onChange={() => { const i = visible.indexOf(r); if (i >= 0) { sel.toggle(i); sel.commit() } }} aria-label={`select ${r.prefix}`} /> : null,
                      }}
                      rowProps={r => { const i = visible.indexOf(r); const state = outcomes.get(r.prefix)!.state; if (i < 0) return { className: `staged-${state}` }; const props = sel.rowProps(i); return { ref: sel.rowRef(i), ...props, className: `${props.className ?? ''} staged-${state}` } }}
                      trail={r => activePrefixes.has(r.prefix) && canRemove(r) && <Tooltip content="Unstage this prefix (does not stop a dispatched run)"><button type="button" className="rm" aria-label="Unstage prefix" disabled={busy} onClick={() => unstage.mutate([r.prefix])}>×</button></Tooltip>}
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

          {admin && activeRows.length > 0 && (
            <div className="dispatch" id="dispatch">
              <h3>Dispatch</h3>
              <label>scan <select value={date} onChange={e => setDate(e.target.value)}>{scans.map(s => <option key={s}>{s}</option>)}</select></label>
              {activeRows.length > 0 && CAPS.bucketCut && planBuckets.length > 1 && (
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
                <button type="button" className="dry" disabled={busy || !date || !activeRows.length || !onBuckets.length} onClick={() => dispatch.mutate({ mode: 'dry', date, buckets: cut, machine })}>dispatch dry-run</button>
                {!armed
                  ? <button type="button" className="danger" disabled={busy || !date || !activeRows.length || !onBuckets.length} onClick={() => setArmed(true)}>real delete…</button>
                  : <>
                      <button type="button" className="danger armed" disabled={busy} onClick={() => dispatch.mutate({ mode: 'real', date, buckets: cut, machine }, { onSuccess: () => setArmed(false) })}>
                        confirm REAL delete of {cutItems} {cutItems === 1 ? 'prefix' : 'prefixes'}{cut && <> ({onBuckets.length} of {planBuckets.length} buckets)</>}
                      </button>
                      <button type="button" onClick={() => setArmed(false)}>cancel</button>
                    </>}
              </div>
              {pendingJob && (
                <p className="dispatch-pending">
                  Dispatched <code>{pendingJob}</code> · {(pendingState ?? 'submitted').toLowerCase()}. Its run appears below {CAPS.startHint}.
                </p>
              )}
              <p className="dispatch-note dim">A run reads the scan you pick and deletes only what it listed; new objects since are left alone.{machine && <> Runs on <code>{machine}</code>.</>}</p>
            </div>
          )}
        </>
      )}

      <RunsSection
        runs={runs}
        jobs={jobList}
        configured={jobs.data?.configured ?? true}
        jobsError={jobs.error}
        admin={admin}
        busy={busy}
        act={a => runAction.mutate(a)}
        fmtBytes={b => fmtBytesPrecise(b, units, suffixB)}
        refreshing={staged.isFetching || jobs.isFetching}
      />
      <SiteKbd />
    </main>
  )
}
