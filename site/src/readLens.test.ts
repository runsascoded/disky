import { describe, expect, it } from 'vitest'
import {
  idleLabel, idleness, idleSortKey, lastTouch, prefixBucket, readDay, readLogged, readState, readThrough,
  readThroughText, unloggedOf, unreadSinceWrite, writtenAfter,
} from './readLens'
import { readTintParam } from './readTint'

const access = { from: 100, to: 110, buckets: ['jc-taxes'] }
// Epoch day 110 = 1970-04-21; the logs run through its 15:00 hour.
const thru = { from: 100, to: 110, through: '1970-04-21T15:00Z' }

describe('readLogged', () => {
  it('covers only the listed buckets; a folded cell or a missing segment is uncovered', () => {
    const logged = readLogged(access)
    expect(['jc-taxes', 'ctbk', 'crashes', '(+2)', undefined].map(logged)).toEqual([true, false, false, false, false])
  })

  it('covers everything when the scan predates `buckets`, and when there is no access window', () => {
    expect([readLogged({ from: 100, to: 110 })('ctbk'), readLogged(undefined)('ctbk')]).toEqual([true, true])
  })
})

describe('readThrough', () => {
  it('is the hour\'s day and label; provisional only when the scan was taken after that hour ended', () => {
    expect([
      readThrough(thru, '1970-04-22T07:01:00Z'),
      readThrough(thru, '1970-04-21T16:00:00Z'),
      readThrough(thru, '1970-04-21T15:30:00Z'),
      readThrough(thru),
    ]).toEqual([
      { day: 110, label: '1970-04-21 15:00Z', provisional: true },
      { day: 110, label: '1970-04-21 15:00Z', provisional: false },
      { day: 110, label: '1970-04-21 15:00Z', provisional: false },
      { day: 110, label: '1970-04-21 15:00Z', provisional: false },
    ])
  })

  it('falls back to the window\'s last day on a scan published before the field (day precision), and is null with no window', () => {
    expect([
      readThrough({ from: 100, to: 110 }, '1970-04-22T07:00:00Z'),
      readThrough({ from: 100, to: 110 }, '1970-04-21T20:00:00Z'),
      readThrough({ from: 100, to: 110 }),
      readThrough(undefined),
    ]).toEqual([
      { day: 110, label: '1970-04-21', provisional: true },
      { day: 110, label: '1970-04-21', provisional: false },
      { day: 110, label: '1970-04-21', provisional: false },
      null,
    ])
  })

  it('refuses a malformed value', () => {
    expect(() => readThrough({ from: 100, to: 110, through: 'yesterday' })).toThrow('access.through: not a time: yesterday')
  })
})

describe('readThroughText', () => {
  it('says where the reads end, and the gap when the scan postdates it', () => {
    expect([
      readThroughText({ day: 110, label: '1970-04-21 15:00Z', provisional: true }),
      readThroughText({ day: 110, label: '1970-04-21 15:00Z', provisional: false }),
    ]).toEqual([
      { through: 'last access data: 1970-04-21 15:00Z', gap: 'reads after 1970-04-21 15:00Z not yet delivered' },
      { through: 'last access data: 1970-04-21 15:00Z', gap: null },
    ])
  })
})

describe('writtenAfter / unreadSinceWrite / lastTouch', () => {
  it('counts a write on `through`\'s own day as after (its hour is unknown)', () => {
    expect([writtenAfter(109, 110), writtenAfter(110, 110), writtenAfter(111, 110), writtenAfter(111, null), writtenAfter(null, 110)])
      .toEqual([false, true, true, false, false])
  })

  it('is "not read since a write since logging began": a same-day read counts as after the write', () => {
    expect([
      unreadSinceWrite(null, 105, 100),
      unreadSinceWrite(103, 105, 100),
      unreadSinceWrite(105, 105, 100),
      unreadSinceWrite(107, 105, 100),
      unreadSinceWrite(null, 95, 100),
      unreadSinceWrite(null, null, 100),
    ]).toEqual([true, true, false, false, false, false])
  })

  it('a directory qualifies only with no read anywhere under it', () => {
    expect([
      unreadSinceWrite(null, 105, 100, true),
      unreadSinceWrite(103, 105, 100, true),
      unreadSinceWrite(null, 95, 100, true),
    ]).toEqual([true, false, false])
  })

  it('is max(read, write), a write before logging began not counting', () => {
    expect([lastTouch(103, 107, 100), lastTouch(107, 103, 100), lastTouch(null, 105, 100), lastTouch(null, 90, 100), lastTouch(103, 90, 100)])
      .toEqual([107, 107, 105, null, 103])
  })
})

