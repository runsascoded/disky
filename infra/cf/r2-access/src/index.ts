/** Private, bounded R2 Observability archive; no deletion-safety claims.
 *
 * The UTC hour is the unit: an hour's aggregate is a pure function of one complete drain of that hour,
 * and a re-drain replaces it. See README "Collection contract and limits".
 */
const HOUR = 3_600_000
const STATE_KEY = 'state.json'
/** Workers' per-invocation subrequest limit (paid plan; fetches and binding calls alike). `settings` refuses a
 * configuration whose worst case (`subrequestBound`) could exceed it. */
export const SUBREQUEST_LIMIT = 1000
type Archive = Pick<Env['ARCHIVE'], 'put' | 'get' | 'delete'>
type CollectorEnv = Omit<Env, 'ARCHIVE' | 'PUBLIC' | 'PUBLIC_BUCKETS' | 'PUBLIC_PREFIX' | 'EXCLUDED_ACCESS_KEY_IDS'> & {
  ARCHIVE: Archive
  /** Optional: where the derived, public per-hour read files go (`PUBLIC_BUCKETS` names which sources). */
  PUBLIC?: Pick<Env['ARCHIVE'], 'put'>
  PUBLIC_BUCKETS?: string
  PUBLIC_PREFIX?: string
  /** Secret, optional: a JSON array of access key ids whose requests are the deployment's own (its scanner's
   * LISTs/HEADs). They count in an hour's totals and sampling check but never in its per-key rows. */
  EXCLUDED_ACCESS_KEY_IDS?: string
}

export class CollectionError extends Error {
  constructor(readonly code: string) { super(`r2-access: ${code}`) }
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new CollectionError('invalid_response')
  return value as Record<string, unknown>
}

function integer(value: string, min: number, max: number): number {
  const n = Number(value)
  if (typeof value !== 'string' || !value || !Number.isSafeInteger(n) || n < min || n > max) throw new CollectionError('invalid_config')
  return n
}

/** Worst-case subrequests of one invocation: the state read and write, the manifest and the one batched delete of
 * superseded aggregates (4); per hour attempted, its count query, aggregate write and public write (3); per event
 * page, its query and raw write (2). */
export function subrequestBound(maxPages: number, maxHours: number): number {
  return 4 + 3 * maxHours + 2 * maxPages
}

export function settings(env: CollectorEnv, cron: string, scheduledTime: number) {
  if (!/^[a-f0-9]{32}$/.test(env.ACCOUNT_ID) || !env.CF_OBSERVABILITY_TOKEN) throw new CollectionError('invalid_config')
  const raw: unknown = JSON.parse(env.SOURCE_BUCKETS)
  if (!Array.isArray(raw) || !raw.length || raw.length > 10) throw new CollectionError('invalid_config')
  const buckets: string[] = []
  for (const name of raw) {
    if (typeof name !== 'string' || !/^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/.test(name) || name === env.ARCHIVE_BUCKET_NAME || buckets.includes(name)) throw new CollectionError('invalid_config')
    buckets.push(name)
  }
  const published = env.PUBLIC_BUCKETS ? JSON.parse(env.PUBLIC_BUCKETS) as unknown : []
  if (!Array.isArray(published) || published.some(name => typeof name !== 'string' || !buckets.includes(name))) throw new CollectionError('invalid_config')
  if (published.length && (!env.PUBLIC || !env.PUBLIC_PREFIX || !/^[a-z0-9][a-z0-9/_-]*[a-z0-9]$/.test(env.PUBLIC_PREFIX))) throw new CollectionError('invalid_config')
  const excluded = env.EXCLUDED_ACCESS_KEY_IDS ? JSON.parse(env.EXCLUDED_ACCESS_KEY_IDS) as unknown : []
  if (!Array.isArray(excluded) || excluded.some(id => typeof id !== 'string' || !id)) throw new CollectionError('invalid_config')
  if (cron !== env.HOURLY_CRON) throw new CollectionError('unwired_cron')
  const lag = integer(env.LAG_HOURS, 1, 24)
  const overlap = integer(env.OVERLAP_HOURS, 1, 24)
  const retention = integer(env.RETENTION_HOURS, 1, 144)
  const lookback = integer(env.LOOKBACK_HOURS, 1, 144)
  const maxPages = integer(env.MAX_PAGES, 1, 450)
  const maxHours = integer(env.MAX_HOURS, 1, 168)
  const verifyHours = integer(env.VERIFY_HOURS, 0, 168)
  if (lag + retention > 166 || lookback > retention || overlap > retention || verifyHours > maxHours) throw new CollectionError('invalid_config')
  if (subrequestBound(maxPages, maxHours) > SUBREQUEST_LIMIT) throw new CollectionError('invalid_config')
  if (!Number.isSafeInteger(scheduledTime) || scheduledTime < 0) throw new CollectionError('invalid_config')
  const to = Math.floor(scheduledTime / HOUR) * HOUR - lag * HOUR
  return {
    buckets, now: scheduledTime, to, floor: to - retention * HOUR,
    published: published as string[], publicPrefix: env.PUBLIC_PREFIX ?? '', excluded: new Set(excluded as string[]),
    lookback: lookback * HOUR, overlap: overlap * HOUR,
    pageSize: integer(env.PAGE_SIZE, 1, 2000),
    maxPages, maxHours, verifyHours,
    maxBytes: integer(env.MAX_PAGE_BYTES, 1024, 8_388_608),
  }
}

