import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import type { HistoryAction, HistoryData, HistoryJob, HistoryMeasuredStorage, HistoryPlan, HistoryPolicy } from '../functions/_lib/historyAdmin'
import { Busy, Loading } from './Busy'
import { isScanId } from './scanSlug'
import { useUnits } from './units'
import './HistoryAdmin.css'

const ENDPOINT = '/api/admin/history'
const KEY = ['admin', 'history'] as const
const CONFIGURED_KEY = ['admin', 'history-configured'] as const
const ADOPT_ENDPOINT = '/api/admin/history-adopt'
const ADOPT_KEY = ['admin', 'history-pointer'] as const
const ACTIVE = new Set(['queued', 'running', 'planning', 'building', 'verifying'])
const num = (n: number): string => n.toLocaleString()
const date = (t: string): string => new Date(t).toLocaleString()
interface AdoptionData { enabled: boolean; reason?: string; pointer: { version: string; etag: string } | null }
export interface HistoryAdminProps {
  onPlanChange?: (plan: HistoryPlan | null) => void
  manualScans?: string[]
  onManualScansChange?: (ids: string[]) => void
  spanScans?: string[]
  onHoverScan?: (id: string | null) => void
  onSelectionModeChange?: (manual: boolean) => void
  onSourceChange?: (scans: string[], pins: string[]) => void
  jobsTarget?: HTMLElement | null
}
export interface HistoryDraft {
  kind: 'off' | HistoryPolicy['kind']
  dropCount: number
  recentDays: number
  growth: number
  samples: number
}
export const DEFAULT_HISTORY_DRAFT: HistoryDraft = { kind: 'off', dropCount: 1, recentDays: 7, growth: 2, samples: 1 }

/** Off is a local draft state, never a server policy or a dispatched job. */
export function historyPolicyForPreview(draft: HistoryDraft, manualScans: string[] = []): HistoryPolicy | null {
  if (draft.kind === 'off') return null
  if (draft.kind === 'manual') {
    if (!manualScans.length || manualScans.length > 10000 || !manualScans.every(isScanId)) return null
    return { kind: 'manual', remove: [...new Set(manualScans)].sort() }
  }
  if (draft.kind === 'low-change') return Number.isSafeInteger(draft.dropCount) && draft.dropCount >= 1 && draft.dropCount <= 10000
    ? { kind: 'low-change', drop_count: draft.dropCount } : null
  return Number.isFinite(draft.recentDays) && draft.recentDays > 0 && Number.isSafeInteger(draft.recentDays * 86400) && draft.recentDays <= 3660
    && Number.isFinite(draft.growth) && draft.growth > 1 && draft.growth <= 100 && Number.isSafeInteger(draft.samples) && draft.samples >= 1 && draft.samples <= 100
    ? { kind: 'geometric', recent_seconds: draft.recentDays * 86400, growth: draft.growth, samples_per_band: draft.samples } : null
}

export function geometricDescription(draft: Pick<HistoryDraft, 'recentDays' | 'growth' | 'samples'>): string {
  if (![draft.recentDays, draft.growth, draft.samples].every(Number.isFinite)) return 'Choose the recent window, age-band growth and samples per band below.'
  return `Keep every scan from the newest ${num(draft.recentDays)} days; grow older age bands by ${num(draft.growth)}×; keep ${num(draft.samples)} ${draft.samples === 1 ? 'sample' : 'samples'} per band.`
}

export function removedHistoryScans(plan: HistoryPlan): string[] {
  const retained = new Set(plan.retained)
  return plan.scans.filter(scan => !retained.has(scan))
}

