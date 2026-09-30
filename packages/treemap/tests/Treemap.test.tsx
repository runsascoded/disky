import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { Treemap } from '../src/Treemap'
import { resolveRing } from '../src/cellStyle'

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

const tree: Node = {
  n: 'root',
  size: 300,
  children: [
    {
      n: 'foo',
      size: 200,
      children: [
        { n: 'a.txt', size: 100 },
        { n: 'b.txt', size: 100 },
      ],
    },
    { n: 'bar', size: 100 },
  ],
}

describe('<Treemap>', () => {
  it('renders the root label in the breadcrumbs bar', () => {
    render(<Treemap root={tree} {...accessors} />)
    // Root shows as the current-position segment
    expect(screen.getByText('root')).toBeInTheDocument()
  })

  it('renders the root size in the breadcrumbs', () => {
    render(<Treemap root={tree} {...accessors} formatSize={n => `${n} B`} />)
    expect(screen.getByText(/300 B/)).toBeInTheDocument()
  })

  it('calls onPathChange with the initial [root] path', () => {
    const onPathChange = vi.fn()
    render(<Treemap root={tree} {...accessors} onPathChange={onPathChange} />)
    expect(onPathChange).toHaveBeenCalled()
    const lastCall = onPathChange.mock.calls[onPathChange.mock.calls.length - 1][0]
    expect(lastCall).toEqual([tree])
  })

  it('drill-in click on a branch pushes to onPathChange', () => {
    const restore = withLayout()
    try {
      // Typed param so `mock.calls` is `[Node[]][]`, not `any[][]`.
      const onPathChange = vi.fn((_p: Node[]) => {})
      const { container } = render(
        <Treemap root={tree} {...accessors} onPathChange={onPathChange} minCellArea={null} />,
      )
      fireEvent.click(container.querySelector('.dt-treemap-map > .dt-treemap-cell.branch')!)
      expect(onPathChange.mock.calls.map(([p]) => p.map(n => n.n))).toEqual([
        ['root'],
        ['root', 'foo'],
      ])
    } finally {
      restore()
    }
  })

  it('reports the reset path exactly once when root changes', () => {
    const restore = withLayout()
    try {
      const onPathChange = vi.fn((_p: Node[]) => {})
      const props = { ...accessors, onPathChange, minCellArea: null }
      const { container, rerender } = render(<Treemap root={tree} {...props} />)
      fireEvent.click(container.querySelector('.dt-treemap-map > .dt-treemap-cell.branch')!)
      // A rescan of the same path: same shape, different object identity.
      rerender(<Treemap root={{ ...tree, n: 'root2' }} {...props} />)
      expect(onPathChange.mock.calls.map(([p]) => p.map(n => n.n))).toEqual([
        ['root'],
        ['root', 'foo'],
        ['root2'],
      ])
    } finally {
      restore()
    }
  })

  it('initialPath mounts drilled and reports that path', () => {
    const restore = withLayout()
    try {
      const onPathChange = vi.fn((_p: Node[]) => {})
      const { container } = render(
        <Treemap
          root={tree}
          initialPath={[tree, tree.children![0]]}
          {...accessors}
          onPathChange={onPathChange}
          minCellArea={null}
        />,
      )
      // Drilled into `foo`: its children are the top-level cells.
      expect(cellLabels(container)).toEqual(['a.txt', 'b.txt'])
      expect(onPathChange.mock.calls.map(([p]) => p.map(n => n.n))).toEqual([['root', 'foo']])
    } finally {
      restore()
    }
  })

  it('controlled `path` renders the prop; gestures report without changing it', () => {
    const restore = withLayout()
    try {
      const onPathChange = vi.fn((_p: Node[]) => {})
      const props = { ...accessors, onPathChange, minCellArea: null }
      const { container, rerender } = render(<Treemap root={tree} path={[tree]} {...props} />)
      expect(cellLabels(container)).toEqual(['foo', 'bar'])

      // A drill gesture only reports — the display follows the prop.
      fireEvent.click(container.querySelector('.dt-treemap-map > .dt-treemap-cell.branch')!)
      expect(onPathChange.mock.calls.map(([p]) => p.map(n => n.n))).toEqual([['root', 'foo']])
      expect(cellLabels(container)).toEqual(['foo', 'bar'])

      // Consumer renders the reported path back: now the drill shows.
      rerender(<Treemap root={tree} path={[tree, tree.children![0]]} {...props} />)
      expect(cellLabels(container)).toEqual(['a.txt', 'b.txt'])
    } finally {
      restore()
    }
  })

  it('Backspace is inert while typing in an input', () => {
    const restore = withLayout()
    try {
      const onPathChange = vi.fn((_p: Node[]) => {})
      const { container } = render(
        <Treemap root={tree} {...accessors} onPathChange={onPathChange} minCellArea={null} />,
      )
      fireEvent.click(container.querySelector('.dt-treemap-map > .dt-treemap-cell.branch')!)
      const input = document.createElement('input')
      document.body.appendChild(input)
      try {
        fireEvent.keyDown(input, { key: 'Backspace' })
        expect(cellLabels(container)).toEqual(['a.txt', 'b.txt'])  // still drilled
        fireEvent.keyDown(document.body, { key: 'Backspace' })
        expect(cellLabels(container)).toEqual(['foo', 'bar'])      // popped
      } finally {
        input.remove()
      }
    } finally {
      restore()
    }
  })

  it('tall leaves put the size on a second line; branch bars keep it inline', () => {
    const restore = withLayout()
    try {
      const { container } = render(
        <Treemap root={tree} {...accessors} formatSize={n => `${n} B`} minCellArea={null} />,
      )
      const cells = [...container.querySelectorAll('.dt-treemap-map > .dt-treemap-cell')]
      const shape = cells.map(el => [
        el.querySelector(':scope > .dt-treemap-lbl')?.textContent,
        el.querySelector(':scope > .dt-treemap-lbl2')?.textContent ?? null,
      ])
      // `foo` is a branch: inline size in its title bar, no 2nd line of its own.
      // `bar` is a tall leaf: name-only first line, size on the 2nd.
      expect(shape).toEqual([
        ['foo200 B', null],
        ['bar', '100 B'],
      ])
    } finally {
      restore()
    }
  })

  it('sizeAlign: the inline size sits after the name by default, at the far edge on "right"', () => {
    const restore = withLayout()
    try {
      const inlineSizeMargin = (align?: 'left' | 'right') => {
        const { container, unmount } = render(
          <Treemap root={tree} {...accessors} formatSize={n => `${n} B`} minCellArea={null} sizeAlign={align} />,
        )
        const sz = container.querySelector('.dt-treemap-map > .dt-treemap-cell > .dt-treemap-lbl > .sz') as HTMLElement
        const out = [sz.textContent, sz.style.marginLeft]
        unmount()
        return out
      }
      // `foo`'s title bar carries the only inline size; a plain flex sibling
      // (6px gap after the name) by default, `margin-left: auto` on "right".
      expect(inlineSizeMargin()).toEqual(['200 B', ''])
      expect(inlineSizeMargin('left')).toEqual(['200 B', ''])
      expect(inlineSizeMargin('right')).toEqual(['200 B', 'auto'])
    } finally {
      restore()
    }
  })

  it('segments render proportional makeup stripes on leaves, never on branches', () => {
    const restore = withLayout()
    try {
      const segments = [{ color: 'red', frac: 3 }, { color: 'blue', frac: 1 }]
      const { container } = render(
        <Treemap root={tree} {...accessors} minCellArea={null} colorForCell={() => ({ segments })} />,
      )
      const [foo, bar] = [...container.querySelectorAll('.dt-treemap-map > .dt-treemap-cell')]
      // `foo` renders child tiles — no stripes despite the style.
      expect([...foo.querySelectorAll(':scope > .dt-treemap-bg > div:not([class])')]).toEqual([])
      // `bar` (leaf, 133.33×300 → vertical slicing): span = 300 − 2·3 − 1 = 293,
      // split 3:1 → 219.75 / 73.25, stacked below the 3px inset with a 1px gap.
      const stripes = [...bar.querySelectorAll(':scope > .dt-treemap-bg > div:not([class])')] as HTMLElement[]
      const round2 = (v: string) => Math.round(parseFloat(v) * 100) / 100
      expect(stripes.map(s => [s.style.background, round2(s.style.top), round2(s.style.height)])).toEqual([
        ['red', 3, 219.75],
        ['blue', 223.75, 73.25],
      ])
    } finally {
      restore()
    }
  })

  describe('tiling', () => {
    const px = (v: string) => Math.round(parseFloat(v) * 100) / 100
    const rootCells = (c: HTMLElement) =>
      [...c.querySelectorAll('.dt-treemap-map > .dt-treemap-cell')] as HTMLElement[]

    it('gaps (default): cells inset 2px inside their squarify rects, rounded, outer ring', () => {
      const restore = withLayout()
      try {
        const { container } = render(<Treemap root={tree} {...accessors} minCellArea={null} />)
        const [foo, bar] = rootCells(container)
        // 400×300 canvas, foo:bar = 2:1 → 266.67 / 133.33 wide, minus the 2px gutter
        expect([foo, bar].map(el => [px(el.style.width), px(el.style.height), el.style.borderRadius])).toEqual([
          [264.67, 298, '3px'],
          [131.33, 298, '3px'],
        ])
        expect(bar.style.boxShadow).toBe('0 0 0 1px var(--dt-treemap-cell-border, transparent)')
        expect(bar.classList.contains('shared')).toBe(false)
        expect((foo.querySelector(':scope > .dt-treemap-inner') as HTMLElement).style.inset).toBe('20px 3px 3px 3px')
      } finally {
        restore()
      }
    })

    it('shared: exact rects, square corners, half of a depth-scaled stroke inset per cell', () => {
      const restore = withLayout()
      try {
        // edgeContrast off so the stroke stays the neutral gutter var — this
        // test is about tiling geometry, not the adaptive-edge default (below).
        const { container } = render(<Treemap root={tree} {...accessors} minCellArea={null} tiling="shared" edgeContrast={false} />)
        const [foo, bar] = rootCells(container)
        expect([foo, bar].map(el => [px(el.style.width), px(el.style.height), el.style.borderRadius])).toEqual([
          [266.67, 300, '0'],
          [133.33, 300, '0'],
        ])
        // depth 0: borderWidth = max(1, 2 − 0) = 2 (the gaps gutter's width,
        // so shared never reads wider than gaps) → each neighbor paints a 1px
        // inset ring, exposed by insetting the paint layer to match (a
        // full-bleed paint layer would cover the shadow entirely). The base
        // stays the container color — the paint layer is translucent, so
        // tinting the base would wash the whole cell toward the stroke.
        expect(bar.style.boxShadow).toBe('inset 0 0 0 1px var(--dt-treemap-edge, var(--dt-treemap-container-bg, #202024))')
        expect(bar.style.background).toBe('var(--dt-treemap-container-bg, #202024)')
        expect((bar.querySelector(':scope > .dt-treemap-bg') as HTMLElement).style.inset).toBe('1px')
        expect(bar.classList.contains('shared')).toBe(true)
        // foo's children fill to foo's own half-stroke (below the 20px title)
        expect((foo.querySelector(':scope > .dt-treemap-inner') as HTMLElement).style.inset).toBe('20px 1px 1px 1px')
        // depth 1: stroke 1 (hairline) → 0.5px ring; children split foo's
        // 264.67×279 canvas 1:1 (taller than wide → stacked)
        const [a, b] = [...foo.querySelectorAll(':scope > .dt-treemap-inner > .dt-treemap-cell')] as HTMLElement[]
        expect([a, b].map(el => [
          px(el.style.width), px(el.style.height),
          (el.querySelector(':scope > .dt-treemap-bg') as HTMLElement).style.inset, el.style.boxShadow,
        ])).toEqual([
          [264.67, 139.5, '0.5px', 'inset 0 0 0 0.5px var(--dt-treemap-edge, var(--dt-treemap-container-bg, #202024))'],
          [264.67, 139.5, '0.5px', 'inset 0 0 0 0.5px var(--dt-treemap-edge, var(--dt-treemap-container-bg, #202024))'],
        ])
      } finally {
        restore()
      }
    })

    it('edgeContrast (default): each shared cell strokes to contrast with its own face', () => {
      const restore = withLayout()
      try {
        const { container } = render(
          <Treemap
            root={tree}
            {...accessors}
            minCellArea={null}
            tiling="shared"
            colorForCell={n => (accessors.getLabel(n) === 'foo'
              ? { bg: '#ffffff', ink: '#000' }
              : { bg: '#000000', ink: '#fff' })}
          />,
        )
        const [foo, bar] = rootCells(container)
        // depth 0 → 1px half-stroke; white face gets a dark stroke, black a
        // light one — grey-on-grey borders can't vanish because the color is
        // derived from each cell's own face.
        expect(foo.style.boxShadow).toBe('inset 0 0 0 1px rgba(0, 0, 0, 0.55)')
        expect(bar.style.boxShadow).toBe('inset 0 0 0 1px rgba(255, 255, 255, 0.42)')
      } finally {
        restore()
      }
    })

    it('foldControl slider scales the fold thresholds live', () => {
      const restore = withLayout()
      try {
        // Dominant cell + 20 tiny siblings. At 400×300 (scale ≈ 11.54) each
        // tiny cell is ~231px²: below minCellArea 300 by default (they fold
        // into one dust tile — collectively wide enough to draw a hatch),
        // above it at the finest slider setting (×0.25 → 75) so they render as
        // their own cells.
        const foldTree: Node = {
          n: 'root',
          size: 10_400,
          children: [
            { n: 'big', size: 10_000 },
            ...Array.from({ length: 20 }, (_, i) => ({ n: `t${i}`, size: 20 })),
          ],
        }
        const cells = (c: HTMLElement) => c.querySelectorAll('.dt-treemap-map > .dt-treemap-cell')
        // The dust tile carries a canvas; a real cell does not — so the canvas
        // count is the number of folded tiles.
        const dustTiles = (c: HTMLElement) => c.querySelectorAll('.dt-treemap-map > .dt-treemap-cell canvas')
        const { container } = render(
          <Treemap root={foldTree} {...accessors} foldControl minCellArea={300} minCellSide={null} />,
        )
        // Default (multiplier 1): the six tiny siblings fold into one dust tile.
        expect(cells(container).length).toBe(2)
        expect(dustTiles(container).length).toBe(1)
        // Drag to the finest setting (value 1 → multiplier 0.25): the tiny
        // siblings drop below the lowered threshold and render individually.
        const slider = container.querySelector('.dt-treemap-fold input') as HTMLInputElement
        fireEvent.change(slider, { target: { value: '1' } })
        expect(cells(container).length).toBe(21)
        expect(dustTiles(container).length).toBe(0)
      } finally {
        restore()
      }
    })

    it('remainderTail widens a dominated tail vs. plain squarify', () => {
      const restore = withLayout(400, 300)
      try {
        // One dominant child + a five-cell tail that plain squarify squeezes
        // into thin full-height slivers. Folding off so both layouts keep all
        // six cells (else foldThin would collapse the default's slivers).
        const remTree: Node = {
          n: 'root',
          size: 10_000,
          children: [
            { n: 'big', size: 9_875 },
            { n: 'a', size: 40 }, { n: 'b', size: 30 }, { n: 'c', size: 25 },
            { n: 'd', size: 18 }, { n: 'e', size: 12 },
          ],
        }
        const widths = (c: HTMLElement) =>
          rootCells(c).map(el => px(el.style.width)).sort((x, y) => x - y)
        const plain = render(
          <Treemap root={remTree} {...accessors} minCellArea={null} minCellSide={null} />,
        )
        const rem = render(
          <Treemap root={remTree} {...accessors} minCellArea={null} minCellSide={null} remainderTail />,
        )
        const wPlain = widths(plain.container)
        const wRem = widths(rem.container)
        // Same six cells either way.
        expect(wPlain.length).toBe(6)
        expect(wRem.length).toBe(6)
        // The narrowest tail cell is wider under the remainder band…
        expect(wRem[0]).toBeGreaterThan(wPlain[0])
        // …and the dominant cell is a little narrower (area traded to the tail).
        expect(wRem[5]).toBeLessThan(wPlain[5])
      } finally {
        restore()
      }
    })

    it('callback decides per subtree, with the children\'s laid-out density', () => {
      const restore = withLayout()
      try {
        const seen: [string, number, number, number][] = []
        const { container } = render(
          <Treemap
            root={tree}
            {...accessors}
            minCellArea={null}
            tiling={(n: Node, _p, depth, ctx) => {
              seen.push([n.n, depth, ctx.nChildren, Math.round(ctx.medianChildArea)])
              return n.n === 'foo' ? 'shared' : 'gaps'
            }}
          />,
        )
        // root's children tiled with gaps; foo's children shared. (The
        // pre-measure render sees an empty canvas — skip it.)
        expect(seen.filter(([, , n]) => n > 0)).toEqual([
          ['root', 0, 2, 80000],   // upper median of 266.67×300 and 133.33×300
          ['foo', 1, 2, 36102],    // gaps-dims first layout of foo's 260.67×277 canvas, stacked halves
        ])
        const [foo] = rootCells(container)
        expect(px(foo.style.width)).toBe(264.67)
        // gaps-mode parent (no own stroke) → shared children fill to its edge
        expect((foo.querySelector(':scope > .dt-treemap-inner') as HTMLElement).style.inset).toBe('20px 0px 0px 0px')
        const [a] = [...foo.querySelectorAll(':scope > .dt-treemap-inner > .dt-treemap-cell')] as HTMLElement[]
        expect(a.classList.contains('shared')).toBe(true)
        // full width of foo's 264.67px box (stacked halves), not the 266.67 rect
        expect(px(a.style.width)).toBe(264.67)
      } finally {
        restore()
      }
    })

    it('borderWidth overrides the stroke', () => {
      const restore = withLayout()
      try {
        const { container } = render(
          <Treemap root={tree} {...accessors} minCellArea={null} tiling="shared" borderWidth={d => 4 - d} />,
        )
        const [foo] = rootCells(container)
        expect(foo.style.boxShadow).toBe('inset 0 0 0 2px var(--dt-treemap-edge, var(--dt-treemap-container-bg, #202024))')
        expect((foo.querySelector(':scope > .dt-treemap-bg') as HTMLElement).style.inset).toBe('2px')
        expect((foo.querySelector(':scope > .dt-treemap-inner') as HTMLElement).style.inset).toBe('20px 2px 2px 2px')
      } finally {
        restore()
      }
    })
  })

  describe('resolveRing', () => {
    it('normalizes every ring form and drops the empty ones', () => {
      expect([
        resolveRing(undefined),
        resolveRing(''),
        resolveRing('rgb(1, 2, 3)'),
        resolveRing({ color: 'rgb(1, 2, 3)' }),
        resolveRing({ color: 'rgb(1, 2, 3)', width: 4, inset: false }),
        resolveRing({ color: '', width: 4 }),
      ]).toEqual([
        null,
        null,
        { color: 'rgb(1, 2, 3)', width: 2, inset: true },
        { color: 'rgb(1, 2, 3)', width: 2, inset: true },
        { color: 'rgb(1, 2, 3)', width: 4, inset: false },
        null,
      ])
    })
  })

  describe('ring (brush emphasis)', () => {
    const rootCells = (c: HTMLElement) =>
      [...c.querySelectorAll('.dt-treemap-map > .dt-treemap-cell')] as HTMLElement[]
    const barRing = (props: Record<string, unknown>): string => {
      const { container } = render(
        <Treemap
          root={tree}
          {...accessors}
          minCellArea={null}
          colorForCell={n => (accessors.getLabel(n) === 'bar' ? { bg: '#123456', ...props } : { bg: '#654321' })}
        />,
      )
      return (rootCells(container)[1] as HTMLElement).style.boxShadow
    }

    it('gaps: an object ring stacks first (on top of) the structural border', () => {
      const restore = withLayout()
      try {
        expect(barRing({ ring: { color: 'rgb(255, 255, 255)', width: 3 } })).toBe(
          'inset 0 0 0 3px rgb(255, 255, 255), 0 0 0 1px var(--dt-treemap-cell-border, transparent)',
        )
      } finally {
        restore()
      }
    })

    it('a bare-string ring is shorthand for that color at the default 2px, inset', () => {
      const restore = withLayout()
      try {
        expect(barRing({ ring: 'rgb(0, 200, 0)' })).toBe(
          'inset 0 0 0 2px rgb(0, 200, 0), 0 0 0 1px var(--dt-treemap-cell-border, transparent)',
        )
      } finally {
        restore()
      }
    })

    it('inset:false draws the ring outside the box (no `inset` keyword)', () => {
      const restore = withLayout()
      try {
        expect(barRing({ ring: { color: 'rgb(0, 0, 0)', inset: false } })).toBe(
          '0 0 0 2px rgb(0, 0, 0), 0 0 0 1px var(--dt-treemap-cell-border, transparent)',
        )
      } finally {
        restore()
      }
    })

    it('shared: the ring stacks over the half-stroke, honored in this mode too', () => {
      const restore = withLayout()
      try {
        const { container } = render(
          <Treemap
            root={tree}
            {...accessors}
            minCellArea={null}
            tiling="shared"
            edgeContrast={false}
            colorForCell={n => (accessors.getLabel(n) === 'bar'
              ? { bg: '#123456', ring: { color: 'rgb(120, 170, 255)', width: 2 } }
              : { bg: '#654321' })}
          />,
        )
        expect((rootCells(container)[1] as HTMLElement).style.boxShadow).toBe(
          'inset 0 0 0 2px rgb(120, 170, 255), inset 0 0 0 1px var(--dt-treemap-edge, var(--dt-treemap-container-bg, #202024))',
        )
      } finally {
        restore()
      }
    })

    it('no ring: box-shadow is the structural border alone (gaps unchanged)', () => {
      const restore = withLayout()
      try {
        expect(barRing({})).toBe('0 0 0 1px var(--dt-treemap-cell-border, transparent)')
      } finally {
        restore()
      }
    })
  })

  describe('cellHref', () => {
    it('leaf-rendered cells become anchors; cells with nested tiles stay divs', () => {
      const restore = withLayout()
      try {
        const { container } = render(
          <Treemap root={tree} {...accessors} minCellArea={null} cellHref={n => `/n/${n.n}`} />,
        )
        const cells = [...container.querySelectorAll('.dt-treemap-map > .dt-treemap-cell')] as HTMLElement[]
        expect(cells.map(el => [el.tagName, el.getAttribute('href'), el.style.cursor, el.style.textDecoration])).toEqual([
          ['DIV', null, 'pointer', ''],             // foo renders children → no <a>
          ['A', '/n/bar', 'pointer', 'none'],
        ])
        // foo's leaf children are anchors too
        const inner = [...cells[0].querySelectorAll('.dt-treemap-inner > .dt-treemap-cell')] as HTMLElement[]
        expect(inner.map(el => [el.tagName, el.getAttribute('href')])).toEqual([['A', '/n/a.txt'], ['A', '/n/b.txt']])
      } finally {
        restore()
      }
    })

    it('plain clicks are prevented and flow to onCellClick; modified clicks keep native behavior', () => {
      const restore = withLayout()
      try {
        const onCellClick = vi.fn((_n: Node) => true)
        const { container } = render(
          <Treemap root={tree} {...accessors} minCellArea={null} cellHref={n => `/n/${n.n}`} onCellClick={onCellClick} />,
        )
        const bar = container.querySelector('.dt-treemap-map > a.dt-treemap-cell')!
        const plain = fireEvent.click(bar)
        expect(plain).toBe(false)   // preventDefault'ed
        expect(onCellClick.mock.calls.map(([n]) => n.n)).toEqual(['bar'])
        const meta = fireEvent.click(bar, { metaKey: true })
        expect(meta).toBe(true)     // native (new tab)
        expect(onCellClick).toHaveBeenCalledTimes(1)
      } finally {
        restore()
      }
    })
  })

  it('clicking a leaf marks that DOM cell .pinned (accent ring); re-click clears it', () => {
    // The `.pinned` class carries the same accent ring the canvas renderer and
    // the pinned tooltip use — committed on click (mouseup), not on the focus
    // that a mousedown lands (`:focus-visible` handles keyboard focus only).
    const restore = withLayout()
    try {
      const { container } = render(<Treemap root={tree} {...accessors} minCellArea={null} />)
      const leaf = [...container.querySelectorAll('.dt-treemap-map > .dt-treemap-cell')]
        .find(el => !el.classList.contains('branch')) as HTMLElement // `bar`
      expect(leaf.classList.contains('pinned')).toBe(false)
      fireEvent.click(leaf)
      const pinned = [...container.querySelectorAll('.dt-treemap-map > .dt-treemap-cell.pinned')]
      expect(pinned.length).toBe(1)
      expect(pinned[0].classList.contains('branch')).toBe(false)
      fireEvent.click(pinned[0] as HTMLElement)
      expect(container.querySelectorAll('.dt-treemap-cell.pinned').length).toBe(0)
    } finally {
      restore()
    }
  })

  it('branch/chain chrome classes are size-gated (min dim ≥ 28px) unless children render', () => {
    const restore = withLayout(400, 300)
    try {
      // 400 tiny drillable dirs → ~17px tiles: drillable, but no `branch` chrome
      const many: Node = {
        n: 'root', size: 400,
        children: Array.from({ length: 400 }, (_, i) => ({ n: `d${i}`, size: 1, children: [{ n: 'f', size: 1 }] })),
      }
      const { container } = render(<Treemap root={many} {...accessors} minCellArea={null} />)
      const cells = [...container.querySelectorAll('.dt-treemap-map > .dt-treemap-cell')] as HTMLElement[]
      expect(cells.length).toBe(400)
      expect(cells.every(el => Math.min(parseFloat(el.style.width), parseFloat(el.style.height)) < 28)).toBe(true)
      expect(cells.filter(el => el.classList.contains('branch')).length).toBe(0)
      // the original tree: foo (266×300, renders children) keeps `branch`
      const { container: c2 } = render(<Treemap root={tree} {...accessors} minCellArea={null} />)
      expect(c2.querySelector('.dt-treemap-map > .dt-treemap-cell')!.classList.contains('branch')).toBe(true)
    } finally {
      restore()
    }
  })

  it('breadcrumb bar shows a single non-link segment for the current node', () => {
    const { container } = render(<Treemap root={tree} {...accessors} />)
    // Root is the current node — no interactive anchor around it
    const nav = container.querySelector('nav[aria-label="Path"]')!
    const links = nav.querySelectorAll('a')
    expect(links.length).toBe(0)
  })

  it('does not render the fullscreen button when fullscreen={false}', () => {
    const { container } = render(<Treemap root={tree} {...accessors} fullscreen={false} />)
    expect(container.querySelector('.dt-treemap-fs')).toBeNull()
  })

  it('renders renderLegend output in the bar', () => {
    render(
      <Treemap
        root={tree}
        {...accessors}
        renderLegend={n => <span>legend:{n.n}</span>}
      />,
    )
    expect(screen.getByText('legend:root')).toBeInTheDocument()
  })

  it('renders renderRollup output above the map', () => {
    render(
      <Treemap
        root={tree}
        {...accessors}
        renderRollup={n => <span>rollup:{n.n}</span>}
      />,
    )
    expect(screen.getByText('rollup:root')).toBeInTheDocument()
  })
})

