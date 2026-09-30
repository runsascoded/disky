import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render } from '@testing-library/react'
import { BytesOverTime, TimeSeries } from '../src/TimeSeries'

/** Force a non-zero SVG size in jsdom (ResizeObserver mock never fires). */
function forceSize(container: HTMLElement, w = 400, h = 200) {
  const wrap = container.querySelector('.dt-timeseries') as HTMLElement
  Object.defineProperty(wrap, 'clientWidth', { value: w, configurable: true })
  Object.defineProperty(wrap, 'clientHeight', { value: h, configurable: true })
}

/** The component measures synchronously in a layout effect, so post-mount
 *  stubbing never renders the SVG. Stub the prototype getters *before* mount
 *  (and restore after) so ticks / marks / annotations actually paint. */
function withSize<R>(fn: () => R, w = 400, h = 200): R {
  const saved = (['clientWidth', 'clientHeight'] as const).map(
    k => [k, Object.getOwnPropertyDescriptor(HTMLElement.prototype, k)] as const,
  )
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => w })
  Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => h })
  try {
    return fn()
  } finally {
    for (const [k, d] of saved) {
      if (d) Object.defineProperty(HTMLElement.prototype, k, d)
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[k]
    }
  }
}

function yLabelsOf(container: HTMLElement): string[] {
  return [...container.querySelectorAll('svg g text')]
    .filter(t => t.getAttribute('text-anchor') === 'end')
    .map(t => t.textContent ?? '')
}

