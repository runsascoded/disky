import { describe, expect, it } from 'vitest'
import type { HistoryPlan } from '../functions/_lib/historyAdmin'
import type { RunSummary, ScanRun } from './scanRunsModel'
import { retentionLabel, retentionRunSelectable, retentionScans, retentionSpanScans, toggleRetentionScan } from './scanRetention'

const A = '2026-11-01', B = '2026-11-02', C = '2026-11-03', D = '2026-11-04', E = '2026-11-05'
function row(scan: string, started_ts: number, extra: Partial<ScanRun> = {}): RunSummary {
  return {
    run: { run_id: `${scan}-${started_ts}`, store: 'primary', scan, kind: 'scan', parent: null, status: 'succeeded',
      failed_phase: null, error: null, started_ts, finished_ts: started_ts + 1, job_name: null, region: null, machine: null,
      spot: null, tasks: null, image: null, cost_usd: null, gen: null, source: 'job', updated_ts: started_ts + 1, ...extra },
    secs: 1, spans: [], out_bytes: null, delta_bytes: null, downstream: 0, cost_usd: null, failed_at: null, phase_issues: [],
  }
}
const RUNS = [row(A, 100), row(B, 200, { status: 'failed' }), row(B, 220), row(B, 240), row(C, 300), row(D, 400, { kind: 'compact' }), row(E, 500, { status: 'failed' })]
const PLAN: HistoryPlan = { digest: 'plan', scans: [A, B, C], retained: [A, C], steps: [{ scan: B, before: A, after: C, score: 4, left: { count: 2 }, right: { count: 4 }, merged: { count: 6 } }] }

describe('scan retention over run history', () => {
  it('deduplicates retries, ignores maintenance, and protects the first/latest published observations', () => {
    const model = retentionScans(RUNS)
    expect([...model.values()]).toEqual([
      { scan: A, available: true, protected: true, reasons: ['first scan'], proposal: null, score: null, left: null, right: null },
      { scan: B, available: true, protected: false, reasons: [], proposal: null, score: null, left: null, right: null },
      { scan: C, available: true, protected: true, reasons: ['latest scan'], proposal: null, score: null, left: null, right: null },
      { scan: E, available: false, protected: false, reasons: [], proposal: null, score: null, left: null, right: null },
    ])
    expect(RUNS.map(s => retentionRunSelectable(s.run, model))).toEqual([false, true, true, true, false, false, false])
  })

  it('uses the reviewed source roster and exact removal step instead of run status or output-byte deltas', () => {
    const model = retentionScans(RUNS, PLAN)
    expect(model.get(B)).toEqual({ scan: B, available: true, protected: false, reasons: [], proposal: 'remove', score: 4, left: 2, right: 4 })
    expect([retentionLabel(model.get(A)), retentionLabel(model.get(B)), retentionLabel(model.get(B), true), retentionLabel(model.get(E))]).toEqual([
      'Keep · first scan', 'Proposed removal', 'Selected for removal', 'Not published in the reviewed history',
    ])
    expect(retentionScans([row(B, 200, { status: 'failed' })], PLAN).get(B)?.available).toBe(true)
    expect(retentionRunSelectable(row(B, 200, { status: 'failed' }).run, model)).toBe(true)
  })

  it('disables successful old run dates outside the current writer source before a preview exists', () => {
    const runs = [...RUNS, row(D, 440)]
    const source = { scans: [B, C, D, E], pinned: [C] }
    const model = retentionScans(runs, null, source)
    expect([...model.values()].map(s => [s.scan, s.available, s.protected, s.reasons])).toEqual([
      [A, false, false, []], [B, true, true, ['first scan']], [C, true, true, ['pinned']],
      [D, true, false, []], [E, true, true, ['latest scan']],
    ])
    expect(runs.map(s => retentionRunSelectable(s.run, model))).toEqual([false, false, false, false, false, false, false, true])
    expect(retentionSpanScans(runs, [100, 500], model)).toEqual([D])
    expect(retentionScans(runs, PLAN, source).get(B)).toEqual({
      scan: B, available: true, protected: false, reasons: [], proposal: 'remove', score: 4, left: 2, right: 4,
    })
  })

  it('a failed head can select a stored source scan while a failed date outside the source remains unavailable', () => {
    const failed = [row(B, 200, { status: 'failed' }), row(E, 500, { status: 'failed' })]
    const model = retentionScans(failed, null, { scans: [A, B, C], pinned: [] })
    expect([model.get(B)?.available, model.get(E)?.available]).toEqual([true, false])
    expect(failed.map(s => retentionRunSelectable(s.run, model))).toEqual([true, false])
    expect(retentionSpanScans(failed, [100, 500], model)).toEqual([B])
    expect(toggleRetentionScan([], B, model)).toEqual([B])
  })

  it('preserves reviewed pins and backend protection reasons even inside the locally loaded roster', () => {
    const model = retentionScans(RUNS, { ...PLAN, required_pins: [B], candidates: [{ scan: B, before: A, after: C, left: { count: 8 }, right: { count: 9 }, protected: true, reasons: ['required-pin'], score: 9 }] })
    expect(model.get(B)).toEqual({ scan: B, available: true, protected: true, reasons: ['required-pin', 'pinned'], proposal: 'remove', score: 4, left: 2, right: 4 })
    expect(toggleRetentionScan([], B, model)).toEqual([])
  })

  it('a window only becomes a unique removable scan selection through the explicit brush action', () => {
    const model = retentionScans(RUNS)
    expect(retentionSpanScans(RUNS, null, model)).toEqual([])
    expect(retentionSpanScans(RUNS, [100, 500], model)).toEqual([B])
    expect(retentionSpanScans(RUNS, [300, 500], model)).toEqual([])
    expect(toggleRetentionScan([B, B], B, model)).toEqual([])
    expect(toggleRetentionScan([], B, model)).toEqual([B])
    expect(toggleRetentionScan([B], A, model)).toEqual([B])
  })
})
