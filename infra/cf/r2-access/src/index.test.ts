import { afterEach, describe, expect, it, vi } from 'vitest'
import config from '../wrangler.json'
import worker, { actionClass, boundedText, capture, collect, parseCalculation, parsePage, settings, span, timeframe } from './index'

const H = 3_600_000
const time = Date.parse('2026-10-12T10:13:00Z')
const to = Date.parse('2026-10-12T08:00:00Z')
const from = Date.parse('2026-10-12T02:00:00Z')
const prefix = (run: string, at = time) => `runs/${at}/${run}`
const json = { httpMetadata: { contentType: 'application/json' } }

/** A provider row: `weight` > 1 is an ingestion-sampled row (counted by
 * calculations, returned once by the events view); `hidden` rows are counted
 * but not yet returned as events (delivered between drain and calculation). */
interface Row { id: string, timestamp: number, action: string, weight?: number, hidden?: boolean }

function eventsPage(rows: Row[], extra: Record<string, unknown> = {}) {
  return JSON.stringify({
    success: true,
    result: {
      run: { status: 'COMPLETED' }, statistics: { abr_level: 1 },
      events: { count: rows.length, events: rows.map(r => ({
        dataset: 'r2', timestamp: r.timestamp, $metadata: { id: r.id }, source: { bucket: 'jc-taxes', action: r.action },
      })) }, ...extra,
    },
  })
}

function calculationPage(aggregates: { count: number, sampleInterval: number }[], extra: Record<string, unknown> = {}) {
  return JSON.stringify({ success: true, result: { run: { status: 'COMPLETED' }, statistics: { abr_level: 1 }, calculations: [{ alias: 'events', aggregates }], ...extra } })
}

/** The telemetry API as measured: `from < timestamp < to`, newest first, cursor = last id. */
function provider(rows: Row[], limit = 2) {
  return vi.fn<typeof fetch>(async (_url, init) => {
    const body = JSON.parse(String(init?.body))
    const inWindow = rows.filter(r => r.timestamp > body.timeframe.from && r.timestamp < body.timeframe.to)
    if (body.view === 'calculations') {
      const byAction = new Map<string, Row[]>()
      for (const r of inWindow) byAction.set(r.action, [...byAction.get(r.action) ?? [], r])
      return new Response(calculationPage([...byAction.values()].map(group => {
        const count = group.reduce((sum, r) => sum + (r.weight ?? 1), 0)
        return { count, sampleInterval: count / group.length }
      })))
    }
    const visible = inWindow.filter(r => !r.hidden).sort((a, b) => b.timestamp - a.timestamp)
    const start = body.offset ? visible.findIndex(r => r.id === body.offset) + 1 : 0
    return new Response(eventsPage(visible.slice(start, start + Math.min(limit, body.limit))))
  })
}

function setup(vars: Partial<typeof config.vars> = {}) {
  const archive = new Map<string, { text: string, etag: string }>()
  const order: string[] = []
  let version = 0
  const put = vi.fn(async (key: string, value: unknown, options?: R2PutOptions) => {
    if (typeof value !== 'string') throw new Error('collector must archive JSON text')
    const match = (options?.onlyIf as R2Conditional | undefined)?.etagMatches
    if (match !== undefined && archive.get(key)?.etag !== match) return null
    archive.set(key, { text: value, etag: `e${++version}` })
    order.push(key)
    return { key } as R2Object
  })
  const get = vi.fn(async (key: string) => {
    const found = archive.get(key)
    return found ? { etag: found.etag, text: async () => found.text } as R2ObjectBody : null
  })
  const env = { ...config.vars, ...vars, CF_OBSERVABILITY_TOKEN: 'test-secret', ARCHIVE: { put, get } as unknown as R2Bucket }
  const read = (key: string) => JSON.parse(archive.get(key)!.text)
  return { archive, order, env, put, read }
}

const at = (iso: string) => Date.parse(iso)
const rows: Row[] = [
  { id: 'a', timestamp: from, action: 'HeadObject' },                      // exactly on the window start: kept
  { id: 'b', timestamp: at('2026-10-12T03:30:00Z'), action: 'GetObject' },
  { id: 'c', timestamp: at('2026-10-12T07:59:59.999Z'), action: 'ListObjectsV2' },
  { id: 'z', timestamp: to, action: 'HeadObject' },                        // exactly on the window end: next window's
]

