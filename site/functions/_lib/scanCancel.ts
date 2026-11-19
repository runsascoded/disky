// "Cancel" on a running scan's /scans page: stop the whole scan, wherever its pieces run. A scan is its head (the Batch
// job its `scan_runs` row names: `job_name`, `region`) plus every Batch job the head's pipeline submitted: Nextflow's
// tasks and the stores' stage jobs, in whichever region each runs, all labelled `scan_id` (the scan, lowercased:
// `scan_head.labels`, `nf/base.config`) and `deployment`. Cancelling only the head leaves tasks running; cancelling
// only a task leaves the head waiting on it forever (Nextflow's google-batch executor never counts CANCELLED as
// done: gcs's 2026-11-19 12:41 scan idled 9 h that way). So the head goes first (no new submits), then every live job
// carrying both labels, found by one listing over all locations (`locations/-`).
//
// `scan_id` alone isn't unique: two deployments in one project can scan at the same minute, so the deployment label
// (`DISKY_LABELS`) is required. Only a top-level scan run is cancelled here (not a compaction, merge, race or access
// ingest, whose jobs share the scan's `scan_id`). The row is then marked failed, `cancelled by <who>`, unless the
// head recorded its own end first.
import type { D1Database } from '@cloudflare/workers-types'
import { MAINT_KINDS, type ScanRun } from '../../src/scanRunsModel.js'

/** Batch states a job can still be stopped in. */
export const LIVE_STATES = ['QUEUED', 'SCHEDULED', 'RUNNING'] as const

/** Run kinds this cancels nothing of: they aren't a scan's own run. */
const NOT_SCANS: readonly string[] = [...MAINT_KINDS, 'access-ingest']

export type Run = Pick<ScanRun, 'run_id' | 'scan' | 'kind' | 'parent' | 'status' | 'job_name' | 'region'>

/** The run, if any. */
export const loadRun = (db: D1Database, runId: string): Promise<Run | null> =>
  db.prepare('SELECT run_id, scan, kind, parent, status, job_name, region FROM scan_runs WHERE run_id = ?').bind(runId).first<Run>()

/** Why `run` can't be cancelled here, or null. */
export function refusal(run: Run): string | null {
  if (run.status !== 'running') return `run ${run.run_id} isn't running (${run.status})`
  if (run.parent) return `run ${run.run_id} belongs to run ${run.parent}; cancel that one`
  if (NOT_SCANS.includes(run.kind)) return `run ${run.run_id} is a ${run.kind} run, not a scan; only a scan run is cancelled here`
  if (!run.job_name || !run.region) return `run ${run.run_id} has no Batch job recorded`
  return null
}

/** The label value a scan's jobs carry (`cost_labels`' rule: lowercased). */
export const scanLabel = (scan: string): string => scan.toLowerCase()

/** The Batch list filter for every job of `scan` in `deployment`. */
export const jobsFilter = (deployment: string, scan: string): string =>
  `labels.deployment="${deployment}" AND labels.scan_id="${scanLabel(scan)}"`

interface Job { name: string; status?: { state?: string } }

/** `jobs`' live ones' resource names (`projects/<p>/locations/<r>/jobs/<j>`), minus `skip`. */
export const liveJobs = (jobs: readonly Job[], skip: string | null): string[] =>
  jobs.filter(j => (LIVE_STATES as readonly string[]).includes(j.status?.state ?? '') && j.name !== skip).map(j => j.name)

/** The head's resource name. */
export const headName = (project: string, run: Run): string => `projects/${project}/locations/${run.region}/jobs/${run.job_name}`

/** One job's cancel: `cancelled`, `gone` (404: deleted) or `already` (409: a cancel is in progress, or it has ended),
 *  else an error. */
export type CancelOutcome = 'cancelled' | 'already' | 'gone' | { error: string }

/** What a cancel did: the head's outcome, and each task's by name. */
export interface Cancelled { head: CancelOutcome; tasks: Record<string, CancelOutcome>; recorded: boolean }

type Fetch = (url: string, init: RequestInit) => Promise<Response>

const BATCH = 'https://batch.googleapis.com/v1'

async function cancelJob(fetch: Fetch, token: string, name: string, reason: string): Promise<CancelOutcome> {
  const r = await fetch(`${BATCH}/${name}:cancel`, {
    method: 'POST',
    headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
    body: JSON.stringify({ reason }),
  })
  if (r.ok) return 'cancelled'
  if (r.status === 404) return 'gone'
  if (r.status === 409) return 'already'
  return { error: `Batch ${r.status}: ${(await r.text()).slice(0, 300)}` }
}

/** Every job of `scan` in `deployment`, all locations, all pages. */
async function listJobs(fetch: Fetch, token: string, project: string, deployment: string, scan: string): Promise<Job[]> {
  const out: Job[] = []
  let page = ''
  for (let i = 0; i < 20; i++) {
    const q = new URLSearchParams({ filter: jobsFilter(deployment, scan), pageSize: '200' })
    if (page) q.set('pageToken', page)
    const r = await fetch(`${BATCH}/projects/${project}/locations/-/jobs?${q}`, { method: 'GET', headers: { authorization: `Bearer ${token}` } })
    if (!r.ok) throw new Error(`Batch list ${r.status}: ${(await r.text()).slice(0, 300)}`)
    const body = (await r.json()) as { jobs?: Job[]; nextPageToken?: string }
    out.push(...(body.jobs ?? []))
    if (!body.nextPageToken) return out
    page = body.nextPageToken
  }
  throw new Error('Batch list: more than 20 pages')
}

/** Cancel `run` (which `refusal` passed): its head, then every live job of its scan; then mark the row failed. */
export async function cancelScan(
  { db, fetch, token, project, deployment, run, who, now }:
  { db: D1Database; fetch: Fetch; token: string; project: string; deployment: string; run: Run; who: string; now: () => number },
): Promise<Cancelled> {
  const reason = `cancelled by ${who} from /scans`
  const head = headName(project, run)
  const headOut = await cancelJob(fetch, token, head, reason)
  const names = liveJobs(await listJobs(fetch, token, project, deployment, run.scan), head)
  const tasks: Record<string, CancelOutcome> = {}
  for (const n of names) tasks[n] = await cancelJob(fetch, token, n, reason)
  const t = now()
  const r = await db.prepare(`UPDATE scan_runs SET status = 'failed', error = ?, finished_ts = COALESCE(finished_ts, ?), updated_ts = ?
      WHERE run_id = ? AND status = 'running'`).bind(`cancelled by ${who}`, t, t, run.run_id).run()
  return { head: headOut, tasks, recorded: (r.meta.changes ?? 0) > 0 }
}
