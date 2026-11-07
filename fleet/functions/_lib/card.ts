/**
 * The fleet card (`/og/fleet.png`, specs/disky-oa-dev.md): three stacked panels over the last week, one trace per
 * deployment in its colour — the `/meta` store's size (log y), each scan's duration (a filled dot: ok; hollow: a step
 * issue still open, or the run failed) and each scan's cost — each trace labelled at its latest point in a right
 * gutter. Designed at `CARD_W` (phone width: Slack shows it about 340 px wide) and rasterized at `ZOOM`.
 *
 * It sits on its own opaque background, so it reads the same in Slack's light and dark themes; `theme: 'light'` is for
 * a page that follows the viewer's scheme. Each deployment's line is drawn in its own colour over a halo (the panel's
 * colour on dark, the colour darkened on light, where a yellow line alone would vanish), its label in the colour
 * nudged toward the ink.
 *
 * An `anon` card (no valid `sig`) has the frame, the deployments' names and colours, and no numbers. Pure and DOM-free.
 */
import { esc, fmtSI, interWidth, niceTicks, spread } from '@disk-tree/pages-kit/og/text'
import type { FleetDoc, FleetRun } from './fleetTypes.js'

export const CARD_W = 480
/** The PNG's scale: 1200 px wide. */
export const ZOOM = 2.5
const PAD = 14
const AXIS = 38
const DAY = 86400
/** Days the panels span, today included. */
export const WINDOW_DAYS = 7

export type Theme = 'dark' | 'light'
interface Palette { bg: string; panel: string; ink: string; ink2: string; ink3: string; grid: string; border: string }
const THEMES: Record<Theme, Palette> = {
  dark: { bg: '#1a1a19', panel: '#232322', ink: '#f4f4f5', ink2: '#c3c2b7', ink3: '#9a988d', grid: '#33332f', border: '#3a3a36' },
  light: { bg: '#ffffff', panel: '#f6f6f3', ink: '#1a1a19', ink2: '#45443e', ink3: '#6b6a62', grid: '#e2e1da', border: '#d9d8d0' },
}

/** `#rrggbb` mixed toward `toward` (`#rrggbb`) by `f` ∈ [0, 1]. */
export function mix(color: string, toward: string, f: number): string {
  const p = (c: string, i: number) => parseInt(c.slice(1 + 2 * i, 3 + 2 * i), 16)
  return `#${[0, 1, 2].map(i => Math.round(p(color, i) + (p(toward, i) - p(color, i)) * f).toString(16).padStart(2, '0')).join('')}`
}

const halo = (color: string, t: Theme): string => (t === 'dark' ? THEMES.dark.panel : mix(color, '#000000', 0.35))
const labelColor = (color: string, t: Theme): string => (t === 'dark' ? mix(color, '#ffffff', 0.2) : mix(color, '#000000', 0.4))

const r1 = (n: number) => Math.round(n * 10) / 10

interface TextOpts { fill: string; weight?: 400 | 600; anchor?: 'start' | 'middle' | 'end' }
function text(x: number, y: number, s: string, px: number, o: TextOpts): string {
  return `<text x="${r1(x)}" y="${r1(y)}" font-size="${px}" font-weight="${o.weight ?? 400}" fill="${o.fill}"${o.anchor && o.anchor !== 'start' ? ` text-anchor="${o.anchor}"` : ''}>${esc(s)}</text>`
}

/** A round byte tick: `1 GB`, `500 MB`. */
export const fmtTick = (b: number): string => fmtSI(b).replace(/\.0+ /, ' ')
/** Hours, to a tenth: `2.1h`; under an hour, minutes: `47m`. */
export const fmtHours = (secs: number): string => (secs < 3600 ? `${Math.round(secs / 60)}m` : `${(secs / 3600).toFixed(1)}h`)
export const fmtUsd = (usd: number): string => `$${usd < 10 ? usd.toFixed(2) : usd.toFixed(0)}`
const md = (t: number) => { const d = new Date(t * 1000); return `${d.getUTCMonth() + 1}/${d.getUTCDate()}` }
const stamp = (t: number) => new Date(t * 1000).toISOString().slice(0, 16).replace('T', ' ')

interface Pt { t: number; v: number; hollow?: boolean }
interface Trace { name: string; color: string; pts: Pt[]; label: string }
interface PanelSpec { title: string; note: string; h: number; log: boolean; traces: Trace[] }

export interface CardInput {
  tier: 'anon' | 'full'
  theme: Theme
  /** The deployments' names and colours, in order (an anonymous card draws only these). */
  deployments: { name: string; color: string }[]
  /** The full card's data. */
  doc?: FleetDoc
  /** The window's end (epoch s): the document's `now`, or the request's for an anonymous card. */
  now: number
}

const runPts = (runs: FleetRun[], from: number, v: (r: FleetRun) => number | null): Pt[] =>
  runs.filter(r => r.started !== null && r.started >= from && r.status !== 'running' && v(r) !== null)
    .map(r => ({ t: r.started!, v: v(r)!, hollow: r.open_issues.length > 0 || r.status === 'failed' }))
    .sort((a, b) => a.t - b.t)

