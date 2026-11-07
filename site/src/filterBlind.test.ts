import { describe, expect, it } from 'vitest'
import { blindNote, blindShown } from './filterBlind'

describe('panels that ignore the name filter: hidden under one, named in one muted line', () => {
  it('shown only without a filter', () => {
    expect([blindShown(false), blindShown(true)]).toEqual([true, false])
  })
  it('the line names the panels the page would show, in page order; none without a filter or a panel', () => {
    expect([
      blindNote(true, ['ownership', 'classes', 'lifecycle', 'age']),
      blindNote(true, ['classes', 'age']),
      blindNote(true, ['lifecycle']),
      blindNote(true, []),
      blindNote(false, ['age', 'classes']),
    ]).toEqual([
      'Bytes by creation date, bucket lifecycle, storage classes and ownership don’t follow the name filter — hidden while it is set.',
      'Bytes by creation date and storage classes don’t follow the name filter — hidden while it is set.',
      'Bucket lifecycle doesn’t follow the name filter — hidden while it is set.',
      null,
      null,
    ])
  })
})