describe('readState', () => {
  const range = { min: 100, max: 110 }
  it('places a read on the window, linearly without an axis, by the axis when given', () => {
    expect([
      readState(105, true, range),
      readState(110, false, range),
      readState(105, true, { ...range, t: d => (d - 100) / 20 }),
    ]).toEqual([{ kind: 'read', t: 0.5 }, { kind: 'read', t: 1 }, { kind: 'read', t: 0.25 }])
  })

  it('puts a read on a one-day window at the new end (not "no observed reads")', () => {
    expect(readState(110, true, { min: 110, max: 110, t: () => NaN })).toEqual({ kind: 'read', t: 1 })
  })

  it('tells a logged cell with no read from an unlogged one', () => {
    expect([readState(null, true, range), readState(undefined, false, range), readState(null, false, null)])
      .toEqual([{ kind: 'never' }, { kind: 'unlogged' }, { kind: 'unlogged' }])
  })

  it('with the write day, an object: each state transition', () => {
    expect([
      // read since its last write: the read's age
      readState(108, true, range, { d: 90 }),
      readState(106, true, range, { d: 106 }),
      // written since logging began, not read since that write: the write's age (hatched)
      readState(102, true, range, { d: 106 }),
      readState(null, true, range, { d: 104 }),
      // written on or after the logs' end (the window's new end): the newest write color
      readState(108, true, range, { d: 112 }),
      // never read, last written before logging began
      readState(null, true, range, { d: 95 }),
      // no logs cover it: the write says nothing about reads
      readState(null, false, range, { d: 104 }),
      readState(104, false, range, { d: 108 }),
    ]).toEqual([
      { kind: 'read', t: 0.8 },
      { kind: 'read', t: 0.6 },
      { kind: 'written', t: 0.6 },
      { kind: 'written', t: 0.4 },
      { kind: 'written', t: 1 },
      { kind: 'never' },
      { kind: 'unlogged' },
      { kind: 'read', t: 0.4 },
    ])
  })

  it('a directory (`hatch`) is `written` only with nothing under it read; any read gives it the read color', () => {
    const dir = (a: number | null, d: number) => readState(a, true, range, { d, dir: true })
    expect([
      // a read on 102 and a newer write on 106 (one new file in a well-read folder): the read's age
      dir(102, 106),
      // written since logging began, nothing under it read: the write's age (hatched)
      dir(null, 104),
      // nothing read, last written before logging began
      dir(null, 95),
      // the same read + newer write as an object: hatched by the write
      readState(102, true, range, { d: 106, dir: false }),
    ]).toEqual([
      { kind: 'read', t: 0.2 },
      { kind: 'written', t: 0.4 },
      { kind: 'never' },
      { kind: 'written', t: 0.6 },
    ])
  })

  it('`touch`: by max(read, write since logging began), no `written` state, files and folders alike', () => {
    const touch = (a: number | null, logged: boolean, d: number | null, dir = false) =>
      readState(a, logged, range, { d, dir, tint: 'touch' })
    expect([
      // read before the newest write: the write's age, unhatched
      touch(102, true, 106),
      touch(102, true, 106, true),
      // read after the newest write: the read's age
      touch(108, true, 104, true),
      // read, last written before logging began: the read's age
      touch(103, true, 90),
      // written since logging began, never read: the write's age
      touch(null, true, 104, true),
      // neither a read nor a write since logging began
      touch(null, true, 95, true),
      touch(null, true, null),
      // not logged: reads alone (a write says nothing about reads nobody logs)
      touch(null, false, 104),
      touch(103, false, 108),
    ]).toEqual([
      { kind: 'read', t: 0.6 },
      { kind: 'read', t: 0.6 },
      { kind: 'read', t: 0.8 },
      { kind: 'read', t: 0.3 },
      { kind: 'read', t: 0.4 },
      { kind: 'never' },
      { kind: 'never' },
      { kind: 'unlogged' },
      { kind: 'read', t: 0.3 },
    ])
  })
})

