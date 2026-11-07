import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render } from '@testing-library/react'
import { XOverview } from '../src/XOverview'

if (typeof globalThis.PointerEvent === 'undefined') {
  class PointerEventPolyfill extends MouseEvent {
    pointerId: number
    isPrimary: boolean
    constructor(type: string, init: PointerEventInit = {}) {
      super(type, init)
      this.pointerId = init.pointerId ?? 1
      this.isPrimary = init.isPrimary ?? true
    }
  }
  globalThis.PointerEvent = PointerEventPolyfill as unknown as typeof PointerEvent
}

/** Mount at a 400 px width: the strip insets like the chart (56 left, 16 right), so x ∈ [0, 4] spans px 56…384
 *  (82 px per unit), and jsdom's zero bounding rect makes clientX that px directly. */
function mount(props: Partial<Parameters<typeof XOverview>[0]> = {}) {
  const saved = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth')
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => 400 })
  const onDomain = vi.fn()
  const onReset = vi.fn()
  try {
    const r = render(<XOverview points={[0, 1, 2, 3, 4].map(x => ({ x, y: 10 + x }))} domain={null} onDomain={onDomain} onReset={onReset} {...props} />)
    return { ...r, onDomain, onReset, svg: r.container.querySelector('svg') as SVGSVGElement }
  } finally {
    if (saved) Object.defineProperty(HTMLElement.prototype, 'clientWidth', saved)
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth
  }
}

/** The lit range's outline (x, width) and the window band (x, width), as drawn. */
function shapes(container: HTMLElement) {
  const box = (el: Element | null) => (el ? [Number(el.getAttribute('x')), Number(el.getAttribute('width'))] : null)
  const dom = container.querySelectorAll('.dt-ts-overview-domain rect')
  return { domain: box(dom[2] ?? null), dimmed: [...dom].slice(0, 2).map(box), window: box(container.querySelector('.dt-ts-overview-window')) }
}

describe('<XOverview>', () => {
  it('unzoomed: nothing dimmed; the window as a band', () => {
    expect(shapes(mount({ window: [1, 2] }).container)).toEqual({ domain: null, dimmed: [], window: [138, 82] })
  })

  it('zoomed: the displayed range outlined, both sides of it dimmed', () => {
    expect(shapes(mount({ domain: [1, 3] }).container)).toEqual({ domain: [138, 164], dimmed: [[56, 82], [302, 82]], window: null })
  })

  it('a drag reports its range (ordered, clamped to the data); a press without a drag reports nothing; a double-click resets', () => {
    const { svg, onDomain, onReset } = mount()
    fireEvent.pointerDown(svg, { clientX: 302, button: 0, pointerId: 1 })
    fireEvent.pointerMove(svg, { clientX: 100 })
    fireEvent.pointerUp(window, { clientX: 10, pointerId: 1 })
    fireEvent.pointerDown(svg, { clientX: 200, button: 0, pointerId: 2 })
    fireEvent.pointerUp(window, { clientX: 201, pointerId: 2 })
    fireEvent.doubleClick(svg)
    expect([onDomain.mock.calls, onReset.mock.calls.length]).toEqual([[[0, 3]], 1])
  })
})