/**
 * jsdom has no layout, so every `clientWidth`/`clientHeight` is 0 and the map
 * renders no cells. Stub the prototype getters *before* mount — the component
 * measures synchronously in `useLayoutEffect`, so post-mount stubbing (what
 * `mountWithSize` does) is already too late.
 */
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

/** Labels of the top-level cells, in render order (label text only, no size). */
function cellLabels(container: HTMLElement): string[] {
  return [...container.querySelectorAll('.dt-treemap-map > .dt-treemap-cell > .dt-treemap-lbl')]
    .map(el => el.childNodes[0]?.textContent ?? '')
}

describe('<Treemap> lazy children', () => {
  // `paged` mirrors a depth-bounded server response: `dir` says it has
  // children, but none came down with this page.
  interface Paged {
    n: string
    size: number
    kids?: Paged[]
    n_children?: number
  }
  const pagedAccessors = {
    getSize: (n: Paged) => n.size,
    getChildren: (n: Paged) => n.kids,
    getLabel: (n: Paged) => n.n,
    hasChildren: (n: Paged) => (n.n_children ?? 0) > 0,
  }
  const paged: Paged = {
    n: 'root',
    size: 300,
    n_children: 2,
    kids: [
      { n: 'deep', size: 200, n_children: 2 },
      { n: 'leaf.txt', size: 100 },
    ],
  }
  const deepKids: Paged[] = [
    { n: 'x.bin', size: 120 },
    { n: 'y.bin', size: 80 },
  ]

  let restore: () => void
  beforeEach(() => { restore = withLayout() })
  afterEach(() => restore())

  it('marks a node with unloaded children drillable, and a true leaf not', () => {
    const { container } = render(
      <Treemap root={paged} {...pagedAccessors} loadChildren={async () => []} minCellArea={null} />,
    )
    const drillable = [...container.querySelectorAll('.dt-treemap-map > .dt-treemap-cell')]
      .map(el => [el.querySelector('.dt-treemap-lbl')?.childNodes[0]?.textContent, el.classList.contains('branch')])
    expect(drillable).toEqual([['deep', true], ['leaf.txt', false]])
  })

  it('fetches once per drill — not once per visible cell', async () => {
    // Typed params so `mock.calls[0]` is a 2-tuple, not `[]`.
    const loadChildren = vi.fn(async (_n: Paged, _path: Paged[]) => deepKids)
    const { container } = render(
      <Treemap root={paged} {...pagedAccessors} loadChildren={loadChildren} minCellArea={null} />,
    )
    // Root's children came with the page, so nothing loads until we drill.
    expect(loadChildren).toHaveBeenCalledTimes(0)

    fireEvent.click(container.querySelector('.dt-treemap-cell.branch')!)
    await screen.findByText('x.bin')
    expect(loadChildren.mock.calls.length).toBe(1)
    expect(loadChildren.mock.calls[0][0]).toEqual(paged.kids![0])
    expect(loadChildren.mock.calls[0][1]).toEqual([paged, paged.kids![0]])
    expect(cellLabels(container)).toEqual(['x.bin', 'y.bin'])
  })

  it('shows the loading state until the fetch resolves', async () => {
    let release: (kids: Paged[]) => void = () => {}
    const { container } = render(
      <Treemap
        root={paged}
        {...pagedAccessors}
        loadChildren={() => new Promise<Paged[]>(res => { release = res })}
        renderLoading={n => <span>loading:{n.n}</span>}
        minCellArea={null}
      />,
    )
    fireEvent.click(container.querySelector('.dt-treemap-cell.branch')!)
    expect(screen.getByText('loading:deep')).toBeInTheDocument()
    expect(cellLabels(container)).toEqual([])

    release(deepKids)
    await screen.findByText('x.bin')
    expect(container.querySelector('.dt-treemap-status')).toBeNull()
  })

  it('caches per node — drilling back in does not refetch', async () => {
    const loadChildren = vi.fn(async () => deepKids)
    const { container } = render(
      <Treemap root={paged} {...pagedAccessors} loadChildren={loadChildren} minCellArea={null} />,
    )
    fireEvent.click(container.querySelector('.dt-treemap-cell.branch')!)
    await screen.findByText('x.bin')

    fireEvent.keyDown(document, { key: 'Backspace' })     // pop back to root
    await screen.findByText('leaf.txt')
    fireEvent.click(container.querySelector('.dt-treemap-cell.branch')!)
    await screen.findByText('x.bin')

    expect(loadChildren).toHaveBeenCalledTimes(1)
    expect(cellLabels(container)).toEqual(['x.bin', 'y.bin'])
  })

  it('surfaces a rejection and refetches on retry', async () => {
    const loadChildren = vi.fn()
      .mockRejectedValueOnce(new Error('502 upstream'))
      .mockResolvedValueOnce(deepKids)
    const { container } = render(
      <Treemap root={paged} {...pagedAccessors} loadChildren={loadChildren} minCellArea={null} />,
    )
    fireEvent.click(container.querySelector('.dt-treemap-cell.branch')!)
    const err = await screen.findByText(/502 upstream/)
    expect(err.textContent).toBe('Couldn’t load deep: 502 upstream')

    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    await screen.findByText('x.bin')
    expect(loadChildren).toHaveBeenCalledTimes(2)
    expect(cellLabels(container)).toEqual(['x.bin', 'y.bin'])
  })

  it('calls onChildrenLoaded with the node, its path, and the children', async () => {
    const onChildrenLoaded = vi.fn()
    const { container } = render(
      <Treemap
        root={paged}
        {...pagedAccessors}
        loadChildren={async () => deepKids}
        onChildrenLoaded={onChildrenLoaded}
        minCellArea={null}
      />,
    )
    fireEvent.click(container.querySelector('.dt-treemap-cell.branch')!)
    await screen.findByText('x.bin')
    expect(onChildrenLoaded.mock.calls).toEqual([[paged.kids![0], [paged, paged.kids![0]], deepKids]])
  })

  it('drops the cache when root changes — a new root is a different tree', async () => {
    const loadChildren = vi.fn(async () => deepKids)
    const { container, rerender } = render(
      <Treemap root={paged} {...pagedAccessors} loadChildren={loadChildren} minCellArea={null} />,
    )
    fireEvent.click(container.querySelector('.dt-treemap-cell.branch')!)
    await screen.findByText('x.bin')

    // Same shape, different object: a rescan of the same path.
    rerender(
      <Treemap root={{ ...paged }} {...pagedAccessors} loadChildren={loadChildren} minCellArea={null} />,
    )
    fireEvent.click(container.querySelector('.dt-treemap-cell.branch')!)
    await screen.findByText('x.bin')
    expect(loadChildren).toHaveBeenCalledTimes(2)
  })

  it('ignores hasChildren without a loader, so eager consumers are unaffected', () => {
    const { container } = render(<Treemap root={paged} {...pagedAccessors} minCellArea={null} />)
    // Both cells render; neither is drillable, since nothing can fetch `deep`.
    expect(cellLabels(container)).toEqual(['deep', 'leaf.txt'])
    expect(container.querySelectorAll('.dt-treemap-map > .dt-treemap-cell.branch').length).toBe(0)
  })
})