describe('readDay', () => {
  it('is the day a cell sits at: `hatch` the write when `written`, else the read; `touch` the last touch; unlogged the read', () => {
    expect([
      readDay(102, true, 100, { d: 106 }),
      readDay(102, true, 100, { d: 106, dir: true }),
      readDay(null, true, 100, { d: 104, dir: true }),
      readDay(102, true, 100, { d: 106, dir: true, tint: 'touch' }),
      readDay(null, true, 100, { d: 95, tint: 'touch' }),
      readDay(null, false, 100, { d: 104 }),
      readDay(103, false, 100, { d: 108, tint: 'touch' }),
    ]).toEqual([106, 102, 104, 106, null, null, 103])
  })
})

describe('readTintParam (`?rt=`)', () => {
  it('omits the default `hatch` from the URL; decodes `touch`, and an absent or unknown value to `hatch`', () => {
    expect([
      readTintParam.encode('hatch'),
      readTintParam.encode('touch'),
      readTintParam.decode(undefined),
      readTintParam.decode('touch'),
      readTintParam.decode('hatch'),
      readTintParam.decode('bogus'),
    ]).toEqual([undefined, 'touch', 'hatch', 'touch', 'hatch', 'hatch'])
  })
})

describe('idleness', () => {
  it('is idle only when read and write both precede the logs\' end, counted from it', () => {
    expect([
      // read on 104, written on 95: idle since the read
      idleness(104, 95, thru),
      // read on 101, written on 107: idle since the write
      idleness(101, 107, thru),
      // never read, written before logging began: at least since logging began
      idleness(null, 95, thru),
      // never read, written since logging began: since the write
      idleness(null, 103, thru),
      // read on the logs' last day: idle 0 days (every read in the data precedes `through`)
      idleness(110, 95, thru),
      // written on the logs' last day, or after: not idle
      idleness(101, 110, thru),
      idleness(null, 111, thru),
    ]).toEqual([
      { idle: true, days: 6 },
      { idle: true, days: 3 },
      { idle: true, days: 10 },
      { idle: true, days: 7 },
      { idle: true, days: 0 },
      { idle: false },
      { idle: false },
    ])
  })

  it('counts an older scan (no `through`) from its window\'s last day; no verdict with no window or on an unlogged path', () => {
    expect([idleness(104, 95, { from: 100, to: 110 }), idleness(104, 95, undefined), idleness(104, 95, thru, false)])
      .toEqual([{ idle: true, days: 6 }, null, null])
  })

  it('labels and sorts: idle by days, not-idle below every idle, no verdict undefined', () => {
    const is = [{ idle: true, days: 6 } as const, { idle: true, days: 0 } as const, { idle: false } as const]
    expect([is.map(idleLabel), [...is, null].map(idleSortKey)])
      .toEqual([['idle 6d', 'idle 0d', 'written after logs'], [6, 0, -1, undefined]])
  })
})

describe('prefixBucket', () => {
  it('is the bucket segment of a scheme prefix', () => {
    expect(['gs://marin-us/a/b/', 's3:///ctbk/x', 'r2://jc-taxes/'].map(prefixBucket)).toEqual(['marin-us', 'ctbk', 'jc-taxes'])
  })
})

describe('unloggedOf', () => {
  it('lists the uncovered top-level names in order, folded cells left out', () => {
    expect(unloggedOf(['crashes', 'jc-taxes', 'ctbk', '(+3)'], readLogged(access))).toEqual(['crashes', 'ctbk'])
  })
})