export type Settings = ReturnType<typeof settings>

/** The telemetry API's `timeframe` is exclusive at BOTH ends (measured
 * 2026-10-17: an event at t is returned for (t-1, t+1) but not for
 * (t, t+1000) or (t-1000, t)). Sending `from - 1` yields exactly `[from, to)`,
 * so abutting windows neither drop nor double-count an event on the edge.
 */
export function timeframe(from: number, to: number) {
  return { from: from - 1, to }
}

function filters(bucket: string) {
  return { datasets: ['r2'], filters: [{ key: 'bucket', operation: 'eq', type: 'string', value: bucket }], filterCombination: 'and' }
}

export function eventsBody(bucket: string, from: number, to: number, limit: number, offset?: string) {
  return {
    queryId: 'disky-r2-archive', dry: true, view: 'events', limit, timeframe: timeframe(from, to),
    parameters: filters(bucket),
    ...(offset ? { offset, offsetDirection: 'next' } : {}),
  }
}

export function calculationBody(bucket: string, from: number, to: number) {
  return {
    queryId: 'disky-r2-sampling', dry: true, view: 'calculations', timeframe: timeframe(from, to),
    parameters: { ...filters(bucket), calculations: [{ operator: 'count', alias: 'events' }], groupBys: [{ type: 'string', value: 'action' }] },
  }
}

export async function boundedText(response: Response, maxBytes: number): Promise<string> {
  if (!response.body) throw new CollectionError('empty_response')
  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8', { fatal: true, ignoreBOM: false })
  let bytes = 0
  let text = ''
  try {
    for (;;) {
      const chunk = await reader.read()
      if (chunk.done) break
      bytes += chunk.value.byteLength
      if (bytes > maxBytes) {
        await reader.cancel()
        throw new CollectionError('page_bytes_exceeded')
      }
      text += decoder.decode(chunk.value, { stream: true })
    }
    return text + decoder.decode()
  } finally { reader.releaseLock() }
}

function completed(text: string) {
  const response = object(JSON.parse(text))
  if (response.success !== true) throw new CollectionError('provider_error')
  const result = object(response.result)
  const run = object(result.run)
  if (run.status !== 'COMPLETED') throw new CollectionError('query_incomplete')
  const statistics = object(result.statistics ?? run.statistics)
  // abr_level > 1 is query-time sampling (a 6-day calculation came back at 10).
  if (statistics.abr_level != null && statistics.abr_level !== 1) throw new CollectionError('query_sampled')
  return result
}

/** `key`: the object key (absent for bucket-level actions: LIST, HeadBucket); `own`: made with an excluded access key. */
export interface Event { id: string, action: string, timestamp: number, key: string | null, own: boolean }

