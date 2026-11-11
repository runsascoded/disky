// "Run a scan" on /scans: the primary store's scan, started now by an admin through the path the deployment's cron
// takes. `SCAN_SUBMIT_JOB` names the cron's submitter, a Cloud Run job (`BatchSubmitCron` in infra/gcp: it holds the
// rendered Batch spec, retries the submit and alerts if it can't); the site runs it as the `GCP_SA_KEY` account
// (`roles/run.jobsExecutorWithOverrides` on that job alone) with `DT_TRIGGER=manual`, the one variable the job lets a
// run override. Its script then names the Batch job for the second (never the cron's day id) and records the scan as
// manual. The site builds no Batch spec of its own.
//
// One scan at a time: a dispatch is refused while a scan run of the primary store is running (not lost:
// `scanRunsModel.isLost`), and while an earlier dispatch is pending: claimed within `PENDING_S`, not failed, and no
// scan run has started since (the run appears only once its head records it, minutes after the dispatch). The claim is
// one conditional insert, so two clicks (or two admins) can't both start one. Every dispatch is logged to
// `admin_edits` (`tbl = 'scan_dispatch'`, pk = the dispatch's id): its `claim`, then `dispatched` (with the Cloud
// Run execution) or `failed` (with why, which releases the claim).
import type { D1Database } from '@cloudflare/workers-types'
import { isLost, isScanProducer, MAINT_KINDS, type ScanRun } from '../../src/scanRunsModel.js'
import { PRIMARY_STORE } from './stores.js'

/** The Cloud Run job's resource name: `projects/<p>/locations/<region>/jobs/<job>`. */
const JOB_RE = /^projects\/[a-z][a-z0-9-]{4,28}[a-z0-9]\/locations\/[a-z]+-[a-z]+\d+\/jobs\/[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$/

/** `SCAN_SUBMIT_JOB` when it is a job's resource name; null when unset or malformed (the route answers 503). */
export const submitJob = (raw: string | undefined): string | null => (raw && JOB_RE.test(raw) ? raw : null)

/** The `admin_edits` table name a dispatch logs under. */
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

const STARTED_SINCE = `NOT EXISTS (SELECT 1 FROM scan_runs r WHERE r.store = ? AND r.parent IS NULL AND r.started_ts >= c.ts
    AND r.kind NOT IN (${[...MAINT_KINDS, 'access-ingest'].map(k => `'${k}'`).join(', ')}))`
/** A claim (`c`) still pending: recent, not failed, no scan run started since. Binds: since, store. */
const PENDING_WHERE = `c.tbl = '${TBL}' AND c.action = 'claim' AND c.ts > ?
  AND NOT EXISTS (SELECT 1 FROM admin_edits f WHERE f.tbl = '${TBL}' AND f.pk = c.pk AND f.action = 'failed')
  AND ${STARTED_SINCE}`

/** The running scan and the pending dispatch that block a new one (either null). */
export async function dispatchState(db: D1Database, now: number): Promise<DispatchState> {
  const [runs, pending] = await Promise.all([
    db.prepare("SELECT run_id, scan, kind, started_ts, updated_ts, status FROM scan_runs WHERE store = ? AND parent IS NULL AND status = 'running'")
      .bind(STORE).all<ScanRun>(),
    db.prepare(`SELECT c.pk AS id, c.ts, c.who,
        (SELECT json_extract(d.new_json, '$.execution') FROM admin_edits d WHERE d.tbl = '${TBL}' AND d.pk = c.pk AND d.action = 'dispatched') AS execution
      FROM admin_edits c WHERE ${PENDING_WHERE} ORDER BY c.ts DESC, c.id DESC LIMIT 1`)
      .bind(now - PENDING_S, STORE).first<Pending>(),
  ])
  const live = runs.results.filter(r => isScanProducer(r.kind) && !isLost(r, now)).sort((a, b) => (b.started_ts ?? 0) - (a.started_ts ?? 0))[0]
  return { running: live ? { run_id: live.run_id, scan: live.scan, started_ts: live.started_ts } : null, pending: pending ?? null }
}

/** Claim the dispatch: logs its `claim` unless one is pending, atomically. False: one is. */
export async function claim(db: D1Database, id: string, who: string, now: number, job: string): Promise<boolean> {
  const r = await db.prepare(`INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json)
      SELECT '${TBL}', ?, 'claim', ?, ?, NULL, ? WHERE NOT EXISTS (SELECT 1 FROM admin_edits c WHERE ${PENDING_WHERE})`)
    .bind(id, who, now, JSON.stringify({ job, trigger: 'manual' }), now - PENDING_S, STORE).run()
  return (r.meta.changes ?? 0) > 0
}

/** The claim's outcome: `dispatched` (the execution) or `failed` (which releases it). */
export const settle = (db: D1Database, id: string, who: string, now: number, action: 'dispatched' | 'failed', detail: object) =>
  db.prepare('INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json) VALUES (?, ?, ?, ?, ?, NULL, ?)')
    .bind(TBL, id, action, who, now, JSON.stringify(detail)).run()

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
