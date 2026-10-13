// Scan runs (specs/scan-runs-ui.md): the D1 reads behind `/api/scan-runs`.
// The three tables are small (a row per job run, its phases, its outputs), so
// each request reads a store's rows whole and derives the views in
// `src/scanRunsModel.ts`. A D1 without the migration (`0016_scan_runs.sql`)
// answers `configured: false` rather than 500.
import type { D1Database } from '@cloudflare/workers-types'
import {
  byRunTime, failedAt, outputIndex, type OutputDelta, phaseSpans, type PhaseSpan, runSecs, type RunSummary, type ScanRun,
  type ScanRunOutput, type ScanRunPhase, summarize, withDeltas,
} from '../../src/scanRunsModel.js'

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
}

export function scanRunDetail(rows: ScanRunRows, id: string, now: number): ScanRunDetail | null {
  const run = rows.runs.find(r => r.run_id === id)
  if (!run) return null
  const phases = rows.phases.filter(p => p.run_id === id).sort((a, b) => a.seq - b.seq)
  const heads = rows.runs.filter(r => !r.parent)
  const scans = [...new Set(heads.map(r => r.scan))].sort()
  const i = scans.indexOf(run.scan)
  return {
    run, secs: runSecs(run, now), failed_at: failedAt(run, phases), phases, spans: phaseSpans(run, phases),
    outputs: withDeltas(run, rows.outputs, outputIndex(rows.runs, rows.outputs)),
    downstream: rows.runs.filter(r => r.parent === id).sort(byRunTime),
    siblings: heads.filter(r => r.scan === run.scan && r.run_id !== id).sort(byRunTime),
    prev_scan: i > 0 ? scans[i - 1] : null,
    next_scan: i >= 0 && i < scans.length - 1 ? scans[i + 1] : null,
  }
}
