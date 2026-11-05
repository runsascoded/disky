import { Link } from 'react-router-dom'
import { Tooltip } from './Tooltip'
import { fmtN } from './types'
import { runHref } from './runHistory'
import { type PrefixExecution } from './prefixExecution'

export function PrefixRecovery({ execution: e, fmtBytes }: { execution: PrefixExecution; fmtBytes: (bytes: number) => string }) {
  if (!e) return <span className="dim">—</span>
  return <Tooltip content={<>
    <div>{e.kind === 'recorded' ? `${fmtN(e.objects)} acknowledged deletions · ${fmtBytes(e.bytes)}` : e.kind === 'live-prefix' ? `${fmtN(e.objects)} acknowledged deletions so far · ${fmtBytes(e.bytes)}` : 'Bucket-level progress only; this does not establish completion of this prefix.'}</div>
    {e.kind === 'recorded' && e.preserved > 0 && <div>{fmtN(e.preserved)} changed/new objects were preserved. To include them, review them, unstage this prefix, then stage it again.</div>}
    {e.kind === 'recorded' && e.noUndo ? <div>This run recorded no undo window: soft delete was off, or it left no record of its retention. Treat its deletes as permanent.</div> : <>
    <div>{e.kind === 'recorded' ? 'Open recovery details. Prefix-filtered restore is available through the CLI; the web undo button restores the whole run.' : 'Stop and drain the run before restoring, so it cannot delete restored objects again.'}</div>
    <div>Soft-delete retention starts at each object’s deletion, not when the run finishes. Empty at a scan is not proof of deletion by this run.</div>
    <div>Undo restores every logged generation. An XML run’s log can’t tell an object it deleted from one someone else deleted after the scan, so undo can bring those back too.</div>
    {e.kind === 'recorded' && e.deadline && <div>Conservative recovery cutoff: {new Date(e.deadline * 1000).toISOString()}</div>}
    </>}
  </>}><Link to={runHref(e.runId, 'recovery')} className="prefix-recovery">
    {e.kind === 'recorded' ? `${fmtN(e.objects)} logged deleted · ${e.preserved > 0 ? `${fmtN(e.preserved)} changed/new preserved · details` : e.undone >= e.objects ? 'restored' : e.noUndo ? 'no undo' : e.expired ? 'conservative window elapsed' : 'recovery info'}`
      : e.kind === 'live-prefix' ? `${fmtN(e.objects)} logged deleted · running`
      : e.done ? 'bucket complete · finalizing' : 'bucket running · recovery after stop'}
  </Link></Tooltip>
}
