import { expect, it } from 'vitest'
import { HttpError } from './batches'
import { dispatchFailure } from './dispatchStatus'

it('distinguishes an explicit refusal from an uncertain submission; never encourages an automatic retry', () => {
  expect([
    dispatchFailure(new HttpError('Agent disconnected; items remain staged.', 503, { code: 'cleanup_agent_offline' })),
    dispatchFailure(new HttpError('Another run is in progress.', 409)),
    dispatchFailure(new Error('Failed to fetch')),
    dispatchFailure(new HttpError('Gateway timeout', 504)),
  ]).toEqual([
    'Agent disconnected; items remain staged.',
    'Another run is in progress.',
    'Submission could not be confirmed. Check Runs before retrying: the request may have reached the executor. Failed to fetch',
    'Submission could not be confirmed. Check Runs before retrying: the request may have reached the executor. Gateway timeout',
  ])
})
