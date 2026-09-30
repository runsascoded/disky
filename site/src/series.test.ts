import { describe, expect, it } from 'vitest'
import { bandCallouts, geneses, pickAnnotations, prominentExtrema, relativeSeries, stackSeries, unitTicks, youngestGenesis } from './series'

const P = { key: 'p', points: [{ x: 1, y: 90 }, { x: 2, y: 100 }, { x: 3, y: 101 }] }
const H = { key: 'h', points: [{ x: 3, y: 40 }] }

describe('geneses', () => {
  it('first x per trace; empty traces have none', () => {
    expect(geneses([P, H, { key: 'e', points: [] }])).toEqual(new Map([['p', 1], ['h', 3]]))
    expect(youngestGenesis([P, H])).toBe(3)
    expect(youngestGenesis([P])).toBeNull()
    expect(youngestGenesis([])).toBeNull()
  })
})

describe('pickAnnotations', () => {
  // first, min, max, last distinct; x=2 is a mid-series dip a window can select.
  const line = [
    { x: 0, y: 80 }, // first
    { x: 1, y: 62 }, // global min
    { x: 2, y: 70 }, // a dip (below mid=78.5)
    { x: 3, y: 95 }, // global max
    { x: 4, y: 88 },
    { x: 5, y: 85 }, // last
  ]

  it('picks max, min, first, last (in that order) with no selection', () => {
    expect(pickAnnotations(line)).toEqual([
      { x: 3, y: 95, below: false },
      { x: 1, y: 62, below: true },
      { x: 0, y: 80, below: false },
      { x: 5, y: 85, below: false },
    ])
  })

  it('adds the point nearest each window edge (the size at the selection ends)', () => {
    expect(pickAnnotations(line, [2, 4])).toEqual([
      { x: 3, y: 95, below: false },
      { x: 1, y: 62, below: true },
      { x: 0, y: 80, below: false },
      { x: 5, y: 85, below: false },
      { x: 2, y: 70, below: true }, // window start: a low → below
      { x: 4, y: 88, below: false }, // window end: a high → above
    ])
  })

  it('snaps a window edge to the nearest point, and collapses when it lands on an existing role', () => {
    // edges 1.6→x2 (dip) and 4.9→x5 (already `last`, so no duplicate)
    expect(pickAnnotations(line, [1.6, 4.9])).toEqual([
      { x: 3, y: 95, below: false },
      { x: 1, y: 62, below: true },
      { x: 0, y: 80, below: false },
      { x: 5, y: 85, below: false },
      { x: 2, y: 70, below: true },
    ])
  })

  it('needs at least two points', () => {
    expect(pickAnnotations([])).toEqual([])
    expect(pickAnnotations([{ x: 0, y: 5 }])).toEqual([])
    expect(pickAnnotations([{ x: 0, y: 5 }], [0, 0])).toEqual([])
  })

  // A W-shape with interior peaks/troughs that are NOT the globals.
  const wave = [
    { x: 0, y: 50 }, // first
    { x: 1, y: 90 }, // interior peak
    { x: 2, y: 40 }, // interior trough
    { x: 3, y: 70 }, // interior peak
    { x: 4, y: 30 }, // global min
    { x: 5, y: 100 }, // global max = last
  ]

  it('radius=1 adds every prominent interior peak/valley not already a global/end', () => {
    // globals/ends first, then interior peaks (x1, x3), then interior valley (x2).
    expect(pickAnnotations(wave, undefined, 1)).toEqual([
      { x: 5, y: 100, below: false }, // global max (= last)
      { x: 4, y: 30, below: true }, // global min
      { x: 0, y: 50, below: true }, // first
      { x: 1, y: 90, below: false }, // interior peak
      { x: 3, y: 70, below: false }, // interior peak
      { x: 2, y: 40, below: true }, // interior valley
    ])
  })

  it('a wider suppression radius keeps only the more prominent of nearby peaks/valleys', () => {
    // r=2.5: peak x1 outranks x3 (|1-3|=2 < 2.5); valley x4 outranks x2.
    expect(pickAnnotations(wave, undefined, 2.5)).toEqual([
      { x: 5, y: 100, below: false },
      { x: 4, y: 30, below: true },
      { x: 0, y: 50, below: true },
      { x: 1, y: 90, below: false }, // the more prominent interior peak
    ])
  })

  // A sharp spike (x=3) shorter than a distant endpoint (x=5): "tallest within
  // ±r" would suppress it, but prominence keeps it (you descend into the x=4
  // trough to reach the taller x=5). This is the case the eye wants labeled.
  const spike = [
    { x: 0, y: 10 }, { x: 1, y: 30 }, { x: 2, y: 15 }, { x: 3, y: 95 }, { x: 4, y: 40 }, { x: 5, y: 100 },
  ]
  it('keeps a prominent spike beside a taller-but-distant endpoint', () => {
    expect(pickAnnotations(spike, undefined, 2.5)).toEqual([
      { x: 5, y: 100, below: false }, // global max (= last)
      { x: 0, y: 10, below: true }, // global min (= first)
      { x: 3, y: 95, below: false }, // the spike — survives despite x=5 being taller
      { x: 4, y: 40, below: true }, // the trough before the endpoint climb
    ])
  })
})

