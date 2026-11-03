import { describe, expect, it } from 'vitest'
import { brushDomain, DAY_MS, fitToWindow, fmtXr, parseXr, presetOf, resolveXDomain, spanDomain, type XDomain } from './xDomain'

// Ten daily scans, 2026-10-01 … 2026-10-10, each at UTC midnight.
const ids = Array.from({ length: 10 }, (_, i) => `2026-10-${String(i + 1).padStart(2, '0')}`)
const xOf = (id: string) => Date.parse(`${id.slice(0, 10)}T00:00:00Z`)
const xs = ids.map(xOf)
const idOf = (x: number) => new Date(x).toISOString().slice(0, 10)
const day = (d: number) => xOf(`2026-10-${String(d).padStart(2, '0')}`)
// A resolved range as scan ids (null = every scan), so expectations read as dates.
const asIds = (r: [number, number] | null) => (r ? r.map(idOf) : null)

describe('parseXr / fmtXr / presetOf', () => {
  it('round-trips presets and spans; a reversed span is put in order; junk is every scan', () => {
    const inputs = [undefined, '', '1w', '1m', 'all', '2026-10-02..2026-10-05', '2026-10-05..2026-10-02', '2026-09-17T1201..2026-10-01', 'bogus', '2026-10-02..', '3d']
    expect(inputs.map(s => { const d = parseXr(s); return [d, fmtXr(d), presetOf(d)] })).toEqual([
      [{ kind: 'all' }, undefined, 'all'],
      [{ kind: 'all' }, undefined, 'all'],
      [{ kind: 'last', days: 7 }, '1w', '1w'],
      [{ kind: 'last', days: 30 }, '1m', '1m'],
      [{ kind: 'all' }, undefined, 'all'],
      [{ kind: 'span', from: '2026-10-02', to: '2026-10-05' }, '2026-10-02..2026-10-05', null],
      [{ kind: 'span', from: '2026-10-02', to: '2026-10-05' }, '2026-10-02..2026-10-05', null],
      [{ kind: 'span', from: '2026-09-17T1201', to: '2026-10-01' }, '2026-09-17T1201..2026-10-01', null],
      [{ kind: 'all' }, undefined, 'all'],
      [{ kind: 'all' }, undefined, 'all'],
      [{ kind: 'all' }, undefined, 'all'],
    ])
  })
})

describe('resolveXDomain', () => {
  const r = (d: XDomain, data: number[] = xs) => asIds(resolveXDomain(d, data, xOf))

  it('presets: the last N days before the latest scan; one covering every scan is unzoomed', () => {
    expect([r({ kind: 'all' }), r({ kind: 'last', days: 7 }), r({ kind: 'last', days: 30 }), r({ kind: 'last', days: 3 })]).toEqual([
      null,
      ['2026-10-03', '2026-10-10'],
      null,
      ['2026-10-07', '2026-10-10'],
    ])
  })

  it('spans clamp to the data; one past both ends is unzoomed', () => {
    expect([
      r({ kind: 'span', from: '2026-10-03', to: '2026-10-06' }),
      r({ kind: 'span', from: '2026-09-01', to: '2026-10-04' }),
      r({ kind: 'span', from: '2026-10-08', to: '2026-12-01' }),
      r({ kind: 'span', from: '2026-09-01', to: '2026-12-01' }),
    ]).toEqual([
      ['2026-10-03', '2026-10-06'],
      ['2026-10-01', '2026-10-04'],
      ['2026-10-08', '2026-10-10'],
      null,
    ])
  })

  it('never fewer than two scans: one scan widens to the next (the previous at the end); a range between scans to the pair around it', () => {
    const sparse = [day(1), day(4), day(8)]
    expect([
      r({ kind: 'span', from: '2026-10-05', to: '2026-10-05' }),
      r({ kind: 'span', from: '2026-10-10', to: '2026-10-10' }),
      r({ kind: 'last', days: 0 }),
      r({ kind: 'span', from: '2026-10-05', to: '2026-10-06' }, sparse),
      r({ kind: 'span', from: '2026-11-01', to: '2026-11-02' }, sparse),
      r({ kind: 'span', from: '2026-09-01', to: '2026-09-02' }, sparse),
    ]).toEqual([
      ['2026-10-05', '2026-10-06'],
      ['2026-10-09', '2026-10-10'],
      ['2026-10-09', '2026-10-10'],
      ['2026-10-04', '2026-10-08'],
      ['2026-10-04', '2026-10-08'],
      ['2026-10-01', '2026-10-04'],
    ])
  })

  it('fewer than two scans, or a span naming an unparseable scan: unzoomed', () => {
    expect([
      resolveXDomain({ kind: 'last', days: 7 }, [day(1)], xOf),
      resolveXDomain({ kind: 'span', from: 'x', to: 'y' }, xs, () => NaN),
    ]).toEqual([null, null])
  })
})

describe('fitToWindow', () => {
  it('the window’s scans plus one of context each side, clamped; every scan → null', () => {
    expect([
      asIds(fitToWindow([day(4), day(6)], xs)),
      asIds(fitToWindow([day(6), day(4)], xs)),
      asIds(fitToWindow([day(1), day(3)], xs)),
      asIds(fitToWindow([day(9), day(10)], xs)),
      asIds(fitToWindow([day(5), day(5)], xs)),
      asIds(fitToWindow([day(4), day(6)], xs, 0)),
      asIds(fitToWindow([day(2), day(9)], xs)),
      asIds(fitToWindow([day(1), day(2)], [day(1), day(2)])),
    ]).toEqual([
      ['2026-10-03', '2026-10-07'],
      ['2026-10-03', '2026-10-07'],
      ['2026-10-01', '2026-10-04'],
      ['2026-10-08', '2026-10-10'],
      ['2026-10-04', '2026-10-06'],
      ['2026-10-04', '2026-10-06'],
      null,
      null,
    ])
  })

  it('a window between scans snaps outward to the scans around it', () => {
    const sparse = [day(1), day(4), day(8), day(10)]
    expect(asIds(fitToWindow([day(5), day(7)], sparse, 0))).toEqual(['2026-10-04', '2026-10-08'])
  })
})

describe('brushDomain / spanDomain', () => {
  it('ends snap to the nearest scan; a one-scan brush is no change; a brush over every scan is all', () => {
    const half = DAY_MS / 2
    expect([
      brushDomain(day(3) + 0.4 * DAY_MS, day(7) - 0.3 * DAY_MS, xs, idOf),
      brushDomain(day(7), day(3), xs, idOf),
      brushDomain(day(5) - 0.2 * half, day(5) + 0.2 * half, xs, idOf),
      brushDomain(day(1) - DAY_MS, day(10) + DAY_MS, xs, idOf),
      brushDomain(day(1), day(2), [], idOf),
      spanDomain(null, idOf),
      spanDomain([day(6), day(2)], idOf),
    ]).toEqual([
      { kind: 'span', from: '2026-10-03', to: '2026-10-07' },
      { kind: 'span', from: '2026-10-03', to: '2026-10-07' },
      null,
      { kind: 'all' },
      null,
      { kind: 'all' },
      { kind: 'span', from: '2026-10-02', to: '2026-10-06' },
    ])
  })
})
