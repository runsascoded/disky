/** Private, bounded R2 Observability archive; no deletion-safety claims. */
const HOUR = 3_600_000
type CollectorEnv = Omit<Env, 'ARCHIVE'> & { ARCHIVE: Pick<Env['ARCHIVE'], 'put'> }

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
  if (cron !== env.HOURLY_CRON && cron !== env.DAILY_CRON) throw new CollectionError('unwired_cron')
  const lag = integer(env.LAG_HOURS, 1, 24)
  const lookback = integer(cron === env.DAILY_CRON ? env.REPLAY_HOURS : env.LOOKBACK_HOURS, 1, 144)
  if (lag + lookback > 166 || !Number.isSafeInteger(scheduledTime) || scheduledTime < 0) throw new CollectionError('invalid_config')
  const to = Math.floor(scheduledTime / HOUR) * HOUR - lag * HOUR
  return {
    buckets, from: to - lookback * HOUR, to,
    pageSize: integer(env.PAGE_SIZE, 1, 2000),
    maxPages: integer(env.MAX_PAGES, 1, 100),
    maxBytes: integer(env.MAX_PAGE_BYTES, 1024, 8_388_608),
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

export function parsePage(text: string, bucket: string) {
  const response = object(JSON.parse(text))
  if (response.success !== true) throw new CollectionError('provider_error')
  const result = object(response.result)
  const run = object(result.run)
  if (run.status !== 'COMPLETED') throw new CollectionError('query_incomplete')
  const statistics = object(result.statistics ?? run.statistics)
  if (statistics.abr_level != null && statistics.abr_level !== 1) throw new CollectionError('query_sampled')
  const page = object(result.events)
  if (!Array.isArray(page.events) || page.count !== page.events.length) throw new CollectionError('page_count_mismatch')
  return page.events.map((value: unknown) => {
    const event = object(value)
    const metadata = object(event.$metadata)
    const source = object(event.source)
    if (event.dataset !== 'r2' || source.bucket !== bucket || typeof metadata.id !== 'string' || !metadata.id) throw new CollectionError('invalid_event')
    if (typeof source.action !== 'string') throw new CollectionError('invalid_event')
    return { id: metadata.id, action: source.action }
  })
}

/** One invocation's immutable pages + final manifest. Replays overlap by ID;
 * no mutable watermark or global latest pointer races across cron invocations.
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
  const windows = []
  let pages = 0
  for (const bucket of config.buckets) {
    const seen = new Set<string>()
    const cursors = new Set<string>()
    const actions: Record<string, number> = Object.create(null)
    const keys: string[] = []
    let offset: string | undefined
    for (;;) {
      if (pages >= config.maxPages) throw new CollectionError('page_cap_exceeded')
      const body = {
        queryId: 'disky-r2-archive', dry: true, view: 'events', limit: config.pageSize,
        timeframe: { from: config.from, to: config.to },
        parameters: { datasets: ['r2'], filters: [{ key: 'bucket', operation: 'eq', type: 'string', value: bucket }], filterCombination: 'and' },
        ...(offset ? { offset, offsetDirection: 'next' } : {}),
      }
      const response = await fetchImpl(`https://api.cloudflare.com/client/v4/accounts/${env.ACCOUNT_ID}/workers/observability/telemetry/query`, {
        method: 'POST', headers: { authorization: `Bearer ${env.CF_OBSERVABILITY_TOKEN}`, 'content-type': 'application/json' },
        body: JSON.stringify(body), signal: AbortSignal.timeout(30_000), redirect: 'error',
      })
      if (!response.ok) {
        await response.body?.cancel()
        throw new CollectionError(`provider_http_${response.status}`)
      }
      const text = await boundedText(response, config.maxBytes)
      const events = parsePage(text, bucket)
      pages++
      if (events.length > config.pageSize) throw new CollectionError('page_size_exceeded')
      const next = events.at(-1)?.id
      if (next && cursors.has(next)) throw new CollectionError('cursor_stalled')
      const key = `${prefix}/${bucket}/${String(keys.length).padStart(4, '0')}.json`
      await env.ARCHIVE.put(key, text, { httpMetadata: { contentType: 'application/json' } })
      keys.push(key)
      for (const event of events) {
        if (!seen.has(event.id)) {
          seen.add(event.id)
          actions[event.action] = (actions[event.action] ?? 0) + 1
        }
      }
      if (!next) break
      cursors.add(next)
      offset = next
    }
    windows.push({ bucket, from: config.from, to: config.to, events: seen.size, actions, pages: keys, query_exhausted: true })
  }
  const manifest = {
    schema_version: 1, account: env.ACCOUNT_ID, scheduled_time: scheduledTime, run_id: runId,
    provider_delivery: 'best_effort', capture_sampling: 'unknown', windows,
  }
  // Only this marker publishes the attempt. Earlier pages without it are
  // incomplete and must not be used to claim a drained window.
  await env.ARCHIVE.put(`${prefix}/_SUCCESS.json`, JSON.stringify(manifest), { httpMetadata: { contentType: 'application/json' } })
  return { run_id: runId, buckets: windows.length, pages, events: windows.reduce((sum, w) => sum + w.events, 0) }
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
