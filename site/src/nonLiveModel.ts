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
