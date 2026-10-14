// The /staged runs table's pure parts (specs/staged-runs.md): joining a plan's
// D1 runs to the executor's Batch jobs, the run-dir files a run reads from
// (`plan-summary.json`, `progress/<bucket>.json`, `<mode>-summary.json`),
// the checks a run's detail shows, and which controls a run offers under
// the deployment's executor capabilities. Nothing here names a deployment.

/** A deletion run as the deployment's executor records it (`deletion_runs`;
 * cw's and gcs's rows share these columns). */
export interface DeletionRun {
  run_id: string
  plan_id: number | null
  mode: 'dry' | 'real'
  scan?: string
  actor?: string
  started_ts: number
  finished_ts: number | null
  deleted_bytes: number
  deleted_objects: number
  skipped_gone: number
  skipped_overwritten?: number
  drift_dirs?: number
  ledger_drift_dirs?: number
  undo_deadline: number | null
  undo_state: string
  purge_state?: string
  log_dir?: string
  /** The `-b` cut, comma-separated (null = every bucket of the plan). */
  buckets?: string | null
  plan_digest?: string | null
  /** A dry run's measured reclaim — what the set as a whole would actually
   *  free (clone/hardlink-shared bytes don't count); null = not measured. */
  freed_bytes?: number | null
}

/** An executor Batch job, as `/api/<executor>/jobs` lists it. */
export interface ExecJob {
  job_id: string
  /** `sweep` = a dry / real run; `undo` / `purge` = an op on run `target`. */
  op?: 'sweep' | 'undo' | 'purge'
  target?: string | null
  /** The plan a run job was dispatched for (gcs; null when unknown). */
  plan_id?: number | null
  state: string
  mode?: string
  created?: string
  run_secs?: number | null
  by?: string | null
  buckets?: string[]
  region?: string
  bucket_region?: string | null
  logs?: string
  last_event?: string | null
  stop_requested?: boolean
  /** The job's run dir (gcs: `plan`; cw: `run`), `gs://<bucket>/…`. */
  plan?: string
  run?: string
}

/** What each executor can do (`Store.executor` → `EXEC_CAPS`). */
export interface ExecCaps {
  /** A live run can be stopped (cw cancels the job; gcs drops a STOP file). */
  stop: boolean
  /** How stopping behaves, for the button's tooltip. */
  stopHint: string
  /** A real run can be undone inside its window. */
  undo: boolean
  /** Undo is offered for a finished real run that recorded no window (cw:
   *  its route allows it); gcs requires the recorded window. */
  undoUnwindowed: boolean
  /** A real run's deleted versions can be purged once its window closes. */
  purge: boolean
  /** A dispatch may name a subset of the plan's buckets. */
  bucketCut: boolean
  /** The run dir (plan / progress / summaries / logs) reads through the
   *  deployment's `/v1/files` proxy. */
  runFiles: boolean
  /** When a just-dispatched run shows up, for the dispatch notice. */
  startHint: string
}

export const EXEC_CAPS: Record<'plan-sweep' | 'sweep' | 'laptop', ExecCaps> = {
  // cw: one bucket per run; its run dirs live in the dispatch's GCS data
  // bucket, which its `/v1/files` (the R2 index store) doesn't serve.
  'plan-sweep': {
    stop: true, stopHint: 'Cancel the Batch job: deletes already made are logged and recoverable; a re-dispatch re-lists and skips them.',
    undo: true, undoUnwindowed: true, purge: true, bucketCut: false, runFiles: false,
    startHint: 'once the job starts (a few minutes while Batch brings up the VM)',
  },
  // gcs: a plan may span buckets; soft delete expires deleted generations on
  // its own (nothing to purge); its files proxy serves the data bucket.
  sweep: {
    stop: true, stopHint: 'Request a clean stop: in-flight work drains and is logged; remaining work is left for a re-run.',
    undo: true, undoUnwindowed: false, purge: false, bucketCut: true, runFiles: true,
    startHint: 'once the job starts (a few minutes while Batch brings up the VM)',
  },
  // m3: the drainer pulls a dispatched run from D1 and trashes on the laptop;
  // no Batch job to stop, and undo is the Trash, not a site action.
  laptop: {
    stop: false, stopHint: '',
    undo: false, undoUnwindowed: false, purge: false, bucketCut: false, runFiles: false,
    startHint: 'when the drainer picks it up (its next poll: 30 s, up to 2½ min while idle)',
  },
}

