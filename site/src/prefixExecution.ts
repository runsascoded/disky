import type { RunBand } from './plans'
import type { ProgressFile } from './runs'
import { type BucketSoftDelete, undoUnavailable, type UndoUnavailable, undoUnseen, type UndoUnseen } from './softDelete'

export interface PrefixBandRecord extends RunBand {
  run_id: string
  started_ts: number
  finished_ts: number | null
  undo_deadline: number | null
  undo_state: string
}
export type PrefixExecution = null | {
  kind: 'recorded'; runId: string; objects: number; bytes: number; preserved: number; undone: number; deadline: number | null; expired: boolean
  /** A finished run that recorded no window: nothing to promise (soft delete off, the usual reason). */
  noUndo?: boolean
  /** Its window is still open and not all of it is restored, but soft delete is off now on the prefix's bucket: nothing there
   *  can be restored. */
  unavailable?: UndoUnavailable
  /** Its window is still open and not all of it is restored, and nothing here says whether soft delete is still on for the
   *  prefix's bucket. */
  unseen?: UndoUnseen
} | {
  kind: 'live-prefix'; runId: string; objects: number; bytes: number
} | { kind: 'live-bucket'; runId: string; done: boolean }

/** Older GCS run records used finish + retention; start + retention is the safe lower bound. */
export function conservativeDeadline(r: { started_ts: number; finished_ts: number | null; undo_deadline: number | null }): number | null {
  if (!r.undo_deadline || !r.finished_ts) return null
  return Math.min(r.undo_deadline, r.started_ts + Math.max(0, r.undo_deadline - r.finished_ts))
}

/** `gs://<bucket>/…` (or a bare `<bucket>/…`) → `<bucket>`. */
export const bucketOfPrefix = (prefix: string): string => prefix.replace(/^[a-z0-9]+:\/\//, '').split('/')[0]

/** `known`: the buckets' soft delete now (`useSoftDeleteNow`): null, no reading; absent, not asked (an executor whose undo
 * isn't a soft-delete restore, or still loading), and nothing is qualified. */
export function prefixExecution(prefix: string, records: readonly PrefixBandRecord[], live: { runId: string; progress: ProgressFile } | undefined, now: number, known?: BucketSoftDelete | null): PrefixExecution {
  const band = live?.progress.bands?.[prefix]
  if (band) return { kind: 'live-prefix', runId: live!.runId, objects: band.objects ?? 0, bytes: band.bytes ?? 0 }
  const r = records.filter(r => r.prefix === prefix && (r.objects > 0 || r.drift_new_objects > 0 || r.overwritten > 0)).sort((a, b) => b.started_ts - a.started_ts)[0]
  if (r) {
    const deadline = conservativeDeadline(r)
    // An undo to lose: inside its window, with something of the band still deleted (its run not undone, the band not restored).
    const open = deadline != null && now < deadline && r.undo_state !== 'full' && r.undone_objects < r.objects
    const unavailable = open ? undoUnavailable([bucketOfPrefix(prefix)], known) : null
    const unseen = open && known !== undefined && !unavailable ? undoUnseen([bucketOfPrefix(prefix)], known) : null
    return { kind: 'recorded', runId: r.run_id, objects: r.objects, bytes: r.bytes, preserved: r.drift_new_objects + r.overwritten, undone: r.undone_objects, deadline, expired: deadline != null && now >= deadline, ...(r.finished_ts != null && r.undo_deadline == null ? { noUndo: true } : {}), ...(unavailable ? { unavailable } : {}), ...(unseen ? { unseen } : {}) }
  }
  return live ? { kind: 'live-bucket', runId: live.runId, done: live.progress.done } : null
}

/** A run's pinned item set: its `plan.json`, as the dispatch snapshotted it. */
export interface RunItems { sweep?: string[]; objects?: string[] }

/** Is `prefix` in this run: an item, or under a prefix item? An unread plan says no: a row claims a run only on the run's own word. */
export const inRunItems = (prefix: string, items: RunItems | null | undefined): boolean =>
  !!items && (!!items.objects?.includes(prefix) || !!items.sweep?.some(item => prefix.startsWith(item)))

/** Which live run `prefix` (staged at `addedTs`) is part of: the index into `targets`, -1 for none. Sharing a bucket with a run isn't being in it. */
export function liveTargetOf(prefix: string, addedTs: number, targets: readonly { bucket: string; run: { started_ts: number }; items?: RunItems | null }[]): number {
  const bucket = bucketOfPrefix(prefix)
  return targets.findIndex(t => t.bucket === bucket && t.run.started_ts >= addedTs && inRunItems(prefix, t.items))
}