afterEach(() => vi.restoreAllMocks())

describe('windows', () => {
  it('derives delayed hourly and daily settings', () => {
    const { env } = setup()
    const base = { buckets: ['jc-taxes'], to, lookback: 6 * H, overlap: H, floor: to - 144 * H, pageSize: 500, maxPages: 50, maxBytes: 4194304 }
    expect(settings(env, env.HOURLY_CRON, time)).toEqual({ ...base, replay: false })
    expect(settings(env, env.DAILY_CRON, time)).toEqual({ ...base, replay: true })
  })

  it.each([
    { SOURCE_BUCKETS: '["disky-r2-access-logs"]' },
    { SOURCE_BUCKETS: '["jc-taxes","jc-taxes"]' },
    { MAX_PAGES: '101' },
    { MAX_PAGE_BYTES: '999999999' },
    { LAG_HOURS: '24', REPLAY_HOURS: '144' },
    { OVERLAP_HOURS: '0' },
    { LOOKBACK_HOURS: '7', REPLAY_HOURS: '6' },
  ])('rejects unsafe configuration %j', vars => {
    const { env } = setup(vars)
    expect(() => settings(env, env.DAILY_CRON, time)).toThrow('r2-access: invalid_config')
  })

  it('rejects an unexpected cron', () => {
    expect(() => settings(setup().env, 'unwired', time)).toThrow('r2-access: unwired_cron')
  })

  it('starts from the lookback, then re-queries the overlap before the watermark', () => {
    const { env } = setup()
    const hourly = settings(env, env.HOURLY_CRON, time)
    expect(span(hourly, undefined)).toEqual({ from, to, gap: null })
    expect(span(hourly, to - H)).toEqual({ from: to - 2 * H, to, gap: null })
    expect(span(hourly, to)).toEqual({ from: to - H, to, gap: null })
    expect(span(settings(env, env.DAILY_CRON, time), to - H)).toEqual({ from: to - 144 * H, to, gap: null })
  })

  it('reports an unrecoverable gap when the watermark is older than retention allows', () => {
    const { env } = setup()
    expect(span(settings(env, env.HOURLY_CRON, time), to - 200 * H)).toEqual({ from: to - 144 * H, to, gap: [to - 200 * H, to - 144 * H] })
  })

  it('refuses a window that would end at or before its start', () => {
    const { env } = setup({ OVERLAP_HOURS: '1' })
    expect(() => span(settings(env, env.HOURLY_CRON, time), to + 2 * H)).toThrow('r2-access: window_regressed')
  })

  it('sends from-1 so the provider\'s exclusive bounds become [from, to)', () => {
    expect(timeframe(from, to)).toEqual({ from: from - 1, to })
  })
})

describe('sampling', () => {
  it('sums weighted counts and keeps the largest sample interval', () => {
    expect(parseCalculation(calculationPage([{ count: 178, sampleInterval: 1.0532544378698225 }, { count: 4, sampleInterval: 1 }]))).toEqual({ estimated: 182, maxInterval: 1.0532544378698225 })
    expect(parseCalculation(calculationPage([]))).toEqual({ estimated: 0, maxInterval: 1 })
  })

  it.each([
    [{ statistics: { abr_level: 10 } }, 'query_sampled'],
    [{ run: { status: 'RUNNING' } }, 'query_incomplete'],
    [{ calculations: [] }, 'invalid_calculation'],
  ])('rejects sampled/incomplete/malformed calculations %j', (extra, code) => {
    expect(() => parseCalculation(calculationPage([], extra))).toThrow(`r2-access: ${code}`)
  })

  it('classifies an hour as sampled, unsampled or inconsistent', () => {
    expect([capture(169, 178, 1.05), capture(4, 4, 1), capture(4, 5, 1), capture(5, 4, 1)]).toEqual(['sampled', 'unsampled', 'inconsistent', 'inconsistent'])
  })

  it('separates content reads from metadata reads, listings, writes and deletes', () => {
    expect(['GetObject', 'HeadObject', 'ListObjectsV2', 'PutObject', 'DeleteObject', 'Mystery'].map(actionClass))
      .toEqual(['content_read', 'metadata_read', 'list', 'write', 'delete', 'other'])
  })
})

