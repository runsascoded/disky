import { describe, expect, it } from 'vitest'
import { confirmUndoTerms, minimumWarning, runHasNoUndo, runNoUndoText, slackUndoTerms } from './softDelete'

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