/** The complete proposal stays reviewable after its worker has finished. */
export function HistoryPlanPreview({ plan, onHoverScan }: { plan: HistoryPlan; onHoverScan?: (id: string | null) => void }) {
  const { fmtBytes } = useUnits()
  const removed = removedHistoryScans(plan), kept = new Set(plan.retained), pinned = new Set([...(plan.pinned ?? []), ...(plan.required_pins ?? [])])
  const hover = (scan: string) => ({ onMouseEnter: () => onHoverScan?.(scan), onMouseLeave: () => onHoverScan?.(null), onFocus: () => onHoverScan?.(scan), onBlur: () => onHoverScan?.(null) })
  return (
    <div className="history-preview">
      <p className="history-counts"><strong>{num(plan.retained.length)} retained</strong><span>{num(removed.length)} removed</span><span>{num(plan.scans.length)} scans reviewed</span></p>
      <p className="hint">Retained scans keep exact snapshot contents. Removing a scan reduces the timing resolution of changes and can hide changes that reversed before the next retained scan.</p>
      <p className="hint">Churn: {plan.churn === 'retained-snapshots' ? 'recalculated from retained snapshots. Changes that fully reversed between retained dates are excluded.' : plan.churn ?? 'the candidate records whether churn uses retained observations or the complete change history.'}</p>
      {plan.estimate && <div className="history-estimate">
        <h4>Estimated canonical interval storage</h4>
        <p><strong>About {fmtBytes(plan.estimate.estimated_saved_bytes)} less</strong>: {fmtBytes(plan.estimate.source_bytes)} → approximately {fmtBytes(plan.estimate.estimated_retained_bytes)}.</p>
        <p>{num(plan.estimate.source_rows)} → {num(plan.estimate.retained_rows)} canonical rows. Projected row counts are exact; compressed byte savings are approximate.</p>
        <p className="hint">This is an estimate for the selected set together. Savings from individual removals are not additive.</p>
        <p className="hint">This covers only the interval store’s folded canonical tables. It excludes search indexes, served sorts, sidecars, churn splits, mirrors, old generations and metadata. Changed payload bytes are a separate metric.</p>
        <p className="hint">A candidate adds storage. Source generations remain until an explicit writer-lineage change and garbage collection; pinning a reader version does not reclaim them.</p>
      </div>}
      {plan.steps?.length ? <div className="history-table-scroll"><table className="history-removals">
        <thead><tr><th>Removed scan</th><th>Previous retained</th><th>Next retained</th><th>Changes before</th><th>Changes after</th><th>Score (max)</th><th>New delta</th></tr></thead>
        <tbody>{plan.steps.map(step => <tr key={step.scan} tabIndex={0} {...hover(step.scan)}>
          <td><code>{step.scan}</code></td><td><code>{step.before}</code></td><td><code>{step.after}</code></td>
          <td>{num(step.left.count)}</td><td>{num(step.right.count)}</td><td>{num(step.score)}</td><td>{num(step.merged.count)}</td>
        </tr>)}</tbody>
      </table></div> : null}
      <details><summary>Scan selection</summary><div className="history-table-scroll"><table>
        <thead><tr><th>Scan</th><th>Decision</th></tr></thead>
        <tbody>{plan.scans.map(scan => <tr key={scan} tabIndex={0} {...hover(scan)}><td><code>{scan}</code></td><td>{kept.has(scan) ? pinned.has(scan) ? 'Retained · pinned' : 'Retained' : 'Remove'}</td></tr>)}</tbody>
      </table></div></details>
      {!!plan.candidates?.length && <details><summary>Adjacent change scores for all scans</summary><div className="history-table-scroll"><table>
        <thead><tr><th>Scan</th><th>Changes before</th><th>Changes after</th><th>Score (max)</th><th>Protection</th></tr></thead>
        <tbody>{plan.candidates.map(candidate => <tr key={candidate.scan} tabIndex={0} {...hover(candidate.scan)}>
          <td><code>{candidate.scan}</code></td><td>{candidate.left ? num(candidate.left.count) : '—'}</td><td>{candidate.right ? num(candidate.right.count) : '—'}</td><td>{candidate.score === null ? '—' : num(candidate.score)}</td><td>{candidate.reasons.join(', ') || 'Eligible'}</td>
        </tr>)}</tbody>
      </table></div></details>}
      <details><summary>Source generations and plan fingerprint</summary><pre>{JSON.stringify({ digest: plan.digest, source: plan.source }, null, 2)}</pre></details>
    </div>
  )
}