describe('<Treemap> depth fade', () => {
  // Single-child spine: root → d1 → d2 → d3 → leaf, so each level nests one
  // cell inside the last and cumulative CSS opacity is the product of the
  // spine's local values.
  const spine: Node = {
    n: 'root',
    size: 100,
    children: [{
      n: 'd1',
      size: 100,
      children: [{
        n: 'd2',
        size: 100,
        children: [{
          n: 'd3',
          size: 100,
          children: [{ n: 'leaf', size: 100 }],
        }],
      }],
    }],
  }

  /** Walk the spine collecting each cell's bg-layer opacity, asserting the
   * cell div itself never fades (label ink stays full-strength). */
  function bgFades(container: HTMLElement): number[] {
    const fades: number[] = []
    let el = container.querySelector('.dt-treemap-map > .dt-treemap-cell') as HTMLElement | null
    while (el) {
      expect(el.style.opacity).toBe('')
      // Opaque container-color base: the faded paint layer composites over
      // this, never over an ancestor's bg.
      expect(el.style.background).toBe('var(--dt-treemap-container-bg, #202024)')
      const bg = el.querySelector(':scope > .dt-treemap-bg') as HTMLElement
      fades.push(Number(bg.style.opacity))
      el = el.querySelector(':scope > .dt-treemap-inner > .dt-treemap-cell')
    }
    return fades
  }

  it('fades backgrounds per depth, floored at fadeFloor; cell divs (and their labels) never fade', () => {
    const restore = withLayout()
    try {
      const { container } = render(<Treemap root={spine} {...accessors} minCellArea={null} />)
      // Bg-layer opacity is max(rootFade × depthFade^d, fadeFloor) per cell —
      // no compounding, since only the paint layer fades, not the subtree:
      //   d0 0.92, d1 0.7544, d2 floored to 0.75, d3 held at 0.75.
      expect(bgFades(container)).toEqual([0.92, 0.92 * 0.82, 0.75, 0.75])
    } finally {
      restore()
    }
  })

  it('depthFade={1} keeps every level at rootFade', () => {
    const restore = withLayout()
    try {
      const { container } = render(
        <Treemap root={spine} {...accessors} minCellArea={null} depthFade={1} />,
      )
      expect(bgFades(container)).toEqual([0.92, 0.92, 0.92, 0.92])
    } finally {
      restore()
    }
  })
})

