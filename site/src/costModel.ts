// /admin/cost/<cloud>'s model: a cloud's daily cost documents (`snapshots/cost/<cloud>/<date>.json`). GCP's come from
// `dt-cloud cost -o` (`deploy_cost.snapshot`): storage per bucket × type × class from Cloud Monitoring, Batch compute
// per `component` cost label, and projections over horizons; all list-price estimates. The other clouds' documents
// join `CostSnapshot` (by `cloud`) and `COST_CLOUDS` as their pages land.

/** Bytes and $/mo at one horizon. */
export interface CostAt {
  bytes: number
  usd_per_month: number
}

/** One bucket × `type` (live / soft-deleted) × `storage_class` series (`deploy_cost.storage_report`'s rows). */
export interface GcpStorageRow {
  bucket: string
  type: string
  storage_class: string
  /** Daily points in the window. */
  days: number
  bytes: number
  /** Live series only; 0 otherwise. */
  slope_bytes_per_day: number
  /** None: an unpriced class. */
  usd_per_month: number | null
}

/** One bucket's rollup with its own projection (`deploy_cost.bucket_rows`). */
export interface GcpBucket {
  bucket: string
  bytes: number
  bytes_by_type: Record<string, number>
  bytes_by_class: Record<string, number>
  live_slope_bytes_per_day: number
  usd_per_month: number
  unpriced_bytes: number
  horizons: Record<string, CostAt>
  /** Daily points (the most of its series); `short`: fewer than the projection trusts. */
  points: number
  short: boolean
  /** A whole-bucket lifecycle Delete: live bytes held flat. */
  ttl_days: number | null
  /** Soft-deleted bytes drain. */
  soft_delete_off: boolean
  /** Only scoped Delete rules: the live slope kept. */
  partial_ttl: boolean
}

export interface GcpStorage {
  bytes: number
  bytes_by_type: Record<string, number>
  live_slope_bytes_per_day: number
  usd_per_month: number
  unpriced_bytes: Record<string, number>
  horizons: Record<string, CostAt>
  rows: GcpStorageRow[]
  buckets: GcpBucket[]
}

/** Batch jobs' window totals for one component / machine / name prefix. */
export interface GcpTally {
  jobs: number
  vm_hours: number
  usd: number
  usd_per_month: number
}

export interface GcpCompute {
  selector: Record<string, string>
  jobs: number
  vm_hours: number
  usd: number
  usd_per_day: number
  usd_per_month: number
  components: Record<string, GcpTally>
  machines: Record<string, GcpTally>
  unpriced_vm_hours: Record<string, number>
  /** Jobs in the window carrying none of the selector's keys, by name prefix: not counted above. */
  unlabeled: Record<string, GcpTally>
}

/** One day's GCP document (`deploy_cost.SNAPSHOT_VERSION` 1). */
export interface GcpCostSnapshot {
  version: 1
  cloud: 'gcp'
  date: string
  name: string
  project: string
  as_of: string
  window_days: number
  /** Days per projection horizon, e.g. `{ "1y": 365.25 }`. */
  horizons: Record<string, number>
  basis: string
  summary: string
  usd_per_month: { storage: number; compute: number; total: number }
  storage: GcpStorage | null
  compute: GcpCompute | null
}

export type CostSnapshot = GcpCostSnapshot

/** The clouds with a cost page: its label and the document version `/api/cost/<cloud>` serves. */
export const COST_CLOUDS: Record<string, { label: string; version: number }> = {
  gcp: { label: 'GCP', version: 1 },
}

export const LIVE = 'live-object'
export const SOFT_DELETED = 'soft-deleted-object'

/** The newest `days` snapshots, oldest first. */
export function lastSnapshots<T extends { date: string }>(snaps: T[], days: number): T[] {
  return [...snaps].sort((a, b) => a.date.localeCompare(b.date)).slice(-days)
}

export interface Component {
  key: string
  label: string
  usd: number
}

