/** The card SVG (specs/done/dogi.md): 1200×630, a header (site, title, subtitle), a
 * treemap of the view, and a footer (totals, and the legend). Pure and
 * DOM-free (`@rdub/treemap/squarify` only), so it runs in the Worker and is
 * tested by its structure.
 *
 * Tiers: an `anon` card draws shapes and colours only. No tile is labelled
 * and the legend carries no names, so nothing on it names a child path or a
 * person (a scraper can't walk the tree card by card). A `full` card labels
 * tiles and names the legend's owners. */
import { squarify } from '@rdub/treemap/squarify'

export const CARD_W = 1200
export const CARD_H = 630

export interface CardTile {
  name: string
  b: number
  color: string
  /** One level of children, drawn inside (unlabelled). */
  kids?: CardTile[]
}

export interface LegendItem { label: string; color: string; b: number }

export interface CardData {
  tier: 'anon' | 'full'
  site: string
  title: string
  /** e.g. `scan 2026-10-02 · filter “tomat”`. */
  subtitle: string
  /** e.g. `3,095 TiB · 562,769,600 objects`. */
  total: string
  tiles: CardTile[]
  /** Owners (or other colour keys) by bytes. Only a `full` card shows labels. */
  legend?: LegendItem[]
  /** Shown instead of a treemap when there's nothing to draw. */
  empty?: string
  /** A heatmap instead of a treemap (`/assignments`): rows × cols, cell
   *  shade by bytes. Labels (people) only on a `full` card. */
  grid?: Grid
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
  const u = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB', 'EiB']
  let i = 0
  let v = n
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++ }
  return i === 0 ? `${v} B` : `${v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2)} ${u[i]}`
}

/** Clip `s` to roughly `px` of width at `size` px Inter (~0.55em per glyph). */
export function clip(s: string, px: number, size: number): string {
  const n = Math.floor(px / (size * 0.56))
  if (n < 2) return ''
  return s.length <= n ? s : `${s.slice(0, Math.max(1, n - 1))}…`
}

/** Black or white text for a hex fill. */
function inkOn(hex: string): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex)
  if (!m) return INK
  const v = parseInt(m[1], 16)
  const [r, g, b] = [(v >> 16) & 255, (v >> 8) & 255, v & 255]
  return 0.299 * r + 0.587 * g + 0.114 * b > 150 ? '#111' : '#fff'
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

/** The card. Tiles are laid out by bytes; children inside each tile get a
 * 4px inset. Labels only on a `full` card, and only where they fit. */
export function cardSvg(d: CardData): string {
  const full = d.tier === 'full'
  const out: string[] = []
  out.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${CARD_W}" height="${CARD_H}" viewBox="0 0 ${CARD_W} ${CARD_H}">`)
  out.push(`<rect width="${CARD_W}" height="${CARD_H}" fill="${BG}"/>`)
  out.push(text(40, 52, clip(d.site, 400, 22), 22, { fill: MUTED, weight: 600 }))
  out.push(text(40, 92, clip(d.title, 1120, 32), 32, { weight: 600 }))
  out.push(text(40, 124, clip(d.subtitle, 1120, 20), 20, { fill: MUTED }))
  const [mx, my, mw, mh] = [40, 144, 1120, 410]
  const tiles = d.tiles.filter(t => t.b > 0)
  if (d.grid?.cells.length) {
    out.push(gridSvg(d.grid, full, mx, my, mw, mh))
  } else if (!tiles.length) {
    out.push(`<rect x="${mx}" y="${my}" width="${mw}" height="${mh}" rx="6" fill="#20232a"/>`)
    out.push(text(mx + mw / 2, my + mh / 2, d.empty ?? 'nothing to show', 26, { fill: MUTED, anchor: 'middle' }))
  } else {
    for (const r of squarify(tiles, mx, my, mw, mh, t => t.b)) {
      const t = r.it
      out.push(`<rect class="tile" x="${r1(r.x)}" y="${r1(r.y)}" width="${r1(r.w)}" height="${r1(r.h)}" fill="${t.color}" stroke="${BG}" stroke-width="2"/>`)
      const inset = 4
      const kids = (t.kids ?? []).filter(k => k.b > 0)
      if (kids.length && r.w > 24 && r.h > 24) {
        const top = full && r.h > 40 ? 22 : inset
        for (const k of squarify(kids, r.x + inset, r.y + top, r.w - 2 * inset, r.h - top - inset, k => k.b)) {
          out.push(`<rect class="kid" x="${r1(k.x)}" y="${r1(k.y)}" width="${r1(k.w)}" height="${r1(k.h)}" fill="${k.it.color}" fill-opacity="0.85" stroke="${BG}" stroke-opacity="0.6" stroke-width="1"/>`)
        }
      }
      if (full && r.w > 70 && r.h > 24) {
        const label = clip(`${t.name} ${fmtB(t.b)}`, r.w - 10, 15)
        if (label) out.push(text(r.x + 6, r.y + 17, label, 15, { fill: inkOn(t.color), weight: 600 }))
      }
    }
  }
  out.push(text(40, 598, d.total, 24, { weight: 600 }))
  if (full && d.legend?.length) {
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
      out.push(`<rect x="${r1(x)}" y="583" width="14" height="14" rx="3" fill="${l.color}"/>`, text(x + 20, 596, label, 16, { fill: MUTED }))
      x += w
    }
  }
  out.push('</svg>')
  return out.join('')
}
