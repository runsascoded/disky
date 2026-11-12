// "Run a scan" on /scans: the primary store's scan, started now by an admin through the path the deployment's cron
// takes. `SCAN_SUBMIT_JOB` names the cron's submitter, a Cloud Run job (`BatchSubmitCron` in infra/gcp: it holds the
// rendered Batch spec, retries the submit and alerts if it can't); the site runs it as the `GCP_SA_KEY` account
// (`roles/run.jobsExecutorWithOverrides` on that job alone) with `DT_TRIGGER=manual`, the one variable the job lets a
// run override. Its script then names the Batch job for the second (never the cron's day id) and records the scan as
// manual. The site builds no Batch spec of its own.
//
// One scan at a time: a dispatch is refused while a scan run of the primary store is running (not lost:
// `scanRunsModel.isLost`), and while an earlier dispatch is pending: claimed within `PENDING_S`, not failed, and no
// scan run has started since (the run appears only once its head records it, minutes after the dispatch). Each
// dispatch is a row of `scan_dispatch` (migration `scan-dispatch`): who claimed it and when, then its outcome,
// `dispatched` (the Cloud Run execution) or `failed` (why, which releases the claim). The claim is one conditional
// insert (`INSERT … SELECT … WHERE NOT EXISTS <a pending claim>`), so two clicks (or two admins) can't both start one;
// pending turns on the time and on `scan_runs`, which no unique index can express. The table is the dispatches' audit
// trail (read-only on /admin's tables, `_lib/tables.ts`); a D1 without it has no "Run a scan".
import type { D1Database } from '@cloudflare/workers-types'
import { isLost, MAINT_KINDS, type ScanRun } from '../../src/scanRunsModel.js'
import { PRIMARY_STORE } from './stores.js'

