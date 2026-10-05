import { describe, expect, it } from 'vitest'
import { stagedDesc } from './staged'

describe('stagedDesc: the /staged unfurl says what the open plan holds, by count', () => {
  it('none, one, many', () => {
    expect([stagedDesc(null, 0), stagedDesc(1, 1), stagedDesc(1, 1242)]).toEqual([
      'Nothing is staged for deletion. Stage prefixes from the map; an admin reviews and dispatches here.',
      '1 prefix staged for deletion · plan #1 · nothing is deleted until an admin dispatches it.',
      '1,242 prefixes staged for deletion · plan #1 · nothing is deleted until an admin dispatches it.',
    ])
  })
})
