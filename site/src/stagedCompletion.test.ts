import { expect, it } from 'vitest'
import type { PrefixExecution } from './prefixExecution'
import { batchCompletion, batchIsCollapsed, isCurrentlyStaged, liveStagedItems, stagedMapTree, stagedState } from './stagedCompletion'

const deleted: PrefixExecution = { kind: 'recorded', runId: 'run', objects: 5, bytes: 50, preserved: 0, undone: 0, deadline: 700, expired: false }

it('distinguishes recorded deletions, empty scans, and files still present at the selected scan', () => {
  expect([
    stagedState(undefined, true, deleted),
    stagedState({ bytes: 0, objects: 0 }, true, deleted),
    stagedState(undefined, true, null),
    stagedState({ bytes: 50, objects: 5 }, true, deleted),
    stagedState({ bytes: 50, objects: 5 }, true, null),
    stagedState({ bytes: 0, objects: 5 }, true, null),
    stagedState(undefined, false, null),
    stagedState(undefined, false, deleted),
  ]).toEqual(['deleted', 'deleted', 'empty', 'recorded', 'pending', 'pending', 'unknown', 'recorded'])
})

it('does not mark restored or actively executing prefixes complete even at an empty scan', () => {
  expect([
    stagedState(undefined, true, { ...deleted, undone: 1 }),
    stagedState(undefined, true, { ...deleted, undone: 5 }),
    stagedState(undefined, true, { kind: 'live-prefix', runId: 'run', objects: 5, bytes: 50 }),
    stagedState(undefined, true, { kind: 'live-bucket', runId: 'run', done: true }),
  ]).toEqual(['restored', 'restored', 'running', 'running'])
})

it('folds settled batches by default without losing explicit expansion, permalinks, or flat rows', () => {
  const settled = batchCompletion(['deleted', 'deleted', 'empty'])
  expect(settled).toEqual({ deleted: 2, empty: 1, restored: 0, recorded: 0, pending: 0, running: 0, unknown: 0, settled: true })
  expect([
    batchIsCollapsed(settled.settled, undefined),
    batchIsCollapsed(settled.settled, false),
    batchIsCollapsed(settled.settled, undefined, true),
    batchIsCollapsed(settled.settled, true, false, true),
    batchIsCollapsed(false, undefined),
    batchIsCollapsed(false, true),
  ]).toEqual([true, false, false, false, false, true])
  expect(batchCompletion(['deleted', 'running', 'restored', 'recorded', 'pending', 'unknown'])).toEqual({ deleted: 1, empty: 0, restored: 1, recorded: 1, pending: 1, running: 1, unknown: 1, settled: false })
  expect(batchCompletion([]).settled).toBe(false)
  expect((['deleted', 'empty', 'restored', 'recorded', 'pending', 'running', 'unknown'] as const).map(isCurrentlyStaged)).toEqual([false, false, true, true, true, true, true])
})

it('skips absent and zero-byte treemaps but preserves populated maps', () => {
  expect(stagedMapTree(['gs://b/a/'], {}, 'staged')).toBeNull()
  expect(stagedMapTree(['gs://b/a/'], { 'gs://b/a/': { bytes: 0, objects: 5 } }, 'staged')).toBeNull()
  expect(stagedMapTree(['gs://b/a/'], { 'gs://b/a/': { bytes: 50, objects: 5 } }, 'staged')).toEqual({ name: 'staged', kind: 'dir', bytes: 50, objects: 5, children: [{ name: 'b', kind: 'dir', bytes: 50, objects: 5, children: [{ name: 'a', kind: 'dir', bytes: 50, objects: 5 }] }] })
})

it('keeps the plan items the newest scan still holds, and every item until sizes are known', () => {
  const items = [{ prefix: 'gs://b/gone/' }, { prefix: 'gs://b/zero/' }, { prefix: 'gs://b/here/' }]
  expect(liveStagedItems(items, { 'gs://b/zero/': { bytes: 0, objects: 0 }, 'gs://b/here/': { bytes: 50, objects: 5 } })).toEqual([{ prefix: 'gs://b/here/' }])
  expect(liveStagedItems(items, undefined)).toEqual(items)
})
