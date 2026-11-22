/**
 * A deployment's status card (open question #87): drawn once, posted under the fleet post (each deployment's own reply,
 * beside its runs list) and on the head of the quiet run thread in `#<deploy>-admin`.
 *
 * (a) Its last runs as bars, newest on the right: height is the run's duration, stacked by step family (`phaseFamily`, as
 * `/scans` groups phases), each second of the run's clock shared equally among the families running then (`familySeconds`),
 * so a bar's segments sum to its duration and a step running beside another shows as half its time, never hidden. Each bar
 * is capped or outlined by its outcome (`runOutcome`: a step issue still open, failed, cancelled, running), a step that
 * failed in it is hatched, and a manual run has a person mark under it. The point: which step is growing, or flaky.
 * (b) Under it, a strip of 4 sparklines over 30 days, each with its current value and 7-day change: the primary store's
 * bytes and objects, and each clean run's cost and duration.
 *
 * Designed at `DEPLOY_W` (Slack shows an image block about 360 px wide on a phone) and rasterized at `DEPLOY_ZOOM`; its own
 * opaque background, `THEMES`' palette (`card.ts`) and outcome colours (`views.ts`). Pure and DOM-free.
 */
import { fmtSI, interWidth } from '@disk-tree/pages-kit/og/text'
import { DAY, fmtCount, fmtUsd, md, mix, r1, stamp, text, type Theme, THEMES } from './card.js'
import { type Outcome, runOutcome } from './runOutcome.js'
import { OUTCOME_COLOR } from './views.js'

export const DEPLOY_W = 400
/** The PNG's scale: 1200 px wide. */
export const DEPLOY_ZOOM = 3
const PAD = 12
const AXIS = 28
/** The sparklines' span, today included. */
export const STRIP_DAYS = 30

/** A phase's family: the per-source phases a scan runs in parallel (`list <bucket>`, `import <bucket>`) are one family;
 *  any other phase is its own (`site/src/scanRunsModel.ts`'s `phaseFamily`, which the fleet can't import). */
export const phaseFamily = (phase: string): string => /^(list|import) \S/.exec(phase)?.[1] ?? phase

/** The family a run's clock is in when no step is (its job's boot and queue, the gaps between steps). */
export const IDLE = ''

export type StepState = 'running' | 'cancelled' | 'failed'
type SpanState = 'done' | StepState
const RANK: Record<SpanState, number> = { done: 0, running: 1, cancelled: 2, failed: 3 }

/** A phase as `/api/scan-runs` serves it (`PhaseSpan`: seconds from the run's start). */
export interface SpanIn { phase: string; start: number; end: number; status: string; note?: string | null }

/** A phase's state as `/scans` shows it (`phaseStatus`): a closed phase whose note names a failure failed; in a cancelled
 *  run, a failed or still-running phase is the cancel's. */
function spanState(s: SpanIn, runStatus: string): SpanState {
  if (s.status === 'running') return runStatus === 'cancelled' ? 'cancelled' : 'running'
  if (s.status === 'cancelled') return 'cancelled'
  const failed = s.status === 'failed' || /(?:^|,\s*)failed\b/i.test(s.note ?? '')
  return failed ? (runStatus === 'cancelled' ? 'cancelled' : 'failed') : 'done'
}

/** A run's seconds per step family, summing to its duration: each stretch of its clock is shared equally among the
 *  families running then (`IDLE` when none is), whole seconds, families in the order they first ran; and each family
 *  whose worst phase didn't finish clean. `secs` null: the clock ends at the last phase's end. */