describe('collect', () => {
  const hours = (cells: Record<string, [number, number, number, string]>) => Object.fromEntries(Object.entries(cells).map(
    ([hour, [observed, estimated, max_sample_interval, capture]]) => [hour, { observed, estimated, max_sample_interval, capture }]))
  const quiet = (from: number, to: number) => Object.fromEntries(Array.from({ length: (to - from) / H }, (_, i) =>
    [new Date(from + i * H).toISOString().slice(11, 13), [0, 0, 1, 'unsampled'] as [number, number, number, string]]))

  it('drains, aggregates, then publishes state before the success marker', async () => {
    const { env, order, read } = setup()
    const fetcher = provider(rows)
    expect(await collect(time, env.HOURLY_CRON, env, fetcher, 'r1')).toEqual({ run_id: 'r1', buckets: 1, pages: 3, events: 3, new_events: 3 })
    const agg = 'agg/jc-taxes/2026-10-12/r1.json'
    expect(order).toEqual([`${prefix('r1')}/jc-taxes/0000.json`, `${prefix('r1')}/jc-taxes/0001.json`, `${prefix('r1')}/jc-taxes/0002.json`, agg, 'state.json', `${prefix('r1')}/_SUCCESS.json`])
    expect(read(agg)).toEqual({
      schema_version: 1, bucket: 'jc-taxes', day: '2026-10-12',
      events: { a: [from, 'HeadObject'], b: [rows[1].timestamp, 'GetObject'], c: [rows[2].timestamp, 'ListObjectsV2'] },
      actions: { GetObject: 1, HeadObject: 1, ListObjectsV2: 1 },
      classes: { content_read: 1, list: 1, metadata_read: 1 },
      hours: hours({ ...quiet(from, to), '02': [1, 1, 1, 'unsampled'], '03': [1, 1, 1, 'unsampled'], '07': [1, 1, 1, 'unsampled'] }),
    })
    expect(read('state.json')).toEqual({
      schema_version: 1, account: env.ACCOUNT_ID, scheduled_time: time, run_id: 'r1',
      buckets: { 'jc-taxes': { collected_through: to, first_from: from, gaps: [], days: { '2026-10-12': agg } } },
    })
    expect(read(`${prefix('r1')}/_SUCCESS.json`)).toEqual({
      schema_version: 2, account: env.ACCOUNT_ID, scheduled_time: time, run_id: 'r1', provider_delivery: 'best_effort',
      windows: [{ bucket: 'jc-taxes', from, to, gap: null, events: 3, new_events: 3, hours: { unsampled: 6, sampled: 0, inconsistent: 0 },
        pages: [`${prefix('r1')}/jc-taxes/0000.json`, `${prefix('r1')}/jc-taxes/0001.json`, `${prefix('r1')}/jc-taxes/0002.json`], query_exhausted: true }],
    })
    const bodies = fetcher.mock.calls.map(([, init]) => JSON.parse(String(init?.body)))
    expect(bodies.slice(0, 3)).toEqual([undefined, 'b', 'a'].map(offset => ({
      queryId: 'disky-r2-archive', dry: true, view: 'events', limit: 500, timeframe: { from: from - 1, to },
      parameters: { datasets: ['r2'], filters: [{ key: 'bucket', operation: 'eq', type: 'string', value: 'jc-taxes' }], filterCombination: 'and' },
      ...(offset ? { offset, offsetDirection: 'next' } : {}),
    })))
    expect(bodies.slice(3).map(b => [b.view, b.timeframe.from + 1, b.timeframe.to])).toEqual(
      Array.from({ length: 6 }, (_, i) => ['calculations', from + i * H, from + (i + 1) * H]))
  })

  it('re-queries the overlap, deduplicates by id and picks up late deliveries', async () => {
    const { env, read } = setup()
    const live = [...rows]
    await collect(time, env.HOURLY_CRON, env, provider(live), 'r1')
    // Delivered after r1 drained: timestamped inside r1's (already published) window.
    live.push({ id: 'late', timestamp: at('2026-10-12T07:30:00Z'), action: 'HeadObject' })
    const next = time + H
    expect(await collect(next, env.HOURLY_CRON, env, provider(live), 'r2')).toEqual({ run_id: 'r2', buckets: 1, pages: 3, events: 3, new_events: 2 })
    const agg = 'agg/jc-taxes/2026-10-12/r2.json'
    expect(read(agg).events).toEqual({
      a: [from, 'HeadObject'], b: [rows[1].timestamp, 'GetObject'], c: [rows[2].timestamp, 'ListObjectsV2'],
      late: [live[4].timestamp, 'HeadObject'], z: [to, 'HeadObject'],
    })
    expect(read(agg).hours['07']).toEqual({ observed: 2, estimated: 2, max_sample_interval: 1, capture: 'unsampled' })
    expect(read(agg).hours['08']).toEqual({ observed: 1, estimated: 1, max_sample_interval: 1, capture: 'unsampled' })
    expect(read('state.json').buckets['jc-taxes']).toEqual({ collected_through: to + H, first_from: from, gaps: [], days: { '2026-10-12': agg } })
    expect(read(`${prefix('r2', next)}/_SUCCESS.json`).windows[0]).toEqual({
      bucket: 'jc-taxes', from: to - H, to: to + H, gap: null, events: 3, new_events: 2,
      hours: { unsampled: 2, sampled: 0, inconsistent: 0 }, pages: [`${prefix('r2', next)}/jc-taxes/0000.json`, `${prefix('r2', next)}/jc-taxes/0001.json`, `${prefix('r2', next)}/jc-taxes/0002.json`], query_exhausted: true,
    })
  })

  it('is idempotent: a replay with nothing new republishes state but no aggregate', async () => {
    const { env, order, read } = setup()
    await collect(time, env.HOURLY_CRON, env, provider(rows), 'r1')
    order.length = 0
    expect(await collect(time, env.HOURLY_CRON, env, provider(rows), 'r2')).toEqual({ run_id: 'r2', buckets: 1, pages: 2, events: 1, new_events: 0 })
    expect(order).toEqual([`${prefix('r2')}/jc-taxes/0000.json`, `${prefix('r2')}/jc-taxes/0001.json`, 'state.json', `${prefix('r2')}/_SUCCESS.json`])
    expect(read('state.json').buckets['jc-taxes'].days).toEqual({ '2026-10-12': 'agg/jc-taxes/2026-10-12/r1.json' })
  })

  it('marks ingestion-sampled and inconsistent hours without inflating observed counts', async () => {
    const { env, read } = setup()
    await collect(time, env.HOURLY_CRON, env, provider([
      { id: 's', timestamp: at('2026-10-12T04:10:00Z'), action: 'ListObjectsV2', weight: 2 },
      { id: 'h', timestamp: at('2026-10-12T05:10:00Z'), action: 'GetObject', hidden: true },
    ]), 'r1')
    const agg = read('agg/jc-taxes/2026-10-12/r1.json')
    expect([agg.events, agg.hours['04'], agg.hours['05']]).toEqual([
      { s: [at('2026-10-12T04:10:00Z'), 'ListObjectsV2'] },
      { observed: 1, estimated: 2, max_sample_interval: 2, capture: 'sampled' },
      { observed: 0, estimated: 1, max_sample_interval: 1, capture: 'inconsistent' },
    ])
  })

  it('splits aggregates at UTC day boundaries', async () => {
    const { env, read } = setup()
    const night = Date.parse('2026-10-13T03:13:00Z')
    const edge = Date.parse('2026-10-13T00:00:00Z')
    await collect(night, env.HOURLY_CRON, env, provider([
      { id: 'd1', timestamp: edge - 1, action: 'HeadObject' },
      { id: 'd2', timestamp: edge, action: 'HeadObject' },
    ]), 'r1')
    expect(read('state.json').buckets['jc-taxes'].days).toEqual({
      '2026-10-12': 'agg/jc-taxes/2026-10-12/r1.json', '2026-10-13': 'agg/jc-taxes/2026-10-13/r1.json',
    })
    expect([read('agg/jc-taxes/2026-10-12/r1.json').events, read('agg/jc-taxes/2026-10-13/r1.json').events]).toEqual([
      { d1: [edge - 1, 'HeadObject'] }, { d2: [edge, 'HeadObject'] },
    ])
  })

  it('records a gap when the previous watermark fell out of retention', async () => {
    const { env, read } = setup()
    await collect(time, env.HOURLY_CRON, env, provider([]), 'r1')
    const later = time + 200 * H
    await collect(later, env.HOURLY_CRON, env, provider([]), 'r2')
    expect(read('state.json').buckets['jc-taxes'].gaps).toEqual([[to, to + 200 * H - 144 * H]])
  })

  it('fails without publishing state or success when the page cap is exhausted', async () => {
    const { env, order } = setup({ MAX_PAGES: '1' })
    await expect(collect(time, env.HOURLY_CRON, env, provider(rows), 'r1')).rejects.toThrow('r2-access: page_cap_exceeded')
    expect(order).toEqual([`${prefix('r1')}/jc-taxes/0000.json`])
  })

  it('refuses to publish over a concurrently advanced state', async () => {
    const { env, archive, order } = setup()
    await collect(time, env.HOURLY_CRON, env, provider(rows), 'r1')
    const fetcher = provider(rows)
    const racing = vi.fn<typeof fetch>(async (...args) => {
      archive.set('state.json', { ...archive.get('state.json')!, etag: 'other-run' })
      return fetcher(...args)
    })
    order.length = 0
    await expect(collect(time + H, env.HOURLY_CRON, env, racing, 'r2')).rejects.toThrow('r2-access: state_conflict')
    expect(order).toEqual([`${prefix('r2', time + H)}/jc-taxes/0000.json`, `${prefix('r2', time + H)}/jc-taxes/0001.json`, 'agg/jc-taxes/2026-10-12/r2.json'])
  })

  it('rejects repeated cursors before archiving the repeated page', async () => {
    const { env, order } = setup()
    const page = eventsPage([rows[1]])
    const fetcher = vi.fn<typeof fetch>(async () => new Response(page))
    await expect(collect(time, env.HOURLY_CRON, env, fetcher, 'r1')).rejects.toThrow('r2-access: cursor_stalled')
    expect(order).toEqual([`${prefix('r1')}/jc-taxes/0000.json`])
  })
})

