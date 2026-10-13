import { isScanId, resolveScan } from './scanSlug'
import { HOT_DATES, HOT_SCOPE, hotRequest, parseRootSummary, type HotBucket, type HotRequest, type HotResult, type HotView, type HotWeights } from './hotModel'
import { REJECT_MESSAGES } from '../functions/_lib/indexedOnly'

export type NamePlan = 'catalog' | 'bounded-name-postings'
export interface NameQualification { qualification_dates: string[]; target: string; patterns: number; selection_contract: string; threshold_paths?: number; threshold_rows?: number; name_rows?: number; max_chars?: number | null; short_chars?: number }
export interface NameExecution {
  plan: NamePlan
  source: string
  validation: Record<string, unknown> & { description: string }
  source_identity: { generation?: string; snapshot_db?: string; history_manifest_sha256?: string; kind?: NameScanKind; target?: string; artifact_sha256?: string; artifact_bytes?: number; source_manifest_sha256?: string; source_prefix_proofs_checked?: true; postings?: string; through?: string; geometry?: 'preorder' | 'ordinal'; catalog?: string; max_rows?: number }
  registry?: NameQualification
  /** A static scan indexed from a dir-only (v1) source: its totals are its folder-name matches only. */
  dirs_only?: true
}
/** `hexRuns`: the static index's hex-run rule applied to this literal (it is hex-affected): matches inside hex ids of `min`+ digits aren't counted. */
export interface NameResult extends HotResult { execution: { after: NameExecution; before?: NameExecution }; logical_store?: string; capabilities?: { bucket_drill: false; child_drill: false; fallback: false }; hexRuns?: { min: number; tail: number } }
/** `consolidated-store-v1`: a scan only the store holds. With the store's consolidated catalog (`catalog` in its identity),
 * literals registered on the scan itself answer from it and the rest on demand from the store's one consolidated name
 * index; without one, every literal answers on demand. Its bucket bounds are path preorder, or (a scan recording no
 * descendant counts) one `ordinal` position per bucket. */
export type NameScanKind = 'frozen-history' | 'daily-scalar-source-v1' | 'consolidated-store-v1' | 'static-names-v1'
export interface NameScan { date: string; plans: NamePlan[]; kind: NameScanKind; qualification_dates?: string[]; source_identity?: NameExecution['source_identity']; registry?: NameQualification }
/** `static`: the static name index's generation and catalog bound V (`static-name-registry-v1`; every scan is `static-names-v1`). */
export interface NameRegistry { dated: boolean; dates: NameScan[]; logical_store?: string; bucket_paths?: string[]; static?: { generation: string; max_rows: number } }
/** A /names entry as the literal it searches, under the map's `simple` term rules (`querySyntax.ts`): a quoted span
 *  is literal (an unterminated quote runs to the end); unquoted, `\^`, `\$` and `\\` are the characters themselves
 *  and any other `\` is itself, while a leading `^` or a trailing `$` is an anchor — `anchored`, which /names (no
 *  anchored reader) refuses rather than answer as a literal. Anchors around nothing (`^`, `$`, `^$`) are the characters. */
export function nameLiteral(raw: string): { anchored: false; text: string } | { anchored: true } {
  const start = raw.startsWith('^')
  let text = '', quoted = false, end = false
  for (let i = start ? 1 : 0; i < raw.length;) {
    const c = raw[i++]
    end = c === '$' && !quoted
    if (c === '\\' && !quoted && i < raw.length && '^$\\'.includes(raw[i])) text += raw[i++]
    else if (c === '"') quoted = !quoted
    else text += c
  }
  if (end) text = text.slice(0, -1)
  if (!text && (start || end)) return { anchored: false, text: `${start ? '^' : ''}${end ? '$' : ''}` }
  return start || end ? { anchored: true } : { anchored: false, text }
}
export function namePageParams(params: URLSearchParams): URLSearchParams {
  const next = new URLSearchParams(params)
  if (!next.has('name')) next.set('name', 'datakit')
  return next
}
/** A /names request against the registry's scans. `date`/`from` are each an
 * exact registry scan id (that scan — a date-only id is not its day's latest)
 * or else a slug, any spelling `scanSlug.ts` reads (`261009`, `26100912`, …),
 * resolved to the latest registry scan it matches (`resolveScan`); absent
 * `date` = the latest scan. */
