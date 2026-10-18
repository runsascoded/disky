/** The monthly digest's image (the cw Slack thread's OP): the month's usage
 * line against its quota, over the diff map of the headline interval. Drawn
 * here, not by the job, so the map is the site's own: the same diff tree
 * (`diffRoot`), cell colours (`diffCellStyle`) and treemap layout + paint
 * (`@rdub/treemap/svg`) as the in-app diff, in the site's dark theme and
 * Inter. Pure and DOM-free; `api/digest-image.ts` reads the diff and
 * rasterizes it. */
import { staticLayout, treemapSvg } from '@rdub/treemap/svg'
import { diffBorderWidth, diffCellStyle } from '../../../src/diffStyle.js'
import type { DiffNode } from '../../../src/diffModel.js'
import { esc, fmtB, fmtSI } from './card.js'
import { interWidth } from './interMetrics.js'

export const DIGEST_W = 900
const PAD = 24
const CHART_H = 250
const MAP_H = 440

// The site's dark theme (app.scss `:root[data-theme='dark']`).
const BG = '#1a1a19'
const PANEL = '#232322'
const INK = '#ffffff'
const INK2 = '#c3c2b7'
const INK3 = '#aaa89c'
const LINE = '#33332f'
const S1 = '#3987e5'
const DANGER = '#f2635a'
const GREW = '#4ade80'
const VARS = { '--s1': S1, '--panel': PANEL, '--ink': INK, '--dt-treemap-ink': '#d0d0d8', '--dt-treemap-container-bg': PANEL }
/** The image's size unit: bytes per unit and the byte formatter (labels, call-out, map cells). */
const UNITS = { TiB: { bytes: 1024 ** 4, fmt: fmtB }, TB: { bytes: 1e12, fmt: fmtSI } } as const
export type DigestUnits = keyof typeof UNITS

export interface DigestPoint {
  /** Epoch ms of the scan. */
  t: number
  bytes: number
}

export interface DigestCardInput {
  /** e.g. `CoreWeave usage — October 2026`. */
  title: string
  /** Header's right side, e.g. `cw-s3.oa.dev · marin-us-east-02a · TiB`. */
  note: string
  points: DigestPoint[]
  /** The x axis' span, epoch ms (the month). */
  from: number
  to: number
  quota?: { bytes: number; name: string } | null
  diff?: { root: DiffNode; title: string } | null
  /** The y axis' unit and every size label's (default `TiB`). */
  units?: DigestUnits
}

const r1 = (n: number) => Math.round(n * 10) / 10

function text(x: number, y: number, s: string, px: number, o: { fill?: string; weight?: 400 | 600; anchor?: 'start' | 'middle' | 'end' } = {}): string {
  return `<text x="${r1(x)}" y="${r1(y)}" font-size="${px}" font-weight="${o.weight ?? 400}" fill="${o.fill ?? INK}"${o.anchor && o.anchor !== 'start' ? ` text-anchor="${o.anchor}"` : ''}>${esc(s)}</text>`
}

/** ~`n` round ticks over `[lo, hi]`: steps of 1, 2, 2.5 or 5 × 10^k. */
export function niceTicks(lo: number, hi: number, n = 6): number[] {
  const raw = (hi - lo) / n
  const mag = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw)!
  const out: number[] = []
  // `|| 0`: a tick at a lo just under zero is -0, which would label `-0`
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Math.round(v * 1e6) / 1e6 || 0)
  return out
}

const fmtSigned = (n: number, fmt: (n: number) => string) => `${n < 0 ? '−' : '+'}${fmt(Math.abs(n))}`
const md = (t: number) => { const d = new Date(t); return `${d.getUTCMonth() + 1}/${d.getUTCDate()}` }

/** The chart's y range over `vals` (and the quota `q`, when drawn): the data with a margin, never narrower than
 *  `MIN_Y_SPAN` of its top value, centred on the data — a change of a few hundred MiB on hundreds of TiB is not
 *  drawn as a full-height cliff. */
export const MIN_Y_SPAN = 0.01
export function yRange(vals: number[], q: number | null): [number, number] {
  let lo = Math.min(...vals)
  let hi = Math.max(...vals, ...(q != null ? [q] : []))
  const min = hi * MIN_Y_SPAN
  if (hi - lo < min) {
    const mid = (Math.min(...vals) + Math.max(...vals)) / 2
    lo = Math.min(lo, mid - min / 2)
    hi = Math.max(hi, mid + min / 2)
  }
  const span = hi - lo || 1
  return [lo - span * 0.08, hi + span * 0.06]
}