export function parsePage(text: string, bucket: string, from: number, to: number, excluded: Set<string> = new Set()): Event[] {
  const page = object(completed(text).events)
  if (!Array.isArray(page.events) || page.count !== page.events.length) throw new CollectionError('page_count_mismatch')
  return page.events.map((value: unknown) => {
    const event = object(value)
    const metadata = object(event.$metadata)
    const source = object(event.source)
    if (event.dataset !== 'r2' || source.bucket !== bucket || typeof metadata.id !== 'string' || !metadata.id) throw new CollectionError('invalid_event')
    if (typeof source.action !== 'string' || !Number.isSafeInteger(event.timestamp)) throw new CollectionError('invalid_event')
    const timestamp = event.timestamp as number
    if (timestamp < from || timestamp >= to) throw new CollectionError('event_outside_window')
    const resource = source.resource == null ? {} : object(source.resource)
    const actor = source.actor == null ? {} : object(source.actor)
    const key = typeof resource.key === 'string' && resource.key ? resource.key : null
    const own = typeof actor.accessKeyId === 'string' && excluded.has(actor.accessKeyId)
    return { id: metadata.id, action: source.action, timestamp, key, own }
  })
}

/** Ingestion sampling is only visible in calculation aggregates: each stored
 * row carries a weight, `count` sums the weights and `sampleInterval` is their
 * mean. Measured: a burst hour drained 169 event rows while `count` was 178
 * (`sampleInterval` 1.053). Per-bucket `series` values are coarse estimates
 * and are deliberately ignored.
 */
export function parseCalculation(text: string) {
  const calculations = completed(text).calculations
  if (!Array.isArray(calculations) || calculations.length !== 1) throw new CollectionError('invalid_calculation')
  const aggregates = object(calculations[0]).aggregates
  if (!Array.isArray(aggregates)) throw new CollectionError('invalid_calculation')
  let estimated = 0
  let maxInterval = 1
  for (const value of aggregates) {
    const aggregate = object(value)
    const { count, sampleInterval } = aggregate
    if (typeof count !== 'number' || !Number.isFinite(count) || count < 0 || typeof sampleInterval !== 'number' || !(sampleInterval >= 1)) throw new CollectionError('invalid_calculation')
    estimated += count
    maxInterval = Math.max(maxInterval, sampleInterval)
  }
  return { estimated: Math.round(estimated), maxInterval }
}

export type Capture = 'unsampled' | 'sampled' | 'inconsistent'
export interface Sampling { observed: number, estimated: number, max_sample_interval: number, capture: Capture }

/** `unsampled` only when every stored row has weight 1 AND the weighted count
 * equals the distinct events drained for that hour; a weight > 1 is provider
 * ingestion sampling (permanent); anything else (e.g. an event delivered
 * between the count and the drain) is `inconsistent` until a re-drain. */
export function capture(observed: number, estimated: number, maxInterval: number): Capture {
  if (maxInterval > 1) return 'sampled'
  return estimated === observed ? 'unsampled' : 'inconsistent'
}

const CLASSES: Record<string, string> = {
  GetObject: 'content_read',
  HeadObject: 'metadata_read', HeadBucket: 'metadata_read',
  ListObjects: 'list', ListObjectsV2: 'list', ListMultipartUploads: 'list', ListParts: 'list', ListBuckets: 'list',
  PutObject: 'write', CopyObject: 'write', CreateMultipartUpload: 'write', UploadPart: 'write', UploadPartCopy: 'write', CompleteMultipartUpload: 'write', AbortMultipartUpload: 'write',
  DeleteObject: 'delete', DeleteObjects: 'delete',
}

/** The actions an hour keeps per-key rows for: object reads, what the read lens shows. Every other action (writes,
 * deletes, listings) is only counted (`actions`, `classes`): a write's recency is the listing's `mtime`, and at a
 * write-heavy bucket per-key write rows were 99.8% of the aggregate (marin-na, 10-17: 258,900 PutObject rows of
 * 259,457 in 8 hours; specs/r2-access-compaction.md). */
export const ROW_ACTIONS: ReadonlySet<string> = new Set(['GetObject', 'HeadObject'])

/** Content reads are kept apart from metadata reads, listings, writes and deletes. */
export function actionClass(action: string): string {
  return CLASSES[action] ?? 'other'
}