export function nameRequest(params: URLSearchParams, availableDates?: readonly string[]): HotRequest {
  if (!availableDates) return hotRequest(namePageParams(params))
  const unavailable = () => new Error('This scan is unavailable in the name-summary registry; it is not a zero-match result.')
  const resolved = new URLSearchParams(params)
  // `date` among every scan; `from` among the scans before it (a `from` of
  // the selected scan's own day means that day's latest *earlier* scan).
  const date = params.getAll('date'), from = params.getAll('from')
  // An exact registry id is that scan (a date-only id is not its day's latest);
  // any other value is a slug, resolved.
  const pick = (v: string, scans: readonly string[]) => (scans.includes(v) ? v : resolveScan(v, scans))
  const after = date.length === 1 ? pick(date[0], availableDates) : date.length ? null : [...availableDates].sort().pop() ?? null
  if (date.length === 1 && !after) throw unavailable()
  if (after && date.length < 2) resolved.set('date', after)
  if (from.length === 1 && after) {
    const before = pick(from[0], availableDates.filter(scan => scan < after))
    if (!before) throw unavailable()
    resolved.set('from', before)
  }
  const request = datedNameRequest(resolved)
  if (!availableDates.includes(request.date) || (request.from !== undefined && !availableDates.includes(request.from))) throw unavailable()
  return request
}
export function datedNameRequest(params: URLSearchParams): HotRequest {
  const values: Record<string, string> = {}
  namePageParams(params).forEach((value, key) => {
    if (!['date', 'name', 'from'].includes(key) || Object.hasOwn(values, key)) throw new Error('Use only one date, name and optional from parameter.')
    values[key] = value
  })
  const date = values.date ?? HOT_DATES[1], name = values.name?.toLowerCase(), from = values.from
  if (!iso(date) || (from !== undefined && !iso(from))) throw new Error('Use valid ISO scan dates.')
  if (from !== undefined && from >= date) throw new Error('Baseline scan must precede the selected scan.')
  if (!name || name.includes('/') || name.includes('\0') || [...name].length > 512) throw new Error('Enter one nonempty name literal without slashes (at most 512 characters).')
  return { date, name, ...(from === undefined ? {} : { from }) }
}
const record = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Name summary returned invalid execution metadata.')
  return value as Record<string, unknown>
}
function execution(value: unknown): NameExecution {
  const body = record(value), validation = record(body.validation), identity = record(body.source_identity)
  if ((body.plan !== 'catalog' && body.plan !== 'bounded-name-postings') || typeof body.source !== 'string' || !body.source.trim() ||
      typeof validation.description !== 'string' || !validation.description.trim() || validation.source_prefix_proofs_checked !== true || validation.independent_query_source_oracle !== false ||
      typeof identity.generation !== 'string' || !identity.generation || typeof identity.snapshot_db !== 'string' || !/^[a-z][a-z0-9_]*$/.test(identity.snapshot_db) ||
      typeof identity.history_manifest_sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(identity.history_manifest_sha256)) throw new Error('Name summary returned invalid execution metadata.')
  if (body.plan === 'catalog') record(validation.catalog)
  else if (Object.hasOwn(validation, 'catalog')) throw new Error('Name summary returned invalid execution metadata.')
  return { plan: body.plan, source: body.source, validation: { ...validation, description: validation.description },
    source_identity: { generation: identity.generation, snapshot_db: identity.snapshot_db, history_manifest_sha256: identity.history_manifest_sha256 } }
}
/** A scan id (`YYYY-MM-DD` or sub-daily `YYYY-MM-DDTHHMM`), never just a date (specs/scan-ids-not-dates.md). */
const iso = isScanId
/** A store's bucket set: at least one, unique, slash- and NUL-free names — any fleet's size (gcs 6, cw 5). */
const bucketPaths = (value: unknown): value is string[] => Array.isArray(value) && value.length > 0 && new Set(value).size === value.length && value.every(path => typeof path === 'string' && !!path && !path.includes('/') && !path.includes('\0'))
const id = (value: unknown): value is string => typeof value === 'string' && /^[a-z][a-z0-9_]*$/.test(value)
const hash = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{64}$/.test(value)
const generation = (value: unknown): value is string => typeof value === 'string' && /^[a-f0-9]{32}$/.test(value)
function fail(): never { throw new Error('Name summary returned an invalid dated contract.') }
const integer = (value: unknown, signed = false): number => typeof value === 'number' && Number.isSafeInteger(value) && (signed || value >= 0) ? value : fail()
const keys = (body: Record<string, unknown>, expected: string[]) => Object.keys(body).sort().join(',') === [...expected].sort().join(',')
const weights = (value: unknown, signed = false): HotWeights => {
  const body = record(value)
  if (!keys(body, ['b', 'o'])) fail()
  return { b: integer(body.b, signed), o: integer(body.o, signed) }
}
const same = (a: HotWeights, b: HotWeights) => a.b === b.b && a.o === b.o
const delta = (a: HotWeights, b: HotWeights): HotWeights => ({ b: integer(b.b - a.b, true), o: integer(b.o - a.o, true) })
function capabilities(value: unknown) {
  const body = record(value)
  if (!keys(body, ['bucket_drill', 'child_drill', 'fallback']) || Object.values(body).some(value => value !== false)) fail()
  return { bucket_drill: false, child_drill: false, fallback: false } as const
}
/** A per-scan source's plans: its registered catalog, or bounded discovery over its own name index or the consolidated store's. */
const CONSOLIDATED_SOURCE = 'bounded name postings over the consolidated store; directory rollups are atomic'
const CONSOLIDATED_CATALOG_SOURCE = "the consolidated catalog: every scan's registered literals precomputed in the store"
/** `static-names-v1`: the static name index on R2, read by the Worker (`functions/_lib/nameSummaryStatic.ts`,
 *  `NAME_SUMMARY_STATIC=1`). A literal with more than V suffix rows, or of one or two characters, answers from the catalog
 *  (`plan: 'catalog'`); every other literal from the suffix postings, at most V rows (`bounded-name-postings`). */