export function familySeconds(spans: readonly SpanIn[], secs: number | null, runStatus: string): { families: [string, number][]; bad: [string, StepState][] } {
  const by = new Map<string, { start: number; end: number; state: SpanState }[]>()
  for (const s of spans) {
    const f = phaseFamily(s.phase)
    const xs = by.get(f) ?? []
    xs.push({ start: s.start, end: s.end, state: spanState(s, runStatus) })
    by.set(f, xs)
  }
  const iv: { f: string; start: number; end: number }[] = []
  const bad: [string, StepState][] = []
  for (const [f, xs] of by) {
    xs.sort((a, b) => a.start - b.start)
    let cur: { f: string; start: number; end: number } | null = null
    let worst: SpanState = 'done'
    for (const x of xs) {
      if (RANK[x.state] > RANK[worst]) worst = x.state
      if (cur && x.start <= cur.end) cur.end = Math.max(cur.end, x.end)
      else { if (cur) iv.push(cur); cur = { f, start: x.start, end: x.end } }
    }
    if (cur) iv.push(cur)
    if (worst !== 'done') bad.push([f, worst])
  }
  const T = secs ?? Math.max(0, ...iv.map(x => x.end))
  const cuts = [...new Set([0, T, ...iv.flatMap(x => [x.start, x.end])])].filter(t => t >= 0 && t <= T).sort((a, b) => a - b)
  const at = new Map<string, number>()
  for (let k = 0; k + 1 < cuts.length; k++) {
    const [lo, hi] = [cuts[k], cuts[k + 1]]
    const on = [...new Set(iv.filter(x => x.start <= lo && x.end >= hi).map(x => x.f))]
    if (!on.length) at.set(IDLE, (at.get(IDLE) ?? 0) + hi - lo)
    for (const f of on) at.set(f, (at.get(f) ?? 0) + (hi - lo) / on.length)
  }
  return { families: [...at].map(([f, v]) => [f, Math.round(v)]), bad }
}

/** A run as the card draws it. */
export interface CardRun {
  scan: string
  status: string
  trigger: 'schedule' | 'manual' | null
  /** Epoch s. */
  started: number | null
  secs: number | null
  cost_usd: number | null
  open_issues: string[]
  /** `familySeconds`' families; absent: not read (the bar is drawn whole, unsplit). */
  families?: [string, number][]
  bad?: [string, StepState][]
}

/** A store's size at a scan (epoch s). */
export interface CardPoint { t: number; bytes: number | null; objects: number | null }

export type BarMode = 'family' | 'outcome'
export type BarWindow = { runs: number } | { days: number }

export interface DeployCardInput {
  name: string
  color: string
  theme: Theme
  /** The card's time (epoch s). */
  now: number
  /** Scan runs, any order. */
  runs: CardRun[]
  /** The primary store's sizes, any order. */
  points: CardPoint[]
  /** Bars stacked by step family (default), or one fill in the run's outcome colour. */
  mode?: BarMode
  /** The last N runs (default 30), or every run started in the last N days. */
  window?: BarWindow
}

/** A duration, short: `42s`, `47m`, `2.1h`. */
export function fmtDur(secs: number): string {
  if (secs < 90) return `${Math.round(secs)}s`
  if (secs < 5400) return `${Math.round(secs / 60)}m`
  return `${(secs / 3600).toFixed(1)}h`
}

/** A change, signed: `+0.6%`, `-12%`, `±0%`. */
export function fmtDelta(cur: number, was: number): string | null {
  if (!(was > 0)) return null
  const pct = (cur / was - 1) * 100
  if (Math.abs(pct) < 0.05) return '±0%'
  return `${pct > 0 ? '+' : '-'}${Math.abs(pct) < 10 ? Math.abs(pct).toFixed(1) : Math.abs(pct).toFixed(0)}%`
}

const median = (xs: number[]): number | null => {
  if (!xs.length) return null
  const s = [...xs].sort((a, b) => a - b)
  const m = s.length >> 1
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2
}

// ── the bars ──────────────────────────────────────────────────────────────

/** Families with a hue of their own wherever they run, so a deployment's card and the next one's agree (the site's
 *  series palette, `--s*`, without its red: red is a failure). */
const KNOWN: Record<string, number> = {
  list: 0, import: 4, 'path-index': 3, 'index-write': 3, 'layer-2': 6, 'static-names': 1, 'interval-store': 5, 'age-store': 6, 'warm-cache': 2,
}
const PALETTE: Record<Theme, string[]> = {
  // s1, s7, s2, s4, s5, s6, s3
  light: ['#2a78d6', '#4a3aa7', '#008300', '#eda100', '#1baf7a', '#eb6834', '#e87ba4'],
  dark: ['#3987e5', '#9085e9', '#2f9e2f', '#c98500', '#199e70', '#d95926', '#d55181'],
}
/** Families drawn in a hue of their own, at most; the rest are one "other steps" segment. */
export const MAX_HUES = 5
/** A family gets a hue when its median share of the window's runs is at least this. */
const MIN_SHARE = 0.03
export const OTHER = 'other steps'