/** The Cloud Run job's resource name: `projects/<p>/locations/<region>/jobs/<job>`. */
const JOB_RE = /^projects\/[a-z][a-z0-9-]{4,28}[a-z0-9]\/locations\/[a-z]+-[a-z]+\d+\/jobs\/[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/

/** `SCAN_SUBMIT_JOB` when it is a job's resource name; null when unset or malformed (the route answers 503). */
export const submitJob = (raw: string | undefined): string | null => (raw && JOB_RE.test(raw) ? raw : null)

/** The table each dispatch is a row of. */
export const TBL = 'scan_dispatch'
/** The store a dispatch scans: the submitter's. */
export const STORE = PRIMARY_STORE
/** How long an unrecorded dispatch blocks another: the submitter's retry window (30 min, `$SUBMIT_RETRY_S`) plus
 *  Batch's queue and the head's start. Past it, a dispatch whose scan never recorded a run blocks nothing. */
export const PENDING_S = 3600
/** The variable the submitter reads (`job/scan-submit.sh`): its run's trigger. */
export const TRIGGER_ENV = 'DT_TRIGGER'

export interface Pending { id: string; ts: number; who: string; execution: string | null }
export type Running = Pick<ScanRun, 'run_id' | 'scan' | 'started_ts'>
export interface DispatchState { running: Running | null; pending: Pending | null }

/** The table a D1 error says is missing (`no such table: <t>`), else null. */
export const missingTable = (e: unknown): string | null => /no such table: (\w+)/i.exec(String((e as Error)?.message ?? e))?.[1] ?? null

/** Run kinds that aren't the store's scan: its maintenance jobs and the access-log ingest. */
const NOT_SCANS = [...MAINT_KINDS, 'access-ingest'].map(k => `'${k}'`).join(', ')
/** A claim (`c`) still pending: recent, not failed, no scan run started since. Binds: since, store. */
const PENDING_WHERE = `c.ts > ? AND c.state <> 'failed'
  AND NOT EXISTS (SELECT 1 FROM scan_runs r WHERE r.store = ? AND r.parent IS NULL AND r.started_ts >= c.ts AND r.kind NOT IN (${NOT_SCANS}))`

/** The running scan and the pending dispatch that block a new one (either null). A D1 without `scan_runs` or
 *  `scan_dispatch` throws its `no such table` (`missingTable`): that deployment has no dispatch. */
export async function dispatchState(db: D1Database, now: number): Promise<DispatchState> {
  const [runs, pending] = await Promise.all([
    db.prepare(`SELECT run_id, scan, kind, started_ts, updated_ts, status FROM scan_runs
        WHERE store = ? AND parent IS NULL AND status = 'running' AND kind NOT IN (${NOT_SCANS})`)
      .bind(STORE).all<ScanRun>(),
    db.prepare(`SELECT c.id, c.ts, c.who, c.execution FROM ${TBL} c WHERE ${PENDING_WHERE} ORDER BY c.ts DESC, c.rowid DESC LIMIT 1`)
      .bind(now - PENDING_S, STORE).first<Pending>(),
  ])
  const live = runs.results.filter(r => !isLost(r, now)).sort((a, b) => (b.started_ts ?? 0) - (a.started_ts ?? 0))[0]
  return { running: live ? { run_id: live.run_id, scan: live.scan, started_ts: live.started_ts } : null, pending: pending ?? null }
}

/** Claim the dispatch: inserts its row (`claimed`) unless one is pending, atomically. False: one is. */
export async function claim(db: D1Database, id: string, who: string, now: number, job: string): Promise<boolean> {
  const r = await db.prepare(`INSERT INTO ${TBL} (id, ts, who, job, state, updated_ts)
      SELECT ?, ?, ?, ?, 'claimed', ? WHERE NOT EXISTS (SELECT 1 FROM ${TBL} c WHERE ${PENDING_WHERE})`)
    .bind(id, now, who, job, now, now - PENDING_S, STORE).run()
  return (r.meta.changes ?? 0) > 0
}

/** A claim's outcome: `dispatched` (the Cloud Run execution and operation) or `failed` (why; it releases the claim). */
export type Outcome =
  | { state: 'dispatched'; execution: string | null; operation: string | null }
  | { state: 'failed'; error: string }

/** Record the claim's outcome. */
export const settle = (db: D1Database, id: string, now: number, o: Outcome) =>
  db.prepare(`UPDATE ${TBL} SET state = ?, execution = ?, operation = ?, error = ?, updated_ts = ? WHERE id = ? AND state = 'claimed'`)
    .bind(
      o.state,
      o.state === 'dispatched' ? o.execution : null,
      o.state === 'dispatched' ? o.operation : null,
      o.state === 'failed' ? o.error : null,
      now,
      id,
    ).run()

/** The Cloud Run Admin API call that runs `job` as a manual scan. */
export const runRequest = (job: string): { url: string; body: unknown } => ({
  url: `https://run.googleapis.com/v2/${job}:run`,
  body: { overrides: { containerOverrides: [{ env: [{ name: TRIGGER_ENV, value: 'manual' }] }] } },
})

/** The execution a `jobs:run` started: its long-running operation's metadata (an Execution), else null. */
export function executionOf(op: unknown): string | null {
  const name = (op as { metadata?: { name?: unknown } } | null)?.metadata?.name
  return typeof name === 'string' && /\/executions\/[^/]+$/.test(name) ? name : null
}

/** Why a dispatch is refused now, for the caller; null when it may go. */
export function refusal({ running, pending }: DispatchState, now: number): string | null {
  const ago = (ts: number) => `${Math.max(1, Math.round((now - ts) / 60))} min ago`
  if (running) return `a scan is already running (${running.scan}, run ${running.run_id}${running.started_ts ? `, started ${ago(running.started_ts)}` : ''}); start another once it finishes`
  if (pending) {
    return `a scan was started ${ago(pending.ts)} by ${pending.who} and hasn't recorded its run yet; another can start once it does, `
      + `or ${Math.ceil((pending.ts + PENDING_S - now) / 60)} min from now if it never does (its submitter alerts when a submit fails)`
  }
  return null
}