const missing = (names: string[], what: string): string => (names.length ? `${names.join(', ')}: ${what}` : '')

/** The panels' contents for a full card. */
function panels(doc: FleetDoc, from: number): PanelSpec[] {
  const ds = doc.deployments
  const meta: Trace[] = []
  const dur: Trace[] = []
  const cost: Trace[] = []
  const noMeta: string[] = []
  const noDur: string[] = []
  const noCost: string[] = []
  for (const d of ds) {
    const m = (d.meta?.daily ?? []).filter(p => p.t >= from && p.bytes > 0).map(p => ({ t: p.t, v: p.bytes }))
    if (m.length) meta.push({ name: d.name, color: d.color, pts: m, label: `${d.name} ${fmtSI(m[m.length - 1].v)}` })
    else noMeta.push(d.name)
    const du = runPts(d.runs, from, r => (r.secs === null ? null : r.secs / 3600))
    if (du.length) dur.push({ name: d.name, color: d.color, pts: du, label: `${d.name} ${du[du.length - 1].v.toFixed(1)}h` })
    else noDur.push(d.name)
    const c = runPts(d.runs, from, r => r.cost_usd)
    if (c.length) cost.push({ name: d.name, color: d.color, pts: c, label: `${d.name} ${fmtUsd(c[c.length - 1].v)}` })
    else noCost.push(d.name)
  }
  return [
    { title: '/meta store size', note: missing(noMeta, 'n/a'), h: 118, log: true, traces: meta },
    { title: 'scan duration (h)', note: missing(noDur, 'no scans'), h: 118, log: false, traces: dur },
    { title: 'cost per scan ($)', note: missing(noCost, 'not recorded'), h: 100, log: false, traces: cost },
  ]
}

const ANON_PANELS: Omit<PanelSpec, 'traces' | 'note'>[] = [
  { title: '/meta store size', h: 118, log: true },
  { title: 'scan duration (h)', h: 118, log: false },
  { title: 'cost per scan ($)', h: 100, log: false },
]

/** A panel's y scale: its ticks (value, label) and value → y. */
function yScale(traces: Trace[], log: boolean, y0: number, h: number): { ticks: [number, string][]; Y: (v: number) => number } {
  const vals = traces.flatMap(t => t.pts.map(p => p.v))
  if (log) {
    const d0 = Math.floor(Math.log10(Math.min(...vals)))
    const d1 = Math.max(d0 + 1, Math.ceil(Math.log10(Math.max(...vals))))
    const ticks: [number, string][] = []
    for (let d = d0; d <= d1; d++) {
      for (const m of d1 - d0 === 1 ? [1, 2, 5] : [1]) if (d < d1 || m === 1) ticks.push([m * 10 ** d, fmtTick(m * 10 ** d)])
    }
    return { ticks, Y: v => y0 + h - ((Math.log10(v) - d0) / (d1 - d0)) * h }
  }
  const top = Math.max(...vals) * 1.12 || 1
  const nt = niceTicks(0, top, 4)
  const hi = Math.max(top, nt[nt.length - 1])
  return { ticks: nt.map(v => [v, v.toLocaleString('en-US')]), Y: v => y0 + h - (v / hi) * h }
}