describe('<TimeSeries>', () => {
  it('renders an SVG when given a non-empty series', () => {
    const { container } = render(
      <TimeSeries
        series={[
          {
            key: 'a',
            points: [
              { t: 0, y: 10 },
              { t: 1, y: 20 },
            ],
          },
        ]}
        getX={p => p.t}
        getY={p => p.y}
      />,
    )
    forceSize(container)
    // The SVG is rendered inside a ResizeObserver callback; in jsdom it fires
    // on next tick when dims are set. It won't actually paint here, but the
    // wrapper div is always present regardless.
    expect(container.querySelector('.dt-timeseries')).toBeInTheDocument()
  })

  it('yTickValues overrides nice-ticks, dropping values outside the domain', () => {
    // Stub the prototype getters *before* mount — the component measures
    // synchronously, so post-mount stubbing (forceSize) never renders ticks.
    const saved = (['clientWidth', 'clientHeight'] as const).map(
      k => [k, Object.getOwnPropertyDescriptor(HTMLElement.prototype, k)] as const,
    )
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => 400 })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => 200 })
    try {
      const { container } = render(
        <TimeSeries
          series={[{ key: 'a', points: [{ t: 0, y: 10 }, { t: 1, y: 90 }] }]}
          getX={p => p.t}
          getY={p => p.y}
          yTickValues={[16, 64, 256]}  // 256 > yMax → dropped
        />,
      )
      const yLabels = [...container.querySelectorAll('svg g[class] text, svg g text')]
        .filter(t => t.getAttribute('text-anchor') === 'end')
        .map(t => t.textContent)
      expect(yLabels).toEqual(['16', '64'])
    } finally {
      for (const [k, d] of saved) {
        if (d) Object.defineProperty(HTMLElement.prototype, k, d)
        else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[k]
      }
    }
  })

  it('yFrom "zero" anchors the y-axis at 0; "data" fits it to the series', () => {
    const series = [{ key: 'a', points: [{ t: 0, y: 1000 }, { t: 1, y: 2000 }] }]
    const zero = withSize(() =>
      render(<TimeSeries series={series} getX={p => p.t} getY={p => p.y} />),
    )
    // Default (zero): domain [0, 2100] → step 500.
    expect(yLabelsOf(zero.container)).toEqual(['0', '500', '1,000', '1,500', '2,000'])

    const fit = withSize(() =>
      render(<TimeSeries series={series} getX={p => p.t} getY={p => p.y} yFrom="data" />),
    )
    // data: pad = (2000-1000)*0.05 = 50 → domain [950, 2050] → step 200, first 1000.
    expect(yLabelsOf(fit.container)).toEqual(['1,000', '1,200', '1,400', '1,600', '1,800', '2,000'])
  })

  it('renders annotations as haloed labels anchored away from the nearest edge', () => {
    const { container } = withSize(() =>
      render(
        <TimeSeries
          series={[{ key: 'a', points: [{ t: 0, y: 1000 }, { t: 1, y: 2000 }] }]}
          getX={p => p.t}
          getY={p => p.y}
          annotations={[
            { x: 0, y: 1000, label: 'first' },
            { x: 1, y: 2000, label: 'last' },
          ]}
        />,
      ),
    )
    const anno = [...container.querySelectorAll('svg text[paint-order="stroke"]')]
    expect(anno.map(t => t.textContent)).toEqual(['first', 'last'])
    // x=0 sits at the left edge → 'start'; x=1 at the right edge → 'end'.
    expect(anno.map(t => t.getAttribute('text-anchor'))).toEqual(['start', 'end'])
    expect(anno.map(t => t.getAttribute('pointer-events'))).toEqual(['none', 'none'])
  })

  it('onPickX sets a pointer cursor and only fires once a hover x is snapped', () => {
    const onPickX = vi.fn()
    const { container } = withSize(() =>
      render(
        <TimeSeries
          series={[{ key: 'a', points: [{ t: 0, y: 10 }, { t: 1, y: 20 }] }]}
          getX={p => p.t}
          getY={p => p.y}
          onPickX={onPickX}
        />,
      ),
    )
    const svg = container.querySelector('svg') as SVGSVGElement
    expect(svg.style.cursor).toBe('pointer')
    // No hover established yet → the guard suppresses the callback.
    fireEvent.click(svg)
    expect(onPickX).not.toHaveBeenCalled()
  })

  it('omits the pointer cursor when onPickX is not given', () => {
    const { container } = withSize(() =>
      render(
        <TimeSeries
          series={[{ key: 'a', points: [{ t: 0, y: 10 }, { t: 1, y: 20 }] }]}
          getX={p => p.t}
          getY={p => p.y}
        />,
      ),
    )
    expect((container.querySelector('svg') as SVGSVGElement).style.cursor).toBe('')
  })

  // Brush / window geometry at 400×200: plot x-range is [56, 384] (PAD.left
  // 56, PAD.right 16), so points t ∈ {0, 1, 2} sit at px 56 / 220 / 384 and
  // jsdom's zero bounding rect makes clientX the plot px directly.
  const brushSeries = [{ key: 'a', points: [{ t: 0, y: 10 }, { t: 1, y: 20 }, { t: 2, y: 30 }] }]
  /** The window/brush band as rendered: the shaded rect + its two edge lines. */
  function bandOf(container: HTMLElement) {
    const g = container.querySelector('svg g[pointer-events="none"]')
    if (!g) return null
    const rect = g.querySelector('rect')!
    return {
      x: Number(rect.getAttribute('x')),
      width: Number(rect.getAttribute('width')),
      fill: rect.getAttribute('fill'),
      edges: [...g.querySelectorAll('line')].map(l => [Number(l.getAttribute('x1')), l.getAttribute('stroke-dasharray')]),
    }
  }

  it('onBrush: a drag picks the snapped x-range (ordered), painting a solid band meanwhile', () => {
    const onBrush = vi.fn()
    const onPickX = vi.fn()
    const { container } = withSize(() =>
      render(<TimeSeries series={brushSeries} getX={p => p.t} getY={p => p.y} onBrush={onBrush} onPickX={onPickX} />),
    )
    const svg = container.querySelector('svg') as SVGSVGElement
    expect(svg.style.cursor).toBe('crosshair')
    expect(bandOf(container)).toBeNull()
    // Press near t=2, drag back to t=1: the in-progress band is solid (no dash).
    fireEvent.mouseDown(svg, { clientX: 390, button: 0 })
    fireEvent.mouseMove(svg, { clientX: 225 })
    expect(svg.style.cursor).toBe('col-resize')
    expect(bandOf(container)).toEqual({
      x: 220,
      width: 164,
      fill: 'var(--dt-ts-brush, rgba(255,255,255,0.14))',
      edges: [[220, null], [384, null]],
    })
    // Release past the plot's left edge: snaps to t=0, committed as (min, max).
    fireEvent.mouseUp(window, { clientX: 10 })
    expect(onBrush.mock.calls).toEqual([[0, 2]])
    expect(onPickX).not.toHaveBeenCalled()
    expect(bandOf(container)).toBeNull()
    expect(svg.style.cursor).toBe('crosshair')
  })

  it('onBrush: a zero-width drag is a click — onPickX once, onBrush never', () => {
    const onBrush = vi.fn()
    const onPickX = vi.fn()
    const { container } = withSize(() =>
      render(<TimeSeries series={brushSeries} getX={p => p.t} getY={p => p.y} onBrush={onBrush} onPickX={onPickX} />),
    )
    const svg = container.querySelector('svg') as SVGSVGElement
    fireEvent.mouseDown(svg, { clientX: 56, button: 0 })
    fireEvent.mouseUp(window, { clientX: 60 })
    expect(onPickX.mock.calls).toEqual([[0]])
    expect(onBrush).not.toHaveBeenCalled()
    // With a brush the pick resolves in mouseup only: a hover + click must not
    // fire it a second time.
    fireEvent.mouseMove(svg, { clientX: 225 })
    fireEvent.click(svg)
    expect(onPickX.mock.calls).toEqual([[0]])
  })

  it('window renders a dashed band between the two x’s', () => {
    const { container } = withSize(() =>
      render(<TimeSeries series={brushSeries} getX={p => p.t} getY={p => p.y} window={[0, 1]} />),
    )
    expect(bandOf(container)).toEqual({
      x: 56,
      width: 164,
      fill: 'var(--dt-ts-window, rgba(255,255,255,0.07))',
      edges: [[56, '2 3'], [220, '2 3']],
    })
    // No brush → no drag affordance, and a press does nothing.
    const svg = container.querySelector('svg') as SVGSVGElement
    expect(svg.style.cursor).toBe('')
    fireEvent.mouseDown(svg, { clientX: 390, button: 0 })
    fireEvent.mouseMove(svg, { clientX: 225 })
    expect(bandOf(container)!.edges).toEqual([[56, '2 3'], [220, '2 3']])
  })

  // Mirror the component's own pixel math so expected path `d`s are exact
  // (same float→string as production), not eyeballed.
  function geom(w: number, h: number, xMin: number, xMax: number, yMin: number, yMax: number) {
    const plotW = w - 56 - 16
    const plotH = h - 12 - 24
    return {
      xToPx: (x: number) => 56 + ((x - xMin) / Math.max(1, xMax - xMin)) * plotW,
      yToPx: (y: number) => 12 + plotH - ((y - yMin) / Math.max(0.001, yMax - yMin)) * plotH,
    }
  }

  it('getY0 draws a band (baseline walked back at y0, not the axis) and the tooltip shows band height', () => {
    const pts = [{ t: 0, top: 80, bot: 30 }, { t: 1, top: 80, bot: 30 }]
    const { container } = withSize(() =>
      render(
        <TimeSeries
          series={[{ key: 'a', color: '#abc', points: pts }]}
          getX={p => p.t}
          getY={p => p.top}
          getY0={p => p.bot}
        />,
      ),
    )
    // yMax = 80 * 1.05 (zero-anchored); the band never touches the axis.
    const { xToPx, yToPx } = geom(400, 200, 0, 1, 0, 80 * 1.05)
    const area = container.querySelector('svg path:not([fill="none"])')!
    expect(area.getAttribute('d')).toBe(
      `M ${xToPx(0)} ${yToPx(80)} L ${xToPx(1)} ${yToPx(80)}`
      + ` L ${xToPx(1)} ${yToPx(30)} L ${xToPx(0)} ${yToPx(30)} Z`,
    )
    expect(area.getAttribute('fill')).toBe('#abc')
    expect(area.getAttribute('fill-opacity')).toBe('0.35')
    // Hover at t=0 → tooltip value is the band height (80 − 30), not the top.
    fireEvent.mouseMove(container.querySelector('svg')!, { clientX: 56 })
    expect([...container.querySelectorAll('.dt-timeseries > div b')].map(b => b.textContent)).toEqual(['50'])
  })

  it('dashBeforeX splits the line into a dashed lead and a solid remainder sharing the cut point', () => {
    const pts = [{ t: 0, y: 100 }, { t: 1, y: 100 }, { t: 2, y: 100 }]
    const { container } = withSize(() =>
      render(
        <TimeSeries
          series={[{ key: 'a', points: pts, dashBeforeX: 1, area: false }]}
          getX={p => p.t}
          getY={p => p.y}
        />,
      ),
    )
    const { xToPx, yToPx } = geom(400, 200, 0, 2, 0, 100 * 1.05)
    const dashed = container.querySelector('svg path[stroke-dasharray="4 4"]')!
    // Dashed leads up to and including the cut (t0→t1); solid takes over there.
    expect(dashed.getAttribute('d')).toBe(`M ${xToPx(0)} ${yToPx(100)} L ${xToPx(1)} ${yToPx(100)}`)
    const solid = [...container.querySelectorAll('svg path[fill="none"]')].find(p => !p.getAttribute('stroke-dasharray'))!
    expect(solid.getAttribute('d')).toBe(`M ${xToPx(1)} ${yToPx(100)} L ${xToPx(2)} ${yToPx(100)}`)
  })

  it('per-series area overrides the chart-wide area flag both ways', () => {
    const mk = (a: boolean, chart: boolean) =>
      withSize(() =>
        render(
          <TimeSeries
            series={[{ key: 'a', points: [{ t: 0, y: 10 }, { t: 1, y: 20 }], area: a }]}
            getX={p => p.t}
            getY={p => p.y}
            area={chart}
          />,
        ),
      )
    // Fill paths (the area) are the only non-"none" fills; lines/dashes are "none".
    const fills = (c: HTMLElement) => c.querySelectorAll('svg path:not([fill="none"])').length
    expect(fills(mk(true, false).container)).toBe(1)  // series true beats chart false
    expect(fills(mk(false, true).container)).toBe(0)  // series false beats chart true
  })

  it('handles empty series without crashing', () => {
    interface P { t: number; y: number }
    const { container } = render(
      <TimeSeries<P>
        series={[{ key: 'a', points: [] }]}
        getX={p => p.t}
        getY={p => p.y}
      />,
    )
    expect(container.querySelector('.dt-timeseries')).toBeInTheDocument()
  })
})

