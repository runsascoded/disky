// What the sweep's soft-delete minimum (`sweep_settings`, `/admin`) lets the product promise about undo. The minimum is the
// narrowest retention a real run accepts: a bucket retaining less refuses it, so a minimum above 0 is a floor under what a run
// can undo, and 0 promises nothing. A run's own record says what it actually had (`deletion_runs.soft_delete_days`). DOM-free:
// the Slack thread (`functions/_lib/stagedSlack.ts`) says the same.

/** `sweep_exec.MIN_SOFT_DELETE_DAYS`: keep identical. No `sweep_settings` row means this. */
export const DEFAULT_MIN_SOFT_DELETE_DAYS = 7
/** GCS's longest retention (and `sweep execute*`'s `-s` range, and the table's CHECK). */
export const MAX_MIN_SOFT_DELETE_DAYS = 90

const days = (n: number): string => `${n} ${n === 1 ? 'day' : 'days'}`

const NO_UNDO_ALLOWED = 'No undo: this deployment doesn\'t require soft delete, so treat these deletes as permanent.'

const recoverableFor = (min: number): string =>
  `Recoverable for at least ${days(min)}${min < DEFAULT_MIN_SOFT_DELETE_DAYS ? `, less than the usual ${DEFAULT_MIN_SOFT_DELETE_DAYS}` : ''}`

/** The Slack confirm's undo sentence, on a deployment with this minimum. */
export const slackUndoTerms = (min: number): string => min > 0 ? `${recoverableFor(min)} (undo in www).` : NO_UNDO_ALLOWED

/** The buckets' soft delete on or off, as of a dated snapshot (the newest `/api/cost/gcp` document). */
export interface BucketSoftDelete { asOf: string; off: Record<string, boolean> }

/** The real-delete confirm's undo sentence, for a run of `buckets`; `known` null: no snapshot to say what they have. */
export function confirmUndoTerms(min: number, buckets: readonly string[], known: BucketSoftDelete | null): string {
  const off = known ? buckets.filter(b => known.off[b]) : []
  if (known && off.length) {
    return min > 0
      ? `This run would be refused: soft delete is off on ${off.join(', ')} (as of ${known.asOf}), and this deployment requires at least ${days(min)}.`
      : `No undo: soft delete is off on ${off.join(', ')} (as of ${known.asOf}), so deletes there are permanent.`
  }
  if (min > 0) return `${recoverableFor(min)}: a bucket retaining less refuses the run.`
  return known && buckets.every(b => b in known.off)
    ? `No undo unless soft delete is still on when the run starts: it was on for these buckets as of ${known.asOf}, but this deployment doesn't require it, so treat these deletes as permanent.`
    : NO_UNDO_ALLOWED
}

/** The warning beside a minimum being set. Null: the usual floor or above, nothing to warn of. */
export function minimumWarning(min: number): string | null {
  if (min >= DEFAULT_MIN_SOFT_DELETE_DAYS) return null
  return min > 0
    ? `Below ${DEFAULT_MIN_SOFT_DELETE_DAYS} days the undo window is shorter: a run accepts a bucket retaining soft deletes for as little as ${days(min)}, and what it deletes can't be restored after that.`
    : 'At 0 there is no undo: a run deletes from a bucket with soft delete off, and those deletes are permanent. Nothing can restore them.'
}

/** Did this run's record say it has no undo? (0 measured on a bucket it touched. Null, a run from before the column: it needed 7 days.) */
export const runHasNoUndo = (r: { mode: string; soft_delete_days?: number | null }): boolean =>
  r.mode === 'real' && r.soft_delete_days === 0

export const RUN_NO_UNDO = 'No undo: a bucket in this run had soft delete off, so what it deleted there is gone for good.'
