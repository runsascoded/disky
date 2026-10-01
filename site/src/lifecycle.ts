// Bucket lifecycle rules as the scan job snapshots them (`<store>/<scan>/
// lifecycle.json`) — the pure parts of the home page's "Bucket lifecycle"
// fold: one human sentence per rule, the added / removed / changed diff
// against the previous scan's snapshot, and the per-bucket / fleet grouping.
//
// Two clouds, one row shape. An S3 / CAIOS snapshot is the `Rules[]` list
// (`ID`, `Filter`, `Status`, `Expiration`, …), normalized and sorted by `ID`.
// A GCS snapshot is the bucket's `{action, condition}` rules, which have no
// ID: `fromGcs` maps what has an S3 counterpart (age → `Expiration.Days`,
// `daysSinceNoncurrentTime` → `NoncurrentVersionExpiration`, a single
// `matchesPrefix` → `Filter.Prefix`, abort-MPU) and words the rest into
// `Extra`, with a display ID synthesized from the content. Which adapter a
// snapshot goes through is the store's `lifecycle.rules` (stores.ts) — the
// job that wrote it knows its cloud, so the reader never sniffs.

export interface LifecycleRule {
  ID: string
  Filter?: { Prefix?: string }
  Status: string
  Expiration?: { Days?: number; ExpiredObjectDeleteMarker?: boolean }
  NoncurrentVersionExpiration?: { NoncurrentDays?: number }
  AbortIncompleteMultipartUpload?: { DaysAfterInitiation?: number }
  /** Actions / conditions with no S3 counterpart (GCS storage-class moves,
   *  suffix matches, custom-time conditions, …), already in words. */
  Extra?: string
}

/** A GCS lifecycle rule as the API returns it. */
export interface GcsRule {
  action: { type: string; storageClass?: string }
  condition: {
    age?: number
    createdBefore?: string
    customTimeBefore?: string
    daysSinceCustomTime?: number
    daysSinceNoncurrentTime?: number
    isLive?: boolean
    matchesPrefix?: string[]
    matchesSuffix?: string[]
    matchesStorageClass?: string[]
    noncurrentTimeBefore?: string
    numNewerVersions?: number
  }
}

/** An S3-shaped row for a GCS rule. The ID is the rule's content in words —
 *  GCS rules have no name, and two rules with the same content are the same
 *  rule. */
export function fromGcs(r: GcsRule): LifecycleRule {
  const c = r.condition ?? {}
  const out: LifecycleRule = { ID: '', Status: 'Enabled' }
  const prefixes = c.matchesPrefix ?? []
  if (prefixes.length === 1) out.Filter = { Prefix: prefixes[0] }
  const extra: string[] = []
  const type = r.action?.type ?? 'unknown'
  if (type === 'Delete') {
    if (c.age != null && c.isLive !== false) out.Expiration = { Days: c.age }
    else if (c.age != null) extra.push(`delete noncurrent versions older than ${c.age} d`)
    if (c.daysSinceNoncurrentTime != null) out.NoncurrentVersionExpiration = { NoncurrentDays: c.daysSinceNoncurrentTime }
  } else if (type === 'AbortIncompleteMultipartUpload') {
    if (c.age != null) out.AbortIncompleteMultipartUpload = { DaysAfterInitiation: c.age }
  } else if (type === 'SetStorageClass') {
    extra.push(`move to ${r.action.storageClass ?? '?'}${c.age != null ? ` after ${c.age} d` : ''}`)
  } else {
    extra.push(type)
  }
  if (c.isLive === false && type !== 'Delete') extra.push('noncurrent versions only')
  if (c.isLive === true) extra.push('live versions only')
  if (c.numNewerVersions != null) extra.push(`when ${c.numNewerVersions} newer version${c.numNewerVersions === 1 ? '' : 's'} exist`)
  if (c.daysSinceCustomTime != null) extra.push(`${c.daysSinceCustomTime} d after the object's custom time`)
  if (c.customTimeBefore) extra.push(`custom time before ${c.customTimeBefore}`)
  if (c.createdBefore) extra.push(`created before ${c.createdBefore}`)
  if (c.noncurrentTimeBefore) extra.push(`noncurrent since before ${c.noncurrentTimeBefore}`)
  if (prefixes.length > 1) extra.push(`prefixes ${prefixes.join(', ')}`)
  if (c.matchesSuffix?.length) extra.push(`suffix ${c.matchesSuffix.join(', ')}`)
  if (c.matchesStorageClass?.length) extra.push(`in ${c.matchesStorageClass.join(' / ')}`)
  if (extra.length) out.Extra = extra.join(', ')
  // The display ID: action, then the conditions in the order the API lists them.
  const conds = Object.entries(c).map(([k, v]) => `${k}=${Array.isArray(v) ? v.join('|') : String(v)}`)
  out.ID = [type.toLowerCase(), ...conds].join(' ')
  return out
}

