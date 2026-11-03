// Scan runs (specs/done/scan-runs-ui.md): the D1 rows `dt-cloud scan-run record |
// backfill` writes, and the pure derivations `/scans` shows — each output's
// delta against the previous scan, a run's summary row, phase lanes, links.
// DOM-free: the Functions (`_lib/scanRuns.ts`) and the pages share it.
import { exactSlug, isScanId, scanTime } from './scanSlug'

export type ScanRunStatus = 'running' | 'succeeded' | 'failed' | 'nop'

export interface ScanRun {
  run_id: string
  store: string
  scan: string
  kind: string
  parent: string | null
  status: ScanRunStatus
  failed_phase: string | null
  error: string | null
  started_ts: number | null
  finished_ts: number | null
  job_name: string | null
  region: string | null
  machine: string | null
  spot: number | null
  tasks: number | null
  image: string | null
  cost_usd: number | null
  gen: string | null
  source: 'job' | 'backfill'
  updated_ts: number
  /** What started it: the deployment's cron, or a hand submit / resume. Absent before migration `scan-run-trigger`;
   * null for runs recorded before it, and backfills. */
  trigger?: 'schedule' | 'manual' | null
}

export interface ScanRunPhase {
  run_id: string
  phase: string
  seq: number
  started_ts: number | null
  finished_ts: number | null
  status: 'running' | 'done' | 'failed'
  note: string | null
  /** The Batch job the phase ran in when not the run's own (a Nextflow step's task), and its machine */
  job_name?: string | null
  machine?: string | null
  /** The pipeline node that ran it: a Nextflow task's `<process> (<tag>)` */
  task?: string | null
}

export interface ScanRunOutput {
  run_id: string
  key: string
  uri: string
  bytes: number | null
  objects: number | null
  rows: number | null
  gen: string | null
}

/** An output beside the previous scan's same structure. */
export interface OutputDelta extends ScanRunOutput {
  prev_scan: string | null
  prev_bytes: number | null
  prev_rows: number | null
  delta_bytes: number | null
  delta_rows: number | null
}

/** The maintenance kinds (`dt_cloud.maint_runs`): a store's compaction and its merges, each its own Batch job and its own
 * row on /scans. Their `scan` is the newest scan they fold or carry (so they sort beside it) and their `gen` is
 * `<store>/<gen>` (a compaction's finish: `<store>/<gen> → <new gen>`); they are never a scan's run (`scanRunOf`, the
 * paging, /health's running scans skip them). */
/** Maintenance runs' kinds: a compaction, a race of one (`compact-probe -U`: built beside it, published only by a
 * compaction adopting it), a merge of deferred carries. */
export const MAINT_KINDS = ['compact', 'race', 'merge'] as const
export const isMaintenance = (kind: string): boolean => (MAINT_KINDS as readonly string[]).includes(kind)

/** A maintenance run's store (`interval-store`, `static-names`): its `gen`'s first segment; null for a scan's run. */
export const maintStore = (run: Pick<ScanRun, 'kind' | 'gen'>): string | null =>
  isMaintenance(run.kind) && run.gen?.includes('/') ? run.gen.split('/')[0] : null

/** Seconds without a write after which a running run reads as quiet: five missed heartbeats. A running run's record
 * is touched every 60 s (its job's `scan-run supervise -H`; each running phase's process, `$DT_HEARTBEAT_S`, with the
 * phase's live note; a maintenance job's recorder), however long its phase, so a quiet run's job has likely stalled or
 * died. */
export const QUIET_S = 300

/** Seconds without a write after which a running run reads as lost: thirty missed heartbeats, past any D1 or network
 * hiccup, so its job most likely died without its exit trap (Batch's maxRunDuration, a lost VM), and `scan-run
 * reconcile` (`RECONCILE_AFTER_S`, the same bound) marks it failed once Batch confirms. Until then its clock stops at
 * its last write (`liveUntil`), so it never stretches a duration chart. */
export const LOST_S = 1800

export const isLost = (run: Pick<ScanRun, 'status' | 'updated_ts'>, now: number): boolean =>
  run.status === 'running' && now - run.updated_ts > LOST_S

