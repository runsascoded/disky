import { describe, expect, it } from 'vitest'
import { withoutStarters, type ScanRunRows } from '../_lib/scanRuns.js'
import type { ScanRun } from '../../src/scanRunsModel.js'

const run = (run_id: string, trigger: 'schedule' | 'manual', trigger_by: string | null) =>
  ({ run_id, scan: '2026-11-16T1055', kind: 'scan', status: 'succeeded', trigger, trigger_by }) as unknown as ScanRun
const rows: ScanRunRows = { runs: [run('a', 'manual', 'ada@example.org'), run('b', 'schedule', null)], phases: [], outputs: [] }

describe('withoutStarters', () => {
  it('keeps a manual run’s starter for a signed-in viewer and a grant', () => {
    expect(withoutStarters(rows, { via: 'session' })).toBe(rows)
    expect(withoutStarters(rows, { via: 'grant' })).toBe(rows)
  })
  it('drops every run’s starter for a public viewer, and nothing else', () => {
    expect(withoutStarters(rows, { via: 'public' })).toEqual({
      runs: [
        { run_id: 'a', scan: '2026-11-16T1055', kind: 'scan', status: 'succeeded', trigger: 'manual' },
        { run_id: 'b', scan: '2026-11-16T1055', kind: 'scan', status: 'succeeded', trigger: 'schedule' },
      ],
      phases: [],
      outputs: [],
    })
  })
  it('passes a D1 without the tables through', () => {
    expect(withoutStarters(null, { via: 'public' })).toBe(null)
  })
})
