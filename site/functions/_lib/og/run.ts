/** A deletion run's card (`/runs/<id>`, `/og/run.png`): its state, done / total, the delete rate over the run and an
 * as-of time. A public unfurl: counts, rates, times and a bucket count, and nothing else (no prefixes, names, actors
 * or bytes), so a run's card has no labelled tier. Pure and DOM-free; `runData.ts` does the reads. */
import { historyPoints, type HistoryPoint, type ProgressSample } from '../../../src/runHistory.js'
import type { ProgressFile } from '../../../src/runs.js'
import { BG, CARD_H, CARD_W, clip, esc, GRID_LINE, INK, MUTED, r1, SERIES_COLOR, text } from './card.js'

const { floor, max, min, round } = Math

/** What a run's card reads of its `deletion_runs` row. */
export interface RunRow {
  mode: 'dry' | 'real'
  scan: string
  started_ts: number
  finished_ts: number | null
  deleted_objects: number
  /** The `-b` cut, comma-separated (null = every bucket of the plan). */
  buckets: string | null
  log_dir: string
}

/** No progress for this long (s): an unfinished run says it stalled, as it never gets a `finished_ts` if its job died. */
export const STALL_S = 1800
/** A running run's image version (and how long a fetcher may keep it): one per this many seconds of samples. */
export const RUNNING_TTL_S = 300
const FINISHED_TTL_S = 30 * 86400
/** Drawn points; a longer run's are averaged into this many even time bins. */
export const MAX_POINTS = 240
/** Progress files read for one card; `deletion_progress`'s newest sample stands in for any bucket past it. */
export const MAX_FILES = 24
/** The Runs table's rule (`RunProgress.tsx`): a gap longer than this between samples isn't joined. */
const GAP_S = 180

const ID = /^[A-Za-z0-9][A-Za-z0-9._:-]*(?:\/[A-Za-z0-9][A-Za-z0-9._:-]*)*$/
/** A run id as the executor writes them (`<plan>/<stamp>`): anything else is never looked up. */
export const validRunId = (id: string): boolean => id.length <= 128 && ID.test(id)

/** The run a `/runs/<id>` pathname names; null for none or one that doesn't decode. */
export function runIdOfPath(pathname: string): string | null {
  try {
    const id = decodeURIComponent(pathname.replace(/^\/runs\/?/, ''))
    return validRunId(id) ? id : null
  } catch {
    return null
  }
}

/** The `-b` cut's buckets, usable as object-key segments. */
export const runBuckets = (buckets: string | null): string[] => (buckets ?? '').split(',').filter(b => /^[a-z0-9][a-z0-9._-]*$/.test(b))

/** The card's version: final once finished; while running, its newest sample's bucket of time. A run with no fresh
 * sample rolls with the clock instead, so "planning" or "stalled" still reaches a new image and a late file is seen. */
export function runVersion(run: Pick<RunRow, 'finished_ts'>, newest: number | null, now: number): string {
  if (run.finished_ts != null) return `f${run.finished_ts}`
  const fresh = newest != null && now - newest <= STALL_S
  return `r${floor((fresh ? newest : now) / RUNNING_TTL_S)}`
}

export const runMaxAge = (run: Pick<RunRow, 'finished_ts'>): number => run.finished_ts != null ? FINISHED_TTL_S : RUNNING_TTL_S

/** Bump when the drawing changes: a finished run's URL is otherwise the same image for its whole cache life. */
const RUN_CARD_V = '2'

/** The card's URL for a run at a version. */
export const runImagePath = ({ id, version }: { id: string; version: string }): string =>
  `/og/run.png?id=${encodeURIComponent(id)}&v=${version}&c=${RUN_CARD_V}`

export type RunState = 'planning' | 'running' | 'stalled' | 'finished'

/** What the card and the page's description say of a run. */
export interface RunShape {
  mode: 'dry' | 'real'
  state: RunState
  started_ts: number
  /** Objects attempted (a dry run's: that would be deleted); null before any progress. */
  done: number | null
  /** Objects planned (`plan-summary.json`); null until the manifest lands, or if it can't be read. */
  total: number | null
  /** Objects with no definitive answer after every retry. */
  unanswered: number
  n_buckets: number
  /** The newest progress sample's time (s); a finished run with none, its finish; null before any progress. */
  as_of: number | null
  /** `[ts, objects/s]`, ascending; a null rate is a gap. */
  series: [number, number | null][]
  peak: number | null
  /** The newest measured rate. */
  latest: number | null
  /** Objects per second between the first and newest samples. */
  avg: number | null
}

/** `points` as at most `limit` `[ts, rate]`s: as they are when they fit, else each even time bin's mean rate (a bin with
 * only gaps stays a gap; an empty one is left out). */