const MINUTE = 60_000

/** Access times are kept to the minute (the start of the minute, epoch ms): an hour's rows say when a key was
 * touched, not the exact request instants. */
export function minute(ms: number): number {
  return Math.floor(ms / MINUTE) * MINUTE
}

/** `[key, action, n, first_ts, last_ts]`; `key` null for bucket-level actions; times truncated to the minute. */
export type HourRow = [string | null, string, number, number, number]

/** One drain of one UTC hour. Deterministic in its events: no run id, no drain time. */
export interface HourAggregate {
  schema_version: 4
  bucket: string
  /** ISO start of the UTC hour. */
  hour: string
  /** Distinct provider events in the drain, the deployment's own included. */
  events: number
  /** Of which made with an `EXCLUDED_ACCESS_KEY_IDS` key: in `actions`/`classes`, never in `rows`. */
  own: number
  actions: Record<string, number>
  classes: Record<string, number>
  sampling: Sampling
  /** Per (key, action) for `ROW_ACTIONS` only, own requests left out, sorted by key then action. */
  rows: HourRow[]
}

/** Accumulates one hour's drain: dedupes event ids within the drain only, counts every action, keeps per-(key,
 * action) rows for `ROW_ACTIONS`. */
export class HourAccumulator {
  private readonly ids = new Set<string>()
  private readonly cells = new Map<string, HourRow>()
  private readonly actions: Record<string, number> = {}
  private own = 0

  add(event: Event): void {
    if (this.ids.has(event.id)) return
    this.ids.add(event.id)
    this.actions[event.action] = (this.actions[event.action] ?? 0) + 1
    if (event.own) { this.own++; return }
    if (!ROW_ACTIONS.has(event.action)) return
    // `\u0000` cannot occur in an R2 key or action name; it separates the two in the map key.
    const id = `${event.key ?? ''}\u0000${event.key === null ? 0 : 1}\u0000${event.action}`
    const row = this.cells.get(id)
    const at = minute(event.timestamp)
    if (!row) this.cells.set(id, [event.key, event.action, 1, at, at])
    else {
      row[2]++
      if (at < row[3]) row[3] = at
      if (at > row[4]) row[4] = at
    }
  }

  get observed(): number { return this.ids.size }

  /** The drain already holds this event (a page can repeat one: the provider delivers some events twice). */
  has(id: string): boolean { return this.ids.has(id) }

  finish(bucket: string, hour: number, estimated: number, maxInterval: number): HourAggregate {
    const classes: Record<string, number> = {}
    for (const [action, n] of Object.entries(this.actions)) classes[actionClass(action)] = (classes[actionClass(action)] ?? 0) + n
    const rows = [...this.cells.values()].sort((a, b) =>
      a[0] === b[0] ? cmp(a[1], b[1]) : a[0] === null ? -1 : b[0] === null ? 1 : cmp(a[0], b[0]))
    const observed = this.ids.size
    return {
      schema_version: 4, bucket, hour: new Date(hour).toISOString(), events: observed, own: this.own,
      actions: sorted(this.actions), classes: sorted(classes),
      sampling: { observed, estimated, max_sample_interval: maxInterval, capture: capture(observed, estimated, maxInterval) },
      rows,
    }
  }
}

/** An hour's public read-recency file: per (key, action) `{key, action, n, last_ts}` only (`last_ts` the start of
 * the minute of the latest request; no ids, IPs, user agents, actors or access key ids; the deployment's own
 * requests left out), plus the hour's capture status (`sampled`/`inconsistent` hours hold fewer events than
 * requests, so `n` undercounts). */
export function publicHour(agg: HourAggregate): string {
  return JSON.stringify({
    schema_version: 1, bucket: agg.bucket, hour: agg.hour,
    capture: agg.sampling.capture, observed: agg.sampling.observed, estimated: agg.sampling.estimated,
    max_sample_interval: agg.sampling.max_sample_interval, excluded: agg.own,
    rows: agg.rows.map(([key, action, n, , last_ts]) => ({ key, action, n, last_ts })),
  })
}

export type Phase = 'new' | 'early' | 'verify'