export const STATIC_SOURCE = 'static suffix postings on R2, one ranged read by the Worker; directory rollups are atomic'
export const STATIC_CATALOG_SOURCE = 'the static catalog on R2: per-bucket running totals precomputed for every scan of the generation, one ranged read by the Worker'
/** A static index generation's name (`2026-10-05`, `2026-10-09cw`): a generation, not a scan. */
const STATIC_GENERATION = /^[0-9a-z][0-9a-z-]*$/
function staticIdentity(value: unknown) {
  const body = record(value)
  if (!keys(body, ['kind', 'generation', 'max_rows']) || body.kind !== 'static-names-v1' || typeof body.generation !== 'string' || !STATIC_GENERATION.test(body.generation) || integer(body.max_rows) < 1) fail()
  return { kind: 'static-names-v1' as const, generation: body.generation as string, max_rows: integer(body.max_rows) }
}
/** A per-scan (scalar-source) answer's source strings, by plan. The wire `kind` is `'daily-scalar-source-v1'` (named for a
 *  daily cadence): any scan id may carry it. */
const SCALAR_SOURCES: Record<NamePlan, string[]> = {
  catalog: ['published dated precomputed batch artifact'],
  'bounded-name-postings': ["bounded dated name postings over the scan's own name index; directory rollups are atomic", CONSOLIDATED_SOURCE],
}
function consolidatedIdentity(value: unknown) {
  const body = record(value), cataloged = 'catalog' in body
  if (!keys(body, ['kind', 'target', 'postings', 'through', 'geometry', ...(cataloged ? ['catalog'] : [])]) || body.kind !== 'consolidated-store-v1' || !id(body.target) || !id(body.postings) || !iso(body.through) ||
      (body.geometry !== 'preorder' && body.geometry !== 'ordinal') || (cataloged && !id(body.catalog))) fail()
  return { kind: 'consolidated-store-v1' as const, target: body.target as string, postings: body.postings as string, through: body.through as string, geometry: body.geometry as 'preorder' | 'ordinal',
    ...(cataloged ? { catalog: body.catalog as string } : {}) }
}
/** A consolidated catalog's registry is the scan's own census: qualified on that scan alone, named by the catalog. */
function catalogRegistry(value: unknown, date: unknown, catalog: string): NameQualification {
  const registry = registryBinding(value, true)
  if (JSON.stringify(registry.qualification_dates) !== JSON.stringify([date]) || registry.target !== catalog) fail()
  return registry
}
const validationKeys = ['description', 'source_prefix_proofs_checked', 'independent_full_catalog_source_oracle']
function scalarSourceIdentity(value: unknown) {
  const body = record(value)
  if (!keys(body, ['target', 'snapshot_db', 'generation', 'artifact_sha256', 'artifact_bytes', 'source_manifest_sha256', 'source_prefix_proofs_checked', 'kind']) ||
      body.kind !== 'daily-scalar-source-v1' || !id(body.target) || !id(body.snapshot_db) || body.target !== body.snapshot_db || !generation(body.generation) ||
      !hash(body.artifact_sha256) || integer(body.artifact_bytes) < 1 || !hash(body.source_manifest_sha256) || body.source_prefix_proofs_checked !== true) fail()
  return { kind: 'daily-scalar-source-v1' as const, target: body.target, snapshot_db: body.snapshot_db, generation: body.generation,
    artifact_sha256: body.artifact_sha256, artifact_bytes: integer(body.artifact_bytes), source_manifest_sha256: body.source_manifest_sha256,
    source_prefix_proofs_checked: true as const }
}
function datedExecution(body: Record<string, unknown>): NameExecution {
  const identity = record(body.source_identity)
  if (identity.kind === 'frozen-history') {
    if (!keys(identity, ['kind', 'generation', 'snapshot_db', 'history_manifest_sha256']) || !generation(identity.generation)) fail()
    const checked = execution(body)
    return { ...checked, source_identity: { ...checked.source_identity, kind: 'frozen-history' } }
  }
  const validation = record(body.validation)
  if (identity.kind === 'static-names-v1') {
    const checked = staticIdentity(identity)
    const plan = body.plan === 'catalog' || body.plan === 'bounded-name-postings' ? body.plan : fail()
    const dirs = 'dirs_only' in body
    if (!keys(body, ['schema', 'logical_store', 'target', 'date', 'pattern', 'path', 'exact', 'incremental', 'levels', 'scope', 'plan', 'source', 'source_identity', 'validation', 'capabilities', 'root', 'buckets', ...(dirs ? ['dirs_only'] : []), ...('hexRuns' in body ? ['hexRuns'] : [])]) ||
        (dirs && body.dirs_only !== true) || body.source !== (plan === 'catalog' ? STATIC_CATALOG_SOURCE : STATIC_SOURCE) || body.target !== 'static_names' || !keys(validation, validationKeys) ||
        typeof validation.description !== 'string' || !validation.description.trim() || validation.source_prefix_proofs_checked !== true || validation.independent_full_catalog_source_oracle !== false) fail()
    return { plan, source: body.source as string, validation: { description: validation.description, source_prefix_proofs_checked: true, independent_full_catalog_source_oracle: false }, source_identity: checked, ...(dirs ? { dirs_only: true as const } : {}) }
  }
  if (identity.kind === 'consolidated-store-v1') {
    const checked = consolidatedIdentity(identity), catalog = checked.catalog
    const plan = body.plan === 'catalog' && catalog ? 'catalog' : body.plan === 'bounded-name-postings' ? 'bounded-name-postings' : fail()
    if (!keys(body, ['schema', 'logical_store', 'target', 'date', 'pattern', 'path', 'exact', 'incremental', 'levels', 'scope', 'plan', 'source', 'source_identity', 'validation', 'capabilities', 'root', 'buckets', ...(catalog ? ['registry'] : [])]) ||
        (plan === 'catalog' ? body.source !== CONSOLIDATED_CATALOG_SOURCE : body.source !== CONSOLIDATED_SOURCE) || body.target !== checked.target || !keys(validation, validationKeys) ||
        typeof validation.description !== 'string' || !validation.description.trim() || validation.source_prefix_proofs_checked !== true || validation.independent_full_catalog_source_oracle !== false) fail()
    return { plan, source: body.source as string, validation: { description: validation.description, source_prefix_proofs_checked: true, independent_full_catalog_source_oracle: false }, source_identity: checked,
      ...(catalog ? { registry: catalogRegistry(body.registry, body.date, catalog) } : {}) }
  }
  const checked = scalarSourceIdentity(identity)
  const plan = body.plan === 'catalog' || body.plan === 'bounded-name-postings' ? body.plan : fail()
  if (!keys(body, ['schema', 'logical_store', 'target', 'date', 'pattern', 'path', 'exact', 'incremental', 'levels', 'scope', 'plan', 'source', 'source_identity', 'registry', 'validation', 'capabilities', 'root', 'buckets']) || !SCALAR_SOURCES[plan].includes(body.source as string) ||
      !keys(validation, validationKeys) ||
      typeof validation.description !== 'string' || !validation.description.trim() || validation.source_prefix_proofs_checked !== true || validation.independent_full_catalog_source_oracle !== false ||
      body.target !== checked.target) fail()
  const registry = registryBinding(body.registry, true)
  return { plan, source: body.source as string, validation: { description: validation.description, source_prefix_proofs_checked: true, independent_full_catalog_source_oracle: false }, source_identity: checked, registry }
}
function datedView(value: unknown): { view: HotView; execution: NameExecution; store: string } {
  const body = record(value)
  if (body.schema !== 'dated-name-summary-v1' || body.path !== '' || body.exact !== true || body.incremental !== false || body.levels !== 1 || body.scope !== HOT_SCOPE || !id(body.target) || !id(body.logical_store) || !iso(body.date) || typeof body.pattern !== 'string' ||
      datedNameRequest(new URLSearchParams({ name: body.pattern, date: body.date })).name !== body.pattern) fail()
  capabilities(body.capabilities)
  if (!Array.isArray(body.buckets) || !body.buckets.length) fail()
  const buckets: HotBucket[] = (body.buckets as unknown[]).map(value => {
    const row = record(value)
    if (!keys(row, ['path', 'pre', 'post', 'b', 'o']) || typeof row.path !== 'string' || !row.path || row.path.includes('/') || row.path.includes('\0')) fail()
    return { path: row.path as string, pre: integer(row.pre), post: integer(row.post), ...weights({ b: row.b, o: row.o }) }
  })
  const order = [...buckets].sort((a, b) => a.pre - b.pre)
  if (new Set(buckets.map(row => row.path)).size !== buckets.length || order[0].pre !== 1 || order.some((row, i) => row.post < row.pre || (i > 0 && order[i - 1].post + 1 !== row.pre))) fail()
  const root = weights(body.root), sum = buckets.reduce((sum, row) => ({ b: integer(sum.b + row.b), o: integer(sum.o + row.o) }), { b: 0, o: 0 })
  if (!same(root, sum)) fail()
  return { view: { target: body.target as string, date: body.date as string, pattern: body.pattern as string, root, buckets: buckets.sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0) }, execution: datedExecution(body), store: body.logical_store as string }
}
/** A body's optional `hexRuns` note (`{ min, tail }`), checked. */
function hexRunsOf(body: Record<string, unknown>): { hexRuns?: { min: number; tail: number } } {
  if (!('hexRuns' in body)) return {}
  const r = record(body.hexRuns)
  if (!keys(r, ['min', 'tail']) || integer(r.min) < 2 || integer(r.tail) >= integer(r.min)) fail()
  return { hexRuns: { min: integer(r.min), tail: integer(r.tail) } }
}
function parseDated(value: unknown, request: HotRequest): NameResult {
  const body = record(value), cap = capabilities(body.capabilities), hex = hexRunsOf(body)
  const after = datedView(request.from ? body.after : body), before = request.from ? datedView(body.before) : undefined
  if (after.view.date !== request.date || after.view.pattern !== request.name || (before && (before.view.date !== request.from || before.view.pattern !== request.name || before.store !== after.store))) fail()
  if (!request.from) return { after: after.view, execution: { after: after.execution }, logical_store: after.store, capabilities: cap, ...hex }
  if (!keys(body, ['schema', 'logical_store', 'from', 'date', 'pattern', 'path', 'exact', 'incremental', 'levels', 'scope', 'before', 'after', 'delta', 'capabilities', 'buckets', ...(hex.hexRuns ? ['hexRuns'] : [])]) || body.schema !== 'dated-name-summary-diff-v1' || body.from !== request.from || body.date !== request.date || body.logical_store !== after.store || body.pattern !== request.name || body.path !== '' || body.exact !== true || body.incremental !== false || body.levels !== 1 || body.scope !== HOT_SCOPE || !Array.isArray(body.buckets) || body.buckets.length !== after.view.buckets.length || before!.view.buckets.length !== after.view.buckets.length) fail()
  const change = delta(before!.view.root, after.view.root)
  if (!same(weights(body.delta, true), change)) fail()
  const rows = new Map((body.buckets as unknown[]).map(value => { const row = record(value); return [row.path, row] }))
  if (rows.size !== after.view.buckets.length) fail()
  before!.view.buckets.forEach((a, i) => {
    const b = after.view.buckets[i], row = rows.get(a.path)
    if (!row || a.path !== b.path || !keys(row, ['path', 'before', 'after', 'delta'])) fail()
    for (const [value, side] of [[row!.before, a], [row!.after, b]] as const) {
      const pair = record(value)
      if (!keys(pair, ['pre', 'post', 'b', 'o']) || integer(pair.pre) !== side.pre || integer(pair.post) !== side.post || !same(weights({ b: pair.b, o: pair.o }), side)) fail()
    }
    if (!same(weights(row!.delta, true), delta(a, b))) fail()
  })
  return { before: before!.view, after: after.view, delta: change, execution: { before: before!.execution, after: after.execution }, logical_store: after.store, capabilities: cap, ...hex }
}
export function parseName(value: unknown, request: HotRequest): NameResult {
  if (record(value).schema === 'dated-name-summary-v1' || record(value).schema === 'dated-name-summary-diff-v1') return parseDated(value, request)
  const result = parseRootSummary(value, request, [{ view: 'name-summary-v1', diff: 'name-summary-diff-v1' }]), body = record(value)
  const after = execution(request.from ? body.after : body), before = request.from ? execution(body.before) : undefined
  if (before && (before.source_identity.generation !== after.source_identity.generation || before.source_identity.history_manifest_sha256 !== after.source_identity.history_manifest_sha256)) throw new Error('Name summary comparison has inconsistent frozen source identities.')
  return { ...result, execution: { after, ...(before ? { before } : {}) } }
}
/** A static scan's dir-only flag (`NameExecution.dirs_only`), in words. */
export const NAME_DIRS_ONLY = 'This scan lists folders only — files aren’t searchable on it: these totals are its folder-name matches, not every match.'
/** A comparison across a dir-only scan and a full one (a 400 `scan-dirs-only`). */
export const NAME_DIRS_SPLIT = 'One of these scans lists folders only (files aren’t searchable on it), so the literal can’t be compared across the two; compare two scans that both list files. This is not a zero-match result.'
export async function loadName(
  request: HotRequest,
  signal?: AbortSignal,
  availableDates?: readonly string[],
): Promise<NameResult> {
  const params = new URLSearchParams({ date: request.date, name: request.name, ...(request.from ? { from: request.from } : {}) })
  const checked = nameRequest(params, availableDates)
  params.set('name', checked.name)
  // The entry as written travels (the server reads it by `nameLiteral` too); the answer's pattern is the literal.
  const literal = nameLiteral(checked.name)
  if (literal.anchored) throw new Error(REJECT_MESSAGES['anchor-not-indexed'])
  const response = await fetch(`/api/name-summary?${params}`, { signal })
  if (!response.ok) {
    if (response.status === 400) {
      const body = await response.json().catch(() => null) as { code?: string } | null
      if (body?.code === 'scan-dirs-only') throw new Error(NAME_DIRS_SPLIT)
      if (body?.code === 'anchor-not-indexed') throw new Error(REJECT_MESSAGES['anchor-not-indexed'])
    }
    throw new Error(response.status === 401 ? 'Sign in to view name summaries.' : response.status === 400
      ? availableDates ? 'This literal or scan is unavailable for the selected name-summary plan; it is not a zero-match result.' : 'Invalid name-summary request. Check the literal and frozen scan dates; this is not a zero-match result.'
      : 'Name summary is unavailable, busy or exceeded its work budget. This is not a zero-match result. Try again.')
  }
  return parseName(await response.json(), { ...checked, name: literal.text })
}