export function rateSeries(points: readonly HistoryPoint[], limit = MAX_POINTS): [number, number | null][] {
  const tenth = (v: number) => round(v * 10) / 10
  if (points.length <= limit) return points.map(p => [round(p.ts), p.rate == null ? null : tenth(p.rate)])
  const t0 = points[0].ts
  const width = (points[points.length - 1].ts - t0) / limit
  const bins = new Map<number, { ts: number; n: number; rates: number; measured: number }>()
  for (const p of points) {
    const i = min(limit - 1, floor((p.ts - t0) / width))
    const b = bins.get(i) ?? { ts: 0, n: 0, rates: 0, measured: 0 }
    b.ts += p.ts
    b.n++
    if (p.rate != null) { b.rates += p.rate; b.measured++ }
    bins.set(i, b)
  }
  return [...bins.values()].map(b => [round(b.ts / b.n), b.measured ? tenth(b.rates / b.measured) : null])
}

/** A run's card, from its row, planned object count, progress files (running only) and rate samples. Each bucket counts
 * its newer of file and sample; `deleted_objects` is only written at the finish. */
export function shapeRun(run: RunRow, total: number | null, files: ReadonlyMap<string, ProgressFile>, samples: readonly ProgressSample[], planBuckets: readonly string[], now: number): RunShape {
  const buckets = new Map<string, { ts: number; deletes: number; failed: number }>()
  for (const s of samples) {
    const b = buckets.get(s.bucket)
    if (!b || s.ts >= b.ts) buckets.set(s.bucket, { ts: s.ts, deletes: s.deletes, failed: s.failed })
  }
  for (const [bucket, f] of files) {
    const ts = f.updated ? Date.parse(f.updated) / 1000 : NaN
    const b = buckets.get(bucket)
    if (Number.isFinite(ts) && (!b || ts >= b.ts)) buckets.set(bucket, { ts, deletes: f.decisions.delete ?? 0, failed: f.decisions.delete_failed ?? 0 })
  }
  const seen = [...buckets.values()]
  const newest = seen.length ? max(...seen.map(b => b.ts)) : null
  const finished = run.finished_ts != null
  const as_of = newest ?? run.finished_ts
  const quiet = now - (newest ?? run.started_ts) > STALL_S
  const points = historyPoints(samples)
  const rates = points.flatMap(p => p.rate == null ? [] : [p.rate])
  const first = points[0], last = points[points.length - 1]
  const named = runBuckets(run.buckets)
  return {
    mode: run.mode,
    state: finished ? 'finished' : quiet ? 'stalled' : seen.length ? 'running' : 'planning',
    started_ts: run.started_ts,
    done: finished ? run.deleted_objects : seen.length ? seen.reduce((n, b) => n + b.deletes, 0) : null,
    total: total && total > 0 ? total : null,
    unanswered: seen.reduce((n, b) => n + b.failed, 0),
    n_buckets: named.length || planBuckets.length,
    as_of: as_of == null ? null : round(as_of),
    series: rateSeries(points),
    peak: rates.length ? max(...rates) : null,
    latest: rates.length ? rates[rates.length - 1] : null,
    avg: first && last.ts > first.ts ? (last.objects - first.objects) / (last.ts - first.ts) : null,
  }
}

export const runTitle = ({ mode, state }: Pick<RunShape, 'mode' | 'state'>): string => `${mode === 'dry' ? 'Dry run' : 'Deletion'} ${state}`

/** XML deletes acknowledge every generation, so a real run's count is what it attempted, not what's verified gone. */
export const doneVerb = (mode: RunShape['mode']): string => mode === 'dry' ? 'would delete' : 'attempted'

