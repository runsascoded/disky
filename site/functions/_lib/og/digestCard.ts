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
import { esc, fmtB } from './card.js'
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
const TIB = 1024 ** 4

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
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(Math.round(v * 1e6) / 1e6)
  return out
}

const fmtSigned = (n: number) => `${n < 0 ? '−' : '+'}${fmtB(Math.abs(n))}`
const md = (t: number) => { const d = new Date(t); return `${d.getUTCMonth() + 1}/${d.getUTCDate()}` }

function chartSvg(d: DigestCardInput, x0: number, y0: number, w: number, h: number): string {
  const out: string[] = []
  const pts = d.points
  const tib = (b: number) => b / TIB
  const vals = pts.map(p => tib(p.bytes))
  const q = d.quota ? tib(d.quota.bytes) : null
  let lo = Math.min(...vals)
  let hi = Math.max(...vals, ...(q != null ? [q] : []))
  const span = hi - lo || hi * 0.1 || 1
  lo -= span * 0.08
  hi += span * 0.06
  const ticks = niceTicks(lo, hi)
  lo = Math.min(lo, ticks[0])
  const left = x0 + 44
  const pw = x0 + w - left
  const X = (t: number) => left + ((t - d.from) / (d.to - d.from)) * pw
  const Y = (v: number) => y0 + h - ((v - lo) / (hi - lo)) * h
  out.push(`<rect x="${r1(left)}" y="${r1(y0)}" width="${r1(pw)}" height="${r1(h)}" fill="${PANEL}"/>`)
  for (const v of ticks) {
    if (v < lo || v > hi) continue
    out.push(`<line x1="${r1(left)}" x2="${r1(left + pw)}" y1="${r1(Y(v))}" y2="${r1(Y(v))}" stroke="${LINE}" stroke-width="1"/>`)
    out.push(text(left - 6, Y(v) + 4, v.toLocaleString('en-US'), 11, { fill: INK3, anchor: 'end' }))
  }
  // Weekly ticks: every Monday in the span.
  const day = 86400_000
  for (let t = Math.ceil(d.from / day) * day; t <= d.to; t += day) {
    if (new Date(t).getUTCDay() !== 1) continue
    out.push(`<line x1="${r1(X(t))}" x2="${r1(X(t))}" y1="${r1(y0 + h)}" y2="${r1(y0 + h + 4)}" stroke="${INK3}"/>`)
    out.push(text(X(t), y0 + h + 17, md(t), 11, { fill: INK3, anchor: 'middle' }))
  }
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
  const call = `${fmtB(last.bytes)}${q != null ? ` · ${Math.round((100 * tib(last.bytes)) / q)}%` : ''}`
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
  const out: string[] = []
  let y = PAD
  out.push(text(PAD, y + 18, d.title, 20, { weight: 600 }))
  out.push(text(DIGEST_W - PAD, y + 18, d.note, 12, { fill: INK3, anchor: 'end' }))
  y += 36
  out.push(chartSvg(d, PAD, y, w, CHART_H))
  y += CHART_H + 30
  if (d.diff) {
    const { root, title } = d.diff
    const ls = leaves(root)
    const grew = ls.reduce((s, n) => s + n.added, 0)
    const shrank = ls.reduce((s, n) => s + n.removed, 0)
    y += 16
    out.push(text(PAD, y + 4, title, 17, { weight: 600 }))
    out.push(text(DIGEST_W - PAD, y + 4, `grew ${fmtSigned(grew)} · shrank ${fmtSigned(-shrank)} · area = |Δ|`, 12, { fill: INK3, anchor: 'end' }))
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
      getSize: n => n.weight, getLabel: label, formatSize: fmtB, formatCell: n => fmtSigned(n.delta),
      colorForCell: n => diffCellStyle(n, 'delta'),
      measure: (s, px) => interWidth(s, px), vars: VARS, fontFamily: 'Inter',
      labelPx: 11.5, labelPxSm: 9.5, depthFade: 1, rootFade: 1,
    }))
    out.push('</g>')
    y += MAP_H
  }
  const H = Math.ceil(y + PAD)
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${DIGEST_W}" height="${H}" viewBox="0 0 ${DIGEST_W} ${H}" font-family="Inter"><rect width="${DIGEST_W}" height="${H}" fill="${BG}"/>${out.join('')}</svg>`
}