/** Where a run's clock runs to while its row says running: `now`, or its last write (`updated_ts`) once lost. */
export const liveUntil = (run: Pick<ScanRun, 'status' | 'updated_ts'>, now: number): number =>
  isLost(run, now) ? run.updated_ts : now

/** What a running run is doing: its running phase (the newest-started, if several), that phase's elapsed seconds and
 * its note (its heartbeat's live progress line; null when none runs), how long since the run last wrote (`updated_ts`:
 * its heartbeat) when that exceeds `QUIET_S`, and whether that exceeds `LOST_S`. Null for a run that isn't running. */
export function liveness(run: Pick<ScanRun, 'status' | 'updated_ts'>, spans: readonly PhaseSpan[], now: number): { phase: string | null; elapsed: number | null; note: string | null; quiet: number | null; lost: boolean } | null {
  if (run.status !== 'running') return null
  const cur = [...spans].filter(s => s.status === 'running').sort((a, b) => b.start - a.start)[0]
  const quiet = now - run.updated_ts
  return { phase: cur?.phase ?? null, elapsed: cur ? cur.end - cur.start : null, note: cur?.note ?? null, quiet: quiet > QUIET_S ? quiet : null, lost: isLost(run, now) }
}

/** A key's structure: the key with its own scan id abstracted, so a per-scan
 * key (`static/deltas/2026-10-08`) compares with the previous scan's. */
export const structureOf = (key: string, scan: string): string => key.split(scan).join('{scan}')

/** A key's top level (`listing/b1` → `listing`). */
export const topOf = (key: string): string => key.split('/')[0]

/** When a run started (epoch s), for ordering runs: its `started_ts`, else its scan id's own time (a date-only id is
 *  00:00 UTC), else its `updated_ts`. `started_ts` can be NULL on rows the job recorded before it knew its start (the
 *  in-job runs before `6842ed63`), and those must not sort below every older run. */
export function runTime(r: Pick<ScanRun, 'started_ts' | 'scan' | 'updated_ts'>): number {
  if (r.started_ts != null) return r.started_ts
  const t = scanTime(r.scan)
  return Number.isNaN(t) ? r.updated_ts : Math.floor(t / 1000)
}

/** Oldest first by `runTime`, then by run id. */
export const byRunTime = (a: ScanRun, b: ScanRun): number => runTime(a) - runTime(b) || a.run_id.localeCompare(b.run_id)

/** A run's elapsed seconds (while it runs, to `liveUntil`: a lost run's last write); null without a start. */
export function runSecs(r: Pick<ScanRun, 'started_ts' | 'finished_ts' | 'status' | 'updated_ts'>, now: number): number | null {
  if (r.started_ts == null) return null
  const end = r.finished_ts ?? (r.status === 'running' ? liveUntil(r, now) : null)
  return end == null ? null : Math.max(0, end - r.started_ts)
}

/** Per scan, per structure: the output of the scan's latest-finished
 * succeeded (or still running) run that has it — what the scan's structure is. */
export function outputIndex(runs: readonly ScanRun[], outputs: readonly ScanRunOutput[]): Map<string, Map<string, ScanRunOutput>> {
  const byId = new Map(runs.map(r => [r.run_id, r]))
  const best = new Map<string, Map<string, { o: ScanRunOutput; t: number }>>()
  for (const o of outputs) {
    const r = byId.get(o.run_id)
    if (!r || (r.status !== 'succeeded' && r.status !== 'running')) continue
    const t = r.finished_ts ?? r.started_ts ?? 0
    const m = best.get(r.scan) ?? new Map()
    best.set(r.scan, m)
    const s = structureOf(o.key, r.scan)
    const cur = m.get(s)
    if (!cur || t > cur.t) m.set(s, { o, t })
  }
  return new Map([...best].map(([scan, m]) => [scan, new Map([...m].map(([s, v]) => [s, v.o]))]))
}

/** `run`'s outputs, each beside the newest earlier scan holding the same
 * structure (null deltas when none does, or a side has no number). */
