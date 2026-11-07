export const HOT_DATES = ['2026-10-04', '2026-10-05'] as const
export const HOT_SCOPE = 'case-insensitive substring within names; directory hits cover descendants; bytes/objects only'
export interface HotRequest { date: string; name: string; from?: string }
export interface HotDraft { date: string; name: string; from: string }
export interface HotWeights { b: number; o: number }
export interface HotBucket extends HotWeights { path: string; pre: number; post: number }
export interface HotView { target: string; date: string; pattern: string; root: HotWeights; buckets: HotBucket[] }
export interface HotResult { after: HotView; before?: HotView; delta?: HotWeights }

function fail(message: string): never { throw new Error(message) }
function record(v: unknown): Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v) ? v as Record<string, unknown> : fail('Preview returned an invalid object.')
}
function integer(v: unknown, signed = false): number {
  return typeof v === 'number' && Number.isSafeInteger(v) && (signed || v >= 0) ? v : fail('Preview returned an unsupported integer total.')
}
function weights(v: unknown, signed = false): HotWeights {
  const row = record(v)
  return { b: integer(row.b, signed), o: integer(row.o, signed) }
}
function same(a: HotWeights, b: HotWeights): boolean { return a.b === b.b && a.o === b.o }
function difference(a: HotWeights, b: HotWeights): HotWeights { return { b: integer(b.b - a.b, true), o: integer(b.o - a.o, true) } }
function contract(v: Record<string, unknown>): void {
  if (v.exact !== true || v.incremental !== false || v.levels !== 1 || v.path !== '' || v.scope !== HOT_SCOPE ||
      typeof v.target !== 'string' || !/^[a-z][a-z0-9_]*$/.test(v.target)) fail('Preview returned an unsupported root-summary contract.')
}
export function hotDraft(params: URLSearchParams): HotDraft {
  return { date: params.get('date') ?? HOT_DATES[1], name: params.get('name') ?? '.json', from: params.get('from') ?? '' }
}
export function changeHotDraft(draft: HotDraft, field: keyof HotDraft, value: string): HotDraft {
  const next = { ...draft, [field]: value }
  if (field === 'date' && next.from >= next.date) next.from = ''
  return next
}
export function hotDraftParams(draft: HotDraft): URLSearchParams {
  const params = new URLSearchParams({ name: draft.name, date: draft.date })
  if (draft.from) params.set('from', draft.from)
  return params
}
export function hotRequest(params: URLSearchParams): HotRequest {
  const values: Record<string, string> = {}
  params.forEach((value, key) => {
    if (!['date', 'name', 'from'].includes(key) || Object.hasOwn(values, key)) fail('Use only one date, name and optional from parameter.')
    values[key] = value
  })
  const date = values.date ?? HOT_DATES[1], name = values.name ?? '.json', from = values.from
  if (!HOT_DATES.some(day => day === date) || (from !== undefined && !HOT_DATES.some(day => day === from))) fail('Only the frozen 2026-10-04 and 2026-10-05 scans are available.')
  if (from !== undefined && from >= date) fail('Baseline scan must precede the selected scan.')
  if (!name || name.includes('/') || name.includes('\0') || [...name].length > 512) fail('Enter one nonempty name literal without slashes (at most 512 characters).')
  return { date, name: name.toLowerCase(), ...(from === undefined ? {} : { from }) }
}
function view(value: unknown, schemas: readonly string[]): HotView {
  const v = record(value)
  contract(v)
  if (!schemas.includes(v.schema as string)) fail('Preview returned an unsupported response schema.')
  if (!HOT_DATES.some(day => day === v.date) || typeof v.pattern !== 'string' ||
      hotRequest(new URLSearchParams({ name: v.pattern })).name !== v.pattern) fail('Preview returned an invalid scan or normalized literal.')
  if (!Array.isArray(v.buckets) || v.buckets.length !== 6) fail('Preview must return all six buckets.')
  const buckets = v.buckets.map(value => {
    const row = record(value)
    if (typeof row.path !== 'string' || !row.path || row.path.includes('/') || row.path.includes('\0')) fail('Preview returned an invalid bucket identity.')
    return { path: row.path as string, pre: integer(row.pre), post: integer(row.post), ...weights(row) }
  })
  const ordered = [...buckets].sort((a, b) => a.pre - b.pre)
  if (new Set(buckets.map(row => row.path)).size !== 6 || ordered[0].pre !== 1 ||
      ordered.some((row, i) => row.pre > row.post || (i > 0 && ordered[i - 1].post + 1 !== row.pre))) fail('Preview buckets do not form a complete disjoint partition.')
  const root = weights(v.root)
  const sum = buckets.reduce((a, row) => ({ b: integer(a.b + row.b), o: integer(a.o + row.o) }), { b: 0, o: 0 })
  if (!same(root, sum)) fail('Preview bucket totals do not equal the fleet total.')
  return { target: v.target as string, date: v.date as string, pattern: v.pattern, root, buckets }
}
/** Shared exact L1 geometry/weights, without changing or inventing response schemas. */
export function parseRootSummary(value: unknown, request: HotRequest, schemas: readonly { view: string; diff: string }[]): HotResult {
  const v = record(value)
  const views = schemas.map(pair => pair.view)
  if (!request.from) {
    const after = view(v, views)
    if (after.date !== request.date || after.pattern !== request.name) fail('Preview does not match the requested scan and literal.')
    return { after }
  }
  contract(v)
  const pair = schemas.find(pair => pair.diff === v.schema)
  if (!pair) fail('Preview returned an unsupported comparison schema.')
  const before = view(v.before, views), after = view(v.after, views)
  const sideSchema = pair!.view
  if (record(v.before).schema !== sideSchema || record(v.after).schema !== sideSchema || before.target !== after.target || v.target !== after.target ||
      before.date !== request.from || after.date !== request.date || before.pattern !== request.name || after.pattern !== request.name || v.pattern !== request.name) fail('Preview comparison does not match the requested scope.')
  if (!Array.isArray(v.buckets) || v.buckets.length !== 6) fail('Preview comparison must return all six buckets.')
  const delta = difference(before.root, after.root)
  if (!same(weights(v.delta, true), delta)) fail('Preview comparison totals are inconsistent.')
  before.buckets.forEach((a, i) => {
    const b = after.buckets[i], row = record((v.buckets as unknown[])[i])
    if (a.path !== b.path || a.pre !== b.pre || a.post !== b.post || row.path !== a.path || row.pre !== a.pre || row.post !== a.post ||
        !same(weights(row.before), a) || !same(weights(row.after), b) || !same(weights(row.delta, true), difference(a, b))) fail('Preview comparison bucket identities or totals are inconsistent.')
  })
  return { before, after, delta }
}
export function exactInteger(value: number, signed = false): string {
  integer(value, signed)
  return `${signed && value > 0 ? '+' : ''}${value.toLocaleString()}`
}