describe('<Treemap renderer>', () => {
  it("default 'dom' renders DOM cells and no canvas", () => {
    const restore = withLayout()
    try {
      const { container } = render(<Treemap root={tree} {...accessors} minCellArea={null} />)
      expect(container.querySelector('.dt-treemap-map canvas')).toBe(null)
      expect(cellLabels(container)).toEqual(['foo', 'bar'])
    } finally {
      restore()
    }
  })

  it("'canvas' renders one canvas and no DOM cells", () => {
    const restore = withLayout()
    try {
      const { container } = render(
        <Treemap root={tree} {...accessors} minCellArea={null} renderer="canvas" />,
      )
      expect(container.querySelectorAll('.dt-treemap-map canvas')).toHaveLength(1)
      expect(container.querySelectorAll('.dt-treemap-map > .dt-treemap-cell')).toHaveLength(0)
    } finally {
      restore()
    }
  })

  it("'canvas' still renders the shared chrome (crumbs + size)", () => {
    render(<Treemap root={tree} {...accessors} renderer="canvas" formatSize={n => `${n} B`} />)
    expect(screen.getByText('root')).toBeInTheDocument()
    expect(screen.getByText(/300 B/)).toBeInTheDocument()
  })
})

describe('<Treemap onCellHover>', () => {
  it('fires node+path on enter, once per cell, null on leave', () => {
    vi.useFakeTimers()
    const restore = withLayout()
    try {
      const calls: [string | null, string[]][] = []
      const onCellHover = (n: Node | null, p: Node[]) => calls.push([n ? n.n : null, p.map(x => x.n)])
      const { container } = render(
        <Treemap root={tree} {...accessors} onCellHover={onCellHover} minCellArea={null} />,
      )
      const leaf = [...container.querySelectorAll('.dt-treemap-map > .dt-treemap-cell')]
        .find(el => !el.classList.contains('branch'))!
      fireEvent.mouseMove(leaf)
      fireEvent.mouseMove(leaf) // same cell: no second enter
      fireEvent.mouseLeave(container.querySelector('.dt-treemap-map')!)
      vi.advanceTimersByTime(200) // past the 180ms leave grace
      expect(calls).toEqual([['bar', ['root', 'bar']], [null, []]])
    } finally {
      restore()
      vi.useRealTimers()
    }
  })

  it('is inert when absent (no throw on hover/leave)', () => {
    const restore = withLayout()
    try {
      const { container } = render(<Treemap root={tree} {...accessors} minCellArea={null} />)
      const leaf = [...container.querySelectorAll('.dt-treemap-map > .dt-treemap-cell')]
        .find(el => !el.classList.contains('branch'))!
      expect(() => fireEvent.mouseMove(leaf)).not.toThrow()
    } finally {
      restore()
    }
  })
})

