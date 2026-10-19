/** The card SVG (specs/done/dogi.md): 1200×630, a header (site, title, subtitle), a
 * treemap of the view, and a footer (totals, and the legend). Pure and
 * DOM-free (`@open-athena/treemap/squarify` only), so it runs in the Worker and is
 * tested by its structure.
 *
 * Tiers: an `anon` card draws shapes and colours only. No tile is labelled
 * and the legend carries no names, so nothing on it names a child path or a
 * person (a scraper can't walk the tree card by card), at any depth. A `full`
 * card labels tiles at every depth where they fit and names the legend's owners. An explicitly public map may also
 * label its paths without changing its unsigned authentication tier. */
import { squarify } from '@open-athena/treemap/squarify'
import { parseColor } from '@open-athena/treemap/parseColor'
import { unitTicks } from '../../../src/series.js'

export const CARD_W = 1200
export const CARD_H = 630

export interface CardTile {
  name: string
  b: number
  color: string
  /** Displayed value independent of layout weight, e.g. a signed net change. */
  value?: string
  /** Children, drawn nested inside (to any depth), labelled where they fit on a labelled card. */
  kids?: CardTile[]
}

export interface LegendItem { label: string; color: string; b: number }

export interface CardData {
  tier: 'anon' | 'full'
  /** Set by the server only for maps whose base read scope is public. Does
   * not confer a signed/full tier or disclose private grids/owner pages. */
  publicLabels?: boolean
  site: string
  title: string
  /** e.g. `scan 2026-10-02 · filter “tomat”`. */
  subtitle: string
  /** e.g. `3,095 TiB · 562,769,600 objects`. */
  total: string
  tiles: CardTile[]
  /** The view depth the tiles were read to (a map card's `deepestView`): diagnostics only, not drawn. */
  depth?: number
  /** Owners (or other colour keys) by bytes. Only a `full` card shows labels. */
  legend?: LegendItem[]
  /** Shown instead of a treemap when there's nothing to draw. */
  empty?: string
  /** The view couldn't be read (an error, not an answer): drawn, but never cached, so the next fetch retries. */
  failed?: boolean
  /** A heatmap instead of a treemap (`/assignments`): rows × cols, cell
   *  shade by bytes. Labels (people) only on a `full` card. */
  grid?: Grid
  /** A time series instead of a treemap (size over time): bytes per scan. Axis values and the latest point's label
   *  only on a labelled card. */
  chart?: Chart
}

/** A size-over-time chart: `total`'s points `[x (ms), bytes]`, ascending x; with `bands` (the store root's buckets,
 *  bottom to top) each band's `[x, y0, y1]` stacked up to the total, which rides their top edge as a thin line. */
export interface Chart {
  total: [number, number][]
  bands?: { color: string; points: [number, number, number][] }[]
  /** `x` (ms) → the axis label (`Oct 12`). */
  fmtX: (x: number) => string
}

export interface Grid {
  rows: string[]
  cols: string[]
  /** `[row, col, bytes]`, indices into `rows` / `cols`. */
  cells: [number, number, number][]
}

const BG = '#16181d'
const INK = '#f4f4f5'
const MUTED = '#9aa0aa'
const FONT = 'Inter'