function MeasuredStorage({ storage }: { storage: HistoryMeasuredStorage }) {
  const { fmtBytes } = useUnits()
  return <div className="history-estimate">
    <h4>Measured generation storage</h4>
    <div className="history-table-scroll"><table><thead><tr><th>Store copy</th><th>Source generation</th><th>New generation</th></tr></thead><tbody>
      {Object.entries(storage.copies).map(([name, copy]) => <tr key={name}><td>{name.replaceAll('_', ' ')}</td><td>{fmtBytes(copy.source_bytes)}</td><td>{fmtBytes(copy.candidate_bytes)}</td></tr>)}
    </tbody></table></div>
    <p>Additional storage before GC: <strong>{fmtBytes(storage.additional_generation_bytes_before_gc)}</strong>.</p>
    <p>{storage.net_reduction_after_source_gc_bytes >= 0 ? `Potential net reduction after source GC: ${fmtBytes(storage.net_reduction_after_source_gc_bytes)}.` : `The new generation is ${fmtBytes(-storage.net_reduction_after_source_gc_bytes)} larger than its sources.`}</p>
    <p className="hint">{storage.reclaim_condition} Reader adoption leaves the source generations in place.</p>
  </div>
}

function Job({ job, build, selectVersion, showPlan, onHoverScan, disabled }: { job: HistoryJob; build: (id: string) => void; selectVersion?: (id: string) => void; showPlan?: (plan: HistoryPlan) => void; onHoverScan?: (id: string | null) => void; disabled: boolean }) {
  const status = job.status, state = status?.state ?? 'queued', active = ACTIVE.has(state)
  const candidateId = status?.candidate?.version ?? status?.candidate?.id
  const plan = status?.plan ?? job.request.plan
  const storage = status?.candidate?.storage ?? status?.candidate?.descriptor?.storage
  return <details className="history-job" open={active || state === 'planned'}>
    <summary><span>{date(job.request.created_at)}</span><span>{job.request.action === 'build' ? 'Candidate build' : 'Preview'}</span><span className={`history-state ${state}`}>{active ? <Loading label={state} /> : state}</span></summary>
    <div className="history-job-body">
      <p className="hint"><code>{job.id}</code>{' · '}<a href={job.request.batch_url} target="_blank" rel="noreferrer">Batch job ↗</a></p>
      {status?.phase && <p>{status.phase}{status.note ? `: ${status.note}` : ''}</p>}
      {status?.error && <p className="err" role="alert">{status.error}</p>}
      {status?.batch_state && <p className="hint">Batch: {status.batch_state.toLowerCase()}</p>}
      {status?.batch_error && <p className="err" role="alert">{status.batch_error}. The worker’s last recorded state is shown above.</p>}
      {plan && <>
        {showPlan && <p><button type="button" onClick={() => showPlan(plan)}>Show proposal on scans</button></p>}
        <HistoryPlanPreview plan={plan} onHoverScan={onHoverScan} />
      </>}
      {state === 'planned' && status?.plan && <p className="history-build-action"><button type="button" disabled={disabled} onClick={() => build(job.id)}>Build immutable candidate</button><span className="hint">Serving generations stay in place. Dev and prod cutovers are separate actions.</span></p>}
      {storage && <MeasuredStorage storage={storage} />}
      {status?.candidate && <details><summary>Candidate generation details</summary><pre>{JSON.stringify(status.candidate, null, 2)}</pre></details>}
      {state === 'built' && typeof candidateId === 'string' && selectVersion && <p className="history-build-action"><button type="button" disabled={disabled} onClick={() => selectVersion(candidateId)}>Pin this environment to this version…</button></p>}
    </div>
  </details>
}

async function call<T>(url: string, init?: RequestInit): Promise<T> {
  const r = await fetch(url, { credentials: 'include', cache: 'no-store', ...init })
  const body = await r.json() as T & { error?: string }
  if (!r.ok) throw new Error(body.error ?? `History request failed (${r.status})`)
  return body
}

/** Whether this deployment uses scan history (any `HISTORY_*` var): admins see its controls only then. */
export function useHistoryConfigured(admin: boolean): boolean {
  const q = useQuery<{ configured: boolean }, Error>({ queryKey: CONFIGURED_KEY, queryFn: () => call(`${ENDPOINT}?configured`), enabled: admin, staleTime: Infinity })
  return admin && q.data?.configured === true
}