export interface HourEntry {
  /** The aggregate object this hour's latest drain produced (or kept, when unchanged). */
  agg: string
  /** SHA-256 of the aggregate's text: a re-drain with the same result writes nothing. */
  digest: string
  /** The invocation's scheduled time of the latest drain. */
  drained_at: number
  /** The latest drain ran at least `OVERLAP_HOURS` after the hour's end. */
  final: boolean
  events: number
  own: number
  estimated: number
  max_sample_interval: number
  capture: Capture
  pages: number
}

export interface BucketState {
  /** Every hour before it has been drained (or recorded as a gap). */
  collected_through: number
  first_from: number
  gaps: [number, number][]
  /** Where the round-robin re-verify resumes (an hour start), or null before the first. */
  verify_cursor: number | null
  /** `YYYY-MM-DDTHH` → its latest drain, for hours still within `RETENTION_HOURS`. */
  hours: Record<string, HourEntry>
  /** `YYYY-MM-DDTHH` → aggregate key, for hours past retention (never re-drained again). */
  frozen: Record<string, string>
  /** Version-1 per-day aggregates (`agg/<bucket>/<day>/<run>.json`), kept reachable after the upgrade. */
  legacy_days?: Record<string, string>
}

export interface State {
  schema_version: 2
  account: string
  scheduled_time: number
  run_id: string
  buckets: Record<string, BucketState>
}

interface LegacyState {
  schema_version: 1
  account: string
  buckets: Record<string, { collected_through: number, first_from: number, gaps: [number, number][], days: Record<string, string> }>
}

/** A version-1 state (per-day aggregates, a watermark) becomes version 2: the watermark and gaps carry over, and the
 * hours it covered get per-hour aggregates as the round-robin re-verify reaches them. */
export function upgrade(state: State | LegacyState): Record<string, BucketState> {
  if (state.schema_version === 2) return state.buckets
  return Object.fromEntries(Object.entries(state.buckets).map(([bucket, b]) => [bucket, {
    collected_through: b.collected_through, first_from: b.first_from, gaps: b.gaps, verify_cursor: null,
    hours: {}, frozen: {}, ...Object.keys(b.days).length ? { legacy_days: b.days } : {},
  }]))
}

export function hourKey(ms: number): string {
  return new Date(ms).toISOString().slice(0, 13)
}

function hourStart(key: string): number {
  return Date.parse(`${key}:00:00Z`)
}

function cmp(a: string, b: string): number {
  return a < b ? -1 : a > b ? 1 : 0
}

function sorted<T>(record: Record<string, T>): Record<string, T> {
  return Object.fromEntries(Object.entries(record).sort(([a], [b]) => cmp(a, b)))
}

/** Pages one hour's drain may need: the count is an upper bound on the rows the events view returns (weights are
 * ≥ 1), the drain ends on an empty page, and one more page of margin absorbs events delivered between the count
 * and the drain. */
export function reservePages(estimated: number, pageSize: number): number {
  return Math.floor(estimated / pageSize) + 2
}

/** This invocation's hours for one bucket, in priority order (each list oldest first):
 * - `new`: settled hours (`end + LAG_HOURS ≤ scheduled`) from `collected_through` on;
 * - `early`: hours whose latest drain ran within `OVERLAP_HOURS` of their end, once a re-drain would be final;
 * - `verify`: up to `VERIFY_HOURS` drained hours within retention, round-robin from `verify_cursor`.
 * A watermark older than retention allows is clamped to the retention floor, the skipped span a `gap`.
 */
export function plan(config: Settings, prior: BucketState | undefined) {
  const start = prior ? prior.collected_through : config.to - config.lookback
  const from = Math.max(start, config.floor)
  const gap: [number, number] | null = prior && from > prior.collected_through ? [prior.collected_through, from] : null
  const fresh: number[] = []
  for (let hour = from; hour < config.to; hour += HOUR) fresh.push(hour)
  const early = Object.entries(prior?.hours ?? {})
    .map(([key, entry]) => [hourStart(key), entry] as const)
    .filter(([hour, entry]) => !entry.final && hour >= config.floor && hour + HOUR + config.overlap <= config.now)
    .map(([hour]) => hour)
    .sort((a, b) => a - b)
  return { from, gap, fresh, early }
}

