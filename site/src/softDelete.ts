// What the sweep's soft-delete minimum (`sweep_settings`, `/admin`) lets the product promise about undo. The minimum is the
// narrowest retention a real run accepts: a bucket retaining less refuses it, so a minimum above 0 is a floor under what a run
// can undo, and 0 promises nothing. A run's own record says what it actually had (`deletion_runs.soft_delete_days`). DOM-free:
// the Slack thread (`functions/_lib/stagedSlack.ts`) says the same.
//
// "Soft delete" is what a bucket keeps after a delete: GCS's soft-delete policy, or on an S3-style store versioning plus a
// noncurrent-version rule (`soft_delete.s3_retention_seconds`). What a bucket has *now* is another matter: GCS refuses to
// restore once the bucket's policy is off, whatever it retained when the run deleted. The site's own account can't read bucket metadata, so the job writes what
// its account reads (the scan step `soft-delete`, `SoftDeleteSnapshot`); a bucket that reading doesn't name is unknown here, and
// the site says so instead of promising, or ruling out, an undo it can't see.

/** `sweep_exec.MIN_SOFT_DELETE_DAYS`: keep identical. The `sweep` executor's minimum with no `sweep_settings` row. */
export const DEFAULT_MIN_SOFT_DELETE_DAYS = 7
/** The minimum with no `sweep_settings` row: S3-style stores (`plan-sweep`) don't require versioning unless asked to. */
export const defaultMinSoftDeleteDays = (executor: string | undefined): number => executor === 'sweep' ? DEFAULT_MIN_SOFT_DELETE_DAYS : 0
/** `soft_delete.NO_EXPIRY_DAYS`: keep identical. A run's `soft_delete_days` on a bucket that keeps deleted data until purged. */
export const NO_EXPIRY_DAYS = 36500
/** GCS's longest retention (and `sweep execute*`'s `-s` range, and the table's CHECK). */
export const MAX_MIN_SOFT_DELETE_DAYS = 90

/** The react-query key of `/api/sweep/settings`. */
export const SWEEP_SETTINGS_KEY = ['sweep', 'settings'] as const

const days = (n: number): string => `${n} ${n === 1 ? 'day' : 'days'}`

const NO_UNDO_ALLOWED = 'No undo: this deployment doesn\'t require soft delete, so treat these deletes as permanent.'

const recoverableFor = (min: number): string =>
  `Recoverable for at least ${days(min)}${min < DEFAULT_MIN_SOFT_DELETE_DAYS ? `, less than the usual ${DEFAULT_MIN_SOFT_DELETE_DAYS}` : ''}`

/** The Slack confirm's undo sentence, on a deployment with this minimum. */
export const slackUndoTerms = (min: number): string => min > 0 ? `${recoverableFor(min)} (undo in www).` : NO_UNDO_ALLOWED

/** The buckets' soft delete on or off, as of when the job read it (`asOf`: `2026-11-05 07:12Z`). A bucket it doesn't name
 * is unknown. */
export interface BucketSoftDelete { asOf: string; off: Record<string, boolean> }

/** The job's reading (`snapshots/soft-delete/<date>.json`, `dt-cloud soft-delete -o`): each bucket's soft-delete retention
 * when read; 0 = off, null = kept until purged (S3). */
export interface SoftDeleteSnapshot { version: number; date: string; as_of: string; buckets: Record<string, { retention_seconds: number | null }> }

/** `soft_delete.SNAPSHOT_VERSION`: the only one read. */
export const SOFT_DELETE_VERSION = 1

/** A reading as the site uses it; null: none, or one of a version this build doesn't know. */
export const bucketSoftDeleteOf = (doc: SoftDeleteSnapshot | null | undefined): BucketSoftDelete | null =>
  doc?.version === SOFT_DELETE_VERSION
    ? { asOf: `${doc.as_of.slice(0, 16).replace('T', ' ')}Z`, off: Object.fromEntries(Object.entries(doc.buckets).map(([b, v]) => [b, v.retention_seconds === 0])) }
    : null

/** A recorded undo that can't be done now: GCS refuses to restore from a bucket whose soft delete is off, whatever the bucket
 * retained when the run deleted from it. `buckets`: the ones the reading shows off, sorted. A run's record (`undo_deadline`)
 * is left as it was written: this is about what is shown and dispatched. */
export interface UndoUnavailable { buckets: string[]; asOf: string }

/** Is the undo of something deleted from `buckets` unavailable, as far as `known` says? Null: none of them is known off (each
 * is on, or nothing says: `undoUnseen`). */
export function undoUnavailable(buckets: readonly string[], known: BucketSoftDelete | null | undefined): UndoUnavailable | null {
  if (!known) return null
  const off = [...new Set(buckets)].filter(b => known.off[b] === true).sort()
  return off.length ? { buckets: off, asOf: known.asOf } : null
}

/** How many buckets a sentence names before it counts the rest. */
const NAMED_BUCKETS = 4