/** The x frame shared by a card's panels: the plot's left edge (after the y labels), its width, and time → x. */
interface Frame { left: number; pw: number; X: (t: number) => number }
function frame(d: { from: number; to: number }, x0: number, w: number): Frame {
  const left = x0 + 44
  const pw = x0 + w - left
  return { left, pw, X: (t: number) => left + ((t - d.from) / (d.to - d.from)) * pw }
}

/** Weekly ticks below `yb`: every Monday in the span, labelled `M/D` unless `labels` is false. */
function mondays(d: { from: number; to: number }, X: (t: number) => number, yb: number, labels = true): string[] {
  const out: string[] = []
  const day = 86400_000
  for (let t = Math.ceil(d.from / day) * day; t <= d.to; t += day) {
    if (new Date(t).getUTCDay() !== 1) continue
    out.push(`<line x1="${r1(X(t))}" x2="${r1(X(t))}" y1="${r1(yb)}" y2="${r1(yb + 4)}" stroke="${INK3}"/>`)
    if (labels) out.push(text(X(t), yb + 17, md(t), 11, { fill: INK3, anchor: 'middle' }))
  }
  return out
}

/** The usage line over `d.points` (with `d.quota`'s line and headroom, when given): y ticks, Monday ticks, the
 *  month-start reference, and the latest point's call-out (`o.call`, default the unit's byte formatter). */
function chartSvg(d: DigestCardInput, x0: number, y0: number, w: number, h: number, o: { xLabels?: boolean; call?: (bytes: number) => string } = {}): string {
  const out: string[] = []
  const pts = d.points
  const u = UNITS[d.units ?? 'TiB']
  const tib = (b: number) => b / u.bytes
  const vals = pts.map(p => tib(p.bytes))
  const q = d.quota ? tib(d.quota.bytes) : null
  let [lo, hi] = yRange(vals, q)
  const ticks = niceTicks(lo, hi)
  lo = Math.min(lo, ticks[0])
  const { left, pw, X } = frame(d, x0, w)
  const Y = (v: number) => y0 + h - ((v - lo) / (hi - lo)) * h
  out.push(`<rect x="${r1(left)}" y="${r1(y0)}" width="${r1(pw)}" height="${r1(h)}" fill="${PANEL}"/>`)
  for (const v of ticks) {
    if (v < lo || v > hi) continue
    out.push(`<line x1="${r1(left)}" x2="${r1(left + pw)}" y1="${r1(Y(v))}" y2="${r1(Y(v))}" stroke="${LINE}" stroke-width="1"/>`)
    out.push(text(left - 6, Y(v) + 4, v.toLocaleString('en-US'), 11, { fill: INK3, anchor: 'end' }))
  }
  out.push(...mondays(d, X, y0 + h, o.xLabels ?? true))
  if (!pts.length) return out.join('')
  const line = pts.map(p => `${r1(X(p.t))},${r1(Y(tib(p.bytes)))}`).join(' ')
  const first = pts[0]
  const last = pts[pts.length - 1]
  // Headroom: hatched between the usage line and the quota.
  if (q != null) {
    out.push(`<defs><pattern id="hatch" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)"><line x1="0" y1="0" x2="0" y2="7" stroke="${GREW}" stroke-opacity="0.22" stroke-width="2"/></pattern></defs>`)
    out.push(`<polygon points="${r1(X(first.t))},${r1(Y(q))} ${line} ${r1(X(last.t))},${r1(Y(q))}" fill="url(#hatch)"/>`)
  }
  out.push(`<polygon points="${r1(X(first.t))},${r1(y0 + h)} ${line} ${r1(X(last.t))},${r1(y0 + h)}" fill="${S1}" fill-opacity="0.18"/>`)
  out.push(`<line x1="${r1(left)}" x2="${r1(left + pw)}" y1="${r1(Y(tib(first.bytes)))}" y2="${r1(Y(tib(first.bytes)))}" stroke="${INK3}" stroke-opacity="0.6" stroke-dasharray="4 4"/>`)
  if (q != null) {
    out.push(`<line x1="${r1(left)}" x2="${r1(left + pw)}" y1="${r1(Y(q))}" y2="${r1(Y(q))}" stroke="${DANGER}" stroke-width="2"/>`)
    out.push(text(left + pw - 4, Y(q) - 6, `${d.quota!.name} quota`, 12, { fill: DANGER, weight: 600, anchor: 'end' }))
  }
  out.push(`<polyline points="${line}" fill="none" stroke="${S1}" stroke-width="2.2" stroke-linejoin="round"/>`)
  const lx = X(last.t)
  const ly = Y(tib(last.bytes))
  out.push(`<circle cx="${r1(lx)}" cy="${r1(ly)}" r="4" fill="${S1}"/>`)
  const call = `${(o.call ?? u.fmt)(last.bytes)}${q != null ? ` · ${Math.round((100 * tib(last.bytes)) / q)}%` : ''}`
  const cw = interWidth(call, 15, 600)
  const right = lx + 10 + cw <= left + pw
  out.push(text(right ? lx + 10 : lx - 10, ly + 5, call, 15, { weight: 600, anchor: right ? 'start' : 'end' }))
  return out.join('')
}

