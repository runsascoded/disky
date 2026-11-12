// "Run a scan" in /scans' header (admins, where the deployment has it): a confirm, then `POST
// /api/scan-runs/dispatch`, which starts the scan through the scheduled scan's submitter as a manual run. The button is
// disabled while a scan runs or an earlier dispatch hasn't recorded its run (`/api/scan-runs`' `dispatch`), and while
// its own request is in flight; the server refuses the same cases, so two clicks or two admins start one scan.
import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Tooltip } from './Tooltip'
import { useIsAdmin } from './auth'
import { aboutDur, type RunSummary, typicalScanSecs } from './scanRunsModel'
import type { DispatchBlock } from './scanRunsUi'
import { ago } from './colors'

interface Started { execution: string | null; operation: string | null }

async function dispatchScan(): Promise<Started> {
  const r = await fetch('/api/scan-runs/dispatch', { method: 'POST', credentials: 'include' })
  const data = (await r.json().catch(() => null)) as (Started & { error?: string; detail?: string }) | null
  if (!r.ok) throw new Error([data?.error ?? `${r.status} ${r.statusText}`, data?.detail].filter(Boolean).join(': '))
  return data as Started
}

/** The execution's own id, from its resource name (`…/executions/<id>`). */
const shortExec = (name: string | null): string | null => name?.split('/').pop() ?? null

/** Why the button is disabled now, or null. */
function blockedBy(block: DispatchBlock): string | null {
  if (block.running) return `A scan is running (run ${block.running}); start another once it finishes.`
  if (block.pending_since != null) return `A scan was started ${ago(block.pending_since)} ago and hasn’t recorded its run yet.`
  return null
}

export function RunScanButton({ block, runs, queryKey }: { block: DispatchBlock | null | undefined; runs: readonly RunSummary[]; queryKey: readonly unknown[] }) {
  const admin = useIsAdmin()
  const [open, setOpen] = useState(false)
  if (!admin || !block) return null
  const why = blockedBy(block)
  const button = <button type="button" className="sr-run-scan" disabled={!!why} onClick={() => setOpen(true)}>Run a scan</button>
  return <>
    <Tooltip content={why ?? 'Start a scan now, as the scheduled ones run (recorded as manual).'}>{why ? <span>{button}</span> : button}</Tooltip>
    {open && <RunScanDialog runs={runs} queryKey={queryKey} onClose={() => setOpen(false)} />}
  </>
}

function RunScanDialog({ runs, queryKey, onClose }: { runs: readonly RunSummary[]; queryKey: readonly unknown[]; onClose: () => void }) {
  const qc = useQueryClient()
  const go = useMutation<Started, Error>({
    mutationFn: dispatchScan,
    onSettled: () => qc.invalidateQueries({ queryKey }),
  })
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape' && !go.isPending) onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, go.isPending])
  const secs = typicalScanSecs(runs)
  const exec = shortExec(go.data?.execution ?? null)
  return (
    <div className="token-backdrop" onClick={() => { if (!go.isPending) onClose() }}>
      <div className="token-modal run-scan-modal" role="dialog" aria-label="Run a scan" onClick={e => e.stopPropagation()}>
        <div className="token-head">
          <strong>Run a scan</strong>
          <button type="button" className="token-x" onClick={onClose} disabled={go.isPending} aria-label="Close">✕</button>
        </div>
        {go.isSuccess ? <>
          <p>Started{exec && <> (<code>{exec}</code>)</>}. It appears below as running once the scan records its run, usually within a few minutes.</p>
          <div className="token-actions"><button type="button" onClick={onClose} autoFocus>Close</button></div>
        </> : <>
          <p>Starts a manual scan now{secs != null && <> ({aboutDur(secs)})</>}; it appears below as running.</p>
          <p className="token-muted token-small">It runs as the scheduled scan does, through the same submitter, and is recorded as manual. Scheduled scans still run at their usual times.</p>
          {go.isError && <p className="token-err">{go.error.message}</p>}
          <div className="token-actions">
            <button type="button" className="run-scan-go" disabled={go.isPending} onClick={() => go.mutate()} autoFocus>{go.isPending ? 'Starting…' : 'Start scan'}</button>
            <button type="button" onClick={onClose} disabled={go.isPending}>Cancel</button>
          </div>
        </>}
      </div>
    </div>
  )
}