/** `undoUnavailableText` after a label that already says "undo unavailable". */
export const undoUnavailableReason = ({ buckets, asOf }: UndoUnavailable): string =>
  `soft delete is off on ${buckets.length > NAMED_BUCKETS ? `${buckets.slice(0, NAMED_BUCKETS - 1).join(', ')} and ${buckets.length - NAMED_BUCKETS + 1} more` : buckets.join(', ')} (as of ${asOf})`

/** The one wording: on every page, in the Slack thread and in the undo route's refusal. */
export const undoUnavailableText = (u: UndoUnavailable): string => `undo unavailable: ${undoUnavailableReason(u)}`

/** Why, beside `undoUnavailableText`. */
export const UNDO_UNAVAILABLE_WHY = 'Undo restores from what the bucket keeps after a delete, so its soft delete (versioning, on S3) has to be on.'

/** A recorded undo nothing here can vouch for: `unseen` are the buckets it depends on that no reading names (all of them with
 * no reading at all; none named when the run's buckets themselves aren't known). */
export interface UndoUnseen { unseen: string[] }

/** Does the undo of something deleted from `buckets` depend on soft delete this site can't see? `known` null: no reading.
 * Null: every one of `buckets` is in the reading. */
export function undoUnseen(buckets: readonly string[], known: BucketSoftDelete | null): UndoUnseen | null {
  const unseen = [...new Set(buckets)].filter(b => !known || !Object.hasOwn(known.off, b)).sort()
  return unseen.length || !buckets.length ? { unseen } : null
}

/** What the site can't promise of such an undo. It never says "unavailable" for it: it doesn't know that either. */
export const undoUnseenText = ({ unseen }: UndoUnseen): string => unseen.length
  ? `undo depends on soft delete still being on for ${unseen.length > NAMED_BUCKETS ? `${unseen.slice(0, NAMED_BUCKETS - 1).join(', ')} and ${unseen.length - NAMED_BUCKETS + 1} more` : unseen.join(', ')}, which this site can't see`
  : 'undo depends on the buckets\' soft delete, which this site can\'t see'

/** Who does know, beside `undoUnseenText`. */
export const UNDO_UNSEEN_WHO = 'The undo job checks the bucket itself, and reports anything it can\'t restore.'

/** The run columns that say whether it still has an undo to qualify. */
export interface RunUndoWindow { mode: string; undo_deadline?: number | null; undo_state?: string }

/** Only a real run inside its recorded window, not already undone, has an undo to lose: a closed window, a run that recorded
 * none, or one whose every deleted object is live again already says so itself. */
const undoOpen = (r: RunUndoWindow, now: number): boolean => r.mode === 'real' && r.undo_deadline != null && now < r.undo_deadline && r.undo_state !== 'full'

/** Is the undo `r` recorded unavailable at `now`, having deleted from `buckets`? */
export const runUndoUnavailable = (
  r: RunUndoWindow,
  buckets: readonly string[],
  known: BucketSoftDelete | null | undefined,
  now: number,
): UndoUnavailable | null => undoOpen(r, now) ? undoUnavailable(buckets, known) : null

/** Is the undo `r` recorded one the site can't vouch for at `now` (and not one it knows unavailable)? `known` undefined: not
 * asked (an executor whose undo isn't a soft-delete restore, or a reading still loading), so nothing is qualified. */
export const runUndoUnseen = (
  r: RunUndoWindow,
  buckets: readonly string[],
  known: BucketSoftDelete | null | undefined,
  now: number,
): UndoUnseen | null => known !== undefined && undoOpen(r, now) && !undoUnavailable(buckets, known) ? undoUnseen(buckets, known) : null

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

/** The retention a run's record stands on, as far as it says. */
export interface RunRetention { soft_delete_days?: number | null; min_soft_delete_days?: number | null }

/** Measured nothing and required nothing: `sweep execute` measures per bucket, so a run of `-s 0` killed before its end record. */
export const runPromisedNothing = (r: RunRetention): boolean => r.soft_delete_days == null && r.min_soft_delete_days === 0

/** Did this run's record say it has no undo? (0 measured on a bucket it deleted from, or `runPromisedNothing`. Both null, a run from
 * before the columns: it needed 7 days.) */
export const runHasNoUndo = (r: RunRetention & { mode: string }): boolean =>
  r.mode === 'real' && (r.soft_delete_days === 0 || runPromisedNothing(r))

/** Why, for a run `runHasNoUndo` says has none. */
export const runNoUndoText = (r: RunRetention): string => runPromisedNothing(r)
  ? 'No undo promised: this run required no soft delete and recorded no retention, so treat its deletes as permanent.'
  : 'No undo: a bucket in this run had soft delete off, so what it deleted there is gone for good.'

/** Why a real run shown `shown` days (a request naming none: 7) can't run under `current`, or null if it can. Terms are rendered
 * when a card or page is, and the minimum is read at dispatch, so a weaker one must not slip under a confirm that promised more;
 * a stronger one only keeps the promise. */
export const weakerThanShown = (current: number, shown: number): string | null => current < shown
  ? `not deleting: the soft-delete minimum is now ${days(current)}, lower than the ${days(shown)} these terms promised, so the undo they describe may not exist. Re-read the terms and confirm again.`
  : null