export interface Seg { family: string; secs: number; failed: boolean }
export interface Bar {
  scan: string
  started: number
  secs: number
  outcome: Outcome
  manual: boolean
  /** Bottom to top: `IDLE`, the hued families in pipeline order, `OTHER`; one `OTHER` segment for an unsplit run. */
  segs: Seg[]
}
export interface Legend { family: string; color: string; last: number | null; median: number | null }

/** The window's runs, oldest first (the newest on the right), and the families given hues. */
export function barModel(c: Pick<DeployCardInput, 'runs' | 'now' | 'window'>): { bars: Bar[]; hued: string[] } {
  const win = c.window ?? { runs: 30 }
  const started = c.runs.filter(r => r.started !== null).sort((a, b) => a.started! - b.started!)
  const shown = 'runs' in win ? started.slice(-win.runs)
    : started.filter(r => r.started! >= (Math.floor(c.now / DAY) - win.days + 1) * DAY)
  // a family's median share of the runs it was read for (absent from a run: 0 there)
  const split = shown.filter(r => r.families?.length && (r.secs ?? 0) > 0)
  const order: string[] = []
  for (const r of [...split].reverse()) for (const [f] of r.families!) if (f !== IDLE && !order.includes(f)) order.push(f)
  const share = (f: string) => median(split.map(r => (r.families!.find(([g]) => g === f)?.[1] ?? 0) / r.secs!)) ?? 0
  const ranked = order.filter(f => share(f) >= MIN_SHARE).sort((a, b) => share(b) - share(a)).slice(0, MAX_HUES)
  // the hued families in pipeline order: by their mean start position across the runs (the order `families` lists)
  const pos = (f: string) => {
    const at = split.map(r => r.families!.findIndex(([g]) => g === f) / r.families!.length).filter(p => p >= 0)
    return at.reduce((s, p) => s + p, 0) / Math.max(1, at.length)
  }
  const hued = [...ranked].sort((a, b) => pos(a) - pos(b))
  const bars = shown.map(r => {
    const failed = new Set((r.bad ?? []).filter(([, s]) => s === 'failed').map(([f]) => f))
    const secs = r.secs ?? 0
    let segs: Seg[]
    if (!r.families?.length) segs = [{ family: OTHER, secs, failed: false }]
    else {
      const get = (f: string) => r.families!.find(([g]) => g === f)?.[1] ?? 0
      const rest = r.families.filter(([f]) => f !== IDLE && !hued.includes(f))
      segs = [
        { family: IDLE, secs: get(IDLE), failed: false },
        ...hued.map(f => ({ family: f, secs: get(f), failed: failed.has(f) })),
        { family: OTHER, secs: rest.reduce((s, [, v]) => s + v, 0), failed: rest.some(([f]) => failed.has(f)) },
      ].filter(s => s.secs > 0 || s.failed)
    }
    return { scan: r.scan, started: r.started!, secs, outcome: runOutcome(r), manual: r.trigger === 'manual', segs }
  })
  return { bars, hued }
}

/** Each hued family's seconds in the newest finished run, and its median over the window's finished runs. */
export function legendModel(bars: Bar[], hued: string[], theme: Theme): Legend[] {
  const done = bars.filter(b => b.outcome !== 'running' && b.outcome !== 'cancelled' && b.segs.some(s => s.family !== OTHER || b.segs.length > 1))
  const last = done[done.length - 1]
  const of = (b: Bar, f: string) => b.segs.find(s => s.family === f)?.secs ?? 0
  return [...hued, OTHER, IDLE].map(f => ({
    family: f,
    color: famColor(f, hued, theme),
    last: last ? of(last, f) : null,
    median: median(done.map(b => of(b, f))),
  }))
}

export function famColor(f: string, hued: string[], theme: Theme): string {
  const P = THEMES[theme]
  if (f === IDLE) return theme === 'dark' ? '#4a4944' : '#dcdbd3'
  if (f === OTHER) return theme === 'dark' ? '#6b6a62' : '#a8a79e'
  const pal = PALETTE[theme]
  const known = KNOWN[f]
  if (known !== undefined) return pal[known]
  // an unknown family: the first hue no known family on this card holds
  const taken = new Set(hued.map(h => KNOWN[h]).filter(k => k !== undefined))
  const free = pal.map((_, i) => i).filter(i => !taken.has(i))
  const k = hued.filter(h => KNOWN[h] === undefined).indexOf(f)
  return pal[free[k % free.length] ?? 0] ?? P.ink3
}