export const LIVE_STATES = new Set(['QUEUED', 'SCHEDULED', 'RUNNING'])

/** The Batch job that ran a run: its log dir's last segment when that is a
 * job id (gcs: `…/sweep/runs/<job>`; cw: `…/sweep/cw/runs/<job>` = its run
 * id), else the run id (a CLI run). */
export function runJobId(r: Pick<DeletionRun, 'run_id' | 'log_dir'>): string {
  const last = (r.log_dir ?? '').replace(/\/+$/, '').split('/').pop() ?? ''
  return /^[a-z0-9]+-(sweep|undo|purge)-/.test(last) ? last : r.run_id
}

/** One runs-table row: the D1 run, its Batch job, or both; and the undo /
 * purge ops targeting the run. */
export interface RunView {
  key: string
  run?: DeletionRun
  job?: ExecJob
  ops: ExecJob[]
}

/** The plan's runs joined to the executor's jobs (newest first): each run with
 * its job (by `runJobId`) and its ops; then the plan's run jobs no row
 * accounts for yet (dispatched, the executor not yet recording — or died
 * before it did). */
export function joinRuns(runs: readonly DeletionRun[], jobs: readonly ExecJob[]): RunView[] {
  const byId = new Map(jobs.map(j => [j.job_id, j]))
  const joined = new Set<string>()
  const views: RunView[] = runs.map(run => {
    const id = runJobId(run)
    const job = byId.get(id)
    if (job) joined.add(id)
    return { key: run.run_id, run, ...(job ? { job } : {}), ops: jobs.filter(j => j.op && j.op !== 'sweep' && j.target === run.run_id) }
  })
  const orphans: RunView[] = jobs
    .filter(j => (j.op ?? 'sweep') === 'sweep' && !joined.has(j.job_id))
    .map(job => ({ key: job.job_id, job, ops: [] }))
  const at = (v: RunView) => v.run?.started_ts ?? (v.job?.created ? Date.parse(v.job.created) / 1000 : 0)
  return [...views, ...orphans].sort((a, b) => at(b) - at(a))
}

/** A run's state word: Batch's while its job is listed, else what D1 says. */
export const viewState = (v: RunView): string => v.job?.stop_requested && v.job.state === 'FAILED' ? 'CANCELLED' : v.job?.state ?? (v.run?.finished_ts ? 'DONE' : 'RECORDING')

export const viewLive = (v: RunView): boolean => LIVE_STATES.has(v.job?.state ?? '')

/** Seconds a run took (or has taken, while live). */
export function elapsed(v: RunView, now: number): number | null {
  if (v.job?.run_secs != null) return v.job.run_secs
  if (v.run?.finished_ts) return v.run.finished_ts - v.run.started_ts
  if (viewLive(v) && v.run) return now - v.run.started_ts
  return null
}

/** Recorded executor timestamps, distinct from Batch runtime and queueing. */
export function elapsedDetails(v: RunView): string[] {
  const stamp = (ts: number) => new Date(ts * 1000).toISOString().replace('T', ' ').replace('.000Z', ' UTC')
  const lines: string[] = []
  if (v.job?.created) lines.push(`Dispatched: ${stamp(Date.parse(v.job.created) / 1000)}`)
  if (v.run) {
    lines.push(`Executor started: ${stamp(v.run.started_ts)}`)
    lines.push(v.run.finished_ts != null ? `Executor finished: ${stamp(v.run.finished_ts)}` : viewLive(v) ? 'Executor still running' : 'Executor end not recorded')
  } else lines.push('Executor start/end not recorded')
  if (v.job?.run_secs != null) lines.push('Elapsed is Batch running time, excluding queueing; executor timestamps cover its recorded work.')
  return lines
}

/** `3m`, `2h 05m`, `1d 3h`. */
export function fmtDur(s: number): string {
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m`
  const h = Math.floor(m / 60)
  if (h < 48) return `${h}h ${String(m % 60).padStart(2, '0')}m`
  return `${Math.floor(h / 24)}d ${h % 24}h`
}

/** A run identifier's timestamp at minute precision. Keep the full opaque ID
 * in the UI's tooltip/copy affordance; this is only its compact table label. */
export function runLabel(id: string): string {
  const compact = id.match(/(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})\d{2}Z$/)
  const dashed = id.match(/(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})\d{2}z$/)
  const m = compact ?? dashed
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}Z` : id.replace(/^[a-z0-9]+-sweep-(dry|real)-/, '')
}

