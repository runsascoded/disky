import { describe, expect, it } from 'vitest'
import { bucketSoftDeleteOf, confirmUndoTerms, minimumWarning, runHasNoUndo, runNoUndoText, runUndoUnavailable, runUndoUnseen, slackUndoTerms, undoneText, undoUnavailable, undoUnavailableText, undoUnseen, undoUnseenText } from './softDelete'

describe('slackUndoTerms', () => {
  it.each([
    [14, 'Recoverable for at least 14 days (undo in www).'],
    [7, 'Recoverable for at least 7 days (undo in www).'],
    [3, 'Recoverable for at least 3 days, less than the usual 7 (undo in www).'],
    [1, 'Recoverable for at least 1 day, less than the usual 7 (undo in www).'],
    [0, 'No undo: this deployment doesn\'t require soft delete, so treat these deletes as permanent.'],
  ])('%i', (min, text) => expect(slackUndoTerms(min)).toBe(text))
})

describe('confirmUndoTerms', () => {
  const known = { asOf: '2026-11-04', off: { on1: false, on2: false, off1: true, off2: true } }
  it.each<[string, number, string[], typeof known | null, string]>([
    ['the default, no snapshot', 7, ['on1'], null, 'Recoverable for at least 7 days: a bucket retaining less refuses the run.'],
    ['a stricter minimum', 14, ['on1'], known, 'Recoverable for at least 14 days: a bucket retaining less refuses the run.'],
    ['under the usual floor', 3, ['on1'], null, 'Recoverable for at least 3 days, less than the usual 7: a bucket retaining less refuses the run.'],
    ['a minimum, a bucket off: refused, not "no undo"', 7, ['on1', 'off1'], known, 'This run would be refused: soft delete is off on off1 (as of 2026-11-04), and this deployment requires at least 7 days.'],
    ['0, buckets off', 0, ['on1', 'off1', 'off2'], known, 'No undo: soft delete is off on off1, off2 (as of 2026-11-04), so deletes there are permanent.'],
    ['0, all on as of the snapshot', 0, ['on1', 'on2'], known, 'No undo unless soft delete is still on when the run starts: it was on for these buckets as of 2026-11-04, but this deployment doesn\'t require it, so treat these deletes as permanent.'],
    ['0, a bucket the snapshot does not name', 0, ['on1', 'other'], known, 'No undo: this deployment doesn\'t require soft delete, so treat these deletes as permanent.'],
    ['0, no snapshot', 0, ['on1'], null, 'No undo: this deployment doesn\'t require soft delete, so treat these deletes as permanent.'],
  ])('%s', (_, min, buckets, snapshot, text) => expect(confirmUndoTerms(min, buckets, snapshot)).toBe(text))
})

describe('minimumWarning', () => {
  it.each<[number, string | null]>([
    [30, null],
    [7, null],
    [6, 'Below 7 days the undo window is shorter: a run accepts a bucket retaining soft deletes for as little as 6 days, and what it deletes can\'t be restored after that.'],
    [1, 'Below 7 days the undo window is shorter: a run accepts a bucket retaining soft deletes for as little as 1 day, and what it deletes can\'t be restored after that.'],
    [0, 'At 0 there is no undo: a run deletes from a bucket with soft delete off, and those deletes are permanent. Nothing can restore them.'],
  ])('%i', (min, text) => expect(minimumWarning(min)).toBe(text))
})

describe('runNoUndoText', () => {
  it.each<[string, { soft_delete_days?: number | null; min_soft_delete_days?: number | null }, string]>([
    ['0 measured', { soft_delete_days: 0, min_soft_delete_days: 0 }, 'No undo: a bucket in this run had soft delete off, so what it deleted there is gone for good.'],
    ['nothing measured or required', { soft_delete_days: null, min_soft_delete_days: 0 }, 'No undo promised: this run required no soft delete and recorded no retention, so treat its deletes as permanent.'],
  ])('%s', (_, r, text) => expect(runNoUndoText(r)).toBe(text))
})