describe('pages', () => {
  it.each([
    [{ run: { status: 'RUNNING' } }, 'query_incomplete'],
    [{ statistics: { abr_level: 2 } }, 'query_sampled'],
    [{ events: { count: 1, events: [] } }, 'page_count_mismatch'],
  ])('rejects incomplete/sampled/mismatched pages %j', (extra, code) => {
    expect(() => parsePage(eventsPage([], extra), 'jc-taxes', from, to)).toThrow(`r2-access: ${code}`)
  })

  it('rejects responses outside the requested bucket or window', () => {
    expect(() => parsePage(eventsPage([rows[0]]), 'other-bucket', from, to)).toThrow('r2-access: invalid_event')
    expect(() => parsePage(eventsPage([rows[3]]), 'jc-taxes', from, to)).toThrow('r2-access: event_outside_window')
  })

  it('bounds streamed response bytes and cancels over-cap bodies', async () => {
    const cancel = vi.fn()
    const response = new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('12345')) }, cancel }))
    await expect(boundedText(response, 4)).rejects.toThrow('r2-access: page_bytes_exceeded')
    expect(cancel.mock.calls).toEqual([[undefined]])
    expect(await boundedText(new Response('1234'), 4)).toBe('1234')
  })
})

describe('handlers', () => {
  it('never exposes an HTTP collection trigger', async () => {
    const response = await worker.fetch()
    expect({ status: response.status, body: await response.text() }).toEqual({ status: 404, body: 'Not found\n' })
  })

  it('logs only a safe error code for an unexpected provider/network failure', async () => {
    const fetcher = vi.spyOn(globalThis, 'fetch').mockRejectedValue(new Error('private-object-key test-secret'))
    const log = vi.spyOn(console, 'error').mockImplementation(() => {})
    const { env } = setup()
    await expect(worker.scheduled({ scheduledTime: time, cron: env.HOURLY_CRON, noRetry() {} }, env)).rejects.toThrow('r2-access: collection_failed')
    expect(fetcher.mock.calls.length).toBe(1)
    expect(log.mock.calls).toEqual([[JSON.stringify({ message: 'r2-access failed', code: 'collection_failed' })]])
  })
})
