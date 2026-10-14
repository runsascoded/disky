import type { RunBand } from './plans'
import type { ProgressFile } from './runs'

export interface PrefixBandRecord extends RunBand {
  run_id: string
  started_ts: number
  finished_ts: number | null
  undo_deadline: number | null
  undo_state: string
}
export type PrefixExecution = null | {
  kind: 'recorded'; runId: string; objects: number; bytes: number; undone: number; deadline: number | null; expired: boolean
} | {
  kind: 'live-prefix'; runId: string; objects: number; bytes: number
} | { kind: 'live-bucket'; runId: string; done: boolean }

/** Older GCS run records used finish + retention; start + retention is the safe lower bound. */
export function conservativeDeadline(r: { started_ts: number; finished_ts: number | null; undo_deadline: number | null }): number | null {
  if (!r.undo_deadline || !r.finished_ts) return null
  return Math.min(r.undo_deadline, r.started_ts + Math.max(0, r.undo_deadline - r.finished_ts))
}

export function prefixExecution(prefix: string, records: readonly PrefixBandRecord[], live: { runId: string; progress: ProgressFile } | undefined, now: number): PrefixExecution {
  const band = live?.progress.bands?.[prefix]
  if (band) return { kind: 'live-prefix', runId: live!.runId, objects: band.objects ?? 0, bytes: band.bytes ?? 0 }
  const r = records.filter(r => r.prefix === prefix && r.objects > 0).sort((a, b) => b.started_ts - a.started_ts)[0]
  if (r) {
    const deadline = conservativeDeadline(r)
    return { kind: 'recorded', runId: r.run_id, objects: r.objects, bytes: r.bytes, undone: r.undone_objects, deadline, expired: deadline != null && now >= deadline }
  }
  return live ? { kind: 'live-bucket', runId: live.runId, done: live.progress.done } : null
}