function leaves(n: DiffNode, out: DiffNode[] = []): DiffNode[] {
  if (!n.children?.length) out.push(n)
  else for (const k of n.children) leaves(k, out)
  return out
}

/** The digest image's SVG, `DIGEST_W` wide (its height in the `height` attribute). */
export function digestSvg(d: DigestCardInput): string {
  const w = DIGEST_W - 2 * PAD
  const { fmt } = UNITS[d.units ?? 'TiB']
  const out = [header(d.title, d.note)]
  let y = PAD + 36
  out.push(chartSvg(d, PAD, y, w, CHART_H))
  y += CHART_H + 30
  if (d.diff) y = diffSection(d.diff, fmt, y, out)
  return page(y, out)
}

/** The headline interval's diff map under a card's charts, from `y`: its title and grew/shrank totals, then the
 *  treemap (the in-app diff's tree, `MAP_H` tall). Returns the y below it. */
function diffSection({ root, title }: { root: DiffNode; title: string }, fmt: (b: number) => string, y: number, out: string[]): number {
  const w = DIGEST_W - 2 * PAD
  const ls = leaves(root)
  const grew = ls.reduce((s, n) => s + n.added, 0)
  const shrank = ls.reduce((s, n) => s + n.removed, 0)
  y += 16
  out.push(text(PAD, y + 4, title, 17, { weight: 600 }))
  out.push(text(DIGEST_W - PAD, y + 4, `grew ${fmtSigned(grew, fmt)} · shrank ${fmtSigned(-shrank, fmt)} · area = |Δ|`, 12, { fill: INK3, anchor: 'end' }))
  y += 14
  const label = (n: DiffNode) => (n.first ? `${n.label} (first scanned)` : n.label)
  const cells = staticLayout(root, {
    width: w, height: MAP_H,
    getSize: n => n.weight, getLabel: label, getChildren: n => n.children,
    collapseChains: true, tiling: 'shared', borderWidth: diffBorderWidth,
  })
  out.push(`<g transform="translate(${PAD},${y})">`)
  out.push(treemapSvg(cells, {
    width: w, height: MAP_H,
    getSize: n => n.weight, getLabel: label, formatSize: fmt, formatCell: n => fmtSigned(n.delta, fmt),
    colorForCell: n => diffCellStyle(n, 'delta'),
    measure: (s, px) => interWidth(s, px), vars: VARS, fontFamily: 'Inter',
    labelPx: 11.5, labelPxSm: 9.5, depthFade: 1, rootFade: 1,
  }))
  out.push('</g>')
  return y + MAP_H
}

/** The card's `<svg>`: `DIGEST_W` wide, its content ending at `y` (+ the bottom pad), on the theme's background. */
function page(y: number, out: string[]): string {
  const H = Math.ceil(y + PAD)
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${DIGEST_W}" height="${H}" viewBox="0 0 ${DIGEST_W} ${H}" font-family="Inter"><rect width="${DIGEST_W}" height="${H}" fill="${BG}"/>${out.join('')}</svg>`
}

/** The card's header: the title, and the note right-aligned. */
function header(title: string, note: string): string {
  return text(PAD, PAD + 18, title, 20, { weight: 600 }) + text(DIGEST_W - PAD, PAD + 18, note, 12, { fill: INK3, anchor: 'end' })
}

// ---- the storage-class card -------------------------------------------------------------------------------------

const TOTAL_H = 150
const STACK_H = 250
/** The tiers' default colours, bottom → top (cold → hot: a stable archive tier at the bottom, the active one on top,
 *  so the total's wiggle lives at the stack's readable top edge). */
export const TIER_COLORS = ['#8a897f', S1, '#e3b341', DANGER, '#a78bfa', '#2dd4bf']

export interface TierPoint {
  /** Epoch ms of the scan. */
  t: number
  /** Bytes per tier, in `TiersCardInput.tiers` order. */
  bytes: number[]
}

export interface TiersCardInput {
  /** e.g. `marin GCS — October 2026`. */
  title: string
  /** Header's right side, e.g. `gcs.oa.dev · TiB`. */
  note: string
  /** The stack's tiers, bottom → top. */
  tiers: { name: string; color: string }[]
  points: TierPoint[]
  /** The x axis' span, epoch ms (the month). */
  from: number
  to: number
  /** The y axes' unit and every size label's (default `TiB`). */
  units?: DigestUnits
  /** The headline interval's diff map, under the legend (as `DigestCardInput.diff`). */
  diff?: { root: DiffNode; title: string } | null
}

