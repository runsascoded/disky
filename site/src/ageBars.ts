import { unitTicks } from './series'
import type { ColorMode } from './types'

/** How the age chart's snapshot bars are filled:
 *  - `facet`: stacked strata in the chart's own axis (read states, tree slots, users)
 *  - `gradient`: the date gradient — only when it stands for the map's date lens (the map is
 *    colored `date`, so the bars are that lens's legend and match its cells), or the chart's
 *    "color by" picker chose it explicitly
 *  - `neutral`: one theme color; the date gradient would only re-encode the x-axis. The read
 *    lens is not a reason to tint created-date bars: its gradient spans read days, not these */
export type AgeFill = 'facet' | 'gradient' | 'neutral'

export const ageFill = (mode: ColorMode, mapMode: ColorMode | undefined, picked: boolean): AgeFill =>
  mode !== 'date' ? 'facet' : picked || mapMode === 'date' ? 'gradient' : 'neutral'

/** Approximate advance widths (em) of the byte formatter's glyphs in the UI sans, a little
 *  generous so an estimate that "fits" really does. */
const EM: Record<string, number> = { ' ': 0.3, '.': 0.3, i: 0.28, '−': 0.6, '+': 0.6 }
export const textWidth = (s: string, fs: number): number =>
  [...s].reduce((w, c) => w + (EM[c] ?? (/[0-9]/.test(c) ? 0.58 : 0.66)), 0) * fs

/** Where a bar's value label goes when it fits in the bar's own column: `inside` the bar's top
 *  when the bar is tall enough for it, else just `above`; null when the bar is narrower than the
 *  text (+ `pad` each side). */
export function barLabelPos(barW: number, barH: number, textW: number, textH: number, pad: number): 'inside' | 'above' | null {
  if (textW + 2 * pad > barW) return null
  return barH >= textH + 2 * pad ? 'inside' : 'above'
}

export interface LabelBar {
  /** Column left edge and width (the bar's slot, gaps included), in plot units. */
  x: number
  w: number
  /** Bar height (0..H). */
  h: number
  /** Label text width. */
  textW: number
}

export interface PlacedLabel {
  /** Index into the bars. */
  i: number
  /** Label box: center x, top y (plot coordinates; y may be negative, into the top pad). */
  cx: number
  y: number
  /** `inside`/`above` sit in the bar's column; `peak` overhangs its neighbors, above every bar it spans. */
  pos: 'inside' | 'above' | 'peak'
}

export interface LabelOpts {
  /** Plot width and height (bars stand on y = H). */
  W: number
  H: number
  /** Room above the plot (y down to −padT) for labels over the tallest bars. */
  padT: number
  textH: number
  pad: number
  /** Overhanging labels beyond the max bar's, and the share of the max a bar needs to get one. */
  peaks: number
  peakMin: number
}

/** The value labels for one view's bars:
 *  1. the max bar is always labeled — in its column if it fits there, else overhanging above it;
 *  2. every other bar whose label fits its column (`barLabelPos`) is labeled there;
 *  3. when bars are too narrow for that, up to `peaks` more local peaks (biggest first, ≥ `peakMin`
 *     of the max) get an overhanging label, each only if it sits above every bar it spans.
 *  No two label boxes overlap (a later one is dropped), and every box stays within [0, W] × [−padT, H]. */
export function placeBarLabels(bars: LabelBar[], o: LabelOpts): PlacedLabel[] {
  const { W, H, padT, textH, pad } = o
  if (!bars.length) return []
  type Box = { x0: number; x1: number; y0: number; y1: number }
  const placed: [PlacedLabel, Box][] = []
  const box = (cx: number, y: number, tw: number): Box => ({ x0: cx - tw / 2, x1: cx + tw / 2, y0: y, y1: y + textH })
  const clear = (b: Box) => placed.every(([, p]) => b.x1 + pad <= p.x0 || p.x1 + pad <= b.x0 || b.y1 <= p.y0 || p.y1 <= b.y0)
  const column = (i: number): [PlacedLabel, Box] | null => {
    const { x, w, h, textW } = bars[i]
    const pos = barLabelPos(w, h, textW, textH, pad)
    if (!pos) return null
    const cx = x + w / 2
    const y = pos === 'inside' ? H - h + pad : H - h - pad - textH
    return [{ i, cx, y, pos }, box(cx, y, textW)]
  }
  const peak = (i: number): [PlacedLabel, Box] | null => {
    const { x, w, h, textW } = bars[i]
    const cx = Math.min(Math.max(x + w / 2, textW / 2), W - textW / 2)
    const y = H - h - pad - textH
    if (y < -padT) return null
    const b = box(cx, y, textW)
    // Above every bar the label spans: their tops are below its bottom.
    const under = bars.every(c => c.x + c.w <= b.x0 || b.x1 <= c.x || H - c.h >= b.y1 + pad / 2)
    return under ? [{ i, cx, y, pos: 'peak' }, b] : null
  }
  const order = bars.map((_, i) => i).sort((a, b) => bars[b].h - bars[a].h || a - b)
  const top = order[0]
  const first = column(top) ?? peak(top)
  if (first) placed.push(first)
  for (let i = 0; i < bars.length; i++) {
    if (i === top) continue
    const c = column(i)
    if (c && clear(c[1])) placed.push(c)
  }
  let extra = 0
  for (const i of order) {
    if (extra >= o.peaks || bars[i].h < o.peakMin * bars[top].h) break
    if (i === top || placed.some(([p]) => p.i === i)) continue
    const p = peak(i)
    if (p && clear(p[1])) { placed.push(p); extra++ }
  }
  return placed.map(([p]) => p).sort((a, b) => a.i - b.i)
}

/** A y-axis tick: its value (bytes; signed on a diff) and its y in the chart's `H`-tall plot. */
export interface YTick { v: number; y: number }

/** The y-axis ticks of the bars (`unitTicks` in the viewer's `base`): a snapshot's from 0 up to the tallest bar
 *  (`max`) measured from the floor; a diff's from the biggest loss (`down`) to the biggest gain (`up`) about the
 *  zero line, which sits `up / (up + down)` of the way down. */
export function yTicks(H: number, base: number, scale: { max: number } | { up: number; down: number }): YTick[] {
  if ('max' in scale) return scale.max > 0 ? unitTicks(0, scale.max, base).map(v => ({ v, y: H - (v / scale.max) * H })) : []
  const span = Math.max(scale.up + scale.down, 1)
  const zeroY = (scale.up / span) * H
  return unitTicks(-scale.down, scale.up, base).map(v => ({ v, y: zeroY - (v / span) * H }))
}
