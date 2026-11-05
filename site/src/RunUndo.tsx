// What a run's row and detail say of its undo (`runs.ts`'s `undoStatus`): the time left in its recorded window, that the
// window closed, that it never had one, that the undo it recorded can't be done now because soft delete is off on a bucket
// it deleted from, or that nothing here says whether it still is on (`softDelete.ts`). The recorded deadline is shown only
// as a record when the undo is unavailable. Each reason sits in a tip a tap pins open (no hover on a phone), and inline
// in the run's detail.
import { Tooltip } from './Tooltip'
import { fmtDur, type UndoStatus, utcMinute as utc } from './runs'
import { UNDO_UNAVAILABLE_WHY, UNDO_UNSEEN_WHO, type UndoUnavailable, undoUnavailableReason, undoUnavailableText, type UndoUnseen, undoUnseenText } from './softDelete'

const sentence = (s: string): string => `${s[0].toUpperCase()}${s.slice(1)}`

/** The "undo by" cell's tip for an undo that can't be done now. */
export const undoUnavailableTip = (u: UndoUnavailable, deadline: number): string =>
  `${sentence(undoUnavailableText(u))}. ${UNDO_UNAVAILABLE_WHY} Recorded deadline: ${utc(deadline)} UTC.`

/** …and for one nothing here can vouch for. */
export const undoUnseenTip = (u: UndoUnseen, deadline: number): string =>
  `Recorded deadline: ${utc(deadline)} UTC. ${sentence(undoUnseenText(u))}. ${UNDO_UNSEEN_WHO}`

/** The runs table's "undo by" cell. */
export function UndoBy({ status: s, now }: { status: UndoStatus; now: number }) {
  switch (s.kind) {
    case 'unavailable': return <Tooltip pinnable content={undoUnavailableTip(s.unavailable, s.deadline)}><span className="err">unavailable</span></Tooltip>
    case 'open': return s.unseen
      ? <Tooltip pinnable content={undoUnseenTip(s.unseen, s.deadline)}><span>{fmtDur(s.deadline - now)}<span className="dim"> · unverified</span></span></Tooltip>
      : <Tooltip content={`${utc(s.deadline)} UTC`}><span>{fmtDur(s.deadline - now)}</span></Tooltip>
    case 'closed': return <Tooltip pinnable content={`Undo window closed ${utc(s.deadline)} UTC.`}><span className="dim">closed</span></Tooltip>
    case 'none': return <Tooltip pinnable content={s.why}><span>none</span></Tooltip>
    default: return <span className="dim">—</span>
  }
}

/** The run detail's undo line, before its state. */
export function UndoLine({ status: s, now, live }: { status: UndoStatus; now: number; live: boolean }) {
  switch (s.kind) {
    case 'unavailable': return <><b className="err">unavailable</b>: {undoUnavailableReason(s.unavailable)}<span className="dim"> · recorded run deadline {utc(s.deadline)} UTC</span></>
    case 'open': return <>recorded run deadline {utc(s.deadline)} UTC <span className="dim">(in {fmtDur(s.deadline - now)})</span>{s.unseen && <> · {undoUnseenText(s.unseen)}</>}</>
    case 'closed': return <>recorded run deadline {utc(s.deadline)} UTC <span className="dim">(closed)</span></>
    case 'none': return <b>none</b>
    default: return <>{live ? 'recovery after stop and drain; final deadline not recorded yet' : 'no window recorded'}</>
  }
}

/** The undo control of a run's row: offered, or shown disabled with why (`runControls`' `undoOff`). `aria-disabled`, not
 * `disabled`: a disabled button takes no pointer or focus events, so its tip could never open; this one opens on hover and
 * focus, and a tap pins it. */
export function UndoControl({ off, tip, busy, onUndo }: { off: string | null; tip: string; busy: boolean; onUndo: () => void }) {
  return off
    ? <Tooltip pinnable content={off}><button type="button" className="undo off" aria-disabled="true">undo</button></Tooltip>
    : <Tooltip content={tip}><button type="button" className="undo" disabled={busy} onClick={onUndo}>undo</button></Tooltip>
}

/** The run detail's recovery section when its undo can't be done now. `buckets`: every bucket the run deleted from; the ones
 * the reading doesn't show off can still be restored, bucket by bucket, from the CLI. */
export function RecoveryUnavailable({ unavailable, buckets, runId }: { unavailable: UndoUnavailable; buckets: readonly string[]; runId: string }) {
  const on = [...new Set(buckets)].filter(b => !unavailable.buckets.includes(b)).sort()
  return <>
    <p><b className="err">{sentence(undoUnavailableText(unavailable))}.</b> {UNDO_UNAVAILABLE_WHY} The web undo is disabled for this run.</p>
    <p className="dim">As the job last read it, after a scan. <code>dt-cloud sweep undo</code> asks GCS itself.</p>
    {on.length > 0 && <>
      <p>Not shown off: {on.join(', ')}. What the run deleted there can still be restored from the CLI, which asks GCS about each bucket first:</p>
      <p><code>dt-cloud sweep undo {on.map(b => `-b ${b}`).join(' ')} {runId}</code></p>
    </>}
  </>
}

/** …and when nothing here says whether soft delete is still on where it deleted. */
export function RecoveryUnseen({ unseen }: { unseen: UndoUnseen }) {
  return <p className="dim">{sentence(undoUnseenText(unseen))}. {UNDO_UNSEEN_WHO}</p>
}
