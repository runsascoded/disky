import { describe, expect, it } from 'vitest'
import { ageFill, barLabelPos, type LabelOpts, placeBarLabels, textWidth } from './ageBars'
import type { ColorMode } from './types'

describe('ageFill: the date gradient only where it encodes a lens', () => {
  it.each<[ColorMode, ColorMode | undefined, boolean, string]>([
    ['date', 'date', false, 'gradient'],  // the map's date lens: the bars are its legend
    ['date', 'tree', false, 'neutral'],   // followed a map axis the strata lack: no gradient
    ['date', 'user', false, 'neutral'],
    ['date', 'read', false, 'neutral'],   // read lens without read strata: its gradient spans read days
    ['date', undefined, false, 'neutral'],
    ['date', 'tree', true, 'gradient'],   // picked in the chart's own "color by"
    ['read', 'read', false, 'facet'],
    ['tree', 'tree', false, 'facet'],
    ['user', 'date', true, 'facet'],
  ])('chart %s, map %s, picked %s → %s', (mode, mapMode, picked, want) => {
    expect(ageFill(mode, mapMode, picked)).toBe(want)
  })
})

describe('barLabelPos: in the column, inside or above, or none', () => {
  it.each<[number, number, number, string | null]>([
    [30, 100, 20, 'inside'],  // wide and tall
    [30, 13.5, 20, 'inside'], // exactly text + 2 pads tall
    [30, 13, 20, 'above'],    // too short for it
    [30, 0, 20, 'above'],
    [24, 100, 20, 'inside'],  // exactly text + 2 pads wide
    [23.9, 100, 20, null],    // narrower than the text
    [7.5, 220, 20, null],     // a day view's bar
  ])('bar %s × %s, text %s → %s', (w, h, tw, want) => {
    expect(barLabelPos(w, h, tw, 9.5, 2)).toBe(want)
  })
})

describe('textWidth', () => {
  it('estimates the formatter\'s glyphs', () => {
    expect([textWidth('52 G', 10), textWidth('1.2 T', 10), textWidth('8 Ti', 10)].map(w => +w.toFixed(2)))
      .toEqual([21.2, 24.2, 18.2])
  })
})

const opts: LabelOpts = { W: 900, H: 220, padT: 13.5, textH: 9.5, pad: 2, peaks: 4, peakMin: 0.25 }
const bars = (hs: number[], w: number, tw = 20) => hs.map((h, i) => ({ x: i * w, w, h, textW: tw }))

describe('placeBarLabels', () => {
  it('labels every bar that fits its column: inside when tall, above when short', () => {
    expect(placeBarLabels(bars([220, 100, 5, 0], 30), opts)).toEqual([
      { i: 0, cx: 15, y: 2, pos: 'inside' },
      { i: 1, cx: 45, y: 122, pos: 'inside' },
      { i: 2, cx: 75, y: 203.5, pos: 'above' },
      { i: 3, cx: 105, y: 208.5, pos: 'above' },
    ])
  })

  it('narrow bars: the max overhangs above itself, then the biggest local peaks that clear every bar and label', () => {
    // 10 bars of 10 units, labels 20 wide: nothing fits a column.
    expect(placeBarLabels(bars([50, 220, 60, 10, 10, 150, 140, 10, 10, 40], 10), opts)).toEqual([
      { i: 1, cx: 15, y: -11.5, pos: 'peak' },
      // 150 at i=5 spans x 45..65: bars 4..6 (10, 150, 140) are all under it
      { i: 5, cx: 55, y: 58.5, pos: 'peak' },
      // 140 (i=6) would overlap 150's label; 60 (i=2) spans bar 1 (220), which is taller; 50 too
      // 40 (i=9) is under the 25% floor (55)
    ])
  })

  it('clamps an overhanging label inside the plot', () => {
    expect(placeBarLabels(bars([10, 10, 10, 220], 5, 20).map((b, i) => ({ ...b, x: 880 + i * 5 })), opts)).toEqual([
      { i: 3, cx: 890, y: -11.5, pos: 'peak' },
    ])
  })

  it('per-bar text widths: the max overhangs when its own text is too wide, its neighbors still fit', () => {
    expect(placeBarLabels([
      { x: 0, w: 24, h: 100, textW: 18 },
      { x: 24, w: 24, h: 220, textW: 24.2 },
      { x: 48, w: 24, h: 5, textW: 18 },
    ], opts)).toEqual([
      { i: 0, cx: 12, y: 122, pos: 'inside' },
      { i: 1, cx: 36, y: -11.5, pos: 'peak' },
      { i: 2, cx: 60, y: 203.5, pos: 'above' },
    ])
  })

  it('no bars, no labels', () => {
    expect(placeBarLabels([], opts)).toEqual([])
  })
})