describe('runHasNoUndo', () => {
  it.each<[string, number | null | undefined, number | null | undefined, boolean]>([
    ['nothing measured, nothing required', null, 0, true],
    ['nothing measured, days required', null, 3, false],
    ['nothing measured, from before the columns', null, null, false],
    ['measured a window, nothing required', 7, 0, false],
  ])('with its minimum: %s', (_, soft_delete_days, min_soft_delete_days, real) => {
    expect(runHasNoUndo({ mode: 'real', soft_delete_days, min_soft_delete_days })).toBe(real)
    expect(runHasNoUndo({ mode: 'dry', soft_delete_days, min_soft_delete_days })).toBe(false)
  })
  it.each<[string, number | null | undefined, boolean, boolean]>([
    ['0 measured', 0, true, false],
    ['a window', 7, false, false],
    ['from before the column', null, false, false],
    ['a row without the column', undefined, false, false],
  ])('%s', (_, soft_delete_days, real, dry) => {
    expect(runHasNoUndo({ mode: 'real', soft_delete_days })).toBe(real)
    expect(runHasNoUndo({ mode: 'dry', soft_delete_days })).toBe(dry)
  })
})

describe('bucketSoftDeleteOf — the job\'s reading as the site uses it', () => {
  const doc = { version: 1, date: '2026-11-05', as_of: '2026-11-05T07:12:03Z', buckets: { a: { retention_seconds: 0 }, b: { retention_seconds: 604800 }, c: { retention_seconds: 3600 } } }
  it('a bucket retaining nothing is off; the reading\'s time to the minute', () => {
    expect(bucketSoftDeleteOf(doc)).toEqual({ asOf: '2026-11-05 07:12Z', off: { a: true, b: false, c: false } })
  })
  it('none, or one of a version this build doesn\'t know: no reading', () => {
    expect([bucketSoftDeleteOf(null), bucketSoftDeleteOf(undefined), bucketSoftDeleteOf({ ...doc, version: 2 })]).toEqual([null, null, null])
  })
})

describe('undoUnseen — a recorded undo this site can\'t vouch for', () => {
  const known = { asOf: '2026-11-05 07:12Z', off: { on1: false, off1: true } }
  it.each<[string, string[], typeof known | null, { unseen: string[] } | null]>([
    ['every bucket in the reading', ['on1', 'off1'], known, null],
    ['a bucket the reading does not name', ['on1', 'other', 'more'], known, { unseen: ['more', 'other'] }],
    ['no reading: every bucket', ['b', 'a', 'a'], null, { unseen: ['a', 'b'] }],
    ['the run\'s buckets not known', [], known, { unseen: [] }],
    ['a bucket named like an object method', ['constructor'], known, { unseen: ['constructor'] }],
  ])('%s', (_, buckets, reading, expected) => expect(undoUnseen(buckets, reading)).toEqual(expected))

  it('says what the undo depends on, and never "unavailable"', () => {
    expect([
      undoUnseenText({ unseen: ['a', 'b'] }),
      undoUnseenText({ unseen: ['a', 'b', 'c', 'd', 'e'] }),
      undoUnseenText({ unseen: [] }),
    ]).toEqual([
      'undo depends on soft delete still being on for a, b, which this site can\'t see',
      'undo depends on soft delete still being on for a, b, c and 2 more, which this site can\'t see',
      'undo depends on the buckets\' soft delete, which this site can\'t see',
    ])
  })

  it('only a real run inside its window, not one known unavailable, and only where the site was asked', () => {
    const r = { mode: 'real', undo_deadline: 900 }
    expect([
      runUndoUnseen(r, ['other'], known, 100),
      runUndoUnseen(r, ['on1'], null, 100),
      runUndoUnseen(r, ['on1'], known, 100),
      runUndoUnseen(r, ['off1', 'other'], known, 100),
      runUndoUnseen(r, ['other'], known, 900),
      runUndoUnseen({ mode: 'dry', undo_deadline: 900 }, ['other'], known, 100),
      runUndoUnseen(r, ['other'], undefined, 100),
    ]).toEqual([{ unseen: ['other'] }, { unseen: ['on1'] }, null, null, null, null, null])
  })
})