/** The buckets a run touched: its recorded cut, else its job's; empty = all. */
export const viewBuckets = (v: RunView): string[] =>
  v.run?.buckets ? v.run.buckets.split(',').map(b => b.trim()).filter(Boolean) : v.job?.buckets ?? []

/** The longest `<word>-` prefix every bucket shares (`marin-us-east1`,
 * `marin-eu-west4` → `marin-`), for compact bucket names; '' when none. */
export function commonBucketPrefix(buckets: readonly string[]): string {
  if (buckets.length < 2) return ''
  let p = buckets[0]
  for (const b of buckets) while (!b.startsWith(p)) p = p.slice(0, -1)
  const i = p.lastIndexOf('-')
  return i > 0 ? p.slice(0, i + 1) : ''
}

/** A run dir's path in the files proxy (`gs://<bucket>/sweep/runs/<job>` →
 * `sweep/runs/<job>/`). */
export const runFilesRel = (logDir: string): string => logDir.replace(/^[a-z0-9]+:\/\/[^/]+\//, '').replace(/\/?$/, '/')

/** Raw files-proxy links for the run table's plan and log artifacts. These
 * deliberately bypass the retired `/files/*` SPA route. */
export const runFileHref = (rel: string): string => `/v1/files/get?path=${encodeURIComponent(rel)}`
export const runDirHref = (rel: string): string => `/v1/files/list?prefix=${encodeURIComponent(rel)}`

/** The log subdir a run's decisions land in. */
export const logSubdir = (mode: 'dry' | 'real'): string => (mode === 'real' ? 'deleted' : 'would-delete')

// ── Run-dir files (gcs's executor; read through `/v1/files` when `runFiles`) ──

export interface Tally { bytes: number; objects: number }

/** `plan-summary.json` (`sweep manifest`): per bucket, the listing's keys by
 * category (`eligible` = under a staged prefix), and the totals. */
export interface PlanSummaryFile {
  date?: string
  plan_id?: number
  buckets: Record<string, { objects?: number; dirs?: number; eligible?: Tally; outside_bands?: Tally }>
  total?: { eligible?: Tally; outside_bands?: Tally }
}

/** What the manifest planned to delete: its total eligible, else the sum of
 * the buckets'. */
export function plannedOf(s: PlanSummaryFile): Tally {
  if (s.total?.eligible) return s.total.eligible
  const t = { bytes: 0, objects: 0 }
  for (const b of Object.values(s.buckets)) { t.bytes += b.eligible?.bytes ?? 0; t.objects += b.eligible?.objects ?? 0 }
  return t
}

/** `progress/<bucket>.json`, written every 30 s while a bucket runs. */
export interface ProgressFile {
  bands?: Record<string, { objects?: number; bytes?: number; gone?: number; overwritten?: number; failed?: number }>
  roots: number
  roots_done: number
  decisions: Record<string, number>
  delete_bytes: number
  started: string
  updated: string | null
  done: boolean
}

export interface Progress { deletes: number; gone: number; bytes: number; roots: number; roots_done: number; rate: number }

/** A live run's progress over its buckets; `rate` = deletes/s, each bucket's
 * own rate summed (they run one after another, so a finished bucket's rate
 * is excluded). */
export function sumProgress(ps: readonly ProgressFile[]): Progress {
  const out = { deletes: 0, gone: 0, bytes: 0, roots: 0, roots_done: 0, rate: 0 }
  for (const p of ps) {
    const d = p.decisions.delete ?? 0
    out.deletes += d
    out.gone += p.decisions.skipped_gone ?? 0
    out.bytes += p.delete_bytes
    out.roots += p.roots
    out.roots_done += p.roots_done
    if (!p.done && p.updated) out.rate += d / Math.max(1, (Date.parse(p.updated) - Date.parse(p.started)) / 1000)
  }
  out.rate = Math.round(out.rate)
  return out
}

/** Decision-based completion, and an explicitly rough fleet ETA at the
 * active bucket(s)' average decision rate. Never extrapolate stale samples. */
export function progressEstimate(ps: readonly ProgressFile[], planned: number | undefined, now: number): { decided: number; percent: number | null; secondsLeft: number | null; stale: boolean } {
  const decided = ps.reduce((n, p) => n + Object.values(p.decisions).reduce((a, b) => a + b, 0), 0)
  const active = ps.filter(p => !p.done)
  const stale = active.some(p => !p.updated || !Number.isFinite(Date.parse(p.updated)) || now - Date.parse(p.updated) / 1000 > 90)
  const rate = active.reduce((n, p) => {
    const seconds = p.updated ? (Date.parse(p.updated) - Date.parse(p.started)) / 1000 : 0
    return n + (seconds > 0 ? Object.values(p.decisions).reduce((a, b) => a + b, 0) / seconds : 0)
  }, 0)
  const percent = planned && planned > 0 ? Math.min(100, decided / planned * 100) : null
  const secondsLeft = planned && planned > decided && !stale && rate > 0 ? Math.ceil((planned - decided) / rate) : null
  return { decided, percent, secondsLeft, stale }
}

/** `<deleted|would-delete>-summary.json` (`sweep execute`): per bucket, the
 * decision counts, bytes, drifted / failed dirs, any interruption. */
export interface LogSummaryFile {
  for_real?: boolean
  buckets: Record<string, {
    decisions?: Record<string, number>
    delete_bytes?: number
    drift_dirs?: unknown[]
    failed_dirs?: unknown[]
    soft_delete_days?: number
    missing_perms?: string[]
    interrupted?: { roots_skipped: number; roots: number }
  }>
}

export interface Logged { decisions: Record<string, number>; bytes: number; driftDirs: number; failedDirs: number; rootsSkipped: number }

/** A log summary's totals over its buckets. */
export function loggedOf(s: LogSummaryFile): Logged {
  const out: Logged = { decisions: {}, bytes: 0, driftDirs: 0, failedDirs: 0, rootsSkipped: 0 }
  for (const b of Object.values(s.buckets)) {
    for (const [k, n] of Object.entries(b.decisions ?? {})) out.decisions[k] = (out.decisions[k] ?? 0) + n
    out.bytes += b.delete_bytes ?? 0
    out.driftDirs += b.drift_dirs?.length ?? 0
    out.failedDirs += b.failed_dirs?.length ?? 0
    out.rootsSkipped += b.interrupted?.roots_skipped ?? 0
  }
  return out
}

export interface Check { label: string; expected: number; actual: number; ok: boolean }

/** A finished run's verification: its decision log against its D1 record
 * (the executor writes both from the same counts, so any gap is a lost
 * record), and what it planned against what it accounted for (a gap is keys
 * in drifted dirs it skipped, or roots a stop left). */
export function verifyRun(run: DeletionRun, planned: Tally | null, logged: Logged | null): Check[] {
  const checks: Check[] = []
  const eq = (label: string, expected: number, actual: number) => checks.push({ label, expected, actual, ok: expected === actual })
  if (logged) {
    eq('deleted objects: log = D1', logged.decisions.delete ?? 0, run.deleted_objects)
    eq('deleted bytes: log = D1', logged.bytes, run.deleted_bytes)
    eq('gone: log = D1', logged.decisions.skipped_gone ?? 0, run.skipped_gone)
    eq('overwritten: log = D1', logged.decisions.skipped_overwritten ?? 0, run.skipped_overwritten ?? 0)
    eq('drifted dirs: log = D1', logged.driftDirs, run.drift_dirs ?? 0)
  }
  if (planned && logged) {
    const accounted = Object.values(logged.decisions).reduce((a, b) => a + b, 0)
    eq('planned objects = decided', planned.objects, accounted)
  }
  return checks
}

/** The run controls a viewer gets at `now`. */
export function runControls(v: RunView, caps: ExecCaps, admin: boolean, now: number): { stop: boolean; undo: boolean; purge: boolean; undoing: boolean } {
  const r = v.run
  const undoing = v.ops.some(o => o.op === 'undo' && LIVE_STATES.has(o.state))
  const purging = v.ops.some(o => o.op === 'purge' && LIVE_STATES.has(o.state))
  const stop = caps.stop && admin && viewLive(v) && !!v.job
  if (!r || !admin || r.mode !== 'real' || !r.finished_ts) return { stop, undo: false, purge: false, undoing }
  const inWindow = r.undo_deadline != null ? now < r.undo_deadline : caps.undoUnwindowed
  const undo = caps.undo && r.undo_state !== 'full' && inWindow && !undoing
  const purge = caps.purge && r.purge_state === 'pending' && r.undo_state !== 'full' && (r.undo_deadline == null || now >= r.undo_deadline) && !purging && !undoing
  return { stop, undo, purge, undoing }
}
