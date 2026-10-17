/** Private, bounded R2 Observability archive; no deletion-safety claims. */
const HOUR = 3_600_000
const DAY = 24 * HOUR
const STATE_KEY = 'state.json'
type Archive = Pick<Env['ARCHIVE'], 'put' | 'get'>
type CollectorEnv = Omit<Env, 'ARCHIVE' | 'PUBLIC' | 'PUBLIC_BUCKETS' | 'PUBLIC_PREFIX' | 'EXCLUDED_ACCESS_KEY_IDS'> & {
  ARCHIVE: Archive
  /** Optional: where the derived, public read-recency files go (`PUBLIC_BUCKETS` names which sources). */
  PUBLIC?: Pick<Env['ARCHIVE'], 'put'>
  PUBLIC_BUCKETS?: string
  PUBLIC_PREFIX?: string
  /** Secret, optional: a JSON array of access key ids whose requests are the deployment's own (its scanner's
   * LISTs/HEADs). They stay in the private aggregates (the sampling check counts them) but never reach `PUBLIC`. */
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
  if (!Number.isSafeInteger(n) || n < min || n > max) throw new CollectionError('invalid_config')
  return n
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
  if (cron !== env.HOURLY_CRON && cron !== env.DAILY_CRON) throw new CollectionError('unwired_cron')
  const lag = integer(env.LAG_HOURS, 1, 24)
  const lookback = integer(env.LOOKBACK_HOURS, 1, 144)
  const replay = integer(env.REPLAY_HOURS, 1, 144)
  const overlap = integer(env.OVERLAP_HOURS, 1, 24)
  if (lag + replay > 166 || lookback > replay || overlap > replay || !Number.isSafeInteger(scheduledTime) || scheduledTime < 0) throw new CollectionError('invalid_config')
  const to = Math.floor(scheduledTime / HOUR) * HOUR - lag * HOUR
  return {
    buckets, to, replay: cron === env.DAILY_CRON,
    published: published as string[], publicPrefix: env.PUBLIC_PREFIX ?? '', excluded: new Set(excluded as string[]),
    lookback: lookback * HOUR, overlap: overlap * HOUR, floor: to - replay * HOUR,
    pageSize: integer(env.PAGE_SIZE, 1, 2000),
    maxPages: integer(env.MAX_PAGES, 1, 100),
    maxBytes: integer(env.MAX_PAGE_BYTES, 1024, 8_388_608),
  }
}

export type Settings = ReturnType<typeof settings>

/** This run's half-open window `[from, to)` for one bucket.
 *
 * Hourly runs re-query `overlap` before the published watermark (late
 * delivery measured at 13–25 s; the overlap is far wider); the daily run
 * replays the whole retention-safe span. A watermark older than that floor
 * cannot be recovered, so the skipped span is reported as a gap.
 */
export function span(config: Settings, collectedThrough: number | undefined) {
  const start = config.replay ? config.floor : collectedThrough === undefined ? config.to - config.lookback : collectedThrough - config.overlap
  const from = Math.max(start, config.floor)
  if (from >= config.to) throw new CollectionError('window_regressed')
  const gap: [number, number] | null = collectedThrough !== undefined && from > collectedThrough ? [collectedThrough, from] : null
  return { from, to: config.to, gap }
}

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
export interface Event { id: string, action: string, timestamp: number, key: string | null, status: number | null, own: boolean }

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
    const response = source.response == null ? {} : object(source.response)
    const actor = source.actor == null ? {} : object(source.actor)
    const key = typeof resource.key === 'string' && resource.key ? resource.key : null
    const status = Number.isSafeInteger(response.status) ? response.status as number : null
    const own = typeof actor.accessKeyId === 'string' && excluded.has(actor.accessKeyId)
    return { id: metadata.id, action: source.action, timestamp, key, status, own }
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
export interface HourCell { observed: number, estimated: number, max_sample_interval: number, capture: Capture }

/** `unsampled` only when every stored row has weight 1 AND the weighted count
 * equals the distinct events archived for that hour; a weight > 1 is provider
 * ingestion sampling (permanent); anything else (e.g. an event delivered
 * between the drain and the calculation) is `inconsistent` until a replay. */
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

/** Content reads are kept apart from metadata reads, listings, writes and deletes. */
export function actionClass(action: string): string {
  return CLASSES[action] ?? 'other'
}

/** `[timestamp, action, key, status, own]`; a version-1 day holds `[timestamp, action]` (no key: never published). */
export type DayEvent = [number, string, string | null, number | null, 0 | 1] | [number, string]

