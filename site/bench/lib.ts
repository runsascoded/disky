import type { PerfEntry, Widget } from '../src/perf.ts'

// The bench's pure half (specs/render-bench.md §2.3): what `bench.ts` writes,
// how a file's loads fold to per-(widget, key) quantiles, how two files diff,
// and the text tables. No Playwright here, so it unit-tests (`lib.test.ts`).

export const COLS = ['wait', 'decode', 'paint', 'settle', 'total'] as const
export type Col = (typeof COLS)[number]
export const WIDGET_ORDER: readonly Widget[] = ['treemap', 'table', 'dtm', 'dtable', 'series', 'age']

export interface RunResult {
  path: string
  /** 1-based run index. */
  run: number
  /** Wall ms from navigation to the last widget's `settled`. */
  ms: number
  /** The wait for `settled` gave up: some loads never finished (listed in `open`). */
  timedOut: boolean
  /** `widget:key` of the loads still open when the run ended. */
  open: string[]
  entries: PerfEntry[]
}

export interface BenchFile {
  stamp: string
  base: string
  paths: string[]
  runs: number
  cold: boolean
  viewport: { width: number; height: number }
  /** Free text from `--note` (what `base` really was, e.g. a local preview proxied to a deploy). */
  note?: string
  results: RunResult[]
}

export interface Q { p50: number; p95: number; n: number }

export interface Agg {
  widget: Widget
  key: string
  /** Loads folded (one per run that had this (widget, key)). */
  n: number
  phases: Partial<Record<Col, Q>>
  server: Record<string, Q>
  /** Cache tiers seen, counted (`{ hit: 2, miss: 1 }`). */
  cache: Record<string, number>
  failed: number
}

/** Linear-interpolated quantile of `xs` (numpy's default); NaN on empty. */
export function quantile(xs: readonly number[], q: number): number {
  if (!xs.length) return NaN
  const s = [...xs].sort((a, b) => a - b)
  const pos = (s.length - 1) * q
  const lo = Math.floor(pos), hi = Math.ceil(pos)
  return lo === hi ? s[lo] : s[lo] + (s[hi] - s[lo]) * (pos - lo)
}

const r1 = (x: number) => Math.round(x * 10) / 10
const qs = (xs: number[]): Q => ({ p50: r1(quantile(xs, 0.5)), p95: r1(quantile(xs, 0.95)), n: xs.length })

/** Fold every run's entries by (widget, key), in widget order then key. */
export function aggregate(results: readonly RunResult[]): Agg[] {
  const groups = new Map<string, PerfEntry[]>()
  for (const r of results) for (const e of r.entries) {
    const k = `${e.widget}\0${e.key}`
    const g = groups.get(k)
    if (g) g.push(e); else groups.set(k, [e])
  }
  const out: Agg[] = []
  for (const es of groups.values()) {
    const { widget, key } = es[0]
    const phases: Partial<Record<Col, Q>> = {}
    for (const c of COLS) {
      const xs = es.map(e => e[c]).filter((x): x is number => x != null)
      if (xs.length) phases[c] = qs(xs)
    }
    const serverNames = [...new Set(es.flatMap(e => Object.keys(e.server)))]
    const server: Record<string, Q> = {}
    for (const name of serverNames) server[name] = qs(es.map(e => e.server[name]).filter((x): x is number => x != null))
    const cache: Record<string, number> = {}
    for (const e of es) if (e.cache) cache[e.cache] = (cache[e.cache] ?? 0) + 1
    out.push({ widget, key, n: es.length, phases, server, cache, failed: es.filter(e => e.failed).length })
  }
  return out.sort((a, b) => WIDGET_ORDER.indexOf(a.widget) - WIDGET_ORDER.indexOf(b.widget) || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0))
}

/** Pad `rows` into columns; `align[i]` is `l` (default) or `r`. */
export function fmtTable(rows: readonly (readonly string[])[], align: readonly ('l' | 'r')[] = []): string {
  const ncol = Math.max(0, ...rows.map(r => r.length))
  const w = Array.from({ length: ncol }, (_, i) => Math.max(0, ...rows.map(r => (r[i] ?? '').length)))
  return rows.map(r => Array.from({ length: ncol }, (_, i) => {
    const cell = r[i] ?? ''
    return align[i] === 'r' ? cell.padStart(w[i]) : cell.padEnd(w[i])
  }).join('  ').replace(/\s+$/, '')).join('\n')
}

