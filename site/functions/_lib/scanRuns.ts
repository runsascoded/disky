// Scan runs (specs/done/scan-runs-ui.md): the D1 reads behind `/api/scan-runs`.
// The three tables are small (a row per job run, its phases, its outputs), so
// each request reads a store's rows whole and derives the views in
// `src/scanRunsModel.ts`. A D1 without the migration (`0016_scan_runs.sql`)
// answers `configured: false` rather than 500.
import type { D1Database } from '@cloudflare/workers-types'
import {
  byRunTime, failedAt, isScanProducer, liveUntil, outputIndex, type OutputDelta, phaseSpans, type PhaseSpan, runSecs, type RunSummary, type ScanRun,
  type ScanRunOutput, type ScanRunPhase, type PhaseIssue, type PhaseRecovery, phaseIssues, phaseRecoveries, summarize, withDeltas,
} from '../../src/scanRunsModel.js'
import type { Dag, RunDagRow } from '../../src/runDag.js'

export interface ScanRunRows { runs: ScanRun[]; phases: ScanRunPhase[]; outputs: ScanRunOutput[] }

/** A store's rows, or null when the tables don't exist. */
export async function loadScanRuns(db: D1Database, store: string): Promise<ScanRunRows | null> {
  try {
    const [runs, phases, outputs] = await db.batch([
      db.prepare('SELECT * FROM scan_runs WHERE store = ?').bind(store),
      db.prepare('SELECT p.* FROM scan_run_phases p JOIN scan_runs r ON r.run_id = p.run_id WHERE r.store = ? ORDER BY p.run_id, p.seq').bind(store),
      db.prepare('SELECT o.* FROM scan_run_outputs o JOIN scan_runs r ON r.run_id = o.run_id WHERE r.store = ? ORDER BY o.run_id, o.key').bind(store),
    ])
    return { runs: runs.results as ScanRun[], phases: phases.results as ScanRunPhase[], outputs: outputs.results as ScanRunOutput[] }
  } catch (e) {
    if (/no such table/i.test(String((e as Error).message ?? e))) return null
    throw e
  }
}

/** A Slack emoji name as the broker answers it: nothing else is passed on. */
const SLACK_EMOJI_RE = /^:[a-z0-9_+'-]{1,100}:$/

/** Each manual run's starter's Slack emoji (`trigger_emoji`), asked of the broker now (`slackEmojiClient.getMany`: one
 *  request for every starter, answers cached per isolate), never stored. A failed lookup leaves the rows as they are
 *  (the thread shows 👤; a later post can be edited). */
export async function withStarterEmoji(
  rows: ScanRunRows | null,
  getMany: (emails: readonly string[]) => Promise<Record<string, string | null>>,
): Promise<ScanRunRows | null> {
  const starters = [...new Set((rows?.runs ?? []).filter(r => r.trigger === 'manual' && r.trigger_by?.includes('@')).map(r => r.trigger_by!.toLowerCase()))]
  if (!rows || !starters.length) return rows
  let found: Record<string, string | null>
  try {
    found = await getMany(starters)
  } catch (e) {
    console.warn('scan runs: starter emoji lookup failed', String((e as Error)?.message ?? e))
    return rows
  }
  return {
    ...rows,
    runs: rows.runs.map(r => {
      const e = r.trigger === 'manual' && r.trigger_by ? found[r.trigger_by.toLowerCase()] : null
      return e && SLACK_EMOJI_RE.test(e) ? { ...r, trigger_emoji: e } : r
    }),
  }
}

/** The rows a viewer may see: a public (anonymous) viewer gets no run's starter (`trigger_by`, an address) and no link to
 * where it was reported (`thread_url`, the deployment's internal Slack). */
export function withoutStarters(rows: ScanRunRows | null, id: { via: 'session' | 'grant' | 'public' }): ScanRunRows | null {
  if (!rows || id.via !== 'public') return rows
  return { ...rows, runs: rows.runs.map(({ trigger_by: _, thread_url: __, ...r }) => r) }
}

/** The run's pipeline DAG (the engine-neutral graph its head stored), or null: a one-VM run, or no `scan_run_dags`. */
export async function loadDag(db: D1Database, runId: string): Promise<RunDagRow | null> {
  try {
    const r = await db.prepare('SELECT engine, dag FROM scan_run_dags WHERE run_id = ?').bind(runId).first<{ engine: string; dag: string }>()
    return r ? { engine: r.engine, dag: JSON.parse(r.dag) as Dag } : null
  } catch (e) {
    if (/no such table/i.test(String((e as Error).message ?? e))) return null
    throw e
  }
}

export interface ScanRunsList { configured: boolean; runs: RunSummary[] }

export const scanRunsList = (rows: ScanRunRows | null, now: number): ScanRunsList =>
  rows ? { configured: true, runs: summarize(rows.runs, rows.phases, rows.outputs, now) } : { configured: false, runs: [] }

export interface ScanRunDetail {
  run: ScanRun
  secs: number | null
  failed_at: string | null
  phases: ScanRunPhase[]
  spans: PhaseSpan[]
  outputs: OutputDelta[]
  /** Its downstream jobs (listing fan-out, …), oldest first. */
  downstream: ScanRun[]
  /** The scan's other orchestrating runs (attempts, REPROCs), oldest first. */
  siblings: ScanRun[]
  /** The scans before and after this run's (with a run), for paging. */
  prev_scan: string | null
  next_scan: string | null
  phase_issues: PhaseIssue[]
  recoveries: PhaseRecovery[]
  /** The pipeline DAG, when its head recorded one */
  dag?: RunDagRow | null
}

export function scanRunDetail(rows: ScanRunRows, id: string, now: number): ScanRunDetail | null {
  const run = rows.runs.find(r => r.run_id === id)
  if (!run) return null
  const phases = rows.phases.filter(p => p.run_id === id).sort((a, b) => a.seq - b.seq)
  // a scan's runs (attempts, REPROCs): a maintenance run is never one, though its `scan` names the scan it follows
  const heads = rows.runs.filter(r => !r.parent && (run.kind === 'access-ingest' ? r.kind === run.kind : isScanProducer(r.kind)))
  const scans = [...new Set(heads.map(r => r.scan))].sort()
  const i = scans.indexOf(run.scan)
  return {
    run, secs: runSecs(run, now), failed_at: failedAt(run, phases), phases, spans: phaseSpans(run, phases, liveUntil(run, now)),
    outputs: withDeltas(run, rows.outputs, outputIndex(rows.runs, rows.outputs)),
    downstream: rows.runs.filter(r => r.parent === id).sort(byRunTime),
    siblings: heads.filter(r => r.scan === run.scan && r.run_id !== id).sort(byRunTime),
    prev_scan: i > 0 ? scans[i - 1] : null,
    next_scan: i >= 0 && i < scans.length - 1 ? scans[i + 1] : null,
    phase_issues: phaseIssues(run, rows.runs, rows.phases),
    recoveries: phaseRecoveries(run, rows.runs, rows.phases, now),
  }
}
