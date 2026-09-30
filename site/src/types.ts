export interface TreeNode {
  n: string
  /** What the path is: an object (`file`) or a directory. A scan indexed
   * before the path store (specs/path-store.md) holds directories only; a
   * fold (`(other)`) is drawn as a branch. Absent from older cached responses. */
  k?: 'file' | 'dir'
  b: number
  o: number
  d?: number                   // bytes-weighted mean created date, epoch days
  a?: number                   // last-read epoch day (access logs; MAX over the whole subtree)
  us?: [string, number][]      // top users -> bytes
  cb?: Record<string, number>  // non-STANDARD class -> bytes ("2" NL, "3" CL, "4" AR); STANDARD = b - sum
  c?: TreeNode[]
  f?: number                   // an `(other)` fold: how many children it stands in for (drives the dust hatch density)
  pv?: Provenance              // provenance of the top owner's inferred attribution (absent when assigned, or no extras for the scan)
}

/** Where an inferred owner came from: the pipeline signal that attributed
 * `prefix` (an ancestor-or-self of the node) to the user, and its evidence
 * (a W&B `entity/project/run_id`, a record path, the matched rule; null when
 * the signal recorded none). */
export type Provenance = [source: string, evidence: string | null, prefix: string]
export const SOURCE_LABELS: Record<string, string> = {
  'user-prefix': 'a users/ path segment',
  'artifact-record': 'an artifact record',
  rule: 'a rule in identities.yaml',
  manual: 'a rule in identities.yaml',
  'wandb-run': 'a W&B run',
  'wandb-config': 'a W&B run config',
  'executor-wandb': 'the executor\'s W&B metadata',
  'iris-path': 'an iris job path',
}

/** Full class mix of a node: cb plus the implied STANDARD remainder. */
export const classMix = (n: Pick<TreeNode, 'b' | 'cb'>): Record<string, number> => {
  const cold = Object.values(n.cb ?? {}).reduce((a, b) => a + b, 0)
  return { ...(n.b > cold && { 1: n.b - cold }), ...n.cb }
}

export interface UserInfo {
  u: string  // canonical user id
  b: number  // total attributed bytes
}

/** Bytes some person owns under a node (Σ of its user slices). */
export const userBytes = (n: Pick<TreeNode, 'us'>): number => (n.us ?? []).reduce((s, [, b]) => s + b, 0)
/** Bytes nobody owns under a node — the unclaimed pool. Ownership has one
 * axis (a person or nobody); there is no group facet. */
export const unclaimedBytes = (n: Pick<TreeNode, 'b' | 'us'>): number => Math.max(0, n.b - userBytes(n))

export interface AgeRow {
  d: number   // created day, epoch days (site aggregates to day/week/month)
  d1?: string // top-level dir (whole-fleet age.json only; the per-path `age` index omits it — specs/age-index.md)
  u?: string  // owning user
  a?: number  // last-read epoch day of the row's dir (access logs; subtree MAX, like TreeNode.a); absent = no read observed
  b: number
  o: number
}

export type Granularity = 'month' | 'week' | 'day'

export type ColorMode = 'tree' | 'date' | 'read' | 'user'

// Key order = the "color by" button row (and ⌘K entry order): the cleanup
// axis leads (read is the best deletion-candidate signal), attribution next,
// chronology/structure last.
export const MODE_LABELS: Record<ColorMode, string> = {
  read: 'read',
  user: 'user',
  date: 'written',
  tree: 'tree',
}

export interface Meta {
  asof: string
  generated: string
  published?: string  // object lastModified (ISO) — real publish time behind a date-only id
  total_bytes: number
  total_objects: number
  class_bytes: Record<string, number>
  users?: UserInfo[]
  user_class_bytes?: Record<string, Record<string, number>>
  /** Access-log observation window (epoch days) — bounds the read-recency lens. */
  access?: { from: number; to: number }
}