export interface DayAggregate {
  schema_version: 1 | 2
  bucket: string
  day: string
  /** Distinct provider events, keyed by immutable event id. */
  events: Record<string, DayEvent>
  actions: Record<string, number>
  classes: Record<string, number>
  /** UTC hour (`"00"`–`"23"`) → the latest sampling check covering it. */
  hours: Record<string, HourCell>
}

export interface BucketState {
  collected_through: number
  first_from: number
  gaps: [number, number][]
  days: Record<string, string>
}

export interface State {
  schema_version: 1
  account: string
  scheduled_time: number
  run_id: string
  buckets: Record<string, BucketState>
}

function utcDay(ms: number): string {
  return new Date(Math.floor(ms / DAY) * DAY).toISOString().slice(0, 10)
}

function hourKey(ms: number): string {
  return new Date(ms).toISOString().slice(11, 13)
}

function sorted<T>(record: Record<string, T>): Record<string, T> {
  return Object.fromEntries(Object.entries(record).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0))
}

export function mergeDay(
  bucket: string,
  day: string,
  previous: DayAggregate | null,
  events: Event[],
  cells: Record<string, { estimated: number, maxInterval: number }>,
): DayAggregate {
  const merged: Record<string, DayEvent> = { ...previous?.events }
  for (const event of events) {
    const known = merged[event.id]
    if (known && (known[0] !== event.timestamp || known[1] !== event.action)) throw new CollectionError('event_identity_changed')
    if (known && known.length > 2 && (known[2] !== event.key || known[3] !== event.status)) throw new CollectionError('event_identity_changed')
    merged[event.id] = [event.timestamp, event.action, event.key, event.status, event.own ? 1 : 0]
  }
  const actions: Record<string, number> = {}
  const classes: Record<string, number> = {}
  const observed: Record<string, number> = {}
  for (const [timestamp, action] of Object.values(merged)) {
    actions[action] = (actions[action] ?? 0) + 1
    const cls = actionClass(action)
    classes[cls] = (classes[cls] ?? 0) + 1
    const hour = hourKey(timestamp)
    observed[hour] = (observed[hour] ?? 0) + 1
  }
  const hours: Record<string, HourCell> = { ...previous?.hours }
  for (const [hour, cell] of Object.entries(cells)) {
    const seen = observed[hour] ?? 0
    hours[hour] = { observed: seen, estimated: cell.estimated, max_sample_interval: cell.maxInterval, capture: capture(seen, cell.estimated, cell.maxInterval) }
  }
  return { schema_version: 2, bucket, day, events: sorted(merged), actions: sorted(actions), classes: sorted(classes), hours: sorted(hours) }
}

/** A day's public read-recency file: one JSON line per event, sorted by (timestamp, id), with only the
 * object key, time, action and status: no IPs, user agents, actors or access key ids. The deployment's own
 * requests (`own`) and version-1 events (no key recorded) are left out. */
export function publicDay(day: DayAggregate): string {
  const lines = Object.entries(day.events)
    .filter((entry): entry is [string, [number, string, string | null, number | null, 0 | 1]] => entry[1].length === 5 && entry[1][4] === 0)
    .sort(([a, [ta]], [b, [tb]]) => ta - tb || (a < b ? -1 : a > b ? 1 : 0))
    .map(([id, [ts, action, key, status]]) => JSON.stringify({ id, bucket: day.bucket, ts, action, key, status }))
  return lines.map(line => `${line}\n`).join('')
}

async function readJson<T>(archive: Archive, key: string): Promise<{ value: T, etag: string } | null> {
  const found = await archive.get(key)
  if (!found) return null
  return { value: JSON.parse(await found.text()) as T, etag: found.etag }
}

