import { describe, expect, it } from 'vitest'
import { inMatchRoots } from './filterTree'

describe('inMatchRoots: under a filter, only rows inside a match root take actions', () => {
  const rows = ['marin-us-east5', 'marin-us-east5/tomat', 'marin-us-east5/tomat/run-1', 'marin-us-east5/tomato-x', 'marin-us-central1', 'marin-us-central1/users/tomat']
  const acts = (f: ((p: string) => boolean) | undefined) => rows.map(p => [p, f ? f(p) : true])

  it('the 2026-10-02 case: a bucket row holding matches is not itself actionable', () => {
    expect(acts(inMatchRoots(['marin-us-east5/tomat', 'marin-us-central1/users/tomat'], true))).toEqual([
      ['marin-us-east5', false],
      ['marin-us-east5/tomat', true],
      ['marin-us-east5/tomat/run-1', true],
      ['marin-us-east5/tomato-x', false],
      ['marin-us-central1', false],
      ['marin-us-central1/users/tomat', true],
    ])
  })

  it('roots still loading: nothing acts; no filter: everything does', () => {
    expect(acts(inMatchRoots(undefined, true)).map(([, a]) => a)).toEqual(rows.map(() => false))
    expect(inMatchRoots(['x'], false)).toBe(undefined)
  })
})
