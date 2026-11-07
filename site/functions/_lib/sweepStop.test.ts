import { expect, it, vi } from 'vitest'
import { intentionalStop } from './sweepStop'
import { NotFoundError } from '@rdub/file-tree'

it('requires both a stop marker and the interrupted executor exit', async () => {
  const job = { job_id: 'gcs-sweep-real-20261004-222343z', state: 'FAILED', last_event: 'Task exit code 130.' }
  const key = `sweep/runs/${job.job_id}/STOP`
  const present = vi.fn(async () => ({ bytes: new Uint8Array([10]) }))
  const absent = vi.fn(async () => { throw new NotFoundError(key) })
  expect([
    await intentionalStop(job, present),
    await intentionalStop(job, absent),
    await intentionalStop({ ...job, last_event: 'Task exit code 1.' }, present),
    await intentionalStop({ ...job, state: 'RUNNING' }, present),
  ]).toEqual([true, false, false, false])
  expect(present.mock.calls).toEqual([[key, { offset: 0, length: 1 }]])
})