function registryBinding(value: unknown, scalar: boolean): NameQualification {
  const body = record(value)
  // A scalar-source registry may declare a short-literal domain: every literal of at most `short_chars` characters is registered whatever its frequency.
  const short = scalar && 'short_chars' in body
  // Its threshold counts direct matching paths, or (`threshold_rows`, with `name_rows` charged per name) the consolidated name index's rows a literal costs to answer on demand.
  const rows = scalar && 'threshold_rows' in body
  const threshold = rows ? ['threshold_rows', 'name_rows'] : ['threshold_paths']
  if (!keys(body, ['qualification_dates', 'target', 'patterns', 'selection_contract', ...(scalar ? [...threshold, 'max_chars'] : []), ...(short ? ['short_chars'] : [])]) || !id(body.target) || integer(body.patterns) < 1 || !Array.isArray(body.qualification_dates) || !body.qualification_dates.length || body.qualification_dates.some(day => !iso(day)) || new Set(body.qualification_dates).size !== body.qualification_dates.length || body.selection_contract !== 'membership on declared qualification dates; no current-scan frequency claim' || (scalar && (integer(rows ? body.threshold_rows : body.threshold_paths) < 1 || (rows && integer(body.name_rows) < 0) || (body.max_chars !== null && (integer(body.max_chars) < 1 || integer(body.max_chars) > 512))))) fail()
  return { qualification_dates: body.qualification_dates.map(day => iso(day) ? day : fail()), target: body.target, patterns: integer(body.patterns), selection_contract: body.selection_contract,
    ...(scalar ? { ...(rows ? { threshold_rows: integer(body.threshold_rows), name_rows: integer(body.name_rows) } : { threshold_paths: integer(body.threshold_paths) }), max_chars: body.max_chars === null ? null : integer(body.max_chars) } : {}),
    ...(short ? { short_chars: integer(body.short_chars) < 1 ? fail() : integer(body.short_chars) } : {}) }
}
export function parseNameRegistry(value: unknown): NameRegistry {
  const body = record(value)
  if (body.schema === 'name-summary-registry-v1') {
    if (!keys(body, ['schema', 'target', 'dates', 'levels', 'scope', 'catalog_patterns', 'source_prefix_proofs_checked', 'cold_slots', 'catalog_slots', 'compute_seconds', 'work_bounds', 'cold_quarantined']) || !id(body.target) || body.levels !== 1 || body.scope !== HOT_SCOPE || JSON.stringify(body.dates) !== JSON.stringify(HOT_DATES) || body.source_prefix_proofs_checked !== true || body.cold_slots !== 1 || body.catalog_slots !== 2 || typeof body.compute_seconds !== 'number' || !Number.isFinite(body.compute_seconds) || body.compute_seconds <= 0 || typeof body.cold_quarantined !== 'boolean') fail()
    const bounds = record(body.work_bounds)
    if (!keys(bounds, ['max_names', 'max_postings', 'max_roots']) || Object.values(bounds).some(bound => integer(bound) < 1)) fail()
    const counts = record(body.catalog_patterns)
    if (Object.keys(counts).sort().join() !== [...body.dates as string[]].sort().join() || Object.values(counts).some(count => integer(count) < 1)) fail()
    return { dated: false, dates: (body.dates as string[]).map(date => ({ date, plans: ['catalog', 'bounded-name-postings'], kind: 'frozen-history' })) }
  }
  if (body.schema === 'static-name-registry-v1') {
    if (!keys(body, ['schema', 'logical_store', 'generation', 'max_rows', 'bucket_paths', 'dates', 'levels', 'scope', 'capabilities']) || !id(body.logical_store) || body.levels !== 1 || body.scope !== HOT_SCOPE ||
        !bucketPaths(body.bucket_paths) ||
        !Array.isArray(body.dates) || !body.dates.length || body.dates.length > 400 || body.dates.some((day, i) => !iso(day) || (i > 0 && (body.dates as string[])[i - 1] >= day))) fail()
    capabilities(body.capabilities)
    const source_identity = staticIdentity({ kind: 'static-names-v1', generation: body.generation, max_rows: body.max_rows })
    return { dated: true, dates: (body.dates as string[]).map(date => ({ date, plans: ['catalog', 'bounded-name-postings'], kind: 'static-names-v1', source_identity })),
      logical_store: body.logical_store as string, bucket_paths: [...body.bucket_paths as string[]], static: { generation: source_identity.generation, max_rows: source_identity.max_rows } }
  }
  if (body.schema !== 'dated-name-summary-registry-v1' || !keys(body, ['schema', 'logical_store', 'bucket_paths', 'dates', 'levels', 'scope', 'daily_catalog_slots', 'legacy', 'capabilities']) || !id(body.logical_store) || body.levels !== 1 || body.scope !== HOT_SCOPE || body.daily_catalog_slots !== 2 || !bucketPaths(body.bucket_paths) || !Array.isArray(body.dates) || !body.dates.length || body.dates.length > 400) fail()
  capabilities(body.capabilities)
  if (record(body.legacy).schema !== 'name-summary-registry-v1') fail()
  const legacy = parseNameRegistry(body.legacy), dates: NameScan[] = (body.dates as unknown[]).map(value => {
    const row = record(value)
    if (!iso(row.date)) fail()
    if (row.kind === 'consolidated-store-v1') {
      const source_identity = consolidatedIdentity({ ...record(row.source), kind: row.kind }), catalog = source_identity.catalog
      const plans: NamePlan[] = catalog ? ['catalog', 'bounded-name-postings'] : ['bounded-name-postings']
      if (!keys(row, ['date', 'plans', 'kind', 'source', ...(catalog ? ['registry'] : [])]) || JSON.stringify(row.plans) !== JSON.stringify(plans) || legacy.dates.some(day => day.date === row.date)) fail()
      if (source_identity.through < (row.date as string)) fail()
      if (!catalog) return { date: row.date as string, plans, kind: 'consolidated-store-v1' as const, source_identity }
      const registry = catalogRegistry(row.registry, row.date, catalog)
      return { date: row.date as string, plans, kind: 'consolidated-store-v1' as const, qualification_dates: [...registry.qualification_dates], source_identity, registry }
    }
    const registry = registryBinding(row.registry, row.kind === 'daily-scalar-source-v1')
    let source_identity: NameExecution['source_identity'] | undefined
    if (row.kind === 'frozen-history') {
      const original = record(body.legacy)
      if (!keys(row, ['date', 'plans', 'kind', 'registry']) || JSON.stringify(row.plans) !== JSON.stringify(['catalog', 'bounded-name-postings']) || !legacy.dates.some(day => day.date === row.date) || registry.target !== original.target || registry.patterns !== record(original.catalog_patterns)[row.date as string]) fail()
    } else {
      if (row.kind !== 'daily-scalar-source-v1' || !keys(row, ['date', 'plans', 'kind', 'registry', 'source', 'generation']) || !['["catalog"]', '["catalog","bounded-name-postings"]'].includes(JSON.stringify(row.plans)) || legacy.dates.some(day => day.date === row.date)) fail()
      source_identity = scalarSourceIdentity({ ...record(row.source), kind: row.kind, generation: row.generation })
    }
    return { date: row.date as string, plans: row.plans as NamePlan[], kind: row.kind as NameScan['kind'], qualification_dates: [...registry.qualification_dates], ...(source_identity ? { source_identity, registry } : {}) }
  })
  if (new Set(dates.map(row => row.date)).size !== dates.length || legacy.dates.some(day => !dates.some(row => row.date === day.date))) fail()
  if (legacy.dated || dates.some((row, i) => i > 0 && dates[i - 1].date >= row.date)) fail()
  return { dated: true, dates, logical_store: body.logical_store as string, bucket_paths: [...body.bucket_paths as string[]] }
}
/** Bind even cached responses to current declared availability before display. */
export function nameResultForRegistry(result: NameResult, registry: NameRegistry): NameResult {
  if (registry.bucket_paths && JSON.stringify([...result.after.buckets.map(row => row.path)].sort()) !== JSON.stringify([...registry.bucket_paths].sort())) throw new Error('Name summary returned a different logical store or bucket set from the available-scan registry.')
  if (result.capabilities && result.logical_store !== registry.logical_store) throw new Error('Name summary returned a different logical store or bucket set from the available-scan registry.')
  const sides = [{ view: result.after, execution: result.execution.after }, ...(result.before ? [{ view: result.before, execution: result.execution.before! }] : [])]
  for (const { view, execution } of sides) {
    const scan = registry.dates.find(scan => scan.date === view.date)
    if (!scan || !scan.plans.includes(execution.plan) || scan.kind !== (execution.source_identity.kind ?? 'frozen-history')) throw new Error('Name summary returned a different scan or execution plan from the available-scan registry.')
    if (scan.source_identity && Object.entries(scan.source_identity).some(([key, value]) => record(execution.source_identity)[key] !== value)) throw new Error('Name summary returned a different pinned per-scan source from the available-scan registry.')
    if (scan.registry && JSON.stringify(scan.registry) !== JSON.stringify(execution.registry)) throw new Error('Name summary returned different registry qualification from the available-scan registry.')
  }
  return result
}
export async function loadNameRegistry(signal?: AbortSignal): Promise<NameRegistry> {
  const response = await fetch('/api/name-summary-registry', { signal })
  if (!response.ok) throw new Error(response.status === 401 ? 'Sign in to view available name-summary scans.' : 'Available name-summary scans could not be loaded. Try again; no dates or zero results have been inferred.')
  return parseNameRegistry(await response.json())
}
