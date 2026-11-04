import { describe, expect, it } from 'vitest'
import { conservativeDeadline, inRunItems, liveTargetOf, prefixExecution, type PrefixBandRecord } from './prefixExecution'
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
  expect(prefixExecution(prefix, [{ ...record, prefix: 'gs://b/ab/', started_ts: 500 }, record], undefined, 300)).toEqual({ kind: 'recorded', runId: 'real', objects: 5, bytes: 50, preserved: 0, undone: 0, deadline: 700, expired: false })
  expect(prefixExecution(prefix, [record], undefined, 700)).toEqual({ kind: 'recorded', runId: 'real', objects: 5, bytes: 50, preserved: 0, undone: 0, deadline: 700, expired: true })
})

it('explains preserved changed/new objects even when the run deleted none of a prefix', () => {
  expect(prefixExecution(prefix, [{ ...record, objects: 0, bytes: 0, drift_new_objects: 9, overwritten: 1 }], undefined, 300)).toEqual({
    kind: 'recorded', runId: 'real', objects: 0, bytes: 0, preserved: 10, undone: 0, deadline: 700, expired: false,
  })
})

describe('a row\'s live run: the run\'s own item set, not its bucket', () => {
  const items = { sweep: ['gs://b/a/', 'gs://b/c/d/'], objects: ['gs://b/e/key'] }
  it('inRunItems: an item, or under one; an unread plan says no', () => {
    expect([
      inRunItems('gs://b/a/', items),
      inRunItems('gs://b/a/x/', items),
      inRunItems('gs://b/e/key', items),
      inRunItems('gs://b/ab/', items),
      inRunItems('gs://b/c/', items),
      inRunItems('gs://b/e/key2', items),
      inRunItems('gs://b/a/', null),
      inRunItems('gs://b/a/', undefined),
      inRunItems('gs://b/a/', {}),
    ]).toEqual([true, true, true, false, false, false, false, false, false])
  })
  const target = (bucket: string, started_ts: number, plan?: { sweep?: string[]; objects?: string[] } | null) => ({ bucket, run: { started_ts }, items: plan })
  it('liveTargetOf: a prefix staged after the dry run, in the bucket of a live run without it, has none', () => {
    const targets = [target('b', 200, items)]
    expect(liveTargetOf('gs://b/a/', 100, targets)).toBe(0)
    expect(liveTargetOf('gs://b/later/', 100, targets)).toBe(-1)
  })
  it('liveTargetOf: another bucket\'s run, a run older than the stage, or a plan not read yet: none', () => {
    expect(liveTargetOf('gs://b2/a/', 100, [target('b', 200, { sweep: ['gs://b2/a/'] })])).toBe(-1)
    expect(liveTargetOf('gs://b/a/', 300, [target('b', 200, items)])).toBe(-1)
    expect(liveTargetOf('gs://b/a/', 100, [target('b', 200, undefined)])).toBe(-1)
    expect(liveTargetOf('gs://b/a/', 100, [target('b', 200, null)])).toBe(-1)
  })
  it('liveTargetOf: of two live runs in a bucket, the one holding it', () => {
    expect(liveTargetOf('gs://b/z/', 100, [target('b', 200, items), target('b', 250, { sweep: ['gs://b/z/'] })])).toBe(1)
  })
  it('a row outside the run reads as not running', () => {
    const ix = liveTargetOf('gs://b/later/', 100, [target('b', 200, items)])
    expect(ix).toBe(-1)
    expect(prefixExecution('gs://b/later/', [], undefined, 300)).toBeNull()
  })
})
