/** Project-owned scan catalogs and deterministic as-of selection.
 * Pure/DOM-free: shared by the picker and server adapters. No scan execution,
 * credentials, fetches, or changes to the existing single-store d= resolver. */
export interface SourceScope { store: string; prefix: string }
export interface ScanArtifacts {
  format: 'path-store-v2'
  path: string
  bysize?: string
  mtime?: string
  search?: string
}
export interface SourceScan {
  id: string
  revision: string
  digest: string
  complete: true
  observed_from: string
  observed_through: string
  published_at: string
  total_bytes: number
  total_objects: number
  artifacts: ScanArtifacts
}
export interface SourceCatalog {
  version: 1
  source: string
  label: string
  mount: string
  scope: SourceScope
  scans: SourceScan[]
}
export interface ScanReference { id: string; revision: string; digest: string }
export interface SourceSelection {
  source: string
  mount: string
  scope: SourceScope
  reference: ScanReference | null
  scan: SourceScan | null
  state: 'ready' | 'unknown' | 'unavailable'
}
export interface FederatedView {
  version: 1
  at: string
  sources: SourceSelection[]
}
export interface FederatedPin {
  version: 1
  at: string
  sources: Pick<SourceSelection, 'source' | 'mount' | 'scope' | 'reference'>[]
}
export interface ScanEvent { at: string; sources: string[] }

const ID = /^[a-z0-9][a-z0-9-]*$/
const ISO = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{3})?Z$/
const compare = (a: string, b: string): number => a < b ? -1 : a > b ? 1 : 0

/** What selection orders and filters scans by: their capture end, then
 * publication, then stable ids. A catalog scan is one; so is an in-deployment
 * store's indexed scan (`functions/_lib/union.ts`), whose D1 rows carry no
 * digest or totals. */
export type Observation = Pick<SourceScan, 'id' | 'revision' | 'observed_through' | 'published_at'>

/** Canonical selection order, independent of browser/server locale. */
export const compareScans = (a: Observation, b: Observation): number =>
  compare(a.observed_through, b.observed_through) || compare(a.published_at, b.published_at) || compare(a.id, b.id) || compare(a.revision, b.revision)

/** The scan a source contributes at cutoff `at` (a canonical UTC instant):
 * the greatest by `compareScans` among those observed through `at`; null =
 * unknown (no eligible scan), never the first later one. */
export function latestObserved<T extends Observation>(scans: readonly T[], at: string): T | null {
  let best: T | null = null
  for (const s of scans) if (s.observed_through <= at && (!best || compareScans(s, best) > 0)) best = s
  return best
}

