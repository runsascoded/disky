// What a dispatch says it selected: display only, carried in the job's env so a run's row has a size before its
// executor records one (`/api/sweep/jobs`), and named in Slack's "dispatched" line.
import { describe, expect, it } from 'vitest'
import { expectedOfVars, expectedVars, parseExpected } from './dispatch.js'
import { runEvent, type RunRow } from './stagedSlack.js'

const run: RunRow = {
  run_id: 'gcs-sweep-real-20261105-022438z', mode: 'real', scan: '2026-11-05T0702', actor: 'ann@example.org', started_ts: 0, finished_ts: null,
  deleted_bytes: 0, deleted_objects: 0, skipped_gone: 0, skipped_overwritten: 0, plan_digest: 'd',
}

describe('parseExpected: a client-sent total, display only', () => {
  it('keeps two finite non-negative numbers', () => {
    expect(parseExpected({ bytes: 137_000, objects: 754 })).toEqual({ bytes: 137_000, objects: 754 })
    expect(parseExpected({ bytes: 0, objects: 0 })).toEqual({ bytes: 0, objects: 0 })
  })
  it('drops anything else, and never refuses the dispatch over it', () => {
    expect([undefined, null, 5, 'x', {}, { bytes: 1 }, { bytes: -1, objects: 1 }, { bytes: '1', objects: 1 }, { bytes: Infinity, objects: 1 }, { bytes: NaN, objects: 1 }].map(parseExpected))
      .toEqual(Array(10).fill(undefined))
  })
})

describe('the job env round trip', () => {
  it('is what the jobs list reads back', () => {
    const vars = expectedVars({ bytes: 137_000, objects: 754 })
    expect(vars).toEqual({ EXPECTED_BYTES: '137000', EXPECTED_OBJECTS: '754' })
    expect(expectedOfVars(vars)).toEqual({ bytes: 137_000, objects: 754 })
  })
  it('a dispatch without a total adds no env, and an older job has none to read', () => {
    expect(expectedVars(undefined)).toEqual({})
    expect(expectedOfVars({ USER: 'ann' })).toBeUndefined()
  })
})

describe("Slack's dispatched line", () => {
  it('names the expected size when the dispatch knew it', () => {
    expect(runEvent(run, 'dispatched', { via: 'www', expected: { bytes: 2 ** 40, objects: 754_167 } }))
      .toBe(':rotating_light: *Real deletion* dispatched by ann via www on scan 2026-11-05T0702 (`gcs-sweep-real-20261105-022438z`): expected 1.0 TiB / 754,167 objects')
  })
  it('says nothing of it otherwise', () => {
    expect(runEvent(run, 'dispatched', { via: 'www' }))
      .toBe(':rotating_light: *Real deletion* dispatched by ann via www on scan 2026-11-05T0702 (`gcs-sweep-real-20261105-022438z`)')
  })
})