// ── the strip ─────────────────────────────────────────────────────────────

export interface Metric { key: 'bytes' | 'objects' | 'cost' | 'secs'; label: string; value: string | null; delta: string | null; pts: [number, number][] }

/** The strip's 4 cells: the store's bytes and objects now (its newest scan) and their change over 7 days (against the newest
 *  scan at or before then); each clean run's cost and duration as the median of the last 7 days' runs, and its change
 *  against the 7 days before. `pts` are the sparklines' points over `STRIP_DAYS`, oldest first. */
export function stripModel(c: Pick<DeployCardInput, 'runs' | 'points' | 'now'>): Metric[] {
  const from = c.now - STRIP_DAYS * DAY
  const series = (k: 'bytes' | 'objects', label: string, fmt: (n: number) => string): Metric => {
    const pts = c.points.filter(p => p[k] !== null).map(p => [p.t, p[k]!] as [number, number]).sort((a, b) => a[0] - b[0])
    const cur = pts[pts.length - 1]
    const was = pts.filter(p => p[0] <= c.now - 7 * DAY).pop()
    return { key: k, label, value: cur ? fmt(cur[1]) : null, delta: cur && was ? fmtDelta(cur[1], was[1]) : null, pts: pts.filter(p => p[0] >= from) }
  }
  const clean = c.runs.filter(r => r.status === 'succeeded' && r.started !== null).sort((a, b) => a.started! - b.started!)
  const perRun = (k: 'cost' | 'secs', label: string, v: (r: CardRun) => number | null, fmt: (n: number) => string): Metric => {
    const pts = clean.flatMap(r => { const x = v(r); return x === null ? [] : [[r.started!, x] as [number, number]] })
    const wk = (lo: number, hi: number) => median(pts.filter(p => p[0] > c.now - hi * DAY && p[0] <= c.now - lo * DAY).map(p => p[1]))
    const [cur, was] = [wk(0, 7), wk(7, 14)]
    return { key: k, label, value: cur === null ? null : fmt(cur), delta: cur !== null && was !== null ? fmtDelta(cur, was) : null, pts: pts.filter(p => p[0] >= from) }
  }
  return [
    series('bytes', 'data', b => fmtSI(b).replace(/\.0+ /, ' ')),
    series('objects', 'objects', fmtCount),
    perRun('cost', '$/run', r => r.cost_usd, fmtUsd),
    perRun('secs', 'run time', r => r.secs, fmtDur),
  ]
}

// ── the SVG ───────────────────────────────────────────────────────────────

/** A person, `w` wide, its top at `y`: a manual run's mark. */
const person = (cx: number, y: number, fill: string): string =>
  `<circle cx="${r1(cx)}" cy="${r1(y + 1.8)}" r="1.8" fill="${fill}"/><path d="M${r1(cx - 3.2)} ${r1(y + 7.6)}Q${r1(cx)} ${r1(y + 2.4)} ${r1(cx + 3.2)} ${r1(y + 7.6)}Z" fill="${fill}"/>`

/** The bars' height scale's top: the longest run, unless it is more than `CLIP`× the median, when the bars above it are cut
 *  (a break and their duration) so one stuck run doesn't flatten the rest. */
const CLIP = 2.2

/** Gridline steps a duration axis takes (s): whole minutes, then whole hours. */
const STEPS = [60, 120, 300, 600, 900, 1200, 1800, 3600, 7200, 10800, 14400, 21600, 43200, 86400]

/** A gridline's label: `20m`, `1h`, `1.5h`. */
export const fmtTick = (s: number): string => (s < 3600 ? `${s / 60}m` : `${+(s / 3600).toFixed(1)}h`)

/** The bars' scale: its top (a whole number of steps over the longest bar, at most 4) and its gridlines. */
export function hiOf(secs: number[]): { hi: number; ticks: number[] } {
  const max = Math.max(1, ...secs)
  const med = median(secs.filter(s => s > 0)) ?? max
  const top = max > CLIP * med ? CLIP * med : max
  const step = STEPS.find(s => Math.ceil(top / s) <= 4) ?? Math.ceil(top / 4)
  const n = Math.ceil(top / step)
  return { hi: n * step, ticks: Array.from({ length: n }, (_, i) => (i + 1) * step) }
}

