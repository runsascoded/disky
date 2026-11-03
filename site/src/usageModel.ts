// /admin/usage's model (specs/usage-metrics.md): the daily `snapshots/usage/<date>.json` documents
// (`dt-cloud cf-usage -o`, `cf_usage.snapshot`) summed over a window — billed units against the plan's included
// amounts, list $ per product per day, and the usage points' attribution by caller or route.

/** The usage points' sums, one attribution row (`cf_usage.AE_SUMS`). */
export interface UsageSums {
  requests: number
  wall_ms: number
  r2_reads: number
  r2_bytes: number
  d1_queries: number
  d1_rows_read: number
  kv_reads: number
  d1_rows_written: number
}

export interface PlanUnit {
  label: string
  /** List price per `per` units, USD. */
  usd: number
  per: number
  /** The plan's monthly included amount. */
  included: number
  /** A GB level billed per month (a day's value is a level, not a count). */
  storage: boolean
}

/** One day's document (`SNAPSHOT_VERSION` 1). */
export interface UsageSnapshot {
  version: 1
  date: string
  project: string
  units: Record<string, number>
  usd: Record<string, number>
  plan: Record<string, PlanUnit>
  functions: Record<string, { requests: number; cpu_ms: number }>
  by_caller: (UsageSums & { caller: string; via: string })[]
  by_route: (UsageSums & { route: string })[]
  notes: string[]
}

export const SUM_KEYS: (keyof UsageSums)[] = ['requests', 'wall_ms', 'r2_reads', 'r2_bytes', 'd1_queries', 'd1_rows_read', 'kv_reads', 'd1_rows_written']

/** The bill's products, as the daily bars stack them (bottom first): the units each sums. D1 rows written first —
 *  the dominant cost (the job's `index-sync` writes footers through D1's HTTP API). */
export const PRODUCTS: { key: string; label: string; units: string[]; color: string }[] = [
  { key: 'd1w', label: 'D1 rows written', units: ['d1_rows_written'], color: 'var(--s8)' },
  { key: 'r2s', label: 'R2 storage', units: ['r2_storage_gb'], color: 'var(--s1)' },
  { key: 'd1r', label: 'D1 rows read', units: ['d1_rows_read'], color: 'var(--s4)' },
  { key: 'r2o', label: 'R2 operations', units: ['r2_class_a', 'r2_class_b'], color: 'var(--s7)' },
  { key: 'fn', label: 'Functions', units: ['functions_requests', 'functions_cpu_ms'], color: 'var(--s5)' },
  { key: 'kv', label: 'KV', units: ['kv_reads', 'kv_writes', 'kv_storage_gb'], color: 'var(--s6)' },
]

/** The newest `days` snapshots, oldest first. */
export function lastDays(snaps: UsageSnapshot[], days: number): UsageSnapshot[] {
  return [...snaps].sort((a, b) => a.date.localeCompare(b.date)).slice(-days)
}

/** Each product's list $ on one day. */
export function productCosts(s: UsageSnapshot): Record<string, number> {
  return Object.fromEntries(PRODUCTS.map(p => [p.key, p.units.reduce((t, u) => t + (s.usd[u] ?? 0), 0)]))
}

export interface UnitTotal {
  unit: string
  label: string
  /** The window's amount: a sum, or a storage unit's mean GB level. */
  total: number
  /** The plan's included amount pro rata: `included × days / 30`. */
  included: number
  /** Past the included amount. */
  over: number
  /** That overage at list price (a storage unit's GB-months: `over × days / 30`). */
  overUsd: number
  /** The window's list $ before the included amounts. */
  listUsd: number
}

/** The window's units against the plan (`cf_usage.overage`'s arithmetic), in the snapshots' `plan` order. */
export function windowTotals(snaps: UsageSnapshot[]): UnitTotal[] {
  if (!snaps.length) return []
  const days = snaps.length
  const plan = snaps[snaps.length - 1].plan
  return Object.entries(plan).map(([unit, p]) => {
    const vals = snaps.map(s => s.units[unit] ?? 0)
    const sum = vals.reduce((a, b) => a + b, 0)
    const total = p.storage ? sum / days : sum
    const included = p.included * days / 30
    const over = Math.max(0, total - included)
    const overUsd = over / p.per * p.usd * (p.storage ? days / 30 : 1)
    const listUsd = snaps.reduce((t, s) => t + (s.usd[unit] ?? 0), 0)
    return { unit, label: p.label, total, included, over, overUsd, listUsd }
  })
}

export type AttrKey = 'caller' | 'route'

export interface AttrRow extends UsageSums {
  name: string
  /** A caller's `via`s (`session`, `token`, …), joined; empty for a route. */
  via: string
}

export const OTHER = '(other)'

/** The window's attribution rows by `key`, summed over days, the `top` with the most `rank` first (ties by name),
 *  the rest and each day's own `(other)` row folded into one `(other)` last. */
export function attribution(snaps: UsageSnapshot[], key: AttrKey, top: number, rank: keyof UsageSums = 'requests'): AttrRow[] {
  const acc = new Map<string, AttrRow & { vias: Set<string> }>()
  for (const s of snaps) {
    for (const r of key === 'caller' ? s.by_caller : s.by_route) {
      const name = key === 'caller' ? (r as { caller: string }).caller : (r as { route: string }).route
      let a = acc.get(name)
      if (!a) acc.set(name, a = { name, via: '', vias: new Set(), ...Object.fromEntries(SUM_KEYS.map(k => [k, 0])) as unknown as UsageSums })
      for (const k of SUM_KEYS) a[k] += r[k] ?? 0
      const via = (r as { via?: string }).via
      if (via) a.vias.add(via)
    }
  }
  const all = [...acc.values()].map(({ vias, ...a }) => ({ ...a, via: [...vias].sort().join(', ') }))
  const named = all.filter(a => a.name !== OTHER).sort((a, b) => b[rank] - a[rank] || a.name.localeCompare(b.name))
  const rest = [...named.slice(top), ...all.filter(a => a.name === OTHER)]
  const out = named.slice(0, top)
  if (rest.length) {
    out.push({ name: OTHER, via: '', ...Object.fromEntries(SUM_KEYS.map(k => [k, rest.reduce((t, r) => t + r[k], 0)])) as unknown as UsageSums })
  }
  return out
}

/** The billed Functions requests in the window, and how many of them carried a usage point (the attribution's
 *  coverage: points began with the meter's deploy, and the dev stack's land in the same dataset). */
export function coverage(snaps: UsageSnapshot[]): { billed: number; attributed: number } {
  return {
    billed: snaps.reduce((t, s) => t + (s.units.functions_requests ?? 0), 0),
    attributed: snaps.reduce((t, s) => t + s.by_caller.reduce((u, r) => u + r.requests, 0), 0),
  }
}

const { abs } = Math

/** A count, 3 significant figures with a K / M / G suffix. */
export function fmtCount(x: number): string {
  for (const [div, suf] of [[1e9, 'G'], [1e6, 'M'], [1e3, 'K']] as const) {
    if (abs(x) >= div) return `${Number((x / div).toPrecision(3))}${suf}`
  }
  return `${Number(x.toPrecision(3))}`
}

/** USD with cents below $100. */
export function fmtDollars(x: number): string {
  return x >= 100 ? `$${Math.round(x).toLocaleString('en-US')}` : `$${x.toFixed(2)}`
}