/** One invocation: immutable raw pages, then immutable per-day aggregates,
 * then ONE conditional `state.json` write that both repoints the days and
 * advances the watermark (the atomic publish), then the run's `_SUCCESS.json`.
 * A failure anywhere before the state write publishes nothing; a concurrent
 * run that published first makes this one fail with `state_conflict`.
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
  const loaded = await readJson<State>(env.ARCHIVE, STATE_KEY)
  if (loaded && (loaded.value.schema_version !== 1 || loaded.value.account !== env.ACCOUNT_ID)) throw new CollectionError('invalid_state')
  const previous = loaded?.value.buckets ?? {}
  const buckets: Record<string, BucketState> = { ...previous }
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
  const windows = []
  let pages = 0
  for (const bucket of config.buckets) {
    const prior = previous[bucket]
    const { from, to, gap } = span(config, prior?.collected_through)
    const events = new Map<string, Event>()
    const cursors = new Set<string>()
    const keys: string[] = []
    let offset: string | undefined
    for (;;) {
      if (pages >= config.maxPages) throw new CollectionError('page_cap_exceeded')
      const text = await query(eventsBody(bucket, from, to, config.pageSize, offset))
      const page = parsePage(text, bucket, from, to, config.excluded)
      pages++
      if (page.length > config.pageSize) throw new CollectionError('page_size_exceeded')
      const next = page.at(-1)?.id
      if (next && cursors.has(next)) throw new CollectionError('cursor_stalled')
      const key = `${prefix}/${bucket}/${String(keys.length).padStart(4, '0')}.json`
      await env.ARCHIVE.put(key, text, { httpMetadata: { contentType: 'application/json' } })
      keys.push(key)
      for (const event of page) events.set(event.id, event)
      if (!next) break
      cursors.add(next)
      offset = next
    }
    // Sampling per UTC hour, checked after the drain so a late delivery shows
    // as `inconsistent` (fixed by a later overlap) rather than hidden.
    const cells: Record<string, Record<string, { estimated: number, maxInterval: number }>> = {}
    for (let hour = from; hour < to; hour += HOUR) {
      const { estimated, maxInterval } = parseCalculation(await query(calculationBody(bucket, hour, hour + HOUR)))
      ;(cells[utcDay(hour)] ??= {})[hourKey(hour)] = { estimated, maxInterval }
    }
    const byDay = new Map<string, Event[]>()
    for (const event of events.values()) {
      const day = utcDay(event.timestamp)
      byDay.set(day, [...byDay.get(day) ?? [], event])
    }
    const days = { ...prior?.days }
    const hours: Record<Capture, number> = { unsampled: 0, sampled: 0, inconsistent: 0 }
    let fresh = 0
    for (const day of [...new Set([...byDay.keys(), ...Object.keys(cells)])].sort()) {
      const before = days[day] ? await readJson<DayAggregate>(env.ARCHIVE, days[day]) : null
      if (days[day] && !before) throw new CollectionError('missing_aggregate')
      const known = before?.value ?? null
      const merged = mergeDay(bucket, day, known, byDay.get(day) ?? [], cells[day] ?? {})
      fresh += Object.keys(merged.events).length - Object.keys(known?.events ?? {}).length
      for (const hour of Object.keys(cells[day] ?? {})) hours[merged.hours[hour].capture]++
      // Public files are rewritten for every day this window touched (idempotent, and a failed write
      // heals on the next run), before the state publish below.
      if (config.published.includes(bucket)) {
        await env.PUBLIC!.put(`${config.publicPrefix}/${bucket}/${day}.jsonl`, publicDay(merged), { httpMetadata: { contentType: 'application/x-ndjson' } })
      }
      const text = JSON.stringify(merged)
      if (known && JSON.stringify(known) === text) continue
      const key = `agg/${bucket}/${day}/${runId}.json`
      await env.ARCHIVE.put(key, text, { httpMetadata: { contentType: 'application/json' } })
      days[day] = key
    }
    buckets[bucket] = {
      collected_through: Math.max(prior?.collected_through ?? to, to),
      first_from: prior?.first_from ?? from,
      gaps: [...prior?.gaps ?? [], ...gap ? [gap] : []],
      days: sorted(days),
    }
    windows.push({ bucket, from, to, gap, events: events.size, new_events: fresh, hours, pages: keys, query_exhausted: true })
  }
  const state: State = { schema_version: 1, account: env.ACCOUNT_ID, scheduled_time: scheduledTime, run_id: runId, buckets: sorted(buckets) }
  // The publish point: aggregates above are immutable new keys, unreachable
  // until this single object names them. A null result is a failed precondition.
  const published = await env.ARCHIVE.put(STATE_KEY, JSON.stringify(state), {
    httpMetadata: { contentType: 'application/json' },
    ...loaded ? { onlyIf: { etagMatches: loaded.etag } } : {},
  })
  if (!published) throw new CollectionError('state_conflict')
  const manifest = {
    schema_version: 2, account: env.ACCOUNT_ID, scheduled_time: scheduledTime, run_id: runId,
    provider_delivery: 'best_effort', windows,
  }
  await env.ARCHIVE.put(`${prefix}/_SUCCESS.json`, JSON.stringify(manifest), { httpMetadata: { contentType: 'application/json' } })
  return { run_id: runId, buckets: windows.length, pages, events: windows.reduce((sum, w) => sum + w.events, 0), new_events: windows.reduce((sum, w) => sum + w.new_events, 0) }
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
