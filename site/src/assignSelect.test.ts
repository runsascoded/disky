import { describe, expect, it } from 'vitest'
import { isBucketPrefix } from './AssignSelect'

describe('isBucketPrefix: the assignments that need a confirmation', () => {
  it('a bare bucket only', () => {
    expect(['gs://marin-us-east5/', 'gs://marin-us-east5/tomat/', 'gs://marin-us-east5', 's3://b/', 'gs://b/x'].map(isBucketPrefix))
      .toEqual([true, false, false, true, false])
  })
})
