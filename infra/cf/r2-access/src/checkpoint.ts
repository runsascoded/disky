/** Optional consumer receipts bound collector bookkeeping. No aggregate object is deleted here. */
export interface ClosedHistory { through: number; checkpoint: string }
interface History { frozen: Record<string, string>; closed?: ClosedHistory; legacy_days?: Record<string, string> }
interface Receipt {
  schema_version: 1
  kind: 'r2-read-checkpoint'
  source: string
  account: string
  checkpoint: { uri: string; generation: string; sha256: string }
  cursors: Record<string, number>
}

export function checkpointReceipt(raw: unknown, account: string, source: string, checkpoint: string): Receipt | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const r = raw as Receipt
  if (r.schema_version !== 1 || r.kind !== 'r2-read-checkpoint' || r.account !== account || r.source !== source
      || r.checkpoint?.uri !== checkpoint || !/^gs:\/\/.+\/.+/.test(checkpoint)
      || typeof r.checkpoint?.generation !== 'string' || !/^[1-9][0-9]*$/.test(r.checkpoint.generation)
      || typeof r.checkpoint?.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(r.checkpoint.sha256)
      || !r.cursors || typeof r.cursors !== 'object' || Array.isArray(r.cursors) || !Object.keys(r.cursors).length
      || Object.values(r.cursors).some(v => !Number.isSafeInteger(v) || v < 0)) return null
  return r
}

/** In-place trim after collection, before the state's conditional publish. Active windows stay intact. A receipt
 * behind any saved bound cannot move the state backwards. Legacy day aggregates have no compatible proof. */
export function compactHistory(buckets: Record<string, History>, receipt: Receipt): number {
  if (Object.entries(buckets).some(([name, b]) => b.closed &&
      (b.closed.checkpoint !== receipt.checkpoint.uri || !Object.hasOwn(receipt.cursors, name) || !(receipt.cursors[name] >= b.closed.through)))) return 0
  let removed = 0
  for (const [name, b] of Object.entries(buckets)) {
    const cursor = receipt.cursors[name]
    if (!Object.hasOwn(receipt.cursors, name) || Object.keys(b.legacy_days ?? {}).length) continue
    for (const hour of Object.keys(b.frozen)) {
      const stamp = Date.parse(hour + (hour.length === 13 ? ':00:00Z' : ':00Z')) / 1000
      if (!Number.isFinite(stamp) || stamp > cursor) continue
      delete b.frozen[hour]
      removed++
      b.closed = { through: Math.max(b.closed?.through ?? 0, stamp), checkpoint: receipt.checkpoint.uri }
    }
  }
  return removed
}