/** Round-robin order of the re-verify candidates: collected hours within retention (`[max(floor, first_from),
 * through)`) from `cursor` on, then wrapping. */
export function verifyOrder(config: Settings, state: Pick<BucketState, 'first_from' | 'collected_through' | 'verify_cursor'>): number[] {
  const hours: number[] = []
  const cursor = state.verify_cursor
  for (let hour = Math.max(config.floor, state.first_from); hour < state.collected_through; hour += HOUR) hours.push(hour)
  const at = cursor === null ? 0 : hours.findIndex(hour => hour >= cursor)
  return at <= 0 ? hours : [...hours.slice(at), ...hours.slice(0, at)]
}

async function readJson<T>(archive: Archive, key: string): Promise<{ value: T, etag: string } | null> {
  const found = await archive.get(key)
  if (!found) return null
  return { value: JSON.parse(await found.text()) as T, etag: found.etag }
}

async function sha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('')
}

export interface HourReport {
  hour: string
  phase: Phase
  events: number
  capture: Capture
  changed: boolean
  pages: string[]
}

/** One invocation: per hour (sequentially, so memory is one hour's drain), its count, its raw pages (immutable,
 * private), its aggregate (a new immutable key when changed) and its public file; then ONE conditional
 * `state.json` write that repoints every drained hour and advances `collected_through` (the atomic publish); then
 * the run's `_SUCCESS.json`. A failure before the state write publishes nothing (a public file already rewritten
 * is rewritten again by the next drain of its hour); a concurrent run that published first makes this one fail
 * with `state_conflict`. An hour too large for one invocation (`hour_over_cap`) holds its bucket's watermark,
 * is recorded in the manifest, and fails the run after the publish, so it alerts every hour until fixed.
 */
