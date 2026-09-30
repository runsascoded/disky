/** The link-card treemap as an SVG string (dynamic OGIs, tier B). Reuses the
 *  live widget's `squarify` layout and `slotColor` palette, so an edge-rendered
 *  card tiles and colors a subtree exactly as the app does. `functions/og`
 *  rasterizes it (resvg-wasm) for sub-paths with no pre-rendered card. Pure and
 *  DOM-free, so it renders identically in a Worker and in Node/CI. */
// Only the pure `squarify` — the barrel (`@rdub/treemap`) pulls the React/DOM
// widget, which won't type-check under the DOM-free cfn tsconfig. `slotColor`
// is mirrored below (parity with the package enforced by `ogSvg.test.ts`).
import { squarify } from '@rdub/treemap/squarify'

/** Mirror of `@rdub/treemap` `colors.DEFAULT_PALETTE` + `slotColor`: the fixed
 *  8-slot categorical palette, then golden-angle hues so a bucket root's many
 *  children stay distinct. Kept byte-identical to the live widget's top-level
 *  coloring (asserted in `ogSvg.test.ts`). */
const PALETTE = [
  'hsl(210 70% 55%)',
  'hsl(30 80% 55%)',
  'hsl(160 55% 45%)',
  'hsl(350 65% 55%)',
  'hsl(280 55% 55%)',
  'hsl(50 75% 55%)',
  'hsl(180 50% 45%)',
  'hsl(120 45% 50%)',
]

function slotColor(i: number): string {
  if (i < PALETTE.length) return PALETTE[i]
  return `hsl(${Math.round((i * 137.508) % 360)} 60% 52%)`
}

export interface OgCell {
  name: string
  size: number
}

export interface OgCardOpts {
  uri: string
  children: OgCell[]
  /** Root bytes for the header; defaults to the sum of `children`. */
  total?: number
  /** Descendant count for the header (omitted if absent). */
  itemCount?: number
  width?: number
  height?: number
}

const K = 1024
const UNITS = ['B', 'K', 'M', 'G', 'T', 'P']

/** Bytes as the UI shows them: base-1024, one decimal under 100, e.g. `916.1 G`. */
export function fmtBytes(n: number): string {
  if (!n || n < 0) return '0 B'
  let v = n
  let i = 0
  while (v >= K && i < UNITS.length - 1) {
    v /= K
    i++
  }
  const s = i === 0 ? String(Math.round(v)) : v.toFixed(1)
  return `${s} ${UNITS[i]}`
}

/** Integers with thousands separators, e.g. `920859` → `920,859`. */
export function fmtCount(n: number): string {
  return n.toLocaleString('en-US')
}

const esc = (s: string): string =>
  s.replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c] as string)

export interface CardCell extends OgCell {
  x: number
  y: number
  w: number
  h: number
  color: string
}

/** Squarified cells for the treemap area `[0, top, width, height-top]`, biggest
 *  first, colored by slot (matching the live top-level coloring). Zero/negative
 *  sizes are dropped (as `squarify` does). */
export function cardCells(children: OgCell[], top: number, width: number, height: number): CardCell[] {
  const items = children.filter(c => c.size > 0).sort((a, b) => b.size - a.size)
  return squarify(items, 0, top, width, height - top, c => c.size).map((r, i) => ({
    ...r.it,
    x: r.x,
    y: r.y,
    w: r.w,
    h: r.h,
    color: slotColor(i),
  }))
}

const n1 = (x: number): string => x.toFixed(1)

/** The full 1200x630 card as an SVG document. Text renders in whatever font the
 *  rasterizer supplies as its default sans (the Worker/CI bundles one). */
export function treemapCardSvg(opts: OgCardOpts): string {
  const W = opts.width ?? 1200
  const H = opts.height ?? 630
  const headerH = 96
  const bg = '#16181d'
  const total = opts.total ?? opts.children.reduce((s, c) => s + Math.max(0, c.size), 0)
  const cells = cardCells(opts.children, headerH, W, H)

  const stats = [fmtBytes(total), opts.itemCount != null ? `${fmtCount(opts.itemCount)} items` : null]
    .filter(Boolean)
    .join('  ·  ')

  const rects = cells
    .map(c => {
      let label = ''
      if (c.w > 82 && c.h > 30) {
        const name = c.name.length > Math.floor((c.w - 20) / 9) ? c.name.slice(0, Math.floor((c.w - 20) / 9) - 1) + '…' : c.name
        label = `<text x="${n1(c.x + 11)}" y="${n1(c.y + 27)}" fill="#ffffff" font-size="18" font-weight="600">${esc(name)}</text>`
        if (c.h > 52) label += `<text x="${n1(c.x + 11)}" y="${n1(c.y + 48)}" fill="#ffffffcc" font-size="15">${esc(fmtBytes(c.size))}</text>`
      }
      return `<rect x="${n1(c.x)}" y="${n1(c.y)}" width="${n1(c.w)}" height="${n1(c.h)}" fill="${c.color}" stroke="${bg}" stroke-width="2"/>${label}`
    })
    .join('')

  return [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">`,
    `<rect width="${W}" height="${H}" fill="${bg}"/>`,
    `<text x="28" y="44" fill="#f2f2f4" font-size="27" font-weight="600">disk-tree</text>`,
    `<text x="${W - 28}" y="44" fill="#9aa0aa" font-size="19" text-anchor="end">${esc(stats)}</text>`,
    `<text x="28" y="78" fill="#c9ccd3" font-size="22">${esc(opts.uri)}</text>`,
    rects,
    `</svg>`,
  ].join('')
}