describe('prominentExtrema', () => {
  const wave = [
    { x: 0, y: 50 }, { x: 1, y: 90 }, { x: 2, y: 40 }, { x: 3, y: 70 }, { x: 4, y: 30 }, { x: 5, y: 100 },
  ]
  it('all interior peaks/valleys survive when the suppression radius is small', () => {
    expect(prominentExtrema(wave, 1)).toEqual({
      maxes: [{ x: 1, y: 90 }, { x: 3, y: 70 }],
      mins: [{ x: 2, y: 40 }, { x: 4, y: 30 }],
    })
  })
  it('within the radius, the more prominent of two peaks/valleys wins', () => {
    expect(prominentExtrema(wave, 2.5)).toEqual({
      maxes: [{ x: 1, y: 90 }], // beats x3
      mins: [{ x: 4, y: 30 }], // beats x2
    })
  })
  it('ranks a spike above a gentle bump by prominence, not raw height', () => {
    const spike = [
      { x: 0, y: 10 }, { x: 1, y: 30 }, { x: 2, y: 15 }, { x: 3, y: 95 }, { x: 4, y: 40 }, { x: 5, y: 100 },
    ]
    // x3 (95) outranks x1 (30) and isn't suppressed by the taller endpoint x5.
    expect(prominentExtrema(spike, 2.5)).toEqual({ maxes: [{ x: 3, y: 95 }], mins: [{ x: 4, y: 40 }] })
  })
  it('a prominence floor drops the wobbles: only extrema that stand out by ≥ minProm survive', () => {
    // Prominences — peaks: x1 40 (90 above the 50 start), x3 30 (70 above the 40 saddle);
    // valleys: x2 30 (40 below the 70 saddle), x4 60 (30 below the 90 saddle).
    expect(prominentExtrema(wave, 1, 40)).toEqual({ maxes: [{ x: 1, y: 90 }], mins: [{ x: 4, y: 30 }] })
    expect(prominentExtrema(wave, 1, 61)).toEqual({ maxes: [], mins: [] })
  })
})

describe('pickAnnotations prominence floor', () => {
  // A big move with a small wobble on the way: the wobble (prominence 2 on a
  // range of 100) earns no callout at a 10% floor, and does at 0.
  const ramp = [
    { x: 0, y: 0 }, { x: 1, y: 50 }, { x: 2, y: 48 }, { x: 3, y: 52 }, { x: 4, y: 100 },
  ]
  it('minPromFrac scales the floor by the series range', () => {
    expect(pickAnnotations(ramp, undefined, 0.5, 0)).toEqual([
      { x: 4, y: 100, below: false },
      { x: 0, y: 0, below: true },
      { x: 1, y: 50, below: false },
      { x: 2, y: 48, below: true },
    ])
    expect(pickAnnotations(ramp, undefined, 0.5, 0.1)).toEqual([
      { x: 4, y: 100, below: false },
      { x: 0, y: 0, below: true },
    ])
  })
})