export function deployCardSvg(c: DeployCardInput): string {
  const P = THEMES[c.theme]
  const O = OUTCOME_COLOR[c.theme]
  const mode = c.mode ?? 'family'
  const win = c.window ?? { runs: 30 }
  const { bars, hued } = barModel(c)
  const out: string[] = []
  const W = DEPLOY_W
  const left = PAD + AXIS
  const right = W - PAD

  // header: the deployment, what the bars are, when
  out.push(`<circle cx="${PAD + 5}" cy="${PAD + 10}" r="5" fill="${c.color}"/>`)
  out.push(text(PAD + 15, PAD + 15, c.name, 16, { fill: P.ink, weight: 600 }))
  const what = 'runs' in win ? `last ${bars.length} scan runs` : `scan runs, last ${win.days} days`
  out.push(text(PAD + 15 + interWidth(c.name, 16, 600) + 8, PAD + 15, what, 11, { fill: P.ink3 }))
  out.push(text(right, PAD + 15, `${stamp(c.now)} UTC`, 10, { fill: P.ink3, anchor: 'end' }))

  // the key above the bars: each hued family (its last run's time, and × its median when that's off by a third), or the
  // outcomes
  let y = PAD + 26
  const items: { swatch: string; label: string; strong?: string }[] = []
  if (mode === 'family') {
    for (const l of legendModel(bars, hued, c.theme)) {
      if (l.last === null || (l.family === IDLE && !l.median)) continue
      const name = l.family === IDLE ? 'overhead' : l.family
      const ratio = l.median ? l.last / l.median : null
      const off = ratio !== null && l.last >= 60 && (ratio >= 1.33 || ratio <= 0.75) ? ` ×${ratio < 10 ? ratio.toFixed(1) : ratio.toFixed(0)}` : ''
      items.push({ swatch: l.color, label: `${name} ${fmtDur(l.last)}`, strong: off })
    }
  } else {
    for (const [o, label] of [['ok', 'clean'], ['issues', 'open issue'], ['failed', 'failed'], ['cancelled', 'cancelled'], ['running', 'running']] as const) {
      if (bars.some(b => b.outcome === o)) items.push({ swatch: O[o], label })
    }
  }
  let x = PAD
  y += 4
  for (const it of items) {
    const w = 12 + interWidth(it.label, 10.5) + (it.strong ? interWidth(it.strong, 10.5, 600) : 0) + 10
    if (x + w - 10 > right && x > PAD) { x = PAD; y += 15 }
    out.push(`<rect x="${r1(x)}" y="${r1(y + 1)}" width="8" height="8" rx="1.5" fill="${it.swatch}"/>`)
    out.push(text(x + 12, y + 9, it.label, 10.5, { fill: P.ink2 }))
    if (it.strong) out.push(text(x + 12 + interWidth(it.label, 10.5), y + 9, it.strong, 10.5, { fill: P.ink, weight: 600 }))
    x += w
  }
  y += 18

  // the bars
  const H = 112
  const y0 = y + 8
  const base = y0 + H
  const { hi, ticks } = hiOf(bars.map(b => b.secs))
  const Y = (s: number) => base - (Math.min(s, hi) / hi) * H
  out.push(`<rect x="${r1(left)}" y="${r1(y0)}" width="${r1(right - left)}" height="${H}" fill="${P.panel}"/>`)
  for (const t of ticks) {
    out.push(`<line x1="${r1(left)}" x2="${r1(right)}" y1="${r1(Y(t))}" y2="${r1(Y(t))}" stroke="${P.grid}"/>`)
    out.push(text(left - 4, Y(t) + 3.3, fmtTick(t), 9.5, { fill: P.ink3, anchor: 'end' }))
  }
  const slots = 'runs' in win ? Math.max(win.runs, bars.length) : Math.max(bars.length, 1)
  const slot = (right - left) / slots
  const bw = Math.max(2, Math.min(10, slot * 0.72))
  const first = slots - bars.length
  const cx = (i: number) => left + (first + i + 0.5) * slot
  // a rule where a UTC day begins, its date under the first bar of the day (spaced so the labels never touch)
  let lastLabel = -Infinity
  const dateY = base + 21
  bars.forEach((b, i) => {
    const day = Math.floor(b.started / DAY)
    if (i > 0 && day !== Math.floor(bars[i - 1].started / DAY)) {
      const xr = cx(i) - slot / 2
      out.push(`<line x1="${r1(xr)}" x2="${r1(xr)}" y1="${r1(y0)}" y2="${r1(base + 12)}" stroke="${P.grid}"/>`)
    }
    if ((i === 0 || day !== Math.floor(bars[i - 1].started / DAY)) && cx(i) - lastLabel >= 34 && cx(i) <= right - 10) {
      out.push(text(Math.max(cx(i), left + 10), dateY, md(b.started), 9.5, { fill: P.ink3, anchor: 'middle' }))
      lastLabel = cx(i)
    }
  })
  out.push('<defs><pattern id="fail" width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
    + `<rect width="2" height="4" fill="${O.failed}"/></pattern></defs>`)
  bars.forEach((b, i) => {
    const x0 = cx(i) - bw / 2
    const top = Y(b.secs)
    const clipped = b.secs > hi
    if (mode === 'outcome') {
      out.push(`<rect x="${r1(x0)}" y="${r1(top)}" width="${r1(bw)}" height="${r1(base - top)}" fill="${O[b.outcome]}"${b.outcome === 'running' ? ' fill-opacity="0.55"' : ''}/>`)
    } else {
      // segments bottom-up, scaled to the bar's (possibly clipped) height
      const k = (base - top) / Math.max(1, b.segs.reduce((s, g) => s + g.secs, 0))
      let yb = base
      for (const s of b.segs) {
        const h = Math.max(s.failed ? 2 : 0, s.secs * k)
        if (h <= 0) continue
        out.push(`<rect x="${r1(x0)}" y="${r1(yb - h)}" width="${r1(bw)}" height="${r1(h)}" fill="${famColor(s.family, hued, c.theme)}"/>`)
        if (s.failed) out.push(`<rect x="${r1(x0)}" y="${r1(yb - h)}" width="${r1(bw)}" height="${r1(h)}" fill="url(#fail)"/>`)
        yb -= h
      }
    }
    // the outcome: a cap for an open issue; an outline (and cap) for a failure; dashed for a cancel or a run still going
    const capY = top - 4
    if (b.outcome === 'issues' || b.outcome === 'failed' || b.outcome === 'running') {
      out.push(`<rect x="${r1(x0 - 1)}" y="${r1(capY)}" width="${r1(bw + 2)}" height="2.6" rx="0.8" fill="${O[b.outcome]}"/>`)
    }
    if (mode === 'family' && b.outcome !== 'ok' && b.outcome !== 'issues') {
      const dash = b.outcome === 'failed' ? '' : ' stroke-dasharray="2 1.6"'
      out.push(`<rect x="${r1(x0 - 0.6)}" y="${r1(top - 0.6)}" width="${r1(bw + 1.2)}" height="${r1(base - top + 1.2)}" fill="none" stroke="${O[b.outcome]}" stroke-width="1.2"${dash}/>`)
    }
    if (clipped) {
      // a break across the bar, and its duration above it
      const by = y0 + 7
      out.push(`<path d="M${r1(x0 - 1.5)} ${r1(by + 1.5)}L${r1(x0 + bw + 1.5)} ${r1(by - 1.5)}M${r1(x0 - 1.5)} ${r1(by + 4.5)}L${r1(x0 + bw + 1.5)} ${r1(by + 1.5)}" stroke="${P.panel}" stroke-width="2"/>`)
      out.push(text(cx(i), y0 - 7, fmtDur(b.secs), 9, { fill: P.ink2, weight: 600, anchor: cx(i) > right - 14 ? 'end' : 'middle' }))
    }
    if (b.manual) out.push(person(cx(i), base + 3, P.ink3))
  })

  // the marks' key, under the dates
  y = dateY + 8
  const keys: [string, string][] = []
  let kx = PAD
  const key = (draw: string, label: string) => { keys.push([draw, label]) }
  if (mode === 'family') {
    if (bars.some(b => b.outcome === 'issues')) key(`<rect x="{x}" y="{y}" width="9" height="2.6" rx="0.8" fill="${O.issues}"/>`, 'open issue')
    if (bars.some(b => b.outcome === 'failed')) key(`<rect x="{x}" y="{y}" width="9" height="8" fill="none" stroke="${O.failed}" stroke-width="1.2"/>`, 'failed')
    if (bars.some(b => b.segs.some(s => s.failed))) key(`<rect x="{x}" y="{y}" width="9" height="8" fill="url(#fail)"/>`, 'step failed')
    if (bars.some(b => b.outcome === 'cancelled')) key(`<rect x="{x}" y="{y}" width="9" height="8" fill="none" stroke="${O.cancelled}" stroke-width="1.2" stroke-dasharray="2 1.6"/>`, 'cancelled')
    if (bars.some(b => b.outcome === 'running')) key(`<rect x="{x}" y="{y}" width="9" height="8" fill="none" stroke="${O.running}" stroke-width="1.2" stroke-dasharray="2 1.6"/>`, 'running')
  }
  if (bars.some(b => b.manual)) key('{person}', 'manual')
  // the times in the family key are the newest finished run's
  if (mode === 'family') key('', 'key times: newest run')
  for (const [draw, label] of keys) {
    const ky = y + 1
    if (draw) out.push(draw === '{person}' ? person(kx + 4.5, ky, P.ink3) : draw.replace('{x}', String(r1(kx))).replace('{y}', String(r1(ky))))
    const lx = draw ? kx + 13 : kx
    out.push(text(lx, y + 8.5, label, 9.5, { fill: P.ink3 }))
    kx = lx + interWidth(label, 9.5) + 12
  }
  if (keys.length) y += 14

  // the strip: 4 cells over 30 days, the last 7 shaded
  y += 6
  const metrics = stripModel(c)
  const gap = 6
  const cw = (W - 2 * PAD - 3 * gap) / 4
  const SH = 24
  const sx0 = c.now - STRIP_DAYS * DAY
  const line = c.theme === 'light' ? mix(c.color, '#000000', 0.3) : c.color
  metrics.forEach((m, i) => {
    const x0 = PAD + i * (cw + gap)
    out.push(`<rect x="${r1(x0)}" y="${r1(y)}" width="${r1(cw)}" height="${SH + 36}" rx="4" fill="${P.panel}"/>`)
    out.push(text(x0 + 6, y + 12, m.label, 9.5, { fill: P.ink3 }))
    out.push(text(x0 + 6, y + 28, m.value ?? 'n/a', 13.5, { fill: m.value ? P.ink : P.ink3, weight: 600 }))
    if (m.delta) out.push(text(x0 + cw - 5, y + 12, m.delta, 9.5, { fill: P.ink2, weight: 600, anchor: 'end' }))
    const sy = y + 32
    const X = (t: number) => x0 + 5 + ((t - sx0) / (c.now - sx0)) * (cw - 10)
    out.push(`<rect x="${r1(X(c.now - 7 * DAY))}" y="${r1(sy)}" width="${r1(X(c.now) - X(c.now - 7 * DAY))}" height="${SH}" fill="${mix(P.panel, P.ink, 0.06)}"/>`)
    if (m.pts.length) {
      const vs = m.pts.map(p => p[1])
      const [lo, hiV] = [Math.min(...vs), Math.max(...vs)]
      const Yv = (v: number) => sy + SH - 2 - (hiV > lo ? (v - lo) / (hiV - lo) : 0.5) * (SH - 4)
      const pts = m.pts.map(p => `${r1(X(p[0]))},${r1(Yv(p[1]))}`).join(' ')
      if (m.pts.length > 1) out.push(`<polyline points="${pts}" fill="none" stroke="${line}" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"/>`)
      const [lt, lv] = m.pts[m.pts.length - 1]
      out.push(`<circle cx="${r1(X(lt))}" cy="${r1(Yv(lv))}" r="2.2" fill="${line}"/>`)
    }
  })
  y += SH + 36 + 13
  out.push(text(PAD, y, '%: change over 7 days. $/run, run time: the median of the last 7 days’ clean runs', 9, { fill: P.ink3 }))
  const Hh = Math.ceil(y + PAD - 2)
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${Hh}" viewBox="0 0 ${W} ${Hh}" font-family="Inter">`
    + `<rect x="0.5" y="0.5" width="${W - 1}" height="${Hh - 1}" rx="8" fill="${P.bg}" stroke="${P.border}"/>${out.join('')}</svg>`
}
