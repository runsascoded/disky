/**
 * The fleet post's other images (specs/disky-oa-dev.md, "Replies"), each drawn by `/og/fleet.png?view=…` for its own
 * reply under the post: `timelineSvg`, each deployment's scan runs over the card's week as start→end bars coloured by
 * outcome (overlaps, long runs and gaps show); `costSvg`, each deployment's scan cost this month, cumulative, against
 * last month's. The card's frame, fonts and palette (`card.ts`), `CARD_W` wide; pure and DOM-free.
 */
import { OUTCOME_COLOR } from '@disk-tree/pages-kit/og/frame'
import { interWidth } from '@disk-tree/pages-kit/og/text'
import { CARD_W, DAY, fmtUsd, labelColor, md, PAD, r1, stamp, text, type Theme, THEMES, WINDOW_DAYS } from './card.js'
import type { FleetDeployment, FleetDoc, FleetRun } from './fleetTypes.js'
import { type Outcome, runOutcome } from './runOutcome.js'

/** Each outcome's colour, per theme (`@disk-tree/pages-kit/og/frame`, shared with the deployment status card). */
export { OUTCOME_COLOR }
const OUTCOME_LABEL: [Outcome, string][] = [['ok', 'ok'], ['issues', 'open issue'], ['failed', 'failed'], ['running', 'running'], ['cancelled', 'cancelled']]

const frame = (theme: Theme, h: number, body: string[]): string => {
  const P = THEMES[theme]
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${CARD_W}" height="${h}" viewBox="0 0 ${CARD_W} ${h}" font-family="Inter">`
    + `<rect x="0.5" y="0.5" width="${CARD_W - 1}" height="${h - 1}" rx="8" fill="${P.bg}" stroke="${P.border}"/>${body.join('')}</svg>`
}

/** The header every image shares: its title, and when the fleet was read. */
const header = (theme: Theme, title: string, now: number): string[] => {
  const P = THEMES[theme]
  return [
    text(PAD, PAD + 15, title, 17, { fill: P.ink, weight: 600 }),
    text(CARD_W - PAD, PAD + 15, `${stamp(now)} UTC`, 11, { fill: P.ink3, anchor: 'end' }),
  ]
}

/** Where a timeline bar ends: now while running, else its start plus its duration (none recorded: a sliver). */
const runEnd = (r: FleetRun, now: number): number => (runOutcome(r) === 'running' ? now : r.started! + (r.secs ?? 0))

/** Each deployment's runs in the card's week (`WINDOW_DAYS`) as bars from start to end, one row per deployment, a run
 *  that overlaps an earlier one on a lane of its own; coloured by outcome (`runOutcome`: a cancel is grey, never red). */