/** The card's SVG, `CARD_W` wide (its height in the `height` attribute). */
export function fleetSvg(c: CardInput): string {
  const P = THEMES[c.theme]
  const to = (Math.floor(c.now / DAY) + 1) * DAY
  const from = to - WINDOW_DAYS * DAY
  const full = c.tier === 'full' && c.doc
  const specs: PanelSpec[] = full ? panels(c.doc!, from) : ANON_PANELS.map(p => ({ ...p, note: '', traces: [] }))
  const labels = specs.flatMap(s => s.traces.map(t => interWidth(t.label, 11, 600)))
  const gutter = Math.max(40, ...labels) + 12
  const left = PAD + AXIS
  const right = CARD_W - PAD - gutter
  const X = (t: number) => left + ((t - from) / (to - from)) * (right - left)
  const out: string[] = []

  // header: the title, the time, then the deployments as a legend
  out.push(text(PAD, PAD + 15, 'disky fleet', 17, { fill: P.ink, weight: 600 }))
  out.push(text(CARD_W - PAD, PAD + 15, full ? `${stamp(c.doc!.now)} UTC` : 'sign in for the numbers', 11, { fill: P.ink3, anchor: 'end' }))
  let lx = PAD
  for (const d of c.deployments) {
    out.push(`<circle cx="${r1(lx + 5)}" cy="${r1(PAD + 34)}" r="5" fill="${d.color}"/>`)
    out.push(text(lx + 14, PAD + 38, d.name, 12, { fill: P.ink2, weight: 600 }))
    lx += 14 + interWidth(d.name, 12, 600) + 16
  }
  if (full) {
    // the dots' key, at the legend's right: `● ok  ○ open issue`, laid out from the right edge
    const issue = CARD_W - PAD - interWidth('open issue', 11)
    const ok = issue - 22 - interWidth('ok', 11)
    out.push(`<circle cx="${r1(ok - 8)}" cy="${r1(PAD + 34)}" r="3.2" fill="${P.ink2}"/>`)
    out.push(text(ok, PAD + 38, 'ok', 11, { fill: P.ink3 }))
    out.push(`<circle cx="${r1(issue - 8)}" cy="${r1(PAD + 34)}" r="3.2" fill="${P.panel}" stroke="${P.ink2}" stroke-width="1.6"/>`)
    out.push(text(issue, PAD + 38, 'open issue', 11, { fill: P.ink3 }))
  }

  let y = PAD + 52
  for (const s of specs) {
    out.push(text(PAD, y + 12, s.title, 12.5, { fill: P.ink2, weight: 600 }))
    if (s.note) out.push(text(PAD + interWidth(s.title, 12.5, 600) + 10, y + 12, s.note, 11, { fill: P.ink3 }))
    const y0 = y + 20
    const h = s.h
    out.push(`<rect x="${r1(left)}" y="${r1(y0)}" width="${r1(right - left)}" height="${h}" fill="${P.panel}"/>`)
    // the days: a rule at each midnight, the date under its column
    for (let t = from; t <= to; t += DAY) out.push(`<line x1="${r1(X(t))}" x2="${r1(X(t))}" y1="${r1(y0)}" y2="${r1(y0 + h)}" stroke="${P.grid}"/>`)
    for (let t = from; t < to; t += DAY) out.push(text(X(t + DAY / 2), y0 + h + 13, md(t), 10, { fill: P.ink3, anchor: 'middle' }))
    if (s.traces.length) {
      const { ticks, Y } = yScale(s.traces, s.log, y0, h)
      for (const [v, label] of ticks) {
        const ty = Y(v)
        if (ty < y0 - 0.5 || ty > y0 + h + 0.5) continue
        out.push(`<line x1="${r1(left)}" x2="${r1(right)}" y1="${r1(ty)}" y2="${r1(ty)}" stroke="${P.grid}"/>`)
        out.push(text(left - 5, ty + 3.5, label, 10, { fill: P.ink3, anchor: 'end' }))
      }
      for (const t of s.traces) {
        const line = t.pts.map(p => `${r1(X(p.t))},${r1(Y(p.v))}`).join(' ')
        if (t.pts.length > 1) {
          out.push(`<polyline points="${line}" fill="none" stroke="${halo(t.color, c.theme)}" stroke-width="4.4" stroke-linejoin="round" stroke-linecap="round"/>`)
          out.push(`<polyline points="${line}" fill="none" stroke="${t.color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"/>`)
        }
        for (const p of t.pts) {
          out.push(p.hollow
            ? `<circle cx="${r1(X(p.t))}" cy="${r1(Y(p.v))}" r="3.4" fill="${P.panel}" stroke="${t.color}" stroke-width="1.8"/>`
            : `<circle cx="${r1(X(p.t))}" cy="${r1(Y(p.v))}" r="3.4" fill="${t.color}" stroke="${halo(t.color, c.theme)}" stroke-width="1"/>`)
        }
      }
      // each trace's label in the gutter, at its latest point's height (pushed apart, a leader when moved)
      const ends = s.traces.map(t => { const p = t.pts[t.pts.length - 1]; return { t, x: X(p.t), y: Y(p.v) } })
      const ys = spread(ends.map(e => e.y + 4), 13, y0 + 9, y0 + h + 4)
      const gx = right + 8
      ends.forEach((e, k) => {
        if (Math.abs(ys[k] - 4 - e.y) > 2 || gx - e.x > 10) {
          out.push(`<polyline points="${r1(e.x + 5)},${r1(e.y)} ${r1(gx - 6)},${r1(e.y)} ${r1(gx - 2)},${r1(ys[k] - 4)}" fill="none" stroke="${e.t.color}" stroke-opacity="0.55"/>`)
        }
        out.push(text(gx, ys[k], e.t.label, 11, { fill: labelColor(e.t.color, c.theme), weight: 600 }))
      })
    } else if (!full) {
      if (s === specs[1]) {
        const msg = 'sign in to see the fleet'
        const w = interWidth(msg, 12) + 20
        out.push(`<rect x="${r1((left + right - w) / 2)}" y="${r1(y0 + h / 2 - 12)}" width="${r1(w)}" height="24" rx="4" fill="${P.bg}"/>`)
        out.push(text((left + right) / 2, y0 + h / 2 + 4, msg, 12, { fill: P.ink2, anchor: 'middle' }))
      }
    } else {
      out.push(text((left + right) / 2, y0 + h / 2 + 4, 'no data this week', 12, { fill: P.ink3, anchor: 'middle' }))
    }
    y = y0 + h + 24
  }
  const H = Math.ceil(y - 10 + PAD)
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${CARD_W}" height="${H}" viewBox="0 0 ${CARD_W} ${H}" font-family="Inter">`
    + `<rect x="0.5" y="0.5" width="${CARD_W - 1}" height="${H - 1}" rx="8" fill="${P.bg}" stroke="${P.border}"/>${out.join('')}</svg>`
}