export function withDeltas(run: ScanRun, outputs: readonly ScanRunOutput[], index: Map<string, Map<string, ScanRunOutput>>): OutputDelta[] {
  const earlier = [...index.keys()].filter(s => s < run.scan).sort().reverse()
  return outputs.filter(o => o.run_id === run.run_id).map(o => {
    const s = structureOf(o.key, run.scan)
    const prevScan = earlier.find(sc => index.get(sc)!.has(s)) ?? null
    const p = prevScan ? index.get(prevScan)!.get(s)! : null
    const d = (a: number | null, b: number | null | undefined) => (a != null && b != null ? a - b : null)
    return {
      ...o, prev_scan: prevScan, prev_bytes: p?.bytes ?? null, prev_rows: p?.rows ?? null,
      delta_bytes: d(o.bytes, p?.bytes), delta_rows: d(o.rows, p?.rows),
    }
  })
}

/** A phase on the run's clock: seconds from the run's start. */
export interface PhaseSpan { phase: string; start: number; end: number; status: ScanRunPhase['status']; note: string | null; lane: number; machine?: string | null; task?: string | null }

/** A phase's status, a closed one's note naming a nonfatal failure (`failed (exit N)`, legacy `wall, failed …`) over
 * its recorded `done`. A running phase stays running: its note is a heartbeat's live output line, whatever it says. */
export function phaseStatus(p: Pick<ScanRunPhase, 'status' | 'note'>): ScanRunPhase['status'] {
  return p.status !== 'running' && /(?:^|,\s*)failed\b/i.test(p.note ?? '') ? 'failed' : p.status
}

export function phaseOutcome(p: Pick<ScanRunPhase, 'status' | 'note'>): string {
  return phaseStatus(p) === 'failed' && /\bexit\s+124\b|\btimed?\s*out\b/i.test(p.note ?? '') ? 'timed out' : phaseStatus(p)
}

export const phaseFill = (p: Pick<PhaseSpan, 'phase' | 'status' | 'note'>, color: (phase: string) => string): string =>
  phaseStatus(p) === 'failed' ? 'var(--danger)' : color(p.phase)

/** Compact charts show the main lane, plus a failure stripe for otherwise hidden overlapping attempts. */
export const overlappingFailures = (spans: readonly PhaseSpan[]): PhaseSpan[] =>
  spans.filter(s => s.lane !== 0 && phaseStatus(s) === 'failed')

/** A closed phase's explicit skip: its note's `skipped[ (kind)]: reason` clause (at the start or after a comma, e.g.
 * `overlapped, skipped (busy): …`; `dt_cloud.scan_runs.phase_skip`), else null. The phase is done, its work is not.
 * A verdict's `2 skipped (…)` is no skip clause, nor is a running phase's heartbeat line. */
export function phaseSkip(p: Pick<ScanRunPhase, 'status' | 'note'>): string | null {
  if (p.status === 'running') return null
  return /(?:^|,\s*)(skipped(?: \([^)]*\))?: .+)$/.exec(p.note ?? '')?.[1] ?? null
}

export interface PhaseRecovery { run: ScanRun; phases: ScanRunPhase[]; secs: number | null }
/** A phase that left the run short: failed (`outcome` its `phaseOutcome`; `attempts` when more than one failed,
 * `detail` the last one's verdict, its note without the exit clause), skipped (`outcome` `skipped`, `reason` its note's
 * skip clause), or done with its search behind (`outcome` `search behind`, `reason` its note's `search behind:` clause:
 * the data is published, only the static name index lags it). */
export interface PhaseIssue { phase: string; outcome: string; recovery: ScanRun | null; reason?: string; attempts?: number; detail?: string }

/** A done phase's `search behind: …` clause (`dt_cloud.scan_steps.probe`: every failure was the search's
 * `scan-not-indexed` refusal), else null. */
export function phaseBehind(p: Pick<ScanRunPhase, 'status' | 'note'>): string | null {
  return p.status === 'done' ? /^search behind: (.+)$/.exec(p.note ?? '')?.[1] ?? null : null
}

export const BEHIND = 'search behind'

export function runOutcome(run: Pick<ScanRun, 'status'>, issues: readonly PhaseIssue[] = []): string {
  if (run.status === 'succeeded' && issues.length) {
    const open = issues.filter(i => i.recovery == null)
    return !open.length ? 'recovered' : open.every(i => i.outcome === BEHIND) ? BEHIND : 'partial'
  }
  return run.status === 'nop' ? 'no-op' : run.status
}

/** Why a `partial` run is partial, one line per unrecovered issue: `<phase> <skip clause>` for a skip (its recorded
 * reason), `<phase> <outcome>` for a failure. */