/** The rule shape a store's scan job snapshots: `s3` rules carry an `ID`,
 *  `gcs` rules are anonymous `{action, condition}` pairs. */
export type RuleShape = 's3' | 'gcs'

const isS3Rule = (r: unknown): r is LifecycleRule => !!r && typeof r === 'object' && typeof (r as LifecycleRule).ID === 'string'
const isGcsRule = (r: unknown): r is GcsRule => !!r && typeof r === 'object' && 'action' in r && 'condition' in r

/** One rule as the store's cloud writes it → the row shape. A rule that isn't
 *  the configured shape is a mis-configured store (or a foreign file), not a
 *  row to guess at. */
const RULE_ADAPTERS: Record<RuleShape, (raw: unknown) => LifecycleRule> = {
  s3: raw => {
    if (!isS3Rule(raw)) throw new Error('lifecycle.json: expected an S3 rule with an ID')
    return raw
  },
  gcs: raw => {
    if (!isGcsRule(raw)) throw new Error('lifecycle.json: expected a GCS {action, condition} rule')
    return fromGcs(raw)
  },
}

/** A rule's actions in words, joined with ` + ` (objects, noncurrent
 * versions, delete markers, multipart uploads, then anything cloud-specific —
 * the order S3 evaluates them in is irrelevant to a reader; this order reads
 * from common to rare). */
export function describeRule(r: LifecycleRule): string {
  const parts: string[] = []
  if (r.Expiration?.Days != null) parts.push(`expire objects after ${r.Expiration.Days} d`)
  if (r.NoncurrentVersionExpiration?.NoncurrentDays != null) parts.push(`expire noncurrent versions after ${r.NoncurrentVersionExpiration.NoncurrentDays} d`)
  if (r.Expiration?.ExpiredObjectDeleteMarker) parts.push('drop expired delete markers')
  if (r.AbortIncompleteMultipartUpload?.DaysAfterInitiation != null) parts.push(`abort incomplete multipart uploads after ${r.AbortIncompleteMultipartUpload.DaysAfterInitiation} d`)
  if (r.Extra) parts.push(r.Extra)
  return parts.length ? parts.join(' + ') : 'no action'
}

export const rulePrefix = (r: LifecycleRule): string => r.Filter?.Prefix ?? ''

/** The rule's name for a table that has its own prefix column: a GCS content
 *  ID minus the `matchesPrefix=…` term the prefix cell already shows. */
export const displayId = (r: LifecycleRule): string => {
  const p = rulePrefix(r)
  return p ? r.ID.replace(` matchesPrefix=${p}`, '') : r.ID
}

/** Rules per bucket. The job writes `{<bucket>: rules[]}` for a multi-bucket
 * scan (specs/done/cw-multi-bucket.md §2); a bare `rules[]` is the primary
 * bucket's (cw scans from before the multi-bucket job; a one-bucket store). */
export type LifecycleSnapshot = Record<string, LifecycleRule[]>