export function HistoryAdmin({ onPlanChange, manualScans, onManualScansChange, spanScans = [], onHoverScan, onSelectionModeChange, onSourceChange, jobsTarget }: HistoryAdminProps = {}) {
  const qc = useQueryClient()
  const { fmtBytes } = useUnits()
  const [draft, setDraft] = useState<HistoryDraft>(() => ({ ...DEFAULT_HISTORY_DRAFT }))
  const [localManual, setLocalManual] = useState<string[]>([])
  const [shownPlan, setShownPlan] = useState<HistoryPlan | null>(null)
  const selected = manualScans ?? localManual
  const { kind, dropCount, recentDays, growth, samples } = draft
  const [pinText, setPinText] = useState('')
  const [cursors, setCursors] = useState<string[]>([])
  const [selectedVersion, setSelectedVersion] = useState<string | null>(null)
  const [pendingPlanId, setPendingPlanId] = useState<string | null>(null)
  useEffect(() => { onSelectionModeChange?.(kind === 'manual') }, [kind, onSelectionModeChange])
  const setManual = (ids: string[]) => {
    const next = [...new Set(ids)].sort()
    setLocalManual(next)
    onManualScansChange?.(next)
  }
  const showPlan = (plan: HistoryPlan | null) => { setShownPlan(plan); onPlanChange?.(plan); onHoverScan?.(null); if (!plan) setPendingPlanId(null) }
  const chooseKind = (next: HistoryDraft['kind']) => {
    setDraft(d => ({ ...d, kind: next }))
    showPlan(null)
    if (next === 'off') setManual([])
  }
  const cursor = cursors.at(-1)
  const q = useQuery<HistoryData, Error>({
    queryKey: [...KEY, cursor ?? ''], queryFn: () => call(`${ENDPOINT}${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`),
    refetchInterval: query => query.state.data?.jobs.some(job => ACTIVE.has(job.status?.state ?? 'queued')) ? 5000 : 30000,
  })
  useEffect(() => {
    if (q.data?.source_scans !== undefined) onSourceChange?.(q.data.source_scans, q.data.required_pins ?? [])
  }, [q.data?.source_scans, q.data?.required_pins, onSourceChange])
  const dispatch = useMutation<{ id: string }, Error, HistoryAction>({
    mutationFn: action => call(ENDPOINT, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(action) }),
    onSuccess: (result, action) => { setCursors([]); if (action.action === 'plan') { setPendingPlanId(result.id); setShownPlan(null); onPlanChange?.(null) } },
    onSettled: async () => { await qc.invalidateQueries({ queryKey: KEY }) },
  })
  useEffect(() => {
    if (!pendingPlanId || kind === 'off') return
    const job = q.data?.jobs.find(row => row.id === pendingPlanId)
    if (job?.status?.state === 'planned' && job.status.plan) {
      setShownPlan(job.status.plan)
      onPlanChange?.(job.status.plan)
      onHoverScan?.(null)
      setPendingPlanId(null)
    } else if (job?.status?.state === 'failed') setPendingPlanId(null)
  }, [pendingPlanId, kind, q.data, onPlanChange, onHoverScan])
  const adoption = useQuery<AdoptionData, Error>({ queryKey: ADOPT_KEY, queryFn: () => call(ADOPT_ENDPOINT) })
  const adopt = useMutation<unknown, Error, { version: string; expected_etag: string | null }>({
    mutationFn: body => call(ADOPT_ENDPOINT, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }),
    onSuccess: async () => { setSelectedVersion(null); await qc.invalidateQueries() },
    onError: async () => { await qc.invalidateQueries({ queryKey: ADOPT_KEY }) },
  })
  const pinned = [...new Set(pinText.split(/[\s,]+/).filter(Boolean))].sort()
  const invalidPin = pinned.find(scan => !isScanId(scan))
  const policy = historyPolicyForPreview(draft, selected)
  const busy = dispatch.isPending || adopt.isPending, enabled = q.data?.enabled === true
  const plan = () => {
    if (policy) dispatch.mutate({ action: 'plan', policy, pinned })
  }
  return <section className="history-admin" id="history">
    <h2>Scan history</h2>
    <p>Keep all scans, or choose a one-time proposal to review. Previews and candidate builds run only when requested. Each environment chooses when to use a new version.</p>
    {q.isPending && <Loading label="Loading history jobs…" />}
    {q.error && <p className="err" role="alert">{q.error.message}</p>}
      <form onSubmit={e => { e.preventDefault(); if (enabled && policy && !invalidPin && !busy) plan() }}>
        <fieldset disabled={busy}>
          <legend>Retention policy</legend>
          <label className="history-policy"><input type="radio" name="history-policy" checked={kind === 'off'} onChange={() => chooseKind('off')} /><span><strong>Off · keep all scans</strong><span>No automatic retention jobs. Existing scans are kept; previews and builds are separate manual actions.</span></span></label>
          <label className="history-policy"><input type="radio" name="history-policy" checked={kind === 'manual'} onChange={() => chooseKind('manual')} /><span><strong>Choose scans manually</strong><span>Select scans in the duration chart or runs table. {num(selected.length)} selected for removal.</span></span></label>
          {kind === 'manual' && <div className="history-settings">
            <p className="history-manual-actions"><strong>{num(selected.length)} scans selected</strong><button type="button" disabled={!selected.length} onClick={() => { setManual([]); showPlan(null) }}>Clear selection</button><button type="button" disabled={!spanScans.length} onClick={() => { setManual(spanScans); showPlan(null) }}>Use brushed scans ({num(spanScans.length)})</button></p>
            <p className="hint">Start with a narrow time range, then select individual quiet observations. Preview checks the exact neighboring changes and required pins before any build.</p>
          </div>}
          <label className="history-policy"><input type="radio" name="history-policy" checked={kind === 'low-change'} onChange={() => chooseKind('low-change')} /><span><strong>Lowest change</strong><span>Remove the middle of the quietest three consecutive scans; recalculate neighbors after each removal.</span></span></label>
          {kind === 'low-change' && <label className="history-setting">Scans to remove <input type="number" min="1" max="10000" step="1" value={Number.isFinite(dropCount) ? dropCount : ''} onChange={e => setDraft(d => ({ ...d, dropCount: e.target.valueAsNumber }))} /></label>}
          <label className="history-policy"><input type="radio" name="history-policy" checked={kind === 'geometric'} onChange={() => chooseKind('geometric')} /><span><strong>Geometric spacing</strong><span>{geometricDescription(draft)}</span></span></label>
          {kind === 'geometric' && <div className="history-settings">
            <label className="history-setting">Keep every scan from the newest <input type="number" min="0.0001" max="3660" step="any" value={Number.isFinite(recentDays) ? recentDays : ''} onChange={e => setDraft(d => ({ ...d, recentDays: e.target.valueAsNumber }))} /> days</label>
            <label className="history-setting">Age band growth <input type="number" min="1.01" max="100" step="any" value={Number.isFinite(growth) ? growth : ''} onChange={e => setDraft(d => ({ ...d, growth: e.target.valueAsNumber }))} /></label>
            <label className="history-setting">Samples per band <input type="number" min="1" max="100" step="1" value={Number.isFinite(samples) ? samples : ''} onChange={e => setDraft(d => ({ ...d, samples: e.target.valueAsNumber }))} /></label>
            <p className="hint">Ages are measured from the newest scan. Each band keeps the newest observation in each occupied time bin; required pins are retained in addition.</p>
          </div>}
          {kind !== 'off' && <>
            <label className="history-pins">Additional pinned scans<textarea rows={3} value={pinText} onChange={e => setPinText(e.target.value)} placeholder="2026-10-20T1202" spellCheck={false} /></label>
            <p className="hint">First and latest scans are always retained. Open staging reviews and active deletion or recovery work add required pins automatically.</p>
          </>}
          {kind !== 'off' && invalidPin && <p className="err">Use complete scan IDs, one per line. Invalid scan: {invalidPin}</p>}
          <p><button type="submit" disabled={!enabled || !policy || !!invalidPin}>{busy ? <Loading label="Dispatching…" /> : 'Preview retention'}</button></p>
          {!enabled && <p className="hint">{q.data?.reason ?? (q.isPending ? 'Checking job availability…' : 'Retention jobs are unavailable. You can explore the controls and manual selection.')}</p>}
        </fieldset>
      </form>
      {dispatch.error && <p className="err" role="alert">{dispatch.error.message}</p>}
      {shownPlan && <p className="history-proposal-active">
        <span>Proposal: <strong>{num(shownPlan.retained.length)} retained</strong>, {num(removedHistoryScans(shownPlan).length)} removed.
          {shownPlan.estimate && <> Approximately {fmtBytes(shownPlan.estimate.estimated_saved_bytes)} less in <strong>canonical interval tables only</strong>; full generation storage is measured after a build.</>}
        </span><button type="button" onClick={() => showPlan(null)}>Clear proposal</button>
      </p>}
      {adoption.isPending && <Loading label="Loading this environment’s history version…" />}
      {adoption.error && <p className="err" role="alert">{adoption.error.message}</p>}
      {adoption.data && <p className="hint">{adoption.data.enabled ? `This environment: ${adoption.data.pointer?.version ?? 'following the scan pipeline'}.` : adoption.data.reason}</p>}
      {selectedVersion && adoption.data?.enabled && <div className="history-adopt-confirm">
        <h3>Pin this environment?</h3>
        <p>Use <code>{selectedVersion}</code> on <strong>{window.location.host}</strong>. This serves a fixed history snapshot. Scheduled scan appends continue separately; they do not advance this pin.</p>
        <p className="hint">Current version: <code>{adoption.data.pointer?.version ?? 'no pin'}</code>. The change is refused if this pointer or required review pins change.</p>
        <button type="button" disabled={busy} onClick={() => adopt.mutate({ version: selectedVersion, expected_etag: adoption.data!.pointer?.etag ?? null })}>{adopt.isPending ? <Loading label="Pinning version…" /> : 'Pin this environment'}</button>{' '}
        <button type="button" disabled={busy} onClick={() => setSelectedVersion(null)}>Cancel</button>
      </div>}
      {adopt.error && <p className="err" role="alert">{adopt.error.message}</p>}
      {adopt.isSuccess && <p role="status">This environment is pinned to the selected history version.</p>}
    {q.data && (jobsTarget ? createPortal(<HistoryJobListing data={q.data} fetching={q.isFetching} enabled={enabled} busy={busy}
      refresh={() => { void q.refetch() }} build={id => dispatch.mutate({ action: 'build', plan_id: id })}
      showPlan={onPlanChange ? showPlan : undefined} onHoverScan={onHoverScan}
      selectVersion={adoption.data?.enabled ? version => { setSelectedVersion(version); adopt.reset() } : undefined}
      cursors={cursors} onNewer={() => setCursors(xs => xs.slice(0, -1))}
      onOlder={() => { if (q.data?.next_cursor) setCursors(xs => [...xs, q.data!.next_cursor!]) }} portaled />, jobsTarget)
      : <HistoryJobListing data={q.data} fetching={q.isFetching} enabled={enabled} busy={busy}
        refresh={() => { void q.refetch() }} build={id => dispatch.mutate({ action: 'build', plan_id: id })}
        showPlan={onPlanChange ? showPlan : undefined} onHoverScan={onHoverScan}
        selectVersion={adoption.data?.enabled ? version => { setSelectedVersion(version); adopt.reset() } : undefined}
        cursors={cursors} onNewer={() => setCursors(xs => xs.slice(0, -1))}
        onOlder={() => { if (q.data?.next_cursor) setCursors(xs => [...xs, q.data!.next_cursor!]) }} />)}
  </section>
}