export function partialReasons(issues: readonly PhaseIssue[]): string[] {
  return issues.filter(i => i.recovery == null).map(i => `${i.phase} ${i.reason ?? i.outcome}`)
}

/** `runOutcome`, a `partial` one with its reasons: `partial: static-names skipped (busy): …`. */
export function runOutcomeLine(run: Pick<ScanRun, 'status'>, issues: readonly PhaseIssue[] = []): string {
  const outcome = runOutcome(run, issues)
  return outcome === 'partial' || outcome === BEHIND ? `${outcome}: ${partialReasons(issues).join('; ')}` : outcome
}

export function phaseRecoveries(run: ScanRun, runs: readonly ScanRun[], phases: readonly ScanRunPhase[], now: number): PhaseRecovery[] {
  return runs.filter(r => r.parent === run.run_id && r.kind === 'phase-recovery' && r.scan === run.scan && r.store === run.store)
    .sort(byRunTime).map(r => ({ run: r, phases: phases.filter(p => p.run_id === r.run_id), secs: runSecs(r, now) }))
}

/** A failed phase's verdict: its note without the `failed (exit N)` clause (`4 passed, 0 skipped, 2 failed`), else
 * undefined. */
function failureDetail(note: string | null): string | undefined {
  const rest = (note ?? '').replace(/^(?:[^,]*,\s*)?failed(?: \([^)]*\))?(?:,\s*|$)/, '').trim()
  return rest || undefined
}

/** The run's issues, one per phase (its last attempt's; `attempts` counts a phase's failed ones). */
export function phaseIssues(run: ScanRun, runs: readonly ScanRun[], phases: readonly ScanRunPhase[]): PhaseIssue[] {
  const own = phases.filter(p => p.run_id === run.run_id && (phaseStatus(p) === 'failed' || phaseSkip(p) != null || phaseBehind(p) != null))
  const last = new Map<string, ScanRunPhase>()
  for (const p of own) last.set(p.phase, p)
  return [...last.values()].map(p => {
    const retries = phaseRecoveries(run, runs, phases, 0).filter(r => r.run.status === 'succeeded' && r.phases.some(a =>
      a.phase === p.phase && phaseStatus(a) === 'done' && phaseSkip(a) == null && phaseBehind(a) == null && a.finished_ts != null
      && p.finished_ts != null && a.finished_ts > p.finished_ts))
    const recovery = retries.at(-1)?.run ?? null
    if (phaseStatus(p) !== 'failed') {
      const skip = phaseSkip(p)
      return skip != null ? { phase: p.phase, outcome: 'skipped', recovery, reason: skip } : { phase: p.phase, outcome: BEHIND, recovery, reason: phaseBehind(p)! }
    }
    const attempts = own.filter(a => a.phase === p.phase && phaseStatus(a) === 'failed').length
    const detail = failureDetail(p.note)
    return { phase: p.phase, outcome: phaseOutcome(p), recovery, ...(attempts > 1 ? { attempts } : {}), ...(detail ? { detail } : {}) }
  })
}

/** A run's phases as spans, in order, each on a lane: a phase overlapping the
 * one before it (an overlapped phase, `access-ingest` beside the listing)
 * takes the next free lane. */
/** A run's step tasks (its phases' own Batch jobs, a Nextflow run's): one per job, in start order. */
export interface RunTask { job_name: string; machine: string | null; phases: string[]; start: number | null; end: number | null; status: ScanRunPhase['status'] }

export function runTasks(phases: readonly ScanRunPhase[]): RunTask[] {
  const by = new Map<string, RunTask>()
  for (const p of [...phases].sort((a, b) => (a.started_ts ?? Infinity) - (b.started_ts ?? Infinity) || a.seq - b.seq)) {
    if (!p.job_name) continue
    const t = by.get(p.job_name) ?? { job_name: p.job_name, machine: null, phases: [], start: null, end: null, status: 'done' as const }
    t.machine ??= p.machine ?? null
    t.phases.push(p.phase)
    if (p.started_ts != null) t.start = t.start == null ? p.started_ts : Math.min(t.start, p.started_ts)
    if (p.finished_ts != null) t.end = t.end == null ? p.finished_ts : Math.max(t.end, p.finished_ts)
    const st = phaseStatus(p)
    if (st === 'failed' || (st === 'running' && t.status === 'done')) t.status = st
    by.set(p.job_name, t)
  }
  return [...by.values()]
}

