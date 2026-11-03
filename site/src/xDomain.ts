// "Size over time"'s displayed x-range (`?xr=`), kept apart from the diff window it highlights: the chart can show
// months of context around a window of two scans, and zooming never moves the window. One state for every way in —
// the `1w` / `1m` / `all` presets, a brush on the overview strip, "fit to the diff window" — resolved against the
// series' own x's here, pure, so the chart and its tests read the same rules.

export const DAY_MS = 86_400_000

/** `all` = every scan; `last` = the N days before the latest scan (follows new scans); `span` = two scan ids. */
export type XDomain =
  | { kind: 'all' }
  | { kind: 'last'; days: number }
  | { kind: 'span'; from: string; to: string }

export type XPreset = '1w' | '1m' | 'all'
export const X_PRESETS: [XPreset, number | null][] = [['1w', 7], ['1m', 30], ['all', null]]

const ALL: XDomain = { kind: 'all' }
// A scan id: `YYYY-MM-DD`, optionally `THHMM` (`scanSlug.isScanId`'s shapes).
const SCAN_ID = /^\d{4}-\d{2}-\d{2}(T\d{4})?$/

/** `?xr=` → the domain: `1w` / `1m`, `<from>..<to>` (scan ids, either order), else every scan. */
export function parseXr(s: string | null | undefined): XDomain {
  if (!s) return ALL
  const preset = X_PRESETS.find(([p]) => p === s)
  if (preset) return preset[1] === null ? ALL : { kind: 'last', days: preset[1] }
  const m = s.split('..')
  if (m.length === 2 && SCAN_ID.test(m[0]) && SCAN_ID.test(m[1])) {
    const [from, to] = m[0] <= m[1] ? m : [m[1], m[0]]
    return { kind: 'span', from, to }
  }
  return ALL
}

/** The domain as `?xr=` (undefined = every scan, the default). */
export function fmtXr(d: XDomain): string | undefined {
  if (d.kind === 'all') return undefined
  if (d.kind === 'span') return `${d.from}..${d.to}`
  return X_PRESETS.find(([, days]) => days === d.days)?.[0] ?? undefined
}

/** The preset button the domain is, if any (a brushed span is none of them). */
export function presetOf(d: XDomain): XPreset | null {
  if (d.kind === 'all') return 'all'
  if (d.kind === 'span') return null
  return X_PRESETS.find(([, days]) => days === d.days)?.[0] ?? null
}

/**
 * The x-range `d` shows over the series' x's (`xs`, any order), or null = all of them. Clamped to the data, and
 * never narrower than two scans: a range holding fewer widens to the scan before / after it (a chart of one point
 * has no line and no x-axis). A range holding every scan is null — the chart is unzoomed whatever the state says.
 */
export function resolveXDomain(d: XDomain, xs: readonly number[], xOf: (id: string) => number): [number, number] | null {
  if (d.kind === 'all' || xs.length < 2) return null
  const s = [...xs].sort((a, b) => a - b)
  const first = s[0]
  const last = s[s.length - 1]
  let lo: number, hi: number
  if (d.kind === 'last') {
    lo = last - d.days * DAY_MS
    hi = last
  } else {
    lo = xOf(d.from)
    hi = xOf(d.to)
    if (!Number.isFinite(lo) || !Number.isFinite(hi)) return null
    if (lo > hi) [lo, hi] = [hi, lo]
  }
  lo = Math.max(lo, first)
  hi = Math.min(hi, last)
  let i0 = s.findIndex(x => x >= lo)
  if (i0 < 0) i0 = s.length - 1
  let i1 = -1
  for (let i = s.length - 1; i >= 0; i--) if (s[i] <= hi) { i1 = i; break }
  if (i1 < 0) i1 = 0
  // Fewer than two scans inside (or an empty range between two): widen to the nearest pair.
  if (i1 - i0 < 1) {
    const at = Math.min(i0, i1)
    if (at >= s.length - 1) { i0 = s.length - 2; i1 = s.length - 1 } else { i0 = at; i1 = at + 1 }
  }
  if (i0 === 0 && i1 === s.length - 1) return null
  return [s[i0], s[i1]]
}

/** Whether `x` is inside the resolved range (null = everything). */
export const inXDomain = (r: readonly [number, number] | null, x: number): boolean => !r || (x >= r[0] && x <= r[1])

/**
 * "Fit to the diff window": the scans `win` spans plus `ctx` scans of context each side, as the range to show
 * (both ends scan x's), or null when that is every scan. A window between scans snaps outward to the scans
 * around it.
 */
export function fitToWindow(win: readonly [number, number], xs: readonly number[], ctx = 1): [number, number] | null {
  if (xs.length < 2) return null
  const s = [...xs].sort((a, b) => a - b)
  const [a, b] = win[0] <= win[1] ? win : [win[1], win[0]]
  let i0 = s.findIndex(x => x >= a)
  if (i0 < 0) i0 = s.length - 1
  let i1 = s.length - 1
  for (let i = s.length - 1; i >= 0; i--) if (s[i] <= b) { i1 = i; break }
  // `a` past a scan: the window starts after it — still show the scan before
  if (s[i0] > a && i0 > 0) i0--
  if (s[i1] < b && i1 < s.length - 1) i1++
  i0 = Math.max(0, Math.min(i0, i1) - ctx)
  i1 = Math.min(s.length - 1, Math.max(i0 + 1, i1 + ctx))
  return i0 === 0 && i1 === s.length - 1 ? null : [s[i0], s[i1]]
}

/** A brushed / fitted x-range as a domain: a span of the scans at its ends (`idOf`), or every scan when it holds
 *  them all (so the URL stays bare). */
export function spanDomain(r: readonly [number, number] | null, idOf: (x: number) => string): XDomain {
  if (!r) return ALL
  const [a, b] = r[0] <= r[1] ? r : [r[1], r[0]]
  return { kind: 'span', from: idOf(a), to: idOf(b) }
}

/** A brush over the overview strip (`x0`, `x1` in data x, either order): each end snapped to the nearest scan, as a
 *  domain — null when the brush lands on one scan (a click, not a range), every scan when it holds them all. */
export function brushDomain(x0: number, x1: number, xs: readonly number[], idOf: (x: number) => string): XDomain | null {
  if (!xs.length) return null
  const snap = (x: number) => xs.reduce((b, v) => (Math.abs(v - x) < Math.abs(b - x) ? v : b), xs[0])
  const a = snap(Math.min(x0, x1))
  const b = snap(Math.max(x0, x1))
  if (a === b) return null
  const s = [...xs].sort((p, q) => p - q)
  return a === s[0] && b === s[s.length - 1] ? ALL : spanDomain([a, b], idOf)
}
