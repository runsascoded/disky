import { describe, expect, it } from 'vitest'
import { authorName, defaultNoteAt, notesInView, shortLabel, type ChartAnnotation } from './chartNotes'

const note = (id: number, at: string, text = `note ${id}`): ChartAnnotation => ({ id, at, text, author: 'ops@example.test', created: 0, updated: 0 })
const midnight = (d: string) => Date.parse(`${d.slice(0, 10)}T00:00:00Z`)
const DAY = 86_400_000

describe('notesInView', () => {
  // The chart's xOf: a scan it plots sits at its start (10-13's at 07:00); any other date at UTC midnight.
  const starts: Record<string, number> = { '2026-10-13': midnight('2026-10-13') + 7 * 3_600_000 }
  const xOf = (at: string) => starts[at] ?? midnight(at)
  const notes = [note(3, '2026-10-13', 'Scope change: oa-gcs-usage-dvx left the meta store\nnot a deletion'), note(1, '2026-09-01'), note(2, '2026-10-17'), note(4, '2026-10-13')]

  it('all: every note inside the plotted span, by x then id, each labelled by its first line', () => {
    expect(notesInView(notes, xOf, midnight('2026-08-20'), midnight('2026-10-18')).map(m => [m.key, m.x, m.label])).toEqual([
      ['note-1', midnight('2026-09-01'), 'note 1'],
      ['note-3', starts['2026-10-13'], 'Scope change: oa-gcs-usage-…'],
      ['note-4', starts['2026-10-13'], 'note 4'],
      ['note-2', midnight('2026-10-17'), 'note 2'],
    ])
  })

  it('1w: notes before the range (and after the latest scan) are hidden; the span’s ends are inclusive', () => {
    const last = midnight('2026-10-17')
    expect([
      notesInView(notes, xOf, last - 7 * DAY, last).map(m => m.key),
      notesInView(notes, xOf, last - 3 * DAY, last - DAY).map(m => m.key),
      notesInView(notes, xOf, starts['2026-10-13'], starts['2026-10-13']).map(m => m.key),
    ]).toEqual([
      ['note-3', 'note-4', 'note-2'],
      [],
      ['note-3', 'note-4'],
    ])
  })

  it('a note whose x is unknown (NaN) is dropped', () => {
    expect(notesInView([note(1, '2026-10-13')], () => NaN, -Infinity, Infinity)).toEqual([])
  })
})

describe('labels and defaults', () => {
  it('shortLabel / defaultNoteAt / authorName', () => {
    expect([
      shortLabel('short'),
      shortLabel('  first line  \nsecond'),
      shortLabel('abcdefghij', 6),
      defaultNoteAt(['2026-10-01', '2026-10-13'], '2026-10-17'),
      defaultNoteAt(undefined, '2026-10-17T1201'),
      defaultNoteAt(undefined, undefined),
      authorName('ops@example.test'),
    ]).toEqual(['short', 'first line', 'abcde…', '2026-10-13', '2026-10-17T1201', '', 'ops'])
  })
})