describe('<Treemap a11yLinks> (canvas overlay)', () => {
  const a11yTree: Node = {
    n: 'root',
    size: 100,
    children: [
      { n: 'big', size: 70 },
      { n: 'mid', size: 20 },
      { n: 'small', size: 10 },
    ],
  }
  const base = { ...accessors, minCellArea: null as null, renderer: 'canvas' as const, formatSize: (n: number) => `${n}` }

  it('builds a bounded anchor overlay (largest-first) with href + aria-label', () => {
    const restore = withLayout(600, 400)
    try {
      const { container } = render(<Treemap root={a11yTree} {...base} a11yMaxCells={2} cellHref={n => `/f/${n.n}`} />)
      const links = [...container.querySelectorAll('.dt-treemap-map a')]
      expect(links.map(a => [a.getAttribute('href'), a.getAttribute('aria-label')])).toEqual([
        ['/f/big', 'big, 70'],
        ['/f/mid', 'mid, 20'],
      ])
    } finally {
      restore()
    }
  })

  it('renders buttons (not anchors) when no cellHref is given', () => {
    const restore = withLayout(600, 400)
    try {
      const { container } = render(<Treemap root={a11yTree} {...base} a11yMaxCells={2} />)
      expect(container.querySelectorAll('.dt-treemap-map a').length).toBe(0)
      expect(container.querySelectorAll('.dt-treemap-map button').length).toBe(2)
    } finally {
      restore()
    }
  })

  it('a11yLinks={false} builds no overlay', () => {
    const restore = withLayout(600, 400)
    try {
      const { container } = render(<Treemap root={a11yTree} {...base} a11yLinks={false} cellHref={n => `/f/${n.n}`} />)
      expect(container.querySelectorAll('.dt-treemap-map a, .dt-treemap-map button').length).toBe(0)
    } finally {
      restore()
    }
  })

  it('a11yMinSide filters out cells below the short-side floor', () => {
    const restore = withLayout(600, 400)
    try {
      const { container } = render(<Treemap root={a11yTree} {...base} a11yMinSide={100000} cellHref={n => `/f/${n.n}`} />)
      expect(container.querySelectorAll('.dt-treemap-map a').length).toBe(0)
    } finally {
      restore()
    }
  })

  it('the DOM renderer ignores a11y overlay props (no extra anchors)', () => {
    const restore = withLayout(600, 400)
    try {
      // DOM cells are real elements already; a11yLinks adds nothing.
      const { container } = render(<Treemap root={a11yTree} {...accessors} minCellArea={null} a11yMaxCells={2} />)
      expect(container.querySelector('.dt-treemap-map canvas')).toBe(null)
    } finally {
      restore()
    }
  })
})