describe('relativeSeries', () => {
  const a = { key: 'a', points: [{ x: 1, y: 200 }, { x: 2, y: 250 }, { x: 3, y: 150 }] }
  const z = { key: 'z', points: [{ x: 2, y: 0 }, { x: 3, y: 10 }] }
  it('delta: bytes since each trace’s own first point', () => {
    expect(relativeSeries([a, z], 'delta')).toEqual([
      { key: 'a', points: [{ x: 1, y: 0 }, { x: 2, y: 50 }, { x: 3, y: -50 }] },
      { key: 'z', points: [{ x: 2, y: 0 }, { x: 3, y: 10 }] },
    ])
  })
  it('pct: the delta as a fraction of the start; a zero start is flat 0', () => {
    expect(relativeSeries([a, z], 'pct')).toEqual([
      { key: 'a', points: [{ x: 1, y: 0 }, { x: 2, y: 0.25 }, { x: 3, y: -0.25 }] },
      { key: 'z', points: [{ x: 2, y: 0 }, { x: 3, y: 0 }] },
    ])
  })
  it('sorts by x first, so the reference is the earliest point', () => {
    expect(relativeSeries([{ key: 'r', points: [{ x: 2, y: 30 }, { x: 1, y: 10 }] }], 'delta')).toEqual([
      { key: 'r', points: [{ x: 1, y: 0 }, { x: 2, y: 20 }] },
    ])
  })
})

describe('bandCallouts', () => {
  // A band whose height goes 10 → 30 → 20 while its base moves too.
  const band = [
    { x: 1, y0: 100, y: 110 },
    { x: 2, y0: 90, y: 120 },
    { x: 3, y0: 95, y: 115 },
  ]
  it('labels the band’s own height (max, min, first, last), placed on the band’s edges at that x', () => {
    expect(bandCallouts(band)).toEqual([
      { x: 2, y: 120, y0: 90, h: 30, below: false }, // max height
      { x: 1, y: 110, y0: 100, h: 10, below: true }, // min height (= first)
      { x: 3, y: 115, y0: 95, h: 20, below: false }, // last
    ])
  })
})

describe('unitTicks', () => {
  it('ticks at unit-nice steps: a 0–3.3 TiB axis in IEC → 1024-based', () => {
    const T = 1024 ** 4
    expect(unitTicks(0, 3.3 * T, 1024)).toEqual([0, T, 2 * T, 3 * T])
  })
  it('a fitted axis covers [min, max] at the same nice step', () => {
    expect(unitTicks(2.9 * 1000, 3.4 * 1000, 1000, 4)).toEqual([2900, 3000, 3100, 3200, 3300, 3400])
  })
  it('a signed axis (Δ traces) puts a tick exactly on 0', () => {
    expect(unitTicks(-1.7 * 1024, 2.6 * 1024, 1024)).toEqual([-1024, 0, 1024, 2048])
  })
  it('all-zero data → a lone 0 tick', () => {
    expect(unitTicks(0, 0, 1024)).toEqual([0])
  })
})

describe('stackSeries', () => {
  it('bands run between running sums; a late root starts at its genesis', () => {
    expect(stackSeries([P, H])).toEqual([
      { key: 'p', points: [{ x: 1, y0: 0, y: 90 }, { x: 2, y0: 0, y: 100 }, { x: 3, y0: 0, y: 101 }] },
      { key: 'h', points: [{ x: 3, y0: 101, y: 141 }] },
    ])
  })
  it('a gap inside a trace counts as 0 for the bands above it', () => {
    const a = { key: 'a', points: [{ x: 1, y: 10 }, { x: 3, y: 10 }] }
    const b = { key: 'b', points: [{ x: 1, y: 5 }, { x: 2, y: 5 }, { x: 3, y: 5 }] }
    expect(stackSeries([a, b])).toEqual([
      { key: 'a', points: [{ x: 1, y0: 0, y: 10 }, { x: 2, y0: 0, y: 0 }, { x: 3, y0: 0, y: 10 }] },
      { key: 'b', points: [{ x: 1, y0: 10, y: 15 }, { x: 2, y0: 0, y: 5 }, { x: 3, y0: 10, y: 15 }] },
    ])
  })
})
