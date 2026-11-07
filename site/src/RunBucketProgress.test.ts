import { expect, it } from 'vitest'
import { bucketNodes, deletionTree } from './RunBucketProgress'
import type { ProgressFile } from './runs'

it('distinguishes future buckets, active work and final interruptions', () => {
  const plan = { buckets: { a: { eligible: { bytes: 1000, objects: 100 } }, b: { eligible: { bytes: 500, objects: 50 } } } }
  const p: ProgressFile = { roots: 1, roots_done: 0, decisions: { delete: 40, skipped_gone: 5 }, delete_bytes: 600, started: 't', updated: 'u', done: false }
  expect(bucketNodes(plan, new Map([['a', p]]), null, true)).toEqual([
    { name: 'a', bytes: 1000, objects: 100, deletedBytes: 600, deletedObjects: 40, known: true, state: 'running', updated: 'u' },
    { name: 'b', bytes: 500, objects: 50, deletedBytes: 0, deletedObjects: 0, known: false, state: 'not started', updated: undefined },
  ])
  expect(bucketNodes(plan, new Map(), { buckets: { a: { decisions: { delete: 40 }, delete_bytes: 600, interrupted: { roots: 1, roots_skipped: 1 } } } }, false)).toEqual([
    { name: 'a', bytes: 1000, objects: 100, deletedBytes: 600, deletedObjects: 40, known: true, state: 'stopped', updated: undefined },
    { name: 'b', bytes: 500, objects: 50, deletedBytes: 0, deletedObjects: 0, known: false, state: 'not recorded', updated: undefined },
  ])
})

it('aggregates final prefix bands into a drillable tree without losing totals', () => {
  const band = (prefix: string, bytes: number, objects: number) => ({ prefix, bytes, objects, gone: 0, overwritten: 0, drift_new_objects: 0, undone_objects: 0 })
  const tree = deletionTree([band('gs://b/checkpoints/a/', 100, 2), band('gs://b/checkpoints/z/', 300, 3)])
  const { children, ...root } = tree
  expect(root).toEqual({ name: 'recorded deletions', bytes: 400, objects: 5, deletedBytes: 400, deletedObjects: 5, known: true, state: 'recorded' })
  const leaf = (name: string, bytes: number, objects: number) => ({ name, prefix: `gs://b/checkpoints/${name}/`, bytes, objects, deletedBytes: bytes, deletedObjects: objects, known: true, state: 'recorded' })
  expect(children).toEqual([{ name: 'b', prefix: 'gs://b/', bytes: 400, objects: 5, deletedBytes: 400, deletedObjects: 5, known: true, state: 'recorded', children: [{ name: 'checkpoints', prefix: 'gs://b/checkpoints/', bytes: 400, objects: 5, deletedBytes: 400, deletedObjects: 5, known: true, state: 'recorded', children: [leaf('a', 100, 2), leaf('z', 300, 3)] }] }])
})
