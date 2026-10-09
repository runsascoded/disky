export interface ProgressSample {
  bucket: string
  ts: number
  deletes: number
  bytes: number
  gone: number
  overwritten: number
  failed: number
  done: number
  bytes_exact: number
}

export interface HistoryPoint { ts: number; objects: number; bytes: number; rate: number | null; byteRate: number | null }

/** Carry each bucket's last cumulative count forward. A bucket's first
 * sample establishes its baseline, not an inferred rate from run creation.
 * Gaps over 3 minutes and counter resets are not joined by invented rates. */
export function historyPoints(samples: readonly ProgressSample[]): HistoryPoint[] {
  const latest = new Map<string, ProgressSample>()
  const rates = new Map<string, number | null>()
  const byteRates = new Map<string, number | null>()
  const groups = new Map<number, ProgressSample[]>()
  for (const s of [...samples].sort((a, b) => a.ts - b.ts || a.bucket.localeCompare(b.bucket))) groups.set(s.ts, [...(groups.get(s.ts) ?? []), s])
  const out: HistoryPoint[] = []
  for (const [ts, rows] of groups) {
    let rate = 0
    let byteRate = 0
    let measured = false
    let bytesMeasured = false
    for (const s of rows) {
      const prev = latest.get(s.bucket)
      const dt = prev ? ts - prev.ts : 0
      rates.set(s.bucket, prev && dt > 0 && dt <= 180 && s.deletes >= prev.deletes ? (s.deletes - prev.deletes) / dt : null)
      byteRates.set(s.bucket, prev && dt > 0 && dt <= 180 && s.bytes >= prev.bytes && s.deletes >= prev.deletes ? (s.bytes - prev.bytes) / dt : null)
      latest.set(s.bucket, s)
    }
    let objects = 0
    let bytes = 0
    for (const [bucket, s] of latest) {
      objects += s.deletes
      bytes += s.bytes
      const r = rates.get(bucket)
      // Keep a finished bucket's final interval only at that sample's time.
      if (r != null && ts - s.ts <= 60 && (!s.done || s.ts === ts)) { measured = true; rate += r }
      const br = byteRates.get(bucket)
      if (br != null && ts - s.ts <= 60 && (!s.done || s.ts === ts)) { bytesMeasured = true; byteRate += br }
    }
    out.push({ ts, objects, bytes, rate: measured ? rate : null, byteRate: bytesMeasured ? byteRate : null })
  }
  return out
}

export const runHref = (id: string, section?: string): string => `/runs/${encodeURIComponent(id)}${section ? `#${section}` : ''}`
export const batchAnchor = (id: number | null): string => `batch-${id ?? 'earlier'}`
