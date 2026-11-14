import { describe, expect, it } from 'vitest'
import { CONTAINER_BG } from '../src/colors'
import { dustGround, dustInk, dustLineCount, dustLines, dustOffsets, dustRules, HATCH_DARK, HATCH_LIGHT, hatchInk } from '../src/dust'

describe('dustOffsets', () => {
  it('places rules whose gaps grow geometrically from the dense end', () => {
    // n=4, ratio 2: gaps 6.67, 13.33, 26.67, 53.33 (Σ = 100). The final rule
    // lands on the far edge and is dropped (it's the border), leaving 3
    // interior offsets from the dense end.
    const off = dustOffsets(100, 4, 2)
    expect(off.map(x => Math.round(x))).toEqual([7, 20, 47])
    // The step between consecutive offsets grows (sparser away from the dense end).
    const gaps = off.map((x, i) => x - (off[i - 1] ?? 0))
    expect(gaps.every((g, i) => i === 0 || g > gaps[i - 1])).toBe(true)
  })

  it('spaces evenly when ratio is 1', () => {
    // Even 25px gaps; the rule at the far edge (100) is dropped.
    expect(dustOffsets(100, 4, 1).map(x => Math.round(x))).toEqual([25, 50, 75])
  })

  it('returns nothing for a non-positive length or count', () => {
    expect(dustOffsets(0, 4, 1.3)).toEqual([])
    expect(dustOffsets(100, 0, 1.3)).toEqual([])
  })
})

describe('dustLineCount', () => {
  it('grows with log2(count) but never packs rules under ~4px apart', () => {
    // base·log2(count+1): count 1 → base·1, count 7 → base·3, count 63 → base·6.
    expect(dustLineCount(400, 1, 3)).toBe(3)
    expect(dustLineCount(400, 7, 3)).toBe(9)
    expect(dustLineCount(400, 63, 3)).toBe(18)
    // Never fewer than 2.
    expect(dustLineCount(400, 0, 3)).toBe(2)
    // Clamped by len/4: a 20px axis holds at most 5 rules however dense.
    expect(dustLineCount(20, 1_000_000, 3)).toBe(5)
  })
})

describe('dustLines', () => {
  it("snaps each axis's rules to pixel centers, dense at the right and bottom edges", () => {
    expect(dustLines(40, 20, 7)).toEqual({
      xs: [39.5, 37.5, 35.5, 33.5, 29.5, 25.5, 19.5, 11.5],
      ys: [18.5, 15.5, 11.5, 6.5, 0.5],
    })
  })
  it('draws nothing in an empty box', () => {
    expect([dustLines(0, 20, 7), dustLines(40, -1, 7)]).toEqual([{ xs: [], ys: [] }, { xs: [], ys: [] }])
  })
})

describe('dustRules', () => {
  it('runs every rule edge to edge with no notch', () => {
    expect(dustRules(40, 20, 7)).toEqual({
      v: [[39.5, 0], [37.5, 0], [35.5, 0], [33.5, 0], [29.5, 0], [25.5, 0], [19.5, 0], [11.5, 0]],
      h: [[18.5, 0], [15.5, 0], [11.5, 0], [6.5, 0], [0.5, 0]],
    })
  })
  it('steps rules around the label notch, dropping a rule wholly inside it', () => {
    // A 30×12.5 notch: verticals left of x=30 start at y=13; horizontals above y=12.5 start at x=30.
    expect(dustRules(40, 20, 7, { w: 30, h: 12.5 })).toEqual({
      v: [[39.5, 0], [37.5, 0], [35.5, 0], [33.5, 0], [29.5, 13], [25.5, 13], [19.5, 13], [11.5, 13]],
      h: [[18.5, 0], [15.5, 0], [11.5, 30], [6.5, 30], [0.5, 30]],
    })
    expect(dustRules(40, 20, 7, { w: 40, h: 20 })).toEqual({ v: [], h: [] })
  })
})