const fmtN = (n: number): string => n.toLocaleString('en-US')
export const utcMinute = (s: number): string => `${new Date(s * 1000).toISOString().slice(0, 16).replace('T', ' ')} UTC`
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
/** A chart end's label (`Nov 4 12:53`, UTC as the as-of line says). */
function tick(s: number): string {
  const d = new Date(s * 1000)
  return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()} ${d.toISOString().slice(11, 16)}`
}

/** `done` of `total` as the card's percent, floored to a tenth: unfinished never reads 100%. */
export function percent(done: number, total: number): string {
  return `${(floor(min(1, done / total) * 1000) / 10).toFixed(1)}%`
}

/** The rate as a filled line in the box, broken at gaps: the Runs table's trace, its whole run. */
function sparkSvg(series: RunShape['series'], x: number, y: number, w: number, h: number): string {
  const t0 = series[0][0], t1 = series[series.length - 1][0]
  const X = (t: number) => t1 > t0 ? x + (t - t0) / (t1 - t0) * w : x + w / 2
  // Scaled to what's drawn: a long run's bins average below its raw peak.
  const top = max(...series.map(p => p[1] ?? 0)) || 1
  const Y = (v: number) => y + h - v / top * h
  // Sparse bins still join: a gap is wider than the samples or bins around it.
  const gap = max(GAP_S, 2 * (t1 - t0) / MAX_POINTS)
  const runs: [number, number][][] = []
  let cur: [number, number][] = []
  let prev: number | null = null
  for (const [t, v] of series) {
    if (v == null || (prev != null && t - prev > gap)) { if (cur.length) runs.push(cur); cur = [] }
    if (v != null) cur.push([t, v])
    prev = t
  }
  if (cur.length) runs.push(cur)
  const out = [
    `<line class="grid" x1="${x}" y1="${r1(y)}" x2="${x + w}" y2="${r1(y)}" stroke="${GRID_LINE}" stroke-width="1"/>`,
    `<line class="grid" x1="${x}" y1="${r1(y + h)}" x2="${x + w}" y2="${r1(y + h)}" stroke="${GRID_LINE}" stroke-width="2"/>`,
  ]
  for (const run of runs) {
    if (run.length === 1) {
      out.push(`<circle class="pt" cx="${r1(X(run[0][0]))}" cy="${r1(Y(run[0][1]))}" r="4" fill="${SERIES_COLOR}"/>`)
      continue
    }
    const line = run.map(([t, v], i) => `${i ? 'L' : 'M'}${r1(X(t))},${r1(Y(v))}`).join('')
    out.push(`<path class="area" d="${line}L${r1(X(run[run.length - 1][0]))},${r1(y + h)}L${r1(X(run[0][0]))},${r1(y + h)}Z" fill="${SERIES_COLOR}" fill-opacity="0.25"/>`)
    out.push(`<path class="line" d="${line}" fill="none" stroke="${SERIES_COLOR}" stroke-width="4" stroke-linejoin="round"/>`)
  }
  return out.join('')
}

/** The card: what the run is; done / total with a progress bar; the delete rate over the run; as-of and bucket count.
 * Type sized to survive a phone's ~⅓ scale. */
export function runCardSvg(s: RunShape, site: string): string {
  const [L, W] = [40, CARD_W - 80]
  const out: string[] = []
  out.push(`<svg xmlns="http://www.w3.org/2000/svg" width="${CARD_W}" height="${CARD_H}" viewBox="0 0 ${CARD_W} ${CARD_H}">`)
  out.push(`<rect width="${CARD_W}" height="${CARD_H}" fill="${BG}"/>`)
  out.push(text(L, 50, clip(site, W, 28), 28, { fill: MUTED, weight: 600 }))
  out.push(text(L, 106, runTitle(s), 50, { weight: 600 }))
  // done / total
  const rest = s.done == null ? '' : `${s.total != null ? ` / ${fmtN(s.total)}` : ''} ${doneVerb(s.mode)}`
  if (s.done == null) out.push(text(L, 182, s.total != null ? `${fmtN(s.total)} objects planned` : 'no manifest yet', 44, { fill: MUTED, weight: 600 }))
  else out.push(`<text class="done" x="${L}" y="182" font-family="Inter"><tspan font-size="58" font-weight="600" fill="${INK}">${fmtN(s.done)}</tspan><tspan font-size="36" fill="${MUTED}">${esc(rest)}</tspan></text>`)
  if (s.done != null && s.total != null) out.push(text(L + W, 182, percent(s.done, s.total), 58, { weight: 600, anchor: 'end' }))
  const fill = s.done != null && s.total != null ? min(1, s.done / s.total) * W : 0
  out.push(`<rect class="track" x="${L}" y="202" width="${W}" height="30" rx="6" fill="#20232a"/>`)
  if (fill > 0) out.push(`<rect class="bar" x="${L}" y="202" width="${r1(max(fill, 8))}" height="30" rx="6" fill="${SERIES_COLOR}"/>`)
  // the rate
  const [cy, ch] = [296, 216]
  const measured = s.peak != null && s.series.some(p => p[1] != null)
  if (measured) {
    const stat = s.state === 'finished' || s.latest == null ? (s.avg != null ? `avg ${fmtN(round(s.avg))}` : null) : `latest ${fmtN(round(s.latest))}`
    out.push(text(L, 280, 'objects/s', 30, { fill: MUTED }))
    out.push(text(L + W, 280, [stat, `peak ${fmtN(round(s.peak!))}`].filter(Boolean).join(' · '), 30, { fill: MUTED, anchor: 'end' }))
    out.push(sparkSvg(s.series, L, cy, W, ch))
    const ends = [s.series[0][0], s.series[s.series.length - 1][0]]
    out.push(text(L, cy + ch + 34, tick(ends[0]), 28, { fill: MUTED }))
    if (ends[1] > ends[0]) out.push(text(L + W, cy + ch + 34, tick(ends[1]), 28, { fill: MUTED, anchor: 'end' }))
  } else {
    out.push(`<rect class="none" x="${L}" y="252" width="${W}" height="${cy + ch - 252}" rx="6" fill="#20232a"/>`)
    out.push(text(CARD_W / 2, (252 + cy + ch) / 2 + 12, s.done == null ? 'no progress yet' : 'no rate history recorded', 36, { fill: MUTED, anchor: 'middle' }))
  }
  // as of · buckets
  const when = s.as_of != null ? `as of ${utcMinute(s.as_of)}` : `started ${utcMinute(s.started_ts)}`
  out.push(text(L, 600, when, 34, { weight: 600 }))
  const counts = [s.n_buckets ? `${fmtN(s.n_buckets)} ${s.n_buckets === 1 ? 'bucket' : 'buckets'}` : '', s.unanswered ? `${fmtN(s.unanswered)} unanswered` : ''].filter(Boolean)
  out.push(text(L + W, 600, counts.join(' · '), 34, { fill: MUTED, anchor: 'end' }))
  out.push('</svg>')
  return out.join('')
}
