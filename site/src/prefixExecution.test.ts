import { expect, it } from 'vitest'
import { conservativeDeadline, prefixExecution, type PrefixBandRecord } from './prefixExecution'
import type { ProgressFile } from './runs'

const prefix = 'gs://b/a/'
const record: PrefixBandRecord = { prefix, run_id: 'real', started_ts: 100, finished_ts: 200, undo_deadline: 800, undo_state: 'none', bytes: 50, objects: 5, gone: 0, overwritten: 0, drift_new_objects: 0, undone_objects: 0 }
const progress: ProgressFile = { roots: 1, roots_done: 0, decisions: { delete: 10 }, delete_bytes: 100, started: '2026-10-05T00:00:00Z', updated: '2026-10-05T00:00:30Z', done: false }

it('does not mistake bucket progress for exact prefix counts or subtract it from a scan', () => {
  expect(prefixExecution(prefix, [], { runId: 'live', progress }, 300)).toEqual({ kind: 'live-bucket', runId: 'live', done: false })
  expect(prefixExecution(prefix, [], { runId: 'live', progress: { ...progress, done: true } }, 300)).toEqual({ kind: 'live-bucket', runId: 'live', done: true })
  expect(prefixExecution(prefix, [], undefined, 300)).toBeNull()
})
it('uses explicit per-prefix live counters when the executor supplies them', () => {
  expect(prefixExecution(prefix, [record], { runId: 'live', progress: { ...progress, bands: { [prefix]: { objects: 2, bytes: 20 } } } }, 300)).toEqual({ kind: 'live-prefix', runId: 'live', objects: 2, bytes: 20 })
})
it('links recorded outcomes with conservative retention, excluding other prefixes', () => {
  expect(conservativeDeadline(record)).toBe(700)
  expect(conservativeDeadline({ ...record, finished_ts: null })).toBeNull()
  expect(prefixExecution(prefix, [{ ...record, prefix: 'gs://b/ab/', started_ts: 500 }, record], undefined, 300)).toEqual({ kind: 'recorded', runId: 'real', objects: 5, bytes: 50, undone: 0, deadline: 700, expired: false })
  expect(prefixExecution(prefix, [record], undefined, 700)).toEqual({ kind: 'recorded', runId: 'real', objects: 5, bytes: 50, undone: 0, deadline: 700, expired: true })
})
