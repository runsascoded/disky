import { describe, expect, it } from 'vitest'
import { noScansYet } from './scan'

// The first-run hint fires on exactly one state of the scan-list query: it
// resolved, and the store published no indexed snapshot. Pending and errored
// lists have their own UI (skeleton, sign-in strip) and must not trip it.
describe('noScansYet', () => {
  const cases: [string, { isSuccess: boolean; data?: string[] }, boolean][] = [
    ['resolved, empty list', { isSuccess: true, data: [] }, true],
    ['resolved, one scan', { isSuccess: true, data: ['2026-09-27'] }, false],
    ['still pending (no data)', { isSuccess: false }, false],
    ['errored (no data)', { isSuccess: false, data: undefined }, false],
    ['refetching after a success, data retained', { isSuccess: true, data: ['2026-09-27', '2026-09-26'] }, false],
  ]
  for (const [name, q, want] of cases) {
    it(name, () => {
      expect(noScansYet(q)).toBe(want)
    })
  }
})
