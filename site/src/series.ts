// Pure helpers for the store root's size-over-time (specs/done/root-geneses.md
// §2): per-root traces stacked into bands, and each root's genesis — the
// first scan its trace has a point for.

export interface Pt { x: number; y: number }
export interface Band extends Pt { y0: number }
export interface Trace { key: string; points: Pt[] }
export interface Callout { x: number; y: number; below: boolean }

/** Interior points that read as prominent peaks (`maxes`) / valleys (`mins`),
 * for size callouts. Ranks each by topographic **prominence** — its drop to the
 * highest saddle on the path to higher terrain (min-space: to lower terrain) —
 * not "tallest within a window". Prominence matches the eye's 2-D sense of how
 * much a point stands out: a sharp spike beside a taller-but-distant peak still
 * scores high (you must descend into the trough between them to reach the taller
 * one), where a literal 2-D disk would mislabel steep slopes. `r` (x-units) is a
 * non-max-suppression radius: of two peaks within `r`, only the more prominent
 * survives (valleys likewise; a peak never suppresses a valley). `minProm`
 * (y-units) is a prominence floor: a wobble smaller than it is not an extremum
 * worth a callout — callers pass a fraction of the trace's range. Endpoints and
 * global extremes are the caller's job. O(n·peaks), fine at ~hundreds of scans. */
export function prominentExtrema(pts: Pt[], r: number, minProm = 0): { maxes: Pt[]; mins: Pt[] } {
  // Peaks of `sign*y`: sign=1 finds maxima, sign=-1 finds minima.
  const peaks = (sign: number): Pt[] => {
    const v = (i: number) => sign * pts[i].y
    const cand: { i: number; prom: number }[] = []
    for (let i = 1; i < pts.length - 1; i++) {
      if (!(v(i) > v(i - 1) && v(i) > v(i + 1))) continue
      // Lowest saddle each way until terrain rises above this peak (or the end).
      let left = v(i)
      for (let j = i - 1; j >= 0 && v(j) <= v(i); j--) left = Math.min(left, v(j))
      let right = v(i)
      for (let j = i + 1; j < pts.length && v(j) <= v(i); j++) right = Math.min(right, v(j))
      const prom = v(i) - Math.max(left, right)
      if (prom >= minProm) cand.push({ i, prom })
    }
    cand.sort((a, b) => b.prom - a.prom)
    const kept: number[] = []
    for (const c of cand) {
      if (kept.some(k => Math.abs(pts[k].x - pts[c.i].x) < r)) continue
      kept.push(c.i)
    }
    return kept.sort((a, b) => a - b).map(i => pts[i])
  }
  return { maxes: peaks(1), mins: peaks(-1) }
}

/** Which points earn a size callout on the over-time line: the series' first &
 * last, its global min & max, and — when a range is selected (`winX` = the
 * window edges' x's) — the point nearest each edge, so a selection reads the
 * size at its own start and end. When `radius` > 0, prominent interior
 * peaks/valleys at that suppression radius (`prominentExtrema`) are added too,
 * those with prominence ≥ `minPromFrac` × the series' range (0 = every one).
 * Points coinciding in a role (first is also max; a window edge lands on the
 * min; a prominent peak is the global max) collapse to one callout, keeping the
 * earliest-assigned role's placement. `below` puts the label under the point
 * (lows) rather than above it (highs). Ordered by first assignment (max, min,
 * first, last, window edges, then prominent peaks, valleys) for a stable render. */
export function pickAnnotations(pts: Pt[], winX?: readonly [number, number], radius?: number, minPromFrac = 0): Callout[] {
  if (pts.length < 2) return []
  let lo = pts[0]
  let hi = pts[0]
  for (const p of pts) {
    if (p.y < lo.y) lo = p
    if (p.y > hi.y) hi = p
  }
  const mid = (lo.y + hi.y) / 2
  const picks = new Map<Pt, boolean>() // point → below?
  picks.set(hi, false)
  picks.set(lo, true)
  for (const p of [pts[0], pts[pts.length - 1]]) if (!picks.has(p)) picks.set(p, p.y < mid)
  if (winX) {
    const nearest = (x: number) => pts.reduce((b, p) => (Math.abs(p.x - x) < Math.abs(b.x - x) ? p : b), pts[0])
    for (const x of winX) {
      const p = nearest(x)
      if (!picks.has(p)) picks.set(p, p.y < mid)
    }
  }
  if (radius && radius > 0) {
    const { maxes, mins } = prominentExtrema(pts, radius, minPromFrac * (hi.y - lo.y))
    for (const p of maxes) if (!picks.has(p)) picks.set(p, false)
    for (const p of mins) if (!picks.has(p)) picks.set(p, true)
  }
  return [...picks].map(([p, below]) => ({ x: p.x, y: p.y, below }))
}