export function timelineSvg(doc: FleetDoc, theme: Theme): string {
  const P = THEMES[theme]
  const C = OUTCOME_COLOR[theme]
  const to = (Math.floor(doc.now / DAY) + 1) * DAY
  const from = to - WINDOW_DAYS * DAY
  const NAME_W = 44
  const left = PAD + NAME_W
  const right = CARD_W - PAD
  const X = (t: number) => left + ((Math.min(Math.max(t, from), to) - from) / (to - from)) * (right - left)
  const BAR = 9
  const LANE = 13
  const out = header(theme, 'disky fleet · scan runs', doc.now)
  // the outcomes' key
  let kx = PAD
  for (const [o, label] of OUTCOME_LABEL) {
    out.push(`<rect x="${r1(kx)}" y="${r1(PAD + 29)}" width="12" height="${BAR}" rx="2" fill="${C[o]}"/>`)
    out.push(text(kx + 16, PAD + 37, label, 11, { fill: P.ink3 }))
    kx += 16 + interWidth(label, 11) + 14
  }
  const top = PAD + 50
  const rows: { d: FleetDeployment; lanes: { r: FleetRun; x0: number; x1: number; lane: number }[]; n: number }[] = []
  for (const d of doc.deployments) {
    const runs = d.runs.filter(r => r.started !== null && r.started < to && runEnd(r, doc.now) > from).sort((a, b) => a.started! - b.started!)
    const ends: number[] = []
    const lanes = runs.map(r => {
      const x0 = X(r.started!)
      const x1 = Math.max(x0 + 3, X(runEnd(r, doc.now)))
      let lane = ends.findIndex(e => e + 2 <= x0)
      if (lane < 0) { lane = ends.length; ends.push(x1) } else ends[lane] = x1
      return { r, x0, x1, lane }
    })
    rows.push({ d, lanes, n: Math.max(1, ends.length) })
  }
  const heights = rows.map(row => row.n * LANE + 9)
  const bottom = top + heights.reduce((s, h) => s + h, 0) + (rows.length - 1) * 4
  // the days: a rule at each midnight across every row, the date under its column; `now` dashed
  out.push(`<rect x="${r1(left)}" y="${r1(top)}" width="${r1(right - left)}" height="${r1(bottom - top)}" fill="${P.panel}"/>`)
  for (let t = from; t <= to; t += DAY) out.push(`<line x1="${r1(X(t))}" x2="${r1(X(t))}" y1="${r1(top)}" y2="${r1(bottom)}" stroke="${P.grid}"/>`)
  for (let t = from; t < to; t += DAY) out.push(text(X(t + DAY / 2), bottom + 13, md(t), 10, { fill: P.ink3, anchor: 'middle' }))
  out.push(`<line x1="${r1(X(doc.now))}" x2="${r1(X(doc.now))}" y1="${r1(top)}" y2="${r1(bottom)}" stroke="${P.ink3}" stroke-dasharray="2 3"/>`)
  let y = top
  rows.forEach((row, i) => {
    const h = heights[i]
    if (i > 0) out.push(`<line x1="${r1(left)}" x2="${r1(right)}" y1="${r1(y - 2)}" y2="${r1(y - 2)}" stroke="${P.border}"/>`)
    out.push(text(PAD, y + h / 2 + 4, row.d.name, 12, { fill: labelColor(row.d.color, theme), weight: 600 }))
    if (!row.lanes.length) {
      out.push(text((left + right) / 2, y + h / 2 + 4, row.d.errors.runs ? 'runs n/a' : 'no runs this week', 11, { fill: P.ink3, anchor: 'middle' }))
    }
    for (const { r, x0, x1, lane } of row.lanes) {
      out.push(`<rect x="${r1(x0)}" y="${r1(y + 5 + lane * LANE)}" width="${r1(x1 - x0)}" height="${BAR}" rx="2" fill="${C[runOutcome(r)]}"/>`)
    }
    y += h + 4
  })
  return frame(theme, Math.ceil(bottom + 13 + PAD), out)
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const daysIn = (y: number, m: number) => new Date(Date.UTC(y, m + 1, 0)).getUTCDate()

/** A month's running total, one point per day boundary: `[0, 0]`, then `[k, total through day k]` for k = 1…`days`. */
function cumulative(costs: { t: number; usd: number }[], y: number, m: number, days: number): [number, number][] {
  const start = Date.UTC(y, m, 1) / 1000
  const per = new Array<number>(days).fill(0)
  for (const c of costs) {
    const k = Math.floor((c.t - start) / DAY)
    if (k >= 0 && k < days) per[k] += c.usd
  }
  const pts: [number, number][] = [[0, 0]]
  per.forEach((v, k) => pts.push([k + 1, pts[k][1] + v]))
  return pts
}

/** Each deployment's scan cost this month, cumulative (solid, through today), over last month's (dashed, the whole
 *  month), on a day-of-month axis: is this month running ahead? Each panel's title gives both to date and last month's
 *  total. From `cost_daily` (every run its deployment lists, since last month began). */
export function costSvg(doc: FleetDoc, theme: Theme): string {
  const P = THEMES[theme]
  const now = new Date(doc.now * 1000)
  const [y, m] = [now.getUTCFullYear(), now.getUTCMonth()]
  const [py, pm] = m === 0 ? [y - 1, 11] : [y, m - 1]
  const today = now.getUTCDate()
  const [cur, prev] = [MONTHS[m], MONTHS[pm]]
  const left = PAD + 40
  const right = CARD_W - PAD
  const X = (day: number) => left + (day / 31) * (right - left)
  const H = 64
  const out = header(theme, 'disky fleet · scan cost by month', doc.now)
  // the key: this month solid, last month dashed
  const ky = PAD + 33
  out.push(`<line x1="${PAD}" x2="${PAD + 18}" y1="${ky}" y2="${ky}" stroke="${P.ink2}" stroke-width="2.2"/>`)
  out.push(text(PAD + 23, ky + 4, `${cur} (to date)`, 11, { fill: P.ink3 }))
  const k2 = PAD + 23 + interWidth(`${cur} (to date)`, 11) + 16
  out.push(`<line x1="${k2}" x2="${k2 + 18}" y1="${ky}" y2="${ky}" stroke="${P.ink2}" stroke-width="1.6" stroke-dasharray="4 3"/>`)
  out.push(text(k2 + 23, ky + 4, prev, 11, { fill: P.ink3 }))
  let top = PAD + 50
  const prevStart = Date.UTC(py, pm, 1) / 1000
  const curStart = Date.UTC(y, m, 1) / 1000
  for (const d of doc.deployments) {
    const costs = d.cost_daily ?? []
    const color = labelColor(d.color, theme)
    out.push(text(PAD, top + 12, d.name, 12.5, { fill: color, weight: 600 }))
    if (!costs.length) {
      out.push(text(PAD + 8 + interWidth(d.name, 12.5, 600), top + 12, d.errors.runs ? 'runs n/a' : 'no cost recorded', 11, { fill: P.ink3 }))
      top += 22
      continue
    }
    const thisMonth = cumulative(costs, y, m, today)
    const soFar = thisMonth[thisMonth.length - 1][1]
    // last month from its first recorded day: a deployment whose listed runs start mid-month draws (and sums) only what
    // it lists, said so, never a flat $0 before
    const first = costs[0].t
    const from = first >= curStart ? null : first > prevStart ? new Date(first * 1000).getUTCDate() : 1
    const lastMonth = from === null ? [] : cumulative(costs, py, pm, daysIn(py, pm)).slice(from - 1)
    const prevTotal = lastMonth.length ? lastMonth[lastMonth.length - 1][1] : 0
    const prevNote = from === null ? `${prev} n/a`
      : from > 1 ? `${prev} from the ${from}${ordinal(from)} ${fmtUsd(prevTotal)}`
      : `${prev} to the ${today}${ordinal(today)} ${fmtUsd(lastMonth[Math.min(today, lastMonth.length - 1)][1])} · all ${prev} ${fmtUsd(prevTotal)}`
    out.push(text(PAD + 8 + interWidth(d.name, 12.5, 600), top + 12, `${cur} so far ${fmtUsd(soFar)} · ${prevNote}`, 11, { fill: P.ink3 }))
    const y0 = top + 20
    out.push(`<rect x="${r1(left)}" y="${r1(y0)}" width="${r1(right - left)}" height="${H}" fill="${P.panel}"/>`)
    for (const k of [0, 7, 14, 21, 28]) out.push(`<line x1="${r1(X(k))}" x2="${r1(X(k))}" y1="${r1(y0)}" y2="${r1(y0 + H)}" stroke="${P.grid}"/>`)
    const peak = Math.max(soFar, prevTotal)
    if (peak > 0) {
      const Y = (v: number) => y0 + H - (v / (peak * 1.1)) * H
      out.push(text(left - 5, Y(peak) + 3.5, fmtUsd(peak), 10, { fill: P.ink3, anchor: 'end' }))
      out.push(`<line x1="${r1(left)}" x2="${r1(right)}" y1="${r1(Y(peak))}" y2="${r1(Y(peak))}" stroke="${P.grid}"/>`)
      out.push(text(left - 5, y0 + H + 3.5, '$0', 10, { fill: P.ink3, anchor: 'end' }))
      const pts = (ps: [number, number][]) => ps.map(([k, v]) => `${r1(X(k))},${r1(Y(v))}`).join(' ')
      if (lastMonth.length > 1) {
        out.push(`<polyline points="${pts(lastMonth)}" fill="none" stroke="${d.color}" stroke-opacity="0.75" stroke-width="1.6" stroke-dasharray="4 3" stroke-linejoin="round"/>`)
      }
      out.push(`<polyline points="${pts(thisMonth)}" fill="none" stroke="${d.color}" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>`)
      const [ek, ev] = thisMonth[thisMonth.length - 1]
      out.push(`<circle cx="${r1(X(ek))}" cy="${r1(Y(ev))}" r="3.2" fill="${d.color}"/>`)
    }
    top = y0 + H + 10
  }
  // the day-of-month axis, under the last panel
  for (const k of [1, 8, 15, 22, 29]) out.push(text(X(k - 0.5), top + 3, String(k), 10, { fill: P.ink3, anchor: 'middle' }))
  return frame(theme, Math.ceil(top + 3 + PAD), out)
}

const ordinal = (n: number): string => (n % 100 >= 11 && n % 100 <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] ?? 'th')