export function esc(s: string): string {
  return s.replace(/[<>&"']/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' })[c]!)
}

/** Binary bytes, 3 significant figures: `51.1 TiB`. */
export function fmtB(n: number): string {
  return fmtUnits(n, 1024, ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB'])
}

/** `fmtB` in decimal units (`15.4 TB`): a store billed in TB, as Hugging Face's digest. */
export function fmtSI(n: number): string {
  return fmtUnits(n, 1000, ['B', 'KB', 'MB', 'GB', 'TB', 'PB', 'EB'])
}

function fmtUnits(n: number, base: number, u: string[]): string {
  let i = 0
  let v = n
  while (v >= base && i < u.length - 1) { v /= base; i++ }
  return i === 0 ? `${v} B` : `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${u[i]}`
}

/** Clip `s` to roughly `px` of width at `size` px Inter (~0.55em per glyph). */
export function clip(s: string, px: number, size: number): string {
  const n = Math.floor(px / (size * 0.56))
  if (n < 2) return ''
  return s.length <= n ? s : `${s.slice(0, Math.max(1, n - 1))}…`
}

/** Black or white text for a hex fill. */
function inkOn(color: string): string {
  const rgb = parseColor(color)
  if (!rgb) return INK
  const [r, g, b] = rgb
  return 0.299 * r + 0.587 * g + 0.114 * b > 150 ? '#111' : '#fff'
}

/** resvg's SVG color parser rejects fractional HSL components even though
 * browsers accept them. Use the core's CSS parser at this rendering boundary. */
export function svgColor(color: string): string {
  if (!/^hsla?\(/.test(color)) return color
  const rgba = parseColor(color)
  if (!rgba) throw new Error(`invalid card color: ${color}`)
  const [r, g, b, a] = rgba
  return a === 1 ? `rgb(${r},${g},${b})` : `rgba(${r},${g},${b},${a})`
}

const r1 = (n: number) => Math.round(n * 10) / 10

function text(x: number, y: number, s: string, size: number, opts: { fill?: string; weight?: number; anchor?: string } = {}): string {
  return `<text x="${r1(x)}" y="${r1(y)}" font-family="${FONT}" font-size="${size}" font-weight="${opts.weight ?? 400}" fill="${opts.fill ?? INK}"${opts.anchor ? ` text-anchor="${opts.anchor}"` : ''}>${esc(s)}</text>`
}

/** A heatmap in the box: shade = log bytes over the accent; a `full` card
 * names rows and columns (people), an `anon` one draws the grid alone. */
function gridSvg(g: Grid, full: boolean, x: number, y: number, w: number, h: number): string {
  const head = full ? 150 : 0
  const top = full ? 34 : 0
  const cw = Math.min(90, (w - head) / Math.max(1, g.cols.length))
  const ch = Math.min(44, (h - top) / Math.max(1, g.rows.length))
  const max = Math.max(1, ...g.cells.map(c => c[2]))
  const out: string[] = []
  if (full) {
    g.cols.forEach((c, i) => out.push(text(x + head + i * cw + cw / 2, y + 20, clip(c, cw - 4, 14), 14, { fill: MUTED, anchor: 'middle' })))
    g.rows.forEach((r, i) => out.push(text(x, y + top + i * ch + ch / 2 + 5, clip(r, head - 8, 14), 14, { fill: MUTED })))
  }
  for (let i = 0; i < g.rows.length; i++) for (let j = 0; j < g.cols.length; j++) {
    out.push(`<rect class="cell" x="${r1(x + head + j * cw)}" y="${r1(y + top + i * ch)}" width="${r1(cw - 2)}" height="${r1(ch - 2)}" fill="#20232a"/>`)
  }
  for (const [i, j, b] of g.cells) {
    const t = Math.log10(b + 1) / Math.log10(max + 1)
    out.push(`<rect class="hot" x="${r1(x + head + j * cw)}" y="${r1(y + top + i * ch)}" width="${r1(cw - 2)}" height="${r1(ch - 2)}" fill="#4269d0" fill-opacity="${(0.15 + 0.85 * t).toFixed(2)}"/>`)
    if (full && cw > 56) out.push(text(x + head + j * cw + cw / 2 - 1, y + top + i * ch + ch / 2 + 5, fmtB(b), 13, { anchor: 'middle' }))
  }
  return out.join('')
}

/** The single trace's colour: the page's `--s1` (dark theme). */
export const SERIES_COLOR = '#3987e5'
const GRID_LINE = '#2a2d35'

/** Evenly spaced x ticks over `[x0, x1]`, `n` of them (the ends included). */
const xTicks = (x0: number, x1: number, n: number): number[] => x1 > x0 ? Array.from({ length: n }, (_, i) => x0 + (x1 - x0) * i / (n - 1)) : [x0]

/** Nice byte ticks from 0 to at least `max`: the page's (`unitTicks`, IEC), one more step when the last falls short. */
function yTicksOf(max: number): number[] {
  const t = unitTicks(0, Math.max(max, 1), 1024)
  const step = t.length > 1 ? t[1] - t[0] : Math.max(max, 1)
  return t[t.length - 1] < max ? [...t, t[t.length - 1] + step] : t.length > 1 ? t : [0, step]
}

/** The chart in the box: gridlines at nice byte ticks (labelled on a labelled card), dates along the bottom, then the
 * area (or the stacked bands and the total over them), and the latest point marked (its value labelled when
 * labelled). Pure geometry: the anon tier draws the same shape, no numbers. */
function chartSvg(c: Chart, labels: boolean, x: number, y: number, w: number, h: number): string {
  const out: string[] = []
  const pts = c.total
  const left = labels ? 92 : 0
  const bottom = 30
  const [px, py, pw, ph] = [x + left, y + 6, w - left - 8, h - bottom - 6]
  const x0 = pts[0][0], x1 = pts[pts.length - 1][0]
  const ticks = yTicksOf(Math.max(...pts.map(p => p[1]), ...(c.bands ?? []).flatMap(b => b.points.map(p => p[2]))))
  const yMax = ticks[ticks.length - 1] || 1
  const X = (v: number) => x1 > x0 ? px + (v - x0) / (x1 - x0) * pw : px + pw / 2
  const Y = (v: number) => py + ph - v / yMax * ph
  for (const t of ticks) {
    out.push(`<line class="grid" x1="${r1(px)}" y1="${r1(Y(t))}" x2="${r1(px + pw)}" y2="${r1(Y(t))}" stroke="${GRID_LINE}" stroke-width="1"/>`)
    if (labels) out.push(text(px - 10, Y(t) + 5, t ? fmtB(t) : '0', 15, { fill: MUTED, anchor: 'end' }))
  }
  // Few points: a tick at each; else evenly spaced. A tick whose label repeats the one before it is dropped.
  const xt = (pts.length <= 5 ? pts.map(p => p[0]) : xTicks(x0, x1, 5)).filter((t, i, a) => i === 0 || c.fmtX(t) !== c.fmtX(a[i - 1]))
  xt.forEach((t, i) => out.push(text(X(t), py + ph + 24, c.fmtX(t), 15, { fill: MUTED, anchor: xt.length === 1 ? 'middle' : i === 0 ? 'start' : i === xt.length - 1 && X(t) > px + pw - 40 ? 'end' : 'middle' })))
  const line = (ps: [number, number][]) => ps.map(([a, b], i) => `${i ? 'L' : 'M'}${r1(X(a))},${r1(Y(b))}`).join('')
  if (c.bands?.length) {
    for (const b of c.bands) {
      if (!b.points.length) continue
      const top = b.points.map(([a, , y1]) => [a, y1] as [number, number])
      const base = b.points.map(([a, y0]) => [a, y0] as [number, number]).reverse()
      out.push(`<path class="band" d="${line(top)}${base.map(([a, v]) => `L${r1(X(a))},${r1(Y(v))}`).join('')}Z" fill="${svgColor(b.color)}" fill-opacity="0.85"/>`)
    }
    out.push(`<path class="total" d="${line(pts)}" fill="none" stroke="${INK}" stroke-opacity="0.7" stroke-width="1.5"/>`)
  } else {
    out.push(`<path class="area" d="${line(pts)}L${r1(X(x1))},${r1(Y(0))}L${r1(X(x0))},${r1(Y(0))}Z" fill="${SERIES_COLOR}" fill-opacity="0.25"/>`)
    out.push(`<path class="line" d="${line(pts)}" fill="none" stroke="${SERIES_COLOR}" stroke-width="3" stroke-linejoin="round"/>`)
    if (pts.length <= 60) for (const [a, b] of pts) out.push(`<circle class="pt" cx="${r1(X(a))}" cy="${r1(Y(b))}" r="2.5" fill="${SERIES_COLOR}"/>`)
  }
  const [lx, ly] = pts[pts.length - 1]
  out.push(`<circle class="last" cx="${r1(X(lx))}" cy="${r1(Y(ly))}" r="6" fill="${c.bands?.length ? INK : SERIES_COLOR}" stroke="${BG}" stroke-width="2"/>`)
  if (labels) out.push(text(X(lx) - 12, Y(ly) - 14, fmtB(ly), 20, { weight: 600, anchor: 'end' }))
  return out.join('')
}

/** A level's label size (px): 15 for the view's children, then 13, 12, 11. */
const LABEL_PX = [15, 13, 12, 11]
export const labelPx = (depth: number): number => LABEL_PX[Math.min(depth, LABEL_PX.length) - 1]
/** A level's padding around its children (px). */
const padOf = (depth: number): number => depth === 1 ? 4 : depth === 2 ? 3 : 2
/** A cell is labelled only past this width (px), and only when its label's line fits its height. */
const LABEL_MIN_W = 70
/** A cell's children are drawn only in an inner box at least this big each way (px). */
const NEST_MIN = 10

/** One cell and, nested inside it, its children: the main map's nesting. A labelled cell gets a header
 *  strip (`name size`) with its children below it, where they still fit. `labels` off (the anon
 *  tier) draws the shapes alone at every depth. Children first, so a label sits over them. */
function cellSvg(out: string[], t: CardTile, x: number, y: number, w: number, h: number, depth: number, labels: boolean): void {
  out.push(depth === 1
    ? `<rect class="tile" x="${r1(x)}" y="${r1(y)}" width="${r1(w)}" height="${r1(h)}" fill="${svgColor(t.color)}" stroke="${BG}" stroke-width="2"/>`
    : `<rect class="kid" x="${r1(x)}" y="${r1(y)}" width="${r1(w)}" height="${r1(h)}" fill="${svgColor(t.color)}" fill-opacity="0.85" stroke="${BG}" stroke-opacity="0.6" stroke-width="1"/>`)
  const size = labelPx(depth)
  const head = size + 7
  const pad = padOf(depth)
  const label = labels && w > LABEL_MIN_W ? clip(`${t.name} ${t.value ?? fmtB(t.b)}`, w - 10, size) : ''
  const headed = !!label && h > size + 10
  const top = headed ? head : pad
  const kids = (t.kids ?? []).filter(k => k.b > 0)
  if (kids.length && w - 2 * pad >= NEST_MIN && h - top - pad >= NEST_MIN) {
    for (const k of squarify(kids, x + pad, y + top, w - 2 * pad, h - top - pad, k => k.b)) {
      cellSvg(out, k.it, k.x, k.y, k.w, k.h, depth + 1, labels)
    }
  }
  if (headed) out.push(text(x + 6, y + size + 2, label, size, { fill: inkOn(t.color), weight: 600 }))
}

/** The chart card's legend: every band is drawn, so every band is named, largest first. One right-aligned row of
 * 16px beside the footer (its width measured) when they all fit; else 13px in two rows, the upper across the card's
 * width, the lower beside the footer, with whatever still doesn't fit (the smallest buckets) counted in a closing
 * "+N more". */
function seriesLegend(out: string[], legend: LegendItem[], footer: string): void {
  const room = CARD_W - 40 - (40 + footer.length * 14 + 32)
  const items = legend.map((l, i) => ({ color: l.color, label: `${l.label} ${fmtB(l.b)}`, i }))
  const widthOf = (label: string, cw: number) => 20 + label.length * cw + 18
  const row = (its: { color: string; label: string }[], y: number, size: number, cw: number, more = '') => {
    let x = CARD_W - 40 - its.reduce((a, l) => a + widthOf(l.label, cw), 0) - (more ? more.length * cw + 4 : 0) + 18
    for (const l of its) {
      out.push(`<rect x="${r1(x)}" y="${y + 2 - size}" width="${size - 2}" height="${size - 2}" rx="3" fill="${svgColor(l.color)}"/>`, text(x + size + 4, y, l.label, size, { fill: MUTED }))
      x += widthOf(l.label, cw)
    }
    if (more) out.push(text(x - 18, y, more, size, { fill: MUTED }))
  }
  if (items.reduce((a, l) => a + widthOf(l.label, 9), 0) <= room) return row(items, 596, 16, 9)
  const cw = 7.4
  const upper: typeof items = []
  let w = 0
  for (const l of items) {
    if (w + widthOf(l.label, cw) > CARD_W - 80) break
    upper.push(l)
    w += widthOf(l.label, cw)
  }
  const rest = items.slice(upper.length)
  let n = rest.length
  const more = (k: number) => k < rest.length ? `+${rest.length - k} more` : ''
  while (n > 0 && rest.slice(0, n).reduce((a, l) => a + widthOf(l.label, cw), 0) + (more(n) ? more(n).length * cw + 4 : 0) > room) n--
  row(upper, rest.length ? 573 : 596, 13, cw)
  if (rest.length) row(rest.slice(0, n), 596, 13, cw, more(n))
}

/** The card. Tiles are laid out by bytes, each with its children nested inside (`cellSvg`). Labels only
 * on a `full` card, and only where they fit. */
export function cardSvg(d: CardData): string {
  const full = d.tier === 'full' || d.publicLabels === true
  const out: string[] = []
  out.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${CARD_W}" height="${CARD_H}" viewBox="0 0 ${CARD_W} ${CARD_H}">`)
  out.push(`<rect width="${CARD_W}" height="${CARD_H}" fill="${BG}"/>`)
  const rootTitle = d.site === d.title
  if (!rootTitle) out.push(text(40, 52, clip(d.site, 1120, 22), 22, { fill: MUTED, weight: 600 }))
  out.push(text(40, rootTitle ? 60 : 92, clip(d.title, 1120, 32), 32, { weight: 600 }))
  out.push(text(40, rootTitle ? 92 : 124, clip(d.subtitle, 1120, 20), 20, { fill: MUTED }))
  const [mx, my, mw, mh] = [40, rootTitle ? 112 : 144, 1120, rootTitle ? 442 : 410]
  const tiles = d.tiles.filter(t => t.b > 0)
  if (d.chart?.total.length) {
    out.push(chartSvg(d.chart, full, mx, my, mw, mh))
  } else if (d.grid?.cells.length) {
    out.push(gridSvg(d.grid, full, mx, my, mw, mh))
  } else if (!tiles.length) {
    out.push(`<rect x="${mx}" y="${my}" width="${mw}" height="${mh}" rx="6" fill="#20232a"/>`)
    out.push(text(mx + mw / 2, my + mh / 2, d.empty ?? 'nothing to show', 26, { fill: MUTED, anchor: 'middle' }))
  } else {
    for (const r of squarify(tiles, mx, my, mw, mh, t => t.b)) cellSvg(out, r.it, r.x, r.y, r.w, r.h, 1, full)
  }
  out.push(text(40, 598, d.total, 24, { weight: 600 }))
  if (full && d.legend?.length && d.chart) seriesLegend(out, d.legend, d.total)
  else if (full && d.legend?.length) {
    // Owners, largest first, left to right, right-aligned: as many as fit.
    const fit: { l: LegendItem; label: string; w: number }[] = []
    let width = 0
    for (const l of d.legend) {
      const label = `${l.label} ${fmtB(l.b)}`
      const w = 20 + label.length * 9 + 18
      if (width + w > CARD_W - 40 - 520) break
      fit.push({ l, label, w })
      width += w
    }
    let x = CARD_W - 40 - width + 18
    for (const { l, label, w } of fit) {
      out.push(`<rect x="${r1(x)}" y="583" width="14" height="14" rx="3" fill="${svgColor(l.color)}"/>`, text(x + 20, 596, label, 16, { fill: MUTED }))
      x += w
    }
  }
  out.push('</svg>')
  return out.join('')
}