export async function collect(
  scheduledTime: number,
  cron: string,
  env: CollectorEnv,
  fetchImpl: typeof fetch = fetch,
  runId: string = crypto.randomUUID(),
) {
  const config = settings(env, cron, scheduledTime)
  if (!/^[a-zA-Z0-9-]+$/.test(runId)) throw new CollectionError('invalid_run_id')
  const prefix = `runs/${scheduledTime}/${runId}`
  const loaded = await readJson<State | LegacyState>(env.ARCHIVE, STATE_KEY)
  if (loaded && ((loaded.value.schema_version !== 1 && loaded.value.schema_version !== 2) || loaded.value.account !== env.ACCOUNT_ID)) throw new CollectionError('invalid_state')
  const previous = loaded ? upgrade(loaded.value) : {}
  const query = async (body: object) => {
    const response = await fetchImpl(`https://api.cloudflare.com/client/v4/accounts/${env.ACCOUNT_ID}/workers/observability/telemetry/query`, {
      method: 'POST', headers: { authorization: `Bearer ${env.CF_OBSERVABILITY_TOKEN}`, 'content-type': 'application/json' },
      body: JSON.stringify(body), signal: AbortSignal.timeout(30_000),
      // Workers' fetch takes only `follow` / `manual` (`error` throws a TypeError: the first scheduled run, 10-17 04:13Z,
      // failed on it in 151 ms); a manual 3xx isn't `ok`, so it fails the run as `provider_http_3xx`.
      redirect: 'manual',
    })
    if (!response.ok) {
      await response.body?.cancel()
      throw new CollectionError(`provider_http_${response.status}`)
    }
    return boundedText(response, config.maxBytes)
  }
  let pagesLeft = config.maxPages
  const superseded: string[] = []
  let hoursLeft = config.maxHours

  /** Drains one hour into `state.hours`, or returns 'wait' when it doesn't fit what's left of the budget. */
  const drain = async (bucket: string, state: BucketState, hour: number, phase: Phase, reports: HourReport[]): Promise<'done' | 'wait'> => {
    if (!hoursLeft) return 'wait'
    hoursLeft--
    const to = hour + HOUR
    const { estimated, maxInterval } = parseCalculation(await query(calculationBody(bucket, hour, to)))
    const reserve = reservePages(estimated, config.pageSize)
    if (reserve > config.maxPages) throw new CollectionError('hour_over_cap')
    if (reserve > pagesLeft) return 'wait'
    const acc = new HourAccumulator()
    const cursors = new Set<string>()
    const keys: string[] = []
    let offset: string | undefined
    for (;;) {
      // Late deliveries outgrew the reservation and the invocation's budget: the hour waits (its raw pages are
      // unpublished and expire with `runs/`).
      if (!pagesLeft) return 'wait'
      const text = await query(eventsBody(bucket, hour, to, config.pageSize, offset))
      const page = parsePage(text, bucket, hour, to, config.excluded)
      pagesLeft--
      if (page.length > config.pageSize) throw new CollectionError('page_size_exceeded')
      // A short page of events already drained is the end: an event delivered twice sorts beside its copy, so the
      // cursor at the last event's first copy returns the second (marin-na 2026-10-17T16 ended on one such page
      // where every other hour ended on an empty one). The count reconciliation still flags anything missing.
      if (page.length && page.length < config.pageSize && page.every(event => acc.has(event.id))) break
      const next = page.at(-1)?.id
      if (next && cursors.has(next)) throw new CollectionError('cursor_stalled')
      const key = `${prefix}/${bucket}/${hourKey(hour)}/${String(keys.length).padStart(4, '0')}.json`
      await env.ARCHIVE.put(key, text, { httpMetadata: { contentType: 'application/json' } })
      keys.push(key)
      for (const event of page) acc.add(event)
      if (!next) break
      cursors.add(next)
      offset = next
    }
    const agg = acc.finish(bucket, hour, estimated, maxInterval)
    const text = JSON.stringify(agg)
    const digest = await sha256(text)
    const known = state.hours[hourKey(hour)]
    const changed = known?.digest !== digest
    let aggKey = known?.agg ?? ''
    if (changed) {
      if (known?.agg) superseded.push(known.agg)
      aggKey = `agg/${bucket}/${agg.hour.slice(0, 10)}/${agg.hour.slice(11, 13)}/${runId}.json`
      await env.ARCHIVE.put(aggKey, text, { httpMetadata: { contentType: 'application/json' } })
      // Rewritten whenever the hour's aggregate is replaced, before the state publish.
      if (config.published.includes(bucket)) {
        await env.PUBLIC!.put(`${config.publicPrefix}/${bucket}/${agg.hour.slice(0, 10)}/${agg.hour.slice(11, 13)}.json`, publicHour(agg), { httpMetadata: { contentType: 'application/json' } })
      }
    }
    state.hours[hourKey(hour)] = {
      agg: aggKey, digest, drained_at: config.now, final: config.now >= to + config.overlap,
      events: agg.events, own: agg.own, estimated, max_sample_interval: maxInterval, capture: agg.sampling.capture, pages: keys.length,
    }
    reports.push({ hour: agg.hour, phase, events: agg.events, capture: agg.sampling.capture, changed, pages: keys })
    return 'done'
  }

  const buckets: Record<string, BucketState> = { ...previous }
  const runs = config.buckets.map(bucket => {
    const prior = previous[bucket]
    const planned = plan(config, prior)
    const state: BucketState = {
      collected_through: planned.from, first_from: prior?.first_from ?? planned.from,
      gaps: [...prior?.gaps ?? [], ...planned.gap ? [planned.gap] : []],
      verify_cursor: prior?.verify_cursor ?? null,
      hours: { ...prior?.hours }, frozen: { ...prior?.frozen },
      ...prior?.legacy_days ? { legacy_days: prior.legacy_days } : {},
    }
    return { bucket, planned, state, reports: [] as HourReport[], waiting: 0, error: null as string | null }
  })
  const overCap = async <T>(run: typeof runs[number], body: () => Promise<T>): Promise<T | 'over_cap'> => {
    try { return await body() } catch (error) {
      if (!(error instanceof CollectionError) || error.code !== 'hour_over_cap') throw error
      run.error = error.code
      return 'over_cap'
    }
  }
  // 1. New settled hours, oldest first; the watermark advances only over contiguous drained hours.
  for (const run of runs) {
    const { fresh } = run.planned
    for (let i = 0; i < fresh.length; i++) {
      const result = await overCap(run, () => drain(run.bucket, run.state, fresh[i], 'new', run.reports))
      if (result !== 'done') { run.waiting = fresh.length - i; break }
      run.state.collected_through = fresh[i] + HOUR
    }
  }
  // 2. Re-drains of hours last drained early.
  for (const run of runs) {
    for (const hour of run.planned.early) {
      if (await overCap(run, () => drain(run.bucket, run.state, hour, 'early', run.reports)) !== 'done') break
    }
  }
  // 3. A bounded round-robin re-verify of older hours within retention.
  for (const run of runs) {
    const done = new Set(run.reports.map(r => Date.parse(r.hour)))
    let budget = config.verifyHours
    for (const hour of verifyOrder(config, run.state)) {
      if (!budget) break
      if (done.has(hour)) continue
      budget--
      if (await overCap(run, () => drain(run.bucket, run.state, hour, 'verify', run.reports)) !== 'done') break
      run.state.verify_cursor = hour + HOUR
    }
  }
  for (const run of runs) {
    const hours: Record<string, HourEntry> = {}
    for (const [key, entry] of Object.entries(run.state.hours)) {
      if (hourStart(key) >= config.floor) hours[key] = entry
      else run.state.frozen[key] = entry.agg
    }
    run.state.hours = sorted(hours)
    run.state.frozen = sorted(run.state.frozen)
    buckets[run.bucket] = run.state
  }
  const state: State = { schema_version: 2, account: env.ACCOUNT_ID, scheduled_time: scheduledTime, run_id: runId, buckets: sorted(buckets) }
  // The publish point: aggregates above are immutable new keys, unreachable
  // until this single object names them. A null result is a failed precondition.
  const published = await env.ARCHIVE.put(STATE_KEY, JSON.stringify(state), {
    httpMetadata: { contentType: 'application/json' },
    ...loaded ? { onlyIf: { etagMatches: loaded.etag } } : {},
  })
  if (!published) throw new CollectionError('state_conflict')
  const manifest = {
    schema_version: 3, account: env.ACCOUNT_ID, scheduled_time: scheduledTime, run_id: runId,
    provider_delivery: 'best_effort',
    budget: { pages: config.maxPages, pages_used: config.maxPages - pagesLeft, hours: config.maxHours, hours_used: config.maxHours - hoursLeft },
    buckets: runs.map(run => ({
      bucket: run.bucket, gap: run.planned.gap, collected_through: run.state.collected_through,
      waiting: run.waiting, error: run.error, hours: run.reports,
    })),
  }
  await env.ARCHIVE.put(`${prefix}/_SUCCESS.json`, JSON.stringify(manifest), { httpMetadata: { contentType: 'application/json' } })
  // Replaced aggregates are unreachable once the state above names their successors: delete them in one batch
  // (at marin-na's volume each version is ~5 MB/hour). A failure here leaves orphans, never a dangling state.
  if (superseded.length) await env.ARCHIVE.delete(superseded)
  if (runs.some(run => run.error)) throw new CollectionError('hour_over_cap')
  const reports = runs.flatMap(run => run.reports)
  return {
    run_id: runId, buckets: runs.length, hours: reports.length, pages: config.maxPages - pagesLeft,
    events: reports.reduce((sum, r) => sum + r.events, 0), changed: reports.filter(r => r.changed).length,
    waiting: runs.reduce((sum, run) => sum + run.waiting, 0),
  }
}

export default {
  async fetch(): Promise<Response> { return new Response('Not found\n', { status: 404 }) },
  async scheduled(event: ScheduledController, env: CollectorEnv): Promise<void> {
    try {
      const summary = await collect(event.scheduledTime, event.cron, env)
      console.log(JSON.stringify({ message: 'r2-access archived', ...summary }))
    } catch (error) {
      // Never emit provider response bodies, raw keys, IPs, actors or tokens.
      const code = error instanceof CollectionError ? error.code : 'collection_failed'
      console.error(JSON.stringify({ message: 'r2-access failed', code }))
      throw new Error(`r2-access: ${code}`)
    }
  },
} satisfies ExportedHandler<Env>
