import { describe, expect, it } from 'vitest'
import { contrastEdge, hierColor, parseColor, slotColor } from '../src/colors'
import { categoricalStyle } from '../src/cellStyle'

describe('parseColor', () => {
  it('parses #rgb / #rrggbb / #rrggbbaa', () => {
    expect(parseColor('#fff')).toEqual([255, 255, 255, 1])
    expect(parseColor('#204')).toEqual([34, 0, 68, 1])
    expect(parseColor('#20242a')).toEqual([32, 36, 42, 1])
    expect(parseColor('#20242a80')).toEqual([32, 36, 42, 128 / 255])
  })

  it('parses rgb() and rgba(), comma- or space-separated', () => {
    expect(parseColor('rgb(10, 20, 30)')).toEqual([10, 20, 30, 1])
    expect(parseColor('rgba(10, 20, 30, 0.5)')).toEqual([10, 20, 30, 0.5])
    expect(parseColor('rgb(10 20 30 / 0.25)')).toEqual([10, 20, 30, 0.25])
  })

  it('parses hsl()/hsla(), including the default palette form', () => {
    // hsl(0 0% 0%) is black, hsl(0 0% 100%) white.
    expect(parseColor('hsl(0 0% 0%)')).toEqual([0, 0, 0, 1])
    expect(parseColor('hsl(0, 0%, 100%)')).toEqual([255, 255, 255, 1])
    // Blue palette slot: hue 210, 70% sat, 55% light.
    expect(parseColor('hsl(210 70% 55%)')).toEqual([60, 140, 221, 1])
    expect(parseColor('hsla(210 70% 55% / 0.4)')).toEqual([60, 140, 221, 0.4])
  })

  it('returns null for colors it cannot parse', () => {
    expect(parseColor('var(--dt-treemap-container-bg, #202024)')).toBeNull()
    expect(parseColor('color-mix(in oklch, red, blue 40%)')).toBeNull()
    expect(parseColor('rebeccapurple')).toBeNull()
    expect(parseColor('linear-gradient(45deg, red, blue)')).toBeNull()
  })
})

describe('contrastEdge', () => {
  it('returns a dark stroke on a light face, light on a dark face', () => {
    expect(contrastEdge('#ffffff')).toBe('rgba(0, 0, 0, 0.55)')
    expect(contrastEdge('#111111')).toBe('rgba(255, 255, 255, 0.42)')
  })

  it('composites over the base at the given fade before deciding', () => {
    // A light face faded hard toward the dark base reads dark → light stroke.
    expect(contrastEdge('#ffffff', 0.1)).toBe('rgba(255, 255, 255, 0.42)')
    // …but unfaded it stays light → dark stroke.
    expect(contrastEdge('#ffffff', 1)).toBe('rgba(0, 0, 0, 0.55)')
  })

  it('returns null for an unparseable face so the caller keeps its fallback', () => {
    expect(contrastEdge('var(--dt-treemap-container-bg, #202024)')).toBeNull()
    expect(contrastEdge(undefined)).toBeNull()
  })
})

describe('slotColor', () => {
  it('returns the fixed palette for the first slots (cross-widget hue identity)', () => {
    expect(slotColor(0)).toBe('hsl(210 70% 55%)')
    expect(slotColor(7)).toBe('hsl(120 45% 50%)')
  })
  it('generates golden-angle hues past the palette so many top-level dirs stay distinct', () => {
    expect(slotColor(8)).toBe('hsl(20 60% 52%)')
    expect(slotColor(9)).toBe('hsl(158 60% 52%)')
  })
})

describe('hierColor', () => {
  it('is identity for an L1 cell (no L2 key, leaf)', () => {
    expect(hierColor('hsl(210 70% 55%)', null, false)).toBe('hsl(210 70% 55%)')
  })
  it('tints a container a darker, desaturated shade of its hue', () => {
    expect(hierColor('hsl(210 70% 55%)', null, true)).toBe('hsl(210 39% 28%)')
  })
  it('nudges hue + lightness per L2 key so sibling subtrees read as related-but-distinct', () => {
    expect(hierColor('hsl(210 70% 55%)', 'md5', false)).toBe('hsl(223 70% 63%)')
    expect(hierColor('hsl(210 70% 55%)', 'md5', true)).toBe('hsl(223 39% 32%)')
    // A different key lands on a different variant, deterministically.
    expect(hierColor('hsl(210 70% 55%)', 'b', false)).toBe('hsl(197 70% 47%)')
  })
  it('passes a non-hsl base through unchanged (var / hex)', () => {
    expect(hierColor('var(--x)', 'k', false)).toBe('var(--x)')
    expect(hierColor('#abcdef', null, false)).toBe('#abcdef')
  })
})

describe('categoricalStyle', () => {
  const label = (n: string) => n
  const slots = ['hsl(210 70% 55%)']
  const top = new Map([['a', 'hsl(210 70% 55%)']])

  it('non-nested: leaf gets the slot hue, container the neutral var (unchanged behavior)', () => {
    expect(categoricalStyle(['root', 'a'], false, label, top, slots, false)).toEqual({ bg: 'hsl(210 70% 55%)', ink: '#fff' })
    expect(categoricalStyle(['root', 'a'], true, label, top, slots, false)).toEqual({ bg: 'var(--dt-treemap-container-bg, #202024)', ink: 'var(--dt-treemap-ink, #d0d0d8)' })
  })

  it('nested: an L1 leaf keeps the macro hue, an L2 leaf gets its micro-variant', () => {
    expect(categoricalStyle(['root', 'a'], false, label, top, slots, true)).toEqual({ bg: 'hsl(210 70% 55%)', ink: '#fff' })
    expect(categoricalStyle(['root', 'a', 'md5'], false, label, top, slots, true)).toEqual({ bg: 'hsl(223 70% 63%)', ink: '#fff' })
  })

  it('nested: a container carries a tinted shade of its group hue instead of grey', () => {
    expect(categoricalStyle(['root', 'a'], true, label, top, slots, true)).toEqual({ bg: 'hsl(210 39% 28%)', ink: 'var(--dt-treemap-ink, #d0d0d8)' })
  })
})
