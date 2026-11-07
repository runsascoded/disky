/**
 * Rendering coverage for the `outlineGroups` overlay — the two bugs that pure
 * `outlines.ts` geometry tests structurally can't catch (spec
 * `specs/done/treemap-outlines-followups.md`), so they only surfaced by eye in
 * mgu:
 *   - a canvas `strokeStyle` of `var(--x)` paints black (must be resolved), and
 *   - the DOM renderer built no `placedCells`, so the overlay drew nothing.
 * We mount a real `<Treemap>` with the *DOM* renderer + an `outlineGroups`, spy
 * the 2d context, and assert what actually reached the canvas.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { render } from '@testing-library/react'
import { Treemap } from '../src/Treemap'

interface Node {
  n: string
  size: number
  children?: Node[]
}

const accessors = {
  getSize: (n: Node) => n.size,
  getChildren: (n: Node) => n.children,
  getLabel: (n: Node) => n.n,
}

// Two same-key top-level cells → a non-empty union outline (≥1 stroked seg).
const tree: Node = {
  n: 'root',
  size: 200,
  children: [
    { n: 'a', size: 100 },
    { n: 'b', size: 100 },
  ],
}

interface Spy {
  strokeStyles: string[]
  moveTos: number
  strokes: number
}

/** Install a spy 2d context on every canvas; returns the recorder + a restore. */
function spyCanvas(): { spy: Spy; restore: () => void } {
  const spy: Spy = { strokeStyles: [], moveTos: 0, strokes: 0 }
  const ctx = {
    _stroke: '',
    get strokeStyle() { return this._stroke },
    set strokeStyle(v: string) { this._stroke = v; spy.strokeStyles.push(v) },
    lineWidth: 0,
    lineCap: '',
    lineJoin: '',
    setTransform: () => {},
    clearRect: () => {},
    beginPath: () => {},
    moveTo: () => { spy.moveTos++ },
    lineTo: () => {},
    stroke: () => { spy.strokes++ },
  }
  const saved = HTMLCanvasElement.prototype.getContext
  HTMLCanvasElement.prototype.getContext = vi.fn(() => ctx) as unknown as typeof saved
  return { spy, restore: () => { HTMLCanvasElement.prototype.getContext = saved } }
}

/** Give the map a measured size (widgets measure `clientWidth/Height` synchronously). */
function withLayout(w = 400, h = 300) {
  const saved = (['clientWidth', 'clientHeight'] as const).map(
    k => [k, Object.getOwnPropertyDescriptor(HTMLElement.prototype, k)] as const,
  )
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => w })
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => h })
  return () => {
    for (const [k, d] of saved) {
      if (d) Object.defineProperty(HTMLElement.prototype, k, d)
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[k]
    }
  }
}

const groupsWith = (color: (k: string) => string) => ({
  key: (n: Node) => (n.n === 'root' ? null : 'mk'),
  color,
})

describe('OutlineOverlay rendering (DOM renderer)', () => {
  const restores: Array<() => void> = []
  afterEach(() => { while (restores.length) restores.pop()!() })

  const mount = (color: (k: string) => string) => {
    const { spy, restore } = spyCanvas()
    const layout = withLayout()
    restores.push(restore, layout)
    render(
      <Treemap root={tree} {...accessors} renderer="dom" minCellArea={null} outlineGroups={groupsWith(color)} />,
    )
    return spy
  }

  it('draws the overlay under the DOM renderer (geometry is built, not empty)', () => {
    // Bug 3: `placedCells` was canvas-only, so the DOM renderer drew nothing.
    const spy = mount(() => '#ff00aa')
    expect(spy.moveTos).toBeGreaterThan(0)
    expect(spy.strokes).toBeGreaterThan(0)
  })

  it('resolves a `var(--x, fallback)` color instead of passing it to strokeStyle raw', () => {
    // Bug 2: a raw `var(...)` strokeStyle paints black; the unset var must fall
    // back to its literal, and no `var(...)` string may reach the canvas.
    const spy = mount(() => 'var(--mk-unset, #010203)')
    expect(spy.strokeStyles).toEqual(['#010203'])
  })

  it('passes a plain (non-var) color through unchanged', () => {
    const spy = mount(() => 'rgb(9, 8, 7)')
    expect(spy.strokeStyles).toEqual(['rgb(9, 8, 7)'])
  })

  it('calls onDrawn with the keys that produced ≥1 segment', () => {
    // Follow-on §2: the legend keys only the groups actually on screen. The
    // two same-key top cells form one visible group → `['mk']`.
    const { restore } = spyCanvas()
    const layout = withLayout()
    restores.push(restore, layout)
    const drawn: string[][] = []
    render(
      <Treemap
        root={tree}
        {...accessors}
        renderer="dom"
        minCellArea={null}
        outlineGroups={{ key: (n: Node) => (n.n === 'root' ? null : 'mk'), color: () => '#ff00aa', onDrawn: keys => drawn.push(keys) }}
      />,
    )
    expect(drawn.at(-1)).toEqual(['mk'])
  })
})
