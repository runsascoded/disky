// A scan's non-live bytes (`<store.base>/<scan>/non-live.json`, the scan step `non-live`; specs/non-live-bytes.md):
// what the store holds, and usually bills, that the map of current objects doesn't show. Phase 1: per-bucket totals,
// summed here per class for the root view's line.

/** One `(bucket, class)` row as the scan step writes it (times ISO 8601 UTC, null where unknown). */
export interface NonLiveRow {
  bucket: string
  class: NonLiveClass
  prefix: string
  bytes: number
  objects: number
  oldest: string | null
  newest: string | null
  expires_min: string | null
  expires_max: string | null
}

export type NonLiveClass = 'soft-deleted' | 'noncurrent' | 'delete-marker' | 'mpu'

/** What each class is called on the page, in this order. */
export const CLASS_LABELS: Record<NonLiveClass, string> = {
  'soft-deleted': 'soft-deleted',
  noncurrent: 'old versions',
  'delete-marker': 'delete markers',
  mpu: 'incomplete uploads',
}

/** One class across the store's buckets: its bytes and objects, the buckets holding any, and when the last of it
 *  expires (null: some of it has no expiry known, or none does). */
export interface ClassTotal { cls: NonLiveClass; bytes: number; objects: number; buckets: string[]; expires: string | null }

/** The classes with anything in them, in `CLASS_LABELS` order. */
export function classTotals(rows: readonly NonLiveRow[]): ClassTotal[] {
  return (Object.keys(CLASS_LABELS) as NonLiveClass[]).flatMap(cls => {
    const rs = rows.filter(r => r.class === cls && r.objects > 0)
    if (!rs.length) return []
    const known = rs.every(r => r.expires_max != null)
    return [{
      cls,
      bytes: rs.reduce((s, r) => s + r.bytes, 0),
      objects: rs.reduce((s, r) => s + r.objects, 0),
      buckets: rs.map(r => r.bucket),
      expires: known ? rs.map(r => r.expires_max!).sort().at(-1)! : null,
    }]
  })
}

// The store's history (`/api/non-live`, the scan step `non-live` with `source: metric`): Cloud Monitoring's daily bucket
// totals, `live` beside each non-live class.

export type SeriesClass = 'live' | NonLiveClass

/** `/api/non-live`'s `series`: per bucket and class, one byte total per day of `dates` (null: the metric had nothing). */
export interface NonLiveSeries {
  as_of: string
  metric: string
  dates: string[]
  buckets: Record<string, Partial<Record<SeriesClass, (number | null)[]>>>
}

export interface BandPoint { x: number; y0: number; y: number }
export interface Band { cls: SeriesClass; points: BandPoint[] }

const DAY_MS = 86_400_000

/** The buckets' bytes per day, summed and stacked: `live` at the bottom, then each non-live class holding any bytes
 *  (`CLASS_LABELS` order). A point's x is its day's midnight UTC. Days before any bucket has data are dropped; a later
 *  day with none is a gap (no point in any band). */
export function stackedBands(s: NonLiveSeries): { bands: Band[]; gaps: number[] } {
  const buckets = Object.values(s.buckets)
  const sumOf = (cls: SeriesClass, i: number): number | null => {
    let tot: number | null = null
    for (const b of buckets) {
      const v = b[cls]?.[i]
      if (v != null) tot = (tot ?? 0) + v
      else if (b.live?.[i] != null) tot = tot ?? 0
    }
    return tot
  }
  const classes = (['live', ...Object.keys(CLASS_LABELS)] as SeriesClass[])
    .filter(c => buckets.some(b => b[c]?.some(v => !!v)))
  const bands: Band[] = classes.map(cls => ({ cls, points: [] }))
  const gaps: number[] = []
  let started = false
  s.dates.forEach((d, i) => {
    const x = Date.parse(`${d}T00:00:00Z`)
    const vals = classes.map(c => sumOf(c, i))
    if (vals.every(v => v == null)) {
      if (started) gaps.push(x)
      return
    }
    started = true
    let y0 = 0
    vals.forEach((v, k) => {
      const y = y0 + (v ?? 0)
      bands[k].points.push({ x, y0, y })
      y0 = y
    })
  })
  return { bands, gaps: gaps.filter(x => x < (bands[0]?.points.at(-1)?.x ?? -Infinity) + DAY_MS) }
}