/** Bytes in a fixed unit, as the y axes read: `3,030 TiB`, `15.4 TB` (never rescaled to the next unit up). */
export function fmtIn(units: DigestUnits): (bytes: number) => string {
  const per = UNITS[units].bytes
  return b => {
    const v = b / per
    return `${v.toLocaleString('en-US', { maximumFractionDigits: v >= 100 ? 0 : 1 })} ${units}`
  }
}

const sum = (xs: number[]) => xs.reduce((s, x) => s + x, 0)

/** The stacked tiers: one band per tier over the scans (a lone scan: a stacked bar), y from 0. */
function stackSvg(d: TiersCardInput, x0: number, y0: number, w: number, h: number): string {
  const out: string[] = []
  const per = UNITS[d.units ?? 'TiB'].bytes
  const pts = d.points
  const top = Math.max(...pts.map(p => sum(p.bytes) / per), 1) * 1.04
  const ticks = niceTicks(0, top, 4)
  const hi = Math.max(ticks[ticks.length - 1], top)
  const { left, pw, X } = frame(d, x0, w)
  const Y = (v: number) => y0 + h - (v / hi) * h
  out.push(`<rect x="${r1(left)}" y="${r1(y0)}" width="${r1(pw)}" height="${r1(h)}" fill="${PANEL}"/>`)
  for (const v of ticks) {
    out.push(`<line x1="${r1(left)}" x2="${r1(left + pw)}" y1="${r1(Y(v))}" y2="${r1(Y(v))}" stroke="${LINE}" stroke-width="1"/>`)
    out.push(text(left - 6, Y(v) + 4, v.toLocaleString('en-US'), 11, { fill: INK3, anchor: 'end' }))
  }
  out.push(...mondays(d, X, y0 + h))
  if (!pts.length) return out.join('')
  // cum[k][i]: the top of tier k at scan i
  const cum: number[][] = []
  d.tiers.forEach((_, k) => cum.push(pts.map((p, i) => (k ? cum[k - 1][i] : 0) + p.bytes[k] / per)))
  const half = 0.4 * 86400_000
  d.tiers.forEach(({ color }, k) => {
    const lo = (i: number) => (k ? cum[k - 1][i] : 0)
    if (pts.length === 1) {
      const [xl, xr] = [X(pts[0].t - half), X(pts[0].t + half)]
      out.push(`<rect x="${r1(xl)}" y="${r1(Y(cum[k][0]))}" width="${r1(xr - xl)}" height="${r1(Y(lo(0)) - Y(cum[k][0]))}" fill="${color}" fill-opacity="0.92"/>`)
      return
    }
    const upper = pts.map((p, i) => `${r1(X(p.t))},${r1(Y(cum[k][i]))}`)
    const lower = pts.map((p, i) => `${r1(X(p.t))},${r1(Y(lo(i)))}`).reverse()
    out.push(`<polygon points="${[...upper, ...lower].join(' ')}" fill="${color}" fill-opacity="0.92"/>`)
  })
  return out.join('')
}

/** The storage-class digest image's SVG, `DIGEST_W` wide: the month's total line (autofit, dashed at its first scan,
 *  the latest called out) over its stacked tiers, then a legend of each tier's latest size, top tier first, then the
 *  headline interval's diff map when given. */
export function tiersSvg(d: TiersCardInput): string {
  const w = DIGEST_W - 2 * PAD
  const units = d.units ?? 'TiB'
  const fmt = fmtIn(units)
  const out = [header(d.title, d.note)]
  let y = PAD + 36
  const totals = d.points.map(p => ({ t: p.t, bytes: sum(p.bytes) }))
  out.push(chartSvg({ title: d.title, note: d.note, points: totals, from: d.from, to: d.to, units }, PAD, y, w, TOTAL_H, { xLabels: false, call: fmt }))
  y += TOTAL_H + 12
  out.push(stackSvg(d, PAD, y, w, STACK_H))
  y += STACK_H + 30
  const last = d.points[d.points.length - 1]
  let x = frame(d, PAD, w).left
  for (let k = d.tiers.length - 1; k >= 0; k--) {
    const { name, color } = d.tiers[k]
    const label = last ? `${name} ${fmt(last.bytes[k])}` : name
    out.push(`<rect x="${r1(x)}" y="${r1(y + 2)}" width="12" height="12" rx="2" fill="${color}"/>`)
    out.push(text(x + 18, y + 13, label, 13, { fill: INK2 }))
    x += 18 + interWidth(label, 13) + 22
  }
  y += 16
  if (d.diff) y = diffSection(d.diff, UNITS[units].fmt, y + 14, out)
  return page(y, out)
}
