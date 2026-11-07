import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter } from 'react-router-dom'
import { expect, it } from 'vitest'
import { PrefixRecovery, recoveryNotes } from './PrefixRecovery'
import { prefixExecution, type PrefixBandRecord } from './prefixExecution'

it('shows preserved objects beside the recorded deletion count, with a run-detail link', () => {
  const markup = renderToStaticMarkup(createElement(MemoryRouter, null, createElement(PrefixRecovery, {
    execution: { kind: 'recorded', runId: 'real-run', objects: 5, bytes: 50, preserved: 10, undone: 0, deadline: null, expired: false },
    fmtBytes: bytes => `${bytes} B`,
  })))
  expect(markup.replace(/<[^>]*>/g, '')).toBe('5 logged deleted · 10 changed/new preserved · details')
  expect([...markup.matchAll(/href="([^"]*)"/g)].map(m => m[1])).toEqual(['/runs/real-run#recovery'])
})

it('a recorded deletion on a bucket with soft delete off reads "undo unavailable", not "recovery info"', () => {
  const label = (extra: object = {}) => renderToStaticMarkup(createElement(MemoryRouter, null, createElement(PrefixRecovery, {
    execution: { kind: 'recorded', runId: 'real-run', objects: 5, bytes: 50, preserved: 0, undone: 0, deadline: 700, expired: false, ...extra },
    fmtBytes: bytes => `${bytes} B`,
  }))).replace(/<[^>]*>/g, '')
  expect([label({ unavailable: { buckets: ['b'], asOf: '2026-11-05' } }), label({ unseen: { unseen: ['b'] } }), label()])
    .toEqual(['5 logged deleted · undo unavailable', '5 logged deleted · recovery info · unverified', '5 logged deleted · recovery info'])
})

it('recoveryNotes: the tooltip states the reason instead of a cutoff', () => {
  const e = { kind: 'recorded' as const, runId: 'real-run', objects: 5, bytes: 50, preserved: 0, undone: 0, deadline: 700, expired: false }
  expect(recoveryNotes({ ...e, unavailable: { buckets: ['b'], asOf: '2026-11-05' } })).toEqual([
    'Undo unavailable: soft delete is off on b (as of 2026-11-05). Undo restores from what the bucket keeps after a delete, so its soft delete (versioning, on S3) has to be on.',
    'Soft-delete retention starts at each object’s deletion, not when the run finishes. Empty at a scan is not proof of deletion by this run.',
  ])
  expect(recoveryNotes(e)).toEqual([
    'Open recovery details. Prefix-filtered restore is available through the CLI; the web undo button restores the whole run.',
    'Soft-delete retention starts at each object’s deletion, not when the run finishes. Empty at a scan is not proof of deletion by this run.',
    'Undo restores every logged generation. An XML run’s log can’t tell an object it deleted from one someone else deleted after the scan, so undo can bring those back too.',
    'Conservative recovery cutoff: 1970-01-01T00:11:40.000Z',
  ])
  expect(recoveryNotes({ ...e, unseen: { unseen: ['b'] } }).slice(3)).toEqual([
    'Conservative recovery cutoff: 1970-01-01T00:11:40.000Z',
    "Undo depends on soft delete still being on for b, which this site can't see. The undo job checks the bucket itself, and reports anything it can't restore.",
  ])
  expect(recoveryNotes({ ...e, deadline: null, noUndo: true })).toEqual(['This run recorded no undo window: soft delete was off, or it left no record of its retention. Treat its deletes as permanent.'])
})

it('a restored band on a bucket with soft delete off: "restored", and a tooltip with no "undo unavailable" note', () => {
  const band: PrefixBandRecord = { prefix: 'gs://b/a/', run_id: 'real-run', started_ts: 100, finished_ts: 200, undo_deadline: 800, undo_state: 'full', bytes: 50, objects: 5, gone: 0, overwritten: 0, drift_new_objects: 0, undone_objects: 5 }
  const execution = prefixExecution(band.prefix, [band], undefined, 300, { asOf: '2026-11-05', off: { b: true } })
  const markup = renderToStaticMarkup(createElement(MemoryRouter, null, createElement(PrefixRecovery, { execution, fmtBytes: bytes => `${bytes} B` })))
  expect(markup.replace(/<[^>]*>/g, '')).toBe('5 logged deleted · restored')
  expect(recoveryNotes(execution!)).toEqual([
    'Open recovery details. Prefix-filtered restore is available through the CLI; the web undo button restores the whole run.',
    'Soft-delete retention starts at each object’s deletion, not when the run finishes. Empty at a scan is not proof of deletion by this run.',
    'Undo restores every logged generation. An XML run’s log can’t tell an object it deleted from one someone else deleted after the scan, so undo can bring those back too.',
    'Conservative recovery cutoff: 1970-01-01T00:11:40.000Z',
  ])
})