describe('<Treemap> canvas pin ring', () => {
  const tree3: Node = {
    n: 'root',
    size: 100,
    children: [
      { n: 'big', size: 70 },
      { n: 'mid', size: 20 },
      { n: 'small', size: 10 },
    ],
  }

  it('clicking a leaf pins it and rings the cell on the canvas', () => {
    const restore = withLayout(600, 400)
    try {
      const { container } = render(<Treemap root={tree3} {...accessors} minCellArea={null} renderer="canvas" />)
      const ringSel = '.dt-treemap-map div[style*="120, 170, 255"]'
      expect(container.querySelector(ringSel)).toBe(null)
      // A leaf isn't drillable → clicking pins it (via the a11y overlay button,
      // which routes through the same click path as a canvas mouse click).
      const bigBtn = [...container.querySelectorAll('.dt-treemap-map button')]
        .find(b => b.getAttribute('aria-label')?.startsWith('big'))!
      fireEvent.click(bigBtn)
      expect(container.querySelector(ringSel)).not.toBe(null)
    } finally {
      restore()
    }
  })

  it('the pinned tip is click-through (container none, × auto) so a covered cell still pins', () => {
    // The tip anchors over a cell's top-left; if it caught clicks, a cell lying
    // under an already-pinned tip could never be pinned. Container must pass
    // clicks through to the canvas/cell; only its × takes pointer events.
    const restore = withLayout(600, 400)
    try {
      const { container } = render(<Treemap root={tree3} {...accessors} minCellArea={null} renderer="canvas" />)
      const bigBtn = [...container.querySelectorAll('.dt-treemap-map button')]
        .find(b => b.getAttribute('aria-label')?.startsWith('big'))!
      fireEvent.click(bigBtn)
      const tip = container.querySelector('.dt-treemap-tip') as HTMLElement
      expect(tip).not.toBe(null)
      expect(tip.classList.contains('pinned')).toBe(true)
      expect(tip.style.pointerEvents).toBe('none')
      const closeBtn = tip.querySelector('button') as HTMLElement
      expect(closeBtn.title).toBe('Unpin (Esc)')
      expect(closeBtn.style.pointerEvents).toBe('auto')
    } finally {
      restore()
    }
  })
})

describe('renderCellExtra chain context', () => {
  it('reports how many single-child levels a collapsed chain cell swallowed', () => {
    const restore = withLayout()
    try {
      const chained: Node = {
        n: 'root',
        size: 300,
        children: [
          { n: 'run', size: 200, children: [{ n: 'checkpoints', size: 200, children: [{ n: 'step-1', size: 100 }, { n: 'step-2', size: 100 }] }] },
          { n: 'bar', size: 100 },
        ],
      }
      const seen: [string, number, number | undefined][] = []
      render(
        <Treemap
          root={chained}
          {...accessors}
          collapseChains
          minCellArea={null}
          renderCellExtra={(n, path, ctx) => { seen.push([n.n, path.length, ctx.chain]); return null }}
        />,
      )
      const top = seen.filter(([, len]) => len <= 3).sort()
      // `run/checkpoints` is one cell: its node is `checkpoints` (path root→run→checkpoints), one level collapsed.
      expect(top).toEqual([['bar', 2, 0], ['checkpoints', 3, 1]])
    } finally {
      restore()
    }
  })
})