export function phaseSpans(run: Pick<ScanRun, 'started_ts'> & Partial<Pick<ScanRun, 'status'>>, phases: readonly ScanRunPhase[], now?: number): PhaseSpan[] {
  const t0 = run.started_ts
  if (t0 == null) return []
  const laneEnds: number[] = []
  return [...phases].sort((a, b) => a.seq - b.seq).flatMap(p => {
    const until = p.finished_ts ?? (p.status === 'running' && run.status === 'running' ? now : undefined)
    if (p.started_ts == null || until == null) return []
    const start = Math.max(0, p.started_ts - t0), end = Math.max(start, until - t0)
    let lane = laneEnds.findIndex(e => e <= start)
    if (lane < 0) { lane = laneEnds.length; laneEnds.push(end) } else laneEnds[lane] = end
    return [{ phase: p.phase, start, end, status: phaseStatus(p), note: p.note, lane, ...(p.machine ? { machine: p.machine } : {}), ...(p.task ? { task: p.task } : {}) }]
  })
}

/** Where a failed run failed: its recorded phase, else after its last phase. */
export function failedAt(run: ScanRun, phases: readonly ScanRunPhase[]): string | null {
  if (run.status !== 'failed') return null
  if (run.failed_phase) return run.failed_phase
  const last = [...phases].filter(p => p.run_id === run.run_id).sort((a, b) => b.seq - a.seq)[0]
  return last ? `after ${last.phase}` : null
}

/** One `/scans` row: an orchestrating run with its downstream jobs folded in. */
export interface RunSummary {
  run: ScanRun
  secs: number | null
  spans: PhaseSpan[]
  /** The run's top-level outputs' bytes, and their sum of deltas vs the previous scan. */
  out_bytes: number | null
  delta_bytes: number | null
  /** The downstream jobs (listing fan-out, …) and their cost. */
  downstream: number
  cost_usd: number | null
  failed_at: string | null
  phase_issues: PhaseIssue[]
}

/** The run behind `scan` among `rows` (`summarize`'s, newest first): its newest succeeded run (a failed attempt's resume),
 *  else its newest; undefined when no run recorded it. */
/** The scans a hand submit started: a scan's trigger is its first run's (a manual resume of a scheduled scan stays
 *  scheduled), and a scan with no recorded trigger is neither. */
export function manualScans(rows: readonly RunSummary[]): Set<string> {
  const first = new Map<string, ScanRun>()
  for (const { run } of rows) {
    const f = first.get(run.scan)
    if (!f || (run.started_ts ?? Infinity) < (f.started_ts ?? Infinity)) first.set(run.scan, run)
  }
  return new Set([...first].filter(([, r]) => r.trigger === 'manual').map(([scan]) => scan))
}

export function scanRunOf(rows: readonly RunSummary[], scan: string): ScanRun | undefined {
  const runs = rows.map(r => r.run).filter(r => r.scan === scan && !isMaintenance(r.kind))
  return runs.find(r => r.status === 'succeeded') ?? runs[0]
}

export function summarize(runs: readonly ScanRun[], phases: readonly ScanRunPhase[], outputs: readonly ScanRunOutput[], now: number): RunSummary[] {
  const index = outputIndex(runs, outputs)
  const kids = new Map<string, ScanRun[]>()
  for (const r of runs) if (r.parent) kids.set(r.parent, [...(kids.get(r.parent) ?? []), r])
  const byRun = new Map<string, ScanRunPhase[]>()
  for (const p of phases) byRun.set(p.run_id, [...(byRun.get(p.run_id) ?? []), p])
  return runs.filter(r => !r.parent || !runs.some(p => p.run_id === r.parent))
    .sort((a, b) => byRunTime(b, a))
    .map(run => {
      const tops = withDeltas(run, outputs, index).filter(o => !o.key.includes('/'))
      const sum = (xs: (number | null)[]) => (xs.some(x => x != null) ? xs.reduce<number>((a, x) => a + (x ?? 0), 0) : null)
      const ks = kids.get(run.run_id) ?? []
      const costs = [run.cost_usd, ...ks.map(k => k.cost_usd)]
      return {
        run, secs: runSecs(run, now), spans: phaseSpans(run, byRun.get(run.run_id) ?? [], liveUntil(run, now)),
        out_bytes: sum(tops.map(o => o.bytes)), delta_bytes: sum(tops.map(o => o.delta_bytes)),
        downstream: ks.length, cost_usd: sum(costs), failed_at: failedAt(run, byRun.get(run.run_id) ?? []),
        phase_issues: phaseIssues(run, runs, phases),
      }
    })
}