// A light theme's container ground (the site's `--panel`).
const PANEL = [242, 241, 238]

describe('hatchInk', () => {
  it('inks with whichever of black and white stands out more from the ground', () => {
    expect([
      hatchInk([128, 128, 128, 1], 1),
      hatchInk([127, 127, 127, 1], 1),
      hatchInk([255, 255, 255, 1], 1),
      hatchInk([0, 0, 0, 1], 1),
    ]).toEqual([HATCH_DARK, HATCH_LIGHT, HATCH_DARK, HATCH_LIGHT])
  })
  it("composites a translucent fill over the base: a faint fill takes the base's ink", () => {
    expect([
      hatchInk([255, 255, 255, 1], 0.4),
      hatchInk([255, 255, 255, 1], 0.6),
      hatchInk([255, 255, 255, 0.5], 0.8),
      hatchInk([0, 0, 0, 1], 0.4, PANEL),
      hatchInk([0, 0, 0, 1], 0.6, PANEL),
    ]).toEqual([HATCH_LIGHT, HATCH_DARK, HATCH_LIGHT, HATCH_DARK, HATCH_LIGHT])
  })
  it('contrasts with the base alone when the fill is unknown', () => {
    expect([hatchInk(null, 1), hatchInk(null, 1, PANEL)]).toEqual([HATCH_LIGHT, HATCH_DARK])
  })
  it('inks at 0.6 alpha, strong enough to read at a glance', () => {
    expect([HATCH_DARK, HATCH_LIGHT]).toEqual(['rgba(0, 0, 0, 0.6)', 'rgba(255, 255, 255, 0.6)'])
  })
})

describe('dustGround', () => {
  it('takes the largest band, else the largest of 2+ makeup segments, else the fill', () => {
    expect([
      dustGround({ bg: '#222', bands: [{ color: '#222', frac: 0.1 }, { color: 'green', frac: 0.9 }] }),
      dustGround({ bg: '#222', segments: [{ color: 'red', frac: 0.3 }, { color: 'blue', frac: 0.7 }] }),
      dustGround({ bg: '#222', segments: [{ color: 'red', frac: 1 }] }),
      dustGround({ bg: '#222' }),
      dustGround({}),
    ]).toEqual(['green', 'blue', '#222', '#222', undefined])
  })
})

describe('dustInk', () => {
  it('chooses contrasting ink on bright and dark fills, banded and segmented ones included', () => {
    expect([
      dustInk({ bg: 'rgb(55, 175, 75)' }),
      dustInk({ bg: 'rgb(35, 35, 35)' }),
      dustInk({ bg: '#222', bands: [{ color: '#222', frac: 0.1 }, { color: 'rgb(55, 175, 75)', frac: 0.9 }] }),
      dustInk({ bg: '#222', segments: [{ color: '#fff', frac: 0.7 }, { color: '#000', frac: 0.3 }] }),
    ]).toEqual([HATCH_DARK, HATCH_LIGHT, HATCH_DARK, HATCH_DARK])
  })
  it("inks the folded tile's faint ground by the container under it, dark theme or light", () => {
    const folded = { bg: 'rgba(120, 120, 135, 0.12)' }
    expect([dustInk(folded, 0.82), dustInk(folded, 0.82, CONTAINER_BG), dustInk(folded, 0.82, PANEL)]).toEqual([HATCH_LIGHT, HATCH_LIGHT, HATCH_DARK])
  })
  it("reads the fill with the caller's parser, falling back to the base when it can't", () => {
    expect([
      dustInk({ bg: 'var(--fill)' }),
      dustInk({ bg: 'var(--fill)' }, 1, PANEL),
      dustInk({ bg: 'color-mix(in oklab, red, white)' }, 1, CONTAINER_BG, () => [250, 200, 200, 1]),
    ]).toEqual([HATCH_LIGHT, HATCH_DARK, HATCH_DARK])
  })
})