/** A snapshot's $/month by component: storage, and each Batch `component` label (`compute:<name>`), most first. */
export function components(s: GcpCostSnapshot): Component[] {
  const out: Component[] = []
  if (s.storage) out.push({ key: 'storage', label: 'Storage', usd: s.storage.usd_per_month })
  for (const [name, t] of Object.entries(s.compute?.components ?? {})) {
    out.push({ key: `compute:${name}`, label: `Batch ${name}`, usd: t.usd_per_month })
  }
  return out.sort((a, b) => b.usd - a.usd || a.key.localeCompare(b.key))
}

const PALETTE = ['var(--s1)', 'var(--s5)', 'var(--s4)', 'var(--s7)', 'var(--s6)', 'var(--s3)', 'var(--s8)', 'var(--s2)']

export interface Series {
  key: string
  label: string
  color: string
}

/** The window's components in one stacking order (bottom first), each with a color: storage first, then the Batch
 *  components by their summed $/month over the window, most first (ties by name). */
export function componentSeries(snaps: GcpCostSnapshot[]): Series[] {
  const acc = new Map<string, { label: string; usd: number }>()
  for (const s of snaps) {
    for (const c of components(s)) {
      const a = acc.get(c.key)
      if (a) a.usd += c.usd
      else acc.set(c.key, { label: c.label, usd: c.usd })
    }
  }
  const keys = [...acc.keys()].sort((a, b) =>
    Number(b === 'storage') - Number(a === 'storage') || acc.get(b)!.usd - acc.get(a)!.usd || a.localeCompare(b))
  return keys.map((key, i) => ({ key, label: acc.get(key)!.label, color: PALETTE[i % PALETTE.length] }))
}

export interface TrendPoint {
  date: string
  /** $/month, in all and per component key. */
  total: number
  byKey: Record<string, number>
  /** Stored bytes, live + soft-deleted. */
  storageBytes: number
}

/** Each snapshot's $/month estimate by component, oldest first. */
export function trend(snaps: GcpCostSnapshot[]): TrendPoint[] {
  return snaps.map(s => {
    const byKey = Object.fromEntries(components(s).map(c => [c.key, c.usd]))
    return { date: s.date, total: s.usd_per_month.total, byKey, storageBytes: s.storage?.bytes ?? 0 }
  })
}

export interface Projection {
  horizon: string
  days: number
  storageBytes: number
  storageUsd: number
  /** Compute is projected flat: the window's rate. */
  computeUsd: number
  totalUsd: number
}

/** A snapshot's $/month at each horizon: its storage projection plus compute held flat. */
export function projections(s: GcpCostSnapshot): Projection[] {
  const computeUsd = s.compute?.usd_per_month ?? 0
  return Object.entries(s.horizons).map(([horizon, days]) => {
    const at = s.storage?.horizons[horizon]
    const storageUsd = at?.usd_per_month ?? 0
    return { horizon, days, storageBytes: at?.bytes ?? 0, storageUsd, computeUsd, totalUsd: storageUsd + computeUsd }
  })
}

/** A snapshot's buckets, the costliest first (then the largest, then by name). */
export function bucketsByCost(s: GcpCostSnapshot): GcpBucket[] {
  return [...(s.storage?.buckets ?? [])].sort((a, b) => b.usd_per_month - a.usd_per_month || b.bytes - a.bytes || a.bucket.localeCompare(b.bucket))
}

/** What a bucket's projection assumes, in words. */
export function bucketNotes(b: GcpBucket): string[] {
  const out = [b.short ? `short window (${b.points} daily points): provisional` : `${b.points} daily points`]
  if (b.ttl_days !== null) out.push(`lifecycle Delete at ${b.ttl_days}d: live bytes held flat`)
  else if (b.partial_ttl) out.push('partially TTL\'d (scoped Delete rules): live slope kept')
  out.push(b.soft_delete_off ? 'soft delete off: soft-deleted bytes drain' : 'soft-deleted bytes held')
  return out
}