/** Each trace's genesis x (its first point). */
export function geneses(traces: Trace[]): Map<string, number> {
  return new Map(traces.filter(t => t.points.length).map(t => [t.key, Math.min(...t.points.map(p => p.x))]))
}

/** The latest genesis among the traces (the x before which the total is
 * missing a root), or null with fewer than two traces. */
export function youngestGenesis(traces: Trace[]): number | null {
  const g = [...geneses(traces).values()]
  return g.length > 1 ? Math.max(...g) : null
}

/** Stack traces in order: each band runs from the running sum below it to
 * the sum including it, at every x any trace has. A trace without a point
 * at an x contributes 0 there (and gets no band point before its genesis). */
export function stackSeries(traces: Trace[]): { key: string; points: Band[] }[] {
  const xs = [...new Set(traces.flatMap(t => t.points.map(p => p.x)))].sort((a, b) => a - b)
  const running = new Map<number, number>(xs.map(x => [x, 0]))
  return traces.map(t => {
    const at = new Map(t.points.map(p => [p.x, p.y]))
    const g = t.points.length ? Math.min(...t.points.map(p => p.x)) : Infinity
    const points: Band[] = []
    for (const x of xs) {
      if (x < g) continue
      const y0 = running.get(x)!
      const y = y0 + (at.get(x) ?? 0)
      running.set(x, y)
      points.push({ x, y0, y })
    }
    return { key: t.key, points }
  })
}

/** Each trace relative to its own first point: `delta` = bytes gained since
 * then (signed), `pct` = that as a fraction of the start (a root that began at
 * 0 reads 0 throughout — there is no "percent of nothing"). Puts roots of very
 * different sizes on one comparable axis: the question is "what moved", not
 * "how big". */
export function relativeSeries(traces: Trace[], mode: 'delta' | 'pct'): Trace[] {
  return traces.map(t => {
    const pts = [...t.points].sort((a, b) => a.x - b.x)
    const ref = pts[0]?.y ?? 0
    return {
      key: t.key,
      points: pts.map(p => ({ x: p.x, y: mode === 'delta' ? p.y - ref : ref === 0 ? 0 : (p.y - ref) / ref })),
    }
  })
}

export interface BandCallout extends Callout { y0: number; h: number }

/** Callouts inside a stacked band, of the band's own height (the root's size,
 * not the stack's running sum): the same roles as `pickAnnotations` applied to
 * the height series, each placed at the band's `[y0, y]` at that x, `h` the
 * height to label. */
export function bandCallouts(band: Band[], radius?: number, minPromFrac = 0): BandCallout[] {
  const at = new Map(band.map(b => [b.x, b]))
  const heights = band.map(b => ({ x: b.x, y: b.y - b.y0 }))
  return pickAnnotations(heights, undefined, radius, minPromFrac).map(c => {
    const b = at.get(c.x)!
    return { x: c.x, y: b.y, y0: b.y0, h: c.y, below: c.below }
  })
}

/** Nice y-ticks aligned to a display unit: a base-10-nice byte value (1e15) is
 * an ugly binary label (909 TiB), so nice-tick in the unit's own base (1024 for
 * IEC → 1024/2048/3072 TiB; 1000 for SI → round TB/PB). `min` > 0 = a fitted
 * axis; `min` < 0 = a signed axis (Δ traces): ticks cover [min, max] at one
 * unit-nice step either side of 0. */
export function unitTicks(min: number, max: number, base: number, count = 4): number[] {
  const big = Math.max(Math.abs(min), Math.abs(max))
  if (big <= 0) return [0]
  const span = Math.max(max - min, big * 1e-6)
  const scale = base ** Math.floor(Math.log(big) / Math.log(base))
  const rawStep = span / scale / count
  const mag = 10 ** Math.floor(Math.log10(rawStep))
  const norm = rawStep / mag
  const step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag * scale
  const out: number[] = []
  // Integer multiples of the step (not a running sum): 0 lands exactly on a
  // signed axis, and no float drift creeps into the labels.
  for (let k = Math.ceil(min / step); k * step <= max + step / 100; k++) out.push(k * step)
  return out
}