const fmtQ = (q: Q | undefined): string => (q ? `${q.p50}/${q.p95}` : '–')
const fmtServer = (s: Record<string, Q>): string => Object.entries(s).map(([k, q]) => `${k} ${q.p50}`).join(' · ')
const fmtCache = (c: Record<string, number>): string => Object.entries(c).map(([k, n]) => `${k}×${n}`).join(' ')

/** One table per widget: p50/p95 per phase (ms), the server phases' p50, cache tiers. */
export function renderAggs(aggs: readonly Agg[]): string {
  const blocks: string[] = []
  for (const w of WIDGET_ORDER) {
    const rows = aggs.filter(a => a.widget === w)
    if (!rows.length) continue
    const table = [
      ['key', 'n', ...COLS.map(c => `${c} p50/p95`), 'cache', 'server p50'],
      ...rows.map(a => [a.key, `${a.n}${a.failed ? ` (${a.failed} failed)` : ''}`, ...COLS.map(c => fmtQ(a.phases[c])), fmtCache(a.cache), fmtServer(a.server)]),
    ]
    blocks.push(`## ${w}\n${fmtTable(table, ['l', 'r', 'r', 'r', 'r', 'r', 'r', 'l', 'l'])}`)
  }
  return blocks.join('\n\n')
}

export interface Delta {
  widget: Widget
  key: string
  /** A phase column, or `server:<name>`. */
  phase: string
  a: number | null
  b: number | null
  /** `b − a` ms (p50), null unless both sides have the phase. */
  d: number | null
  /** `d / a` as a percentage, null when `a` is 0 or missing. */
  pct: number | null
}

/** Per (widget, key, phase): p50 on each side and the change from `a` to `b`. */
export function diffAggs(a: readonly Agg[], b: readonly Agg[]): Delta[] {
  const byKey = (xs: readonly Agg[]) => new Map(xs.map(x => [`${x.widget}\0${x.key}`, x]))
  const ma = byKey(a), mb = byKey(b)
  const keys = [...new Set([...ma.keys(), ...mb.keys()])].sort((x, y) => {
    const [wx, kx] = x.split('\0'), [wy, ky] = y.split('\0')
    return WIDGET_ORDER.indexOf(wx as Widget) - WIDGET_ORDER.indexOf(wy as Widget) || (kx < ky ? -1 : kx > ky ? 1 : 0)
  })
  const out: Delta[] = []
  for (const k of keys) {
    const [widget, key] = k.split('\0') as [Widget, string]
    const xa = ma.get(k), xb = mb.get(k)
    const phases: string[] = [...COLS, ...new Set([...Object.keys(xa?.server ?? {}), ...Object.keys(xb?.server ?? {})].map(s => `server:${s}`))]
    for (const phase of phases) {
      const pick = (x: Agg | undefined): number | null => {
        if (!x) return null
        const q = phase.startsWith('server:') ? x.server[phase.slice(7)] : x.phases[phase as Col]
        return q ? q.p50 : null
      }
      const pa = pick(xa), pb = pick(xb)
      if (pa == null && pb == null) continue
      const d = pa != null && pb != null ? r1(pb - pa) : null
      const pct = d != null && pa !== 0 ? r1((100 * d) / pa!) : null
      out.push({ widget, key, phase, a: pa, b: pb, d, pct })
    }
  }
  return out
}

const fmtN = (x: number | null): string => (x == null ? '–' : `${x}`)
const fmtD = (x: number | null): string => (x == null ? '–' : x > 0 ? `+${x}` : `${x}`)

export function renderDiff(ds: readonly Delta[]): string {
  const blocks: string[] = []
  for (const w of WIDGET_ORDER) {
    const rows = ds.filter(d => d.widget === w)
    if (!rows.length) continue
    const table = [['key', 'phase', 'a p50', 'b p50', 'Δ ms', 'Δ %'], ...rows.map(d => [d.key, d.phase, fmtN(d.a), fmtN(d.b), fmtD(d.d), d.pct == null ? '–' : `${fmtD(d.pct)}%`])]
    blocks.push(`## ${w}\n${fmtTable(table, ['l', 'l', 'r', 'r', 'r', 'r'])}`)
  }
  return blocks.join('\n\n')
}