/** Only explicit UTC instants; reject invalid calendar dates and date-only IDs. */
export function utcInstant(value: string): string {
  if (!ISO.test(value)) throw new Error('expected a UTC ISO timestamp')
  const n = Date.parse(value)
  if (!Number.isFinite(n)) throw new Error('invalid UTC timestamp')
  const canonical = new Date(n).toISOString()
  if (canonical !== (value.length === 20 ? value.replace('Z', '.000Z') : value)) throw new Error('invalid UTC timestamp')
  return canonical
}

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('expected an object')
  return value as Record<string, unknown>
}
function string(value: unknown): string {
  if (typeof value !== 'string' || !value) throw new Error('expected a nonempty string')
  return value
}
function identifier(value: unknown): string {
  const s = string(value)
  if (!ID.test(s)) throw new Error('invalid source or mount')
  return s
}
function count(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) throw new Error('expected a nonnegative safe integer')
  return value
}
function relative(value: unknown, directory = false): string {
  const s = string(value)
  const parts = (directory ? s.replace(/\/$/, '') : s).split('/')
  if (parts.some(p => !p || p === '.' || p === '..') || /[\\%?#:]/.test(s)) throw new Error('expected a relative artifact key or scope prefix')
  return s
}
function scopeOf(value: unknown): SourceScope {
  const s = record(value)
  const prefix = s.prefix === '' ? '' : relative(s.prefix, true)
  if (prefix && !prefix.endsWith('/')) throw new Error('nonempty scope prefix must end with /')
  return { store: string(s.store), prefix }
}
function contentDigest(value: unknown): string {
  const s = string(value)
  if (!/^[a-f0-9]{64}$/.test(s)) throw new Error('expected a SHA-256 content digest')
  return s
}

/** A catalog advertises completed exports only. Never coerce failed/pending
 * producer attempts or a health inventory into successful zero-size scans. */
export function parseSourceCatalog(value: unknown): SourceCatalog {
  const c = record(value)
  if (c.version !== 1 || !Array.isArray(c.scans)) throw new Error('unsupported source catalog')
  const scope = scopeOf(c.scope)
  const scans = c.scans.map(value => {
    const s = record(value)
    if (s.complete !== true) throw new Error('catalog advertises an incomplete scan')
    const observed_from = utcInstant(string(s.observed_from))
    const observed_through = utcInstant(string(s.observed_through))
    const published_at = utcInstant(string(s.published_at))
    if (observed_from > observed_through || observed_through > published_at) throw new Error('invalid capture/publication ordering')
    const a = record(s.artifacts)
    if (a.format !== 'path-store-v2') throw new Error('unsupported scan artifact format')
    const artifacts: ScanArtifacts = { format: 'path-store-v2', path: relative(a.path) }
    for (const key of ['bysize', 'mtime', 'search'] as const) if (a[key] !== undefined) artifacts[key] = relative(a[key])
    const digest = contentDigest(s.digest)
    return {
      id: string(s.id), revision: string(s.revision), digest, complete: true as const,
      observed_from, observed_through, published_at,
      total_bytes: count(s.total_bytes), total_objects: count(s.total_objects), artifacts,
    }
  })
  const refs = scans.map(s => JSON.stringify([s.id, s.revision]))
  if (new Set(refs).size !== refs.length) throw new Error('duplicate scan revision')
  return {
    version: 1, source: identifier(c.source), label: string(c.label), mount: identifier(c.mount),
    scope, scans,
  }
}

const reference = (s: SourceScan): ScanReference => ({ id: s.id, revision: s.revision, digest: s.digest })
const sameScope = (a: SourceScope, b: SourceScope) => a.store === b.store && a.prefix === b.prefix
const scopePrefix = (p: string) => p === '' || p.endsWith('/') ? p : p + '/'

function sourcesChecked<T extends Pick<SourceCatalog, 'source' | 'mount' | 'scope'>>(catalogs: readonly T[]): T[] {
  const ordered = [...catalogs].sort((a, b) => compare(a.source, b.source))
  if (new Set(ordered.map(c => c.source)).size !== ordered.length || new Set(ordered.map(c => c.mount)).size !== ordered.length) throw new Error('duplicate source or mount')
  ordered.forEach((a, i) => ordered.slice(i + 1).forEach(b => {
    const ap = scopePrefix(a.scope.prefix), bp = scopePrefix(b.scope.prefix)
    if (a.scope.store === b.scope.store && (ap.startsWith(bp) || bp.startsWith(ap))) throw new Error('overlapping source scopes')
  }))
  return ordered
}

const selection = (c: Pick<SourceCatalog, 'source' | 'mount' | 'scope'>, scan: SourceScan | null): SourceSelection => ({
  source: c.source, mount: c.mount, scope: { ...c.scope },
  reference: scan ? reference(scan) : null, scan, state: scan ? 'ready' : 'unknown',
})

/** Selection uses capture end, not scan ID or publication time. Backfills are
 * eligible once published; replaying what was known at T is a separate mode.
 * Same-observation corrections resolve by publication time, then stable IDs. */
export function resolveFederation(catalogs: readonly SourceCatalog[], cutoff: string): FederatedView {
  const at = utcInstant(cutoff)
  const sources = sourcesChecked(catalogs).map(c => {
    return selection(c, latestObserved(c.scans, at))
  })
  return { version: 1, at, sources }
}

/** Immutable logical references, not mutable catalog order or physical files. */
export function pinFederation(view: FederatedView): FederatedPin {
  return { version: 1, at: view.at, sources: view.sources.map(({ source, mount, scope, reference }) => ({
    source, mount, scope: { ...scope }, reference: reference ? { ...reference } : null,
  })) }
}

/** Validate persisted/URL-resolved pins before using them as reader input. */
export function parseFederatedPin(value: unknown): FederatedPin {
  const p = record(value)
  if (p.version !== 1 || !Array.isArray(p.sources)) throw new Error('unsupported federated pin')
  const sources = p.sources.map(value => {
    const s = record(value)
    const ref = s.reference === null ? null : record(s.reference)
    return {
      source: identifier(s.source), mount: identifier(s.mount), scope: scopeOf(s.scope),
      reference: ref ? { id: string(ref.id), revision: string(ref.revision), digest: contentDigest(ref.digest) } : null,
    }
  })
  return { version: 1, at: utcInstant(string(p.at)), sources: sourcesChecked(sources) }
}

/** Missing pinned artifacts stay unavailable; never jump to another scan.
 * A pinned unknown source stays unknown even if a backfill arrives later. */
export function restoreFederation(pin: FederatedPin, catalogs: readonly SourceCatalog[]): FederatedView {
  const checked = parseFederatedPin(pin)
  const at = checked.at
  const available = new Map(sourcesChecked(catalogs).map(c => [c.source, c]))
  const sources = checked.sources.map(p => {
    const c = available.get(p.source)
    const scan = c?.mount === p.mount && sameScope(c.scope, p.scope) ? c.scans.find(s =>
      s.id === p.reference?.id && s.revision === p.reference.revision && s.digest === p.reference.digest &&
      s.observed_through <= at) ?? null : null
    return { ...p, scope: { ...p.scope }, reference: p.reference ? { ...p.reference } : null, scan,
      state: p.reference ? scan ? 'ready' as const : 'unavailable' as const : 'unknown' as const }
  })
  return { version: 1, at, sources }
}

/** Includes cutoff: response freshness/relative-age fields can depend on it
 * even when two cutoffs select identical content. No secrets/artifact URLs. */
export const federatedViewKey = (view: FederatedView): string => JSON.stringify(pinFederation(view))

/** Picker/step-chart events: sorted, simultaneous observations deduplicated. */
export function federatedEvents(catalogs: readonly SourceCatalog[]): ScanEvent[] {
  const times = new Map<string, Set<string>>()
  for (const c of sourcesChecked(catalogs)) for (const s of c.scans) {
    const ids = times.get(s.observed_through) ?? new Set<string>()
    ids.add(c.source)
    times.set(s.observed_through, ids)
  }
  return [...times].sort(([a], [b]) => compare(a, b)).map(([at, ids]) => ({ at, sources: [...ids].sort() }))
}

export interface SourceDelta {
  source: string
  kind: 'unchanged' | 'changed' | 'coverage'
  bytes: number | null
  objects: number | null
}

/** Root deltas over comparable source coverage, not fleet-membership changes.
 * Path-level diff adapters use these same selected scan references. */
export function federatedDeltas(before: FederatedView, after: FederatedView): SourceDelta[] {
  const b = new Map(before.sources.map(s => [s.source, s]))
  const a = new Map(after.sources.map(s => [s.source, s]))
  return [...new Set([...b.keys(), ...a.keys()])].sort().map(source => {
    const from = b.get(source), to = a.get(source)
    if (from?.state !== 'ready' || to?.state !== 'ready' || !from.scan || !to.scan || from.mount !== to.mount || !sameScope(from.scope, to.scope)) return { source, kind: 'coverage', bytes: null, objects: null }
    return {
      source, kind: from.scan.digest === to.scan.digest ? 'unchanged' : 'changed',
      bytes: to.scan.total_bytes - from.scan.total_bytes, objects: to.scan.total_objects - from.scan.total_objects,
    }
  })
}