describe('undoUnavailable — a recorded undo GCS can no longer do', () => {
  const known = { asOf: '2026-11-05', off: { on1: false, on2: false, off1: true, off2: true } }
  it.each<[string, string[], typeof known | null | undefined, { buckets: string[]; asOf: string } | null]>([
    ['a bucket the snapshot shows off', ['off1'], known, { buckets: ['off1'], asOf: '2026-11-05' }],
    ['buckets it shows on', ['on1', 'on2'], known, null],
    ['mixed: the off ones, sorted, once each', ['on1', 'off2', 'off1', 'off2'], known, { buckets: ['off1', 'off2'], asOf: '2026-11-05' }],
    ['a bucket it does not name: no claim', ['other'], known, null],
    ['no buckets known for the run: no claim', [], known, null],
    ['no snapshot', ['off1'], null, null],
    ['a snapshot still loading, or a deployment that reads none', ['off1'], undefined, null],
  ])('%s', (_, buckets, snapshot, expected) => expect(undoUnavailable(buckets, snapshot)).toEqual(expected))

  it('says which buckets and as of when, counting the rest past four', () => {
    expect([
      undoUnavailableText({ buckets: ['off1'], asOf: '2026-11-05' }),
      undoUnavailableText({ buckets: ['a', 'b', 'c', 'd'], asOf: '2026-11-05' }),
      undoUnavailableText({ buckets: ['a', 'b', 'c', 'd', 'e', 'f'], asOf: '2026-11-05' }),
    ]).toEqual([
      'undo unavailable: soft delete is off on off1 (as of 2026-11-05)',
      'undo unavailable: soft delete is off on a, b, c, d (as of 2026-11-05)',
      'undo unavailable: soft delete is off on a, b, c and 3 more (as of 2026-11-05)',
    ])
  })

  it('only a real run inside its recorded window has an undo to lose', () => {
    const off = { buckets: ['off1'], asOf: '2026-11-05' }
    expect([
      runUndoUnavailable({ mode: 'real', undo_deadline: 900 }, ['off1'], known, 899),
      runUndoUnavailable({ mode: 'real', undo_deadline: 900 }, ['off1'], known, 900),
      runUndoUnavailable({ mode: 'real', undo_deadline: null }, ['off1'], known, 100),
      runUndoUnavailable({ mode: 'dry', undo_deadline: 900 }, ['off1'], known, 100),
      runUndoUnavailable({ mode: 'real', undo_deadline: 900 }, ['on1'], known, 100),
      runUndoUnavailable({ mode: 'real', undo_deadline: 900 }, ['off1'], null, 100),
    ]).toEqual([off, null, null, null, null, null])
  })
  it('a run already undone has no undo left to lose, or to vouch for; one partly undone still has', () => {
    const off = { buckets: ['off1'], asOf: '2026-11-05' }
    const undone = { mode: 'real', undo_deadline: 900, undo_state: 'full' }
    const partly = { mode: 'real', undo_deadline: 900, undo_state: 'partial' }
    expect([
      runUndoUnavailable(undone, ['off1'], known, 100),
      runUndoUnseen(undone, ['other'], known, 100),
      runUndoUnseen(undone, ['on1'], null, 100),
      runUndoUnavailable(partly, ['off1'], known, 100),
      runUndoUnseen(partly, ['other'], known, 100),
    ]).toEqual([null, null, null, off, { unseen: ['other'] }])
  })
})

describe('undoneText', () => {
  it('a run\'s `undo_state`, in the words of its row\'s tag; nothing undone, or a row from before the column, has none', () => {
    expect([{ undo_state: 'full' }, { undo_state: 'partial' }, { undo_state: 'none' }, {}].map(undoneText)).toEqual(['undone', 'partly undone', null, null])
  })
})