export function parseLifecycle(json: unknown, primary: string, shape: RuleShape = 's3'): LifecycleSnapshot {
  const conv = (rules: unknown): LifecycleRule[] => {
    if (!Array.isArray(rules)) throw new Error('lifecycle.json: expected Rules[] or {bucket: Rules[]}')
    return rules.map(RULE_ADAPTERS[shape])
  }
  if (Array.isArray(json)) return { [primary]: conv(json) }
  if (json && typeof json === 'object') return Object.fromEntries(Object.entries(json).map(([b, rules]) => [b, conv(rules)]))
  throw new Error('lifecycle.json: expected Rules[] or {bucket: Rules[]}')
}

export type RuleChange = 'new' | 'removed' | 'changed' | null

export interface LifecycleRow {
  rule: LifecycleRule
  change: RuleChange
  /** The previous scan's version of a `changed` rule. */
  prev?: LifecycleRule
}

export interface BucketLifecycleRow extends LifecycleRow {
  bucket: string
}

/** What a rule's row compares by: prefix, status and the described action —
 * two rules that read the same are the same. */
const key = (r: LifecycleRule): string => JSON.stringify([rulePrefix(r), r.Status, describeRule(r)])
// Numeric-aware so `ttl-2d` sorts before `ttl-14d`, whichever cloud named the rule.
const byId = (a: LifecycleRule, b: LifecycleRule): number => a.ID.localeCompare(b.ID, undefined, { numeric: true })

/** Rows for the current snapshot (sorted by ID) — each marked `new` /
 * `changed` against `prev` — followed by `prev`'s rules that are gone, marked
 * `removed`. With no previous snapshot (`null`) every change is `null`. */
export function lifecycleDiff(prev: LifecycleRule[] | null, cur: LifecycleRule[]): LifecycleRow[] {
  const before = new Map((prev ?? []).map(r => [r.ID, r]))
  const rows: LifecycleRow[] = [...cur].sort(byId).map(rule => {
    if (!prev) return { rule, change: null }
    const p = before.get(rule.ID)
    if (!p) return { rule, change: 'new' }
    return key(p) === key(rule) ? { rule, change: null } : { rule, change: 'changed', prev: p }
  })
  const now = new Set(cur.map(r => r.ID))
  for (const rule of [...(prev ?? [])].sort(byId)) if (!now.has(rule.ID)) rows.push({ rule, change: 'removed' })
  return rows
}

/** `lifecycleDiff` per bucket, in `cur`'s bucket order, then the buckets only
 * `prev` has (every rule `removed`). Rule IDs are compared within a bucket —
 * Marin's `marin-ttl-<N>d` rules exist in every bucket. */
export function lifecycleDiffByBucket(prev: LifecycleSnapshot | null, cur: LifecycleSnapshot): BucketLifecycleRow[] {
  const rows: BucketLifecycleRow[] = []
  for (const [bucket, rules] of Object.entries(cur)) {
    for (const row of lifecycleDiff(prev ? prev[bucket] ?? [] : null, rules)) rows.push({ bucket, ...row })
  }
  for (const [bucket, rules] of Object.entries(prev ?? {})) {
    if (bucket in cur) continue
    for (const rule of [...rules].sort(byId)) rows.push({ bucket, rule, change: 'removed' })
  }
  return rows
}

export interface GroupedRow extends LifecycleRow {
  /** The buckets this exact row (rule + change) holds for, sorted. */
  buckets: string[]
}

/** Per-bucket rows folded into one table: rows that read the same (same ID,
 * content, change and previous version) across buckets become one row with
 * a `buckets` list, so a fleet-wide rule shows once. Ordered by ID, removed
 * rows last, then by first bucket. */
export function groupRows(rows: BucketLifecycleRow[]): GroupedRow[] {
  const groups = new Map<string, GroupedRow>()
  for (const { bucket, ...row } of rows) {
    const k = JSON.stringify([row.rule.ID, key(row.rule), row.change, row.prev ? key(row.prev) : null])
    const g = groups.get(k)
    if (g) g.buckets.push(bucket)
    else groups.set(k, { ...row, buckets: [bucket] })
  }
  const out = [...groups.values()]
  for (const g of out) g.buckets.sort()
  out.sort((a, b) => Number(a.change === 'removed') - Number(b.change === 'removed') || byId(a.rule, b.rule) || a.buckets[0].localeCompare(b.buckets[0]))
  return out
}