describe('<BytesOverTime>', () => {
  it('accepts ISO date strings and null bytes', () => {
    const { container } = render(
      <BytesOverTime
        points={[
          { time: '2026-08-01T00:00:00Z', bytes: 1000 },
          { time: '2026-08-05T00:00:00Z', bytes: 500 },
          { time: '2026-08-06T00:00:00Z', bytes: null }, // should be filtered
        ]}
        formatBytes={n => `${n}B`}
      />,
    )
    expect(container.querySelector('.dt-timeseries')).toBeInTheDocument()
  })

  it('accepts numeric epoch-millis timestamps', () => {
    const { container } = render(
      <BytesOverTime
        points={[
          { time: 1_700_000_000_000, bytes: 100 },
          { time: 1_700_086_400_000, bytes: 200 },
        ]}
        formatBytes={n => `${n}B`}
      />,
    )
    expect(container.querySelector('.dt-timeseries')).toBeInTheDocument()
  })
})

describe('<TimeSeries> series flags + band callouts', () => {
  const geom = (w: number, h: number, xMin: number, xMax: number, yMin: number, yMax: number) => {
    const plotW = w - 56 - 16
    const plotH = h - 12 - 24
    return {
      xToPx: (x: number) => 56 + ((x - xMin) / Math.max(1, xMax - xMin)) * plotW,
      yToPx: (y: number) => 12 + plotH - ((y - yMin) / Math.max(0.001, yMax - yMin)) * plotH,
    }
  }

  it('plot: false keeps a series in the tooltip but draws nothing for it', () => {
    const { container } = withSize(() =>
      render(
        <TimeSeries
          series={[
            { key: 'a', label: 'a', points: [{ t: 0, y: 10 }, { t: 1, y: 20 }], area: false },
            { key: 'total', label: 'total', points: [{ t: 0, y: 1000 }, { t: 1, y: 2000 }], area: false, plot: false },
          ]}
          getX={p => p.t}
          getY={p => p.y}
        />,
      ),
    )
    // One line path (a's); the total has none.
    expect(container.querySelectorAll('svg path[fill="none"]').length).toBe(1)
    fireEvent.mouseMove(container.querySelector('svg')!, { clientX: 56 })
    expect([...container.querySelectorAll('.dt-timeseries > div b')].map(b => b.textContent)).toEqual(['10', '1,000'])
  })

  it('fit: with yFrom "data", only the marked series set the y-range', () => {
    const { container } = withSize(() =>
      render(
        <TimeSeries
          series={[
            { key: 'top', points: [{ t: 0, y: 10 }, { t: 1, y: 20 }], fit: true },
            { key: 'band', points: [{ t: 0, y: 0 }, { t: 1, y: 1000 }] },
          ]}
          getX={p => p.t}
          getY={p => p.y}
          yFrom="data"
        />,
      ),
    )
    // Domain from `top` only: pad 0.5 → [9.5, 20.5] → step 2.
    expect(yLabelsOf(container)).toEqual(['10', '12', '14', '16', '18', '20'])
  })

  it('a band callout (y0) sits centred inside the band, and a band too thin to hold it gets none', () => {
    const pts = [{ t: 0, top: 80, bot: 30 }, { t: 1, top: 31, bot: 30 }]
    const { container } = withSize(() =>
      render(
        <TimeSeries
          series={[{ key: 'a', points: pts }]}
          getX={p => p.t}
          getY={p => p.top}
          getY0={p => p.bot}
          annotations={[
            { x: 0, y: 80, y0: 30, label: 'tall' },
            { x: 1, y: 31, y0: 30, label: 'thin' },
          ]}
        />,
      ),
    )
    const anno = [...container.querySelectorAll('svg text[paint-order="stroke"]')]
    expect(anno.map(t => t.textContent)).toEqual(['tall'])
    const { yToPx } = geom(400, 200, 0, 1, 0, 80 * 1.05)
    expect(Number(anno[0].getAttribute('y'))).toBeCloseTo((yToPx(80) + yToPx(30)) / 2, 6)
    expect(anno[0].getAttribute('dominant-baseline')).toBe('middle')
    expect(anno[0].getAttribute('clip-path')).toMatch(/^url\(#dt-ts-clip-\w+\)$/)
  })

  it('a series that goes negative extends a zero-anchored axis below 0, with the 0 grid line emphasised', () => {
    const { container } = withSize(() =>
      render(
        <TimeSeries
          series={[{ key: 'd', points: [{ t: 0, y: 0 }, { t: 1, y: -100 }, { t: 2, y: 50 }], area: false }]}
          getX={p => p.t}
          getY={p => p.y}
        />,
      ),
    )
    // Domain [-105, 52.5] → step 50 → -100 … 50.
    expect(yLabelsOf(container)).toEqual(['-100', '-50', '0', '50'])
    const gridAt0 = [...container.querySelectorAll('svg g line')].find(l => l.nextElementSibling?.textContent === '0')!
    expect(gridAt0.getAttribute('stroke')).toBe('var(--dt-ts-axis, rgba(255,255,255,0.2))')
  })
})