/** Phase names in their usual order (by mean seq), for a stable color per phase. */
export function phaseOrder(phases: readonly Pick<ScanRunPhase, 'phase' | 'seq'>[]): string[] {
  const acc = new Map<string, { n: number; s: number }>()
  for (const p of phases) { const a = acc.get(p.phase) ?? { n: 0, s: 0 }; a.n++; a.s += p.seq; acc.set(p.phase, a) }
  return [...acc].sort((a, b) => a[1].s / a[1].n - b[1].s / b[1].n || a[0].localeCompare(b[0])).map(([k]) => k)
}

/** The `/meta` tree's page for an output (`META_TREE_URL` = `<origin>/meta`):
 * a prefix opens its directory, an object (or a split part, `…/path-index`)
 * its parent directory. Null without a base or for a non-store URI. */
export function metaHref(base: string | null | undefined, uri: string): string | null {
  const m = base ? /^[a-z0-9]+:\/\/([^/]+)\/(.*)$/.exec(uri) : null
  if (!m) return null
  const key = m[2]
  const dir = key.endsWith('/') ? key.slice(0, -1) : key.split('/').slice(0, -1).join('/')
  return `${base!.replace(/\/$/, '')}/${m[1]}${dir ? `/${dir}` : ''}`
}

/** The map at a scan (`/?d=<slug>`): `slug` its canonical slug (`useMinSlug`),
 * else its minute (`exactSlug`; never the day slug, the day's latest scan). */
export const mapHref = (scan: string, storePath = '/', slug: (id: string) => string = exactSlug): string | null =>
  isScanId(scan) ? `${storePath.replace(/\/$/, '')}/?d=${slug(scan)}` : null

export const batchHref = (project: string, region: string, job: string, tab: 'details' | 'logs' = 'details'): string =>
  `https://console.cloud.google.com/batch/jobsDetail/regions/${region}/jobs/${job}/${tab}?project=${encodeURIComponent(project)}`

/** Logs Explorer's `;query=` is parsed as URL params: `(`/`)` must be escaped
 * too (`encodeURIComponent` leaves them), or the query is cut at the first `(`. */
export const logsQueryParam = (query: string): string => encodeURIComponent(query).replace(/\(/g, '%28').replace(/\)/g, '%29')

/** A step task's logs: Logs Explorer over its Batch job by name (`labels.task_group_name` = `…/jobs/<name>/taskGroups/…`).
 * Not the job's Logs tab: Nextflow deletes a task's job when it finishes, and its logs outlive it. */
export function taskLogsHref(project: string, job: string, since: number | null): string {
  const t = since != null ? new Date((since - 600) * 1000).toISOString() : null
  const query = `log_id("batch_task_logs")\nlabels.task_group_name:"/jobs/${job}/"${t ? `\ntimestamp >= "${t}"` : ''}`
  return `https://console.cloud.google.com/logs/query;query=${logsQueryParam(query)}?project=${encodeURIComponent(project)}`
}

/** The run's logs: its Batch job's Logs tab (task + agent logs, no query to
 * get wrong), else Logs Explorer over its task logs (`labels.job_uid` is the
 * run id). */
export function logsHref(project: string, run: Pick<ScanRun, 'run_id' | 'started_ts' | 'job_name' | 'region'>): string {
  if (run.job_name && run.region) return batchHref(project, run.region, run.job_name, 'logs')
  const since = run.started_ts != null ? new Date((run.started_ts - 600) * 1000).toISOString() : null
  const query = `labels.job_uid="${run.run_id}"\nlog_id("batch_task_logs")${since ? `\ntimestamp >= "${since}"` : ''}`
  return `https://console.cloud.google.com/logs/query;query=${logsQueryParam(query)}?project=${encodeURIComponent(project)}`
}
