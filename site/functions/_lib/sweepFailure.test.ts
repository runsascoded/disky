import { expect, it, vi } from 'vitest'
import { NotFoundError } from '@rdub/file-tree'
import { failureReason } from './sweepFailure'

const JOB = 'gcs-sweep-real-20261104-120000z'
const file = (doc: unknown) => ({ bytes: new TextEncoder().encode(JSON.stringify(doc)) })
const missing = (key: string) => { throw new NotFoundError(key) }

it('reads the run dir first, then a real run\'s `source/`, then gives up', async () => {
  const ranged = { offset: 0, length: 8192 }
  const execute = vi.fn(async () => file({ step: 'execute-manifest', reason: 'refused', ts: 1 }))
  expect(await failureReason(JOB, execute)).toBe('refused')
  expect(execute.mock.calls).toEqual([[`sweep/runs/${JOB}/failure.json`, ranged]])

  const manifest = vi.fn(async (key: string) => key.includes('/source/') ? file({ step: 'manifest', reason: 'no listing', ts: 1 }) : missing(key))
  expect(await failureReason(JOB, manifest)).toBe('no listing')
  expect(manifest.mock.calls).toEqual([[`sweep/runs/${JOB}/failure.json`, ranged], [`sweep/runs/${JOB}/source/failure.json`, ranged]])

  expect(await failureReason(JOB, async key => missing(key))).toBe(null)
})

it('a store error is not "no reason"', async () => {
  await expect(failureReason(JOB, async () => { throw new Error('503') })).rejects.toThrow('503')
})