function HistoryJobListing({ data, fetching, enabled, busy, refresh, build, showPlan, onHoverScan, selectVersion, cursors, onNewer, onOlder, portaled = false }: {
  data: HistoryData; fetching: boolean; enabled: boolean; busy: boolean; refresh: () => void
  build: (id: string) => void; showPlan?: (plan: HistoryPlan) => void; onHoverScan?: (id: string | null) => void
  selectVersion?: (id: string) => void; cursors: string[]; onNewer: () => void; onOlder: () => void; portaled?: boolean
}) {
  return <div className={portaled ? 'history-admin history-job-list' : undefined}>
    <div className="history-jobs busy-host" aria-busy={fetching}>
        <div className="history-jobs-heading"><h3>History jobs</h3><button type="button" disabled={fetching} onClick={refresh}>Refresh</button></div>
        {fetching && <Busy corner label="Refreshing history jobs…" />}
        {!data.jobs.length ? <p className="hint">No history jobs yet. Preview a retention policy to see which scans it would remove.</p>
          : data.jobs.map(job => <Job key={job.id} job={job} disabled={!enabled || busy} build={build}
            showPlan={showPlan} onHoverScan={onHoverScan} selectVersion={selectVersion} />)}
        {(cursors.length > 0 || data.next_cursor) && <nav className="history-pages" aria-label="History job pages">
          <button type="button" disabled={!cursors.length} onClick={onNewer}>Newer jobs</button>
          <button type="button" disabled={!data.next_cursor} onClick={onOlder}>Older jobs</button>
        </nav>}
      </div>
  </div>
}
