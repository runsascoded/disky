// "Cancel" on a running scan's page (admins): a confirm, then `POST /api/scan-runs/cancel`, which cancels the scan's
// head Batch job and every job its pipeline submitted, in every region, and marks the run cancelled (`_lib/scanCancel.ts`).
import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Tooltip } from './Tooltip'
import { useIsAdmin } from './auth'
import type { ScanRun } from './scanRunsModel'

type Outcome = 'cancelled' | 'already' | 'gone' | { error: string }
interface Cancelled { head: Outcome; tasks: Record<string, Outcome>; tasks_error?: string; recorded: 'cancelled' | 'failed' | null }

async function cancelScan(runId: string): Promise<Cancelled> {
  const r = await fetch('/api/scan-runs/cancel', {
    method: 'POST', credentials: 'include', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ run_id: runId }),
  })
  const data = (await r.json().catch(() => null)) as (Cancelled & { error?: string }) | null
  if (!r.ok) throw new Error(data?.error ?? `${r.status} ${r.statusText}`)
  return data as Cancelled
}

const failures = (c: Cancelled): string[] =>
  Object.entries(c.tasks).flatMap(([n, o]) => (typeof o === 'object' ? [`${n.split('/').pop()}: ${o.error}`] : []))

/** What the cancel did to the head, as a sentence. */
function headSays(c: Cancelled): string {
  if (typeof c.head === 'object') return `The head job’s cancel failed (${c.head.error}); it is still running, and so is the run.`
  const marked = c.recorded === 'cancelled' ? ' The run is marked cancelled.'
    : c.recorded === 'failed' ? ' The run stays failed: a step had already failed before the cancel.' : ''
  return `Cancelled the head job${c.head === 'already' ? ' (its cancel was already under way)' : c.head === 'gone' ? ' (already gone)' : ''}.${marked}`
}

/** The button, for an admin on a running scan run (not a compaction, merge, race, access ingest or recovery). */
export function CancelScanButton({ run, invalidate }: { run: ScanRun; invalidate: readonly (readonly unknown[])[] }) {
  const admin = useIsAdmin()
  const [open, setOpen] = useState(false)
  if (!admin || run.status !== 'running' || run.parent || !run.job_name) return null
  return <>
    <Tooltip content="Cancel this scan: its head job and every task it started, in every region.">
      <button type="button" className="sr-cancel-scan" onClick={() => setOpen(true)}>Cancel</button>
    </Tooltip>
    {open && <CancelScanDialog run={run} invalidate={invalidate} onClose={() => setOpen(false)} />}
  </>
}

function CancelScanDialog({ run, invalidate, onClose }: { run: ScanRun; invalidate: readonly (readonly unknown[])[]; onClose: () => void }) {
  const qc = useQueryClient()
  const go = useMutation<Cancelled, Error>({
    mutationFn: () => cancelScan(run.run_id),
    onSettled: () => Promise.all(invalidate.map(queryKey => qc.invalidateQueries({ queryKey }))),
  })
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !go.isPending) onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, go.isPending])
  const bad = go.data ? failures(go.data) : []
  const n = go.data ? Object.keys(go.data.tasks).length : 0
  return (
    <div className="token-backdrop" onClick={() => { if (!go.isPending) onClose() }}>
      <div className="token-modal run-scan-modal" role="dialog" aria-label="Cancel this scan" onClick={e => e.stopPropagation()}>
        <div className="token-head">
          <strong>Cancel scan <code>{run.scan}</code></strong>
          <button type="button" className="token-x" onClick={onClose} disabled={go.isPending} aria-label="Close">✕</button>
        </div>
        {go.isSuccess ? <>
          <p>{headSays(go.data)} {go.data.tasks_error ? '' : `Cancelled ${n} running task${n === 1 ? '' : 's'}.`}</p>
          {typeof go.data.head === 'object' && <p className="token-err">Try again, or cancel <code>{run.job_name}</code> in the Batch console.</p>}
          {go.data.tasks_error && <p className="token-err">Couldn’t list the scan’s tasks, so none was cancelled: {go.data.tasks_error}</p>}
          {bad.length > 0 && <p className="token-err">Not cancelled: {bad.join('; ')}</p>}
          <div className="token-actions"><button type="button" onClick={onClose} autoFocus>Close</button></div>
        </> : <>
          <p>Stops this scan now: its head job (<code>{run.job_name}</code>) and every task it started, in every region. The run is marked cancelled (not a failure), unless a step had already failed.</p>
          <p className="token-muted token-small">Steps that already finished keep their outputs. Whatever a cancelled step was writing is left as it stood; the next scan starts from what was published.</p>
          {go.isError && <p className="token-err">{go.error.message}</p>}
          <div className="token-actions">
            <button type="button" className="run-scan-go sr-cancel-go" disabled={go.isPending} onClick={() => go.mutate()} autoFocus>{go.isPending ? 'Cancelling…' : 'Cancel the scan'}</button>
            <button type="button" onClick={onClose} disabled={go.isPending}>Keep it running</button>
          </div>
        </>}
      </div>
    </div>
  )
}