// GCS list prices, $/GiB·mo, US regions, by storage_class_id
export const CLASS_PRICE_US: Record<string, number> = { 1: 0.02, 2: 0.01, 3: 0.004, 4: 0.0012 }
export const CLASS_NAMES: Record<string, string> = { 1: 'Standard', 2: 'Nearline', 3: 'Coldline', 4: 'Archive' }
// Class swatches: warm→cold as blue → teal → lavender → pale slate; every
// step legible on the dark ground (a darkness ramp wasn't).
export const CLASS_COLORS: Record<string, string> = { 1: '#4c9aff', 2: '#31c4b3', 3: '#a78bfa', 4: '#cbd5e1' }

// blended $/byte·mo for a storage-class byte mix
export const ratePerByte = (classBytes: Record<string, number>): number => {
  let usd = 0
  let bytes = 0
  for (const [c, b] of Object.entries(classBytes)) {
    usd += (b / 1024 ** 3) * (CLASS_PRICE_US[c] ?? 0.02)
    bytes += b
  }
  return bytes ? usd / bytes : 0
}

export interface Pricing {
  blended: number                       // $/byte·mo across the whole fleet
  userRates?: Record<string, number>    // class-aware $/byte·mo per user
  userMix?: Record<string, Record<string, number>>  // user -> class -> bytes (tooltip breakdowns)
}

export const fmtUsd = (x: number): string =>
  '$' + (x >= 100 ? Math.round(x).toLocaleString('en-US') : x.toFixed(x >= 1 ? 0 : 2))

export interface RuleUser {
  u: string
  aliases: string[]
  name?: string
  github?: string
  note?: string
}

export interface RulePrefix {
  prefix: string
  user?: string
  note?: string
}

export interface Rules {
  users: RuleUser[]
  prefix_owners: RulePrefix[]
}

export type Units = 'si' | 'iec'

export const fmtBytesSi = (b: number, suffixB = false): string => {
  const B = suffixB ? 'B' : ''
  if (b === 0) return '0'
  if (b >= 1e12) return (b / 1e12).toFixed(b >= 1e13 ? 0 : 1) + ' T' + B
  if (b >= 1e9) return (b / 1e9).toFixed(b >= 1e10 ? 0 : 1) + ' G' + B
  if (b >= 1e6) return (b / 1e6).toFixed(0) + ' M' + B
  return Math.round(b / 1e3) + ' K' + B
}

const Ki = 1024, Mi = Ki ** 2, Gi = Ki ** 3, Ti = Ki ** 4

// One unit for a whole list: `fmtBytesLike(row, max)` renders every row at
// the scale `max` would take, so a legend reads `48 Ti · 12 Ti · 0.7 Ti`, not
// `48 TiB · 12 TiB · 682 GiB` (a unit change mid-list hides the ratio).
type Scale = [div: number, name: string]
const iecScale = (b: number): Scale => (b >= Ti ? [Ti, 'Ti'] : b >= Gi ? [Gi, 'Gi'] : b >= Mi ? [Mi, 'Mi'] : [Ki, 'Ki'])
const siScale = (b: number): Scale => (b >= 1e12 ? [1e12, 'T'] : b >= 1e9 ? [1e9, 'G'] : b >= 1e6 ? [1e6, 'M'] : [1e3, 'K'])
export const fmtBytesLike = (b: number, ref: number, units: Units, suffixB = false): string => {
  if (b === 0) return '0'
  const [div, name] = (units === 'iec' ? iecScale : siScale)(ref)
  const v = b / div
  return (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + ' ' + name + (suffixB ? 'B' : '')
}

export const fmtBytesIec = (b: number, suffixB = false): string => {
  const B = suffixB ? 'B' : ''
  if (b === 0) return '0'
  if (b >= Ti) return (b / Ti).toFixed(b >= 10 * Ti ? 0 : 1) + ' Ti' + B
  if (b >= Gi) return (b / Gi).toFixed(b >= 10 * Gi ? 0 : 1) + ' Gi' + B
  if (b >= Mi) return (b / Mi).toFixed(0) + ' Mi' + B
  return Math.round(b / Ki) + ' Ki' + B
}

export const fmtN = (n: number): string => n.toLocaleString('en-US')
