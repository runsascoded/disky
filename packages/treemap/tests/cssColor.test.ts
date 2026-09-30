/** `cssColor` / `colorResolver` (spec `specs/done/treemap-outlines-followups-2.md`
 *  §1): a canvas `fillStyle`/`strokeStyle` ignores a `var()` reference (paints
 *  black), so the renderers resolve it against the element first. */
import { describe, expect, it, vi } from 'vitest'
import { colorResolver, cssColor } from '../src/cssColor'

describe('cssColor', () => {
  it('passes a non-var color through unchanged', () => {
    const el = document.createElement('div')
    expect(cssColor(el, 'rgb(1, 2, 3)')).toBe('rgb(1, 2, 3)')
    expect(cssColor(el, '#abcdef')).toBe('#abcdef')
  })

  it('resolves a custom property set on the element', () => {
    const el = document.createElement('div')
    el.style.setProperty('--mk', '#123456')
    expect(cssColor(el, 'var(--mk)')).toBe('#123456')
  })

  it('falls back to the literal when the property is unset', () => {
    const el = document.createElement('div')
    expect(cssColor(el, 'var(--nope, #010203)')).toBe('#010203')
  })

  it('returns the original var string when unset and no fallback', () => {
    const el = document.createElement('div')
    expect(cssColor(el, 'var(--nope)')).toBe('var(--nope)')
  })
})

describe('colorResolver', () => {
  it('resolves vars and passes non-vars through', () => {
    const el = document.createElement('div')
    el.style.setProperty('--mk', '#123456')
    const resolve = colorResolver(el)
    expect(resolve('rgb(1, 2, 3)')).toBe('rgb(1, 2, 3)')
    expect(resolve('var(--mk)')).toBe('#123456')
    expect(resolve('var(--nope, #010203)')).toBe('#010203')
  })

  it('memoizes: getComputedStyle runs once per distinct var across a pass', () => {
    const el = document.createElement('div')
    el.style.setProperty('--mk', '#123456')
    const spy = vi.spyOn(window, 'getComputedStyle')
    const resolve = colorResolver(el)
    resolve('var(--mk)')
    resolve('var(--mk)')
    resolve('rgb(1, 2, 3)') // non-var never touches getComputedStyle
    expect(spy).toHaveBeenCalledTimes(1)
    spy.mockRestore()
  })
})
