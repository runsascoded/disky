import { afterEach, describe, expect, it, vi } from 'vitest'
import config from '../wrangler.json'
import worker, {
  actionClass, boundedText, capture, collect, HourAccumulator, type HourAggregate, minute, parseCalculation, parsePage, plan,
  publicHour, reservePages, settings, subrequestBound, timeframe, upgrade, verifyOrder,
} from './index'

const H = 3_600_000
const M = 60_000
const time = Date.parse('2026-10-12T10:13:00Z')
const to = Date.parse('2026-10-12T09:00:00Z')    // LAG_HOURS 1
const from = Date.parse('2026-10-12T03:00:00Z')  // LOOKBACK_HOURS 6
const at = (iso: string) => Date.parse(iso)
const hr = (h: string, day = '12') => at(`2026-10-${day}T${h}:00:00Z`)
const prefix = (run: string, when = time) => `runs/${when}/${run}`
const page = (run: string, hour: string, n: number, when = time) => `${prefix(run, when)}/jc-taxes/2026-10-12T${hour}/${String(n).padStart(4, '0')}.json`
const agg = (hour: string, run: string) => `agg/jc-taxes/2026-10-12/${hour}/${run}.json`

/** A provider row: `weight` > 1 is an ingestion-sampled row (counted by
 * calculations, returned once by the events view); `hidden` rows are counted
 * but not yet returned as events (delivered between count and drain). */
interface Row { id: string, timestamp: number, action: string, weight?: number, hidden?: boolean, key?: string, keyId?: string }

function eventsPage(rows: Row[], extra: Record<string, unknown> = {}) {
  return JSON.stringify({
    success: true,
    result: {
      run: { status: 'COMPLETED' }, statistics: { abr_level: 1 },
      events: { count: rows.length, events: rows.map(r => ({
        dataset: 'r2', timestamp: r.timestamp, $metadata: { id: r.id }, source: {
          bucket: 'jc-taxes', action: r.action,
          ...r.key ? { resource: { key: r.key, type: 'object', size: 5 } } : {},
          response: { status: 200, bytes: 5 },
          ...r.keyId ? { actor: { type: 'S3', id: 'a-user', accessKeyId: r.keyId }, requestMetadata: { ip: '192.0.2.1', userAgent: 'ua' } } : {},
        },
      })) }, ...extra,
    },
  })
}

function calculationPage(aggregates: { count: number, sampleInterval: number }[], extra: Record<string, unknown> = {}) {
  return JSON.stringify({ success: true, result: { run: { status: 'COMPLETED' }, statistics: { abr_level: 1 }, calculations: [{ alias: 'events', aggregates }], ...extra } })
}

/** The telemetry API as measured: `from < timestamp < to`, newest first, cursor = last id; `limit` caps a page
 * below the request's own `limit` (small pages exercise pagination). `rows` is read on every call: tests push
 * late deliveries into it. */
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
    const visible = inWindow.filter(r => !r.hidden).sort((a, b) => b.timestamp - a.timestamp || (a.id < b.id ? 1 : -1))
    const start = body.offset ? visible.findIndex(r => r.id === body.offset) + 1 : 0
    return new Response(eventsPage(visible.slice(start, start + Math.min(limit, body.limit))))
  })
}

function setup(vars: Partial<typeof config.vars> = {}) {
  const archive = new Map<string, { text: string, etag: string }>()
  const order: string[] = []
  const pub = new Map<string, string>()
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
  const del = vi.fn(async (keys: string | string[]) => {
    for (const key of [keys].flat()) archive.delete(key)
    order.push(`delete:${[keys].flat().join(',')}`)
  })
  const PUBLIC = { put: vi.fn(async (key: string, value: unknown) => { pub.set(key, String(value)); order.push(`public:${key}`); return { key } as R2Object }) }
  // The deployed public publish is opted into per test (`PUBLIC_*`).
  const { PUBLIC_BUCKETS: _buckets, PUBLIC_PREFIX: _prefix, ...deployed } = config.vars
  const env = { ...deployed, ...vars, CF_OBSERVABILITY_TOKEN: 'test-secret', ARCHIVE: { put, get, delete: del } as unknown as R2Bucket, PUBLIC }
  const read = (key: string) => JSON.parse(archive.get(key)!.text)
  return { archive, order, env, put, read, pub }
}

async function sha(text: string) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('')
}

/** The rows drained by the first run (hours 03–08); `z` sits on `to` (next run's), `early` before `from`. */
const rows: Row[] = [
  { id: 'early', timestamp: from - 1, action: 'GetObject', key: 'data/a' },
  { id: 'a', timestamp: from, action: 'HeadObject', key: 'data/a' },                 // exactly on the hour start: kept
  { id: 'b', timestamp: at('2026-10-12T03:30:10.500Z'), action: 'GetObject', key: 'data/a' },
  { id: 'b2', timestamp: at('2026-10-12T03:45:59Z'), action: 'GetObject', key: 'data/a' },
  { id: 'c', timestamp: at('2026-10-12T05:59:59.999Z'), action: 'ListObjectsV2' },
  { id: 'd', timestamp: at('2026-10-12T07:10:00Z'), action: 'GetObject', key: 'data/b' },
  { id: 'z', timestamp: to, action: 'HeadObject', key: 'data/b' },
]

const sampling = (observed: number, estimated = observed, max_sample_interval = 1, capture = 'unsampled') => ({ observed, estimated, max_sample_interval, capture })

function hourAgg(hour: string, fields: Partial<HourAggregate> & Pick<HourAggregate, 'events' | 'rows'>) {
  return { schema_version: 4, bucket: 'jc-taxes', hour: new Date(hr(hour)).toISOString(), own: 0, actions: {}, classes: {}, sampling: sampling(fields.events), ...fields }
}

const quiet = (hour: string) => hourAgg(hour, { events: 0, rows: [] })

afterEach(() => vi.restoreAllMocks())

describe('settings', () => {
  it('derives the hour schedule and budget', () => {
    const { env } = setup()
    expect(settings(env, env.HOURLY_CRON, time)).toEqual({
      buckets: ['jc-taxes'], now: time, to, floor: to - 144 * H, published: [], publicPrefix: '', excluded: new Set(),
      lookback: 6 * H, overlap: 2 * H, pageSize: 500, maxPages: 50, maxHours: 48, verifyHours: 6, maxBytes: 4194304,
    })
  })

  it('bounds an invocation\'s subrequests under the Workers limit', () => {
    expect([subrequestBound(50, 48), subrequestBound(450, 30), subrequestBound(450, 31)]).toEqual([248, 994, 997])
  })

  it.each([
    { SOURCE_BUCKETS: '["disky-r2-access-logs"]' },
    { SOURCE_BUCKETS: '["jc-taxes","jc-taxes"]' },
    { MAX_PAGES: '451' },
    { MAX_PAGES: '450', MAX_HOURS: '33' },
    { MAX_PAGE_BYTES: '999999999' },
    { LAG_HOURS: '23', RETENTION_HOURS: '144' },
    { OVERLAP_HOURS: '0' },
    { LOOKBACK_HOURS: '7', RETENTION_HOURS: '6' },
    { VERIFY_HOURS: '49' },
    { RETENTION_HOURS: '' },
  ])('rejects unsafe configuration %j', vars => {
    const { env } = setup(vars)
    expect(() => settings(env, env.HOURLY_CRON, time)).toThrow('r2-access: invalid_config')
  })

  it('rejects any cron but the hourly one (the daily replay is retired)', () => {
    expect(['17 7 * * *', 'unwired'].map(cron => { try { settings(setup().env, cron, time) } catch (e) { return String(e) } }))
      .toEqual(['Error: r2-access: unwired_cron', 'Error: r2-access: unwired_cron'])
  })

  it.each([
    [{ PUBLIC_BUCKETS: '["other"]', PUBLIC_PREFIX: 'access/r2' }],
    [{ PUBLIC_BUCKETS: '["jc-taxes"]' }],
    [{ PUBLIC_BUCKETS: '["jc-taxes"]', PUBLIC_PREFIX: '/access/' }],
  ])('refuses a public config that names a non-source bucket or lacks a prefix: %j', vars => {
    const { env } = setup(vars)
    expect(() => settings(env, env.HOURLY_CRON, time)).toThrow('r2-access: invalid_config')
  })

  it('sends from-1 so the provider\'s exclusive bounds become [from, to)', () => {
    expect(timeframe(from, to)).toEqual({ from: from - 1, to })
  })
})

describe('plan', () => {
  const config = settings(setup().env, setup().env.HOURLY_CRON, time)
  const state = (collected_through: number, hours: Record<string, { final: boolean }> = {}) => ({
    collected_through, first_from: from, gaps: [], verify_cursor: null, frozen: {},
    hours: Object.fromEntries(Object.entries(hours).map(([k, v]) => [k, { ...v, agg: '', digest: '', drained_at: 0, events: 0, own: 0, estimated: 0, max_sample_interval: 1, capture: 'unsampled' as const, pages: 1 }])),
  })

  it('starts from the lookback, then from the watermark; settled hours only', () => {
    expect(plan(config, undefined)).toEqual({ from, gap: null, fresh: [3, 4, 5, 6, 7, 8].map(h => hr(String(h).padStart(2, '0'))), early: [] })
    expect(plan(config, state(to))).toEqual({ from: to, gap: null, fresh: [], early: [] })
  })

  it('re-drains an early hour only once a re-drain would be final (end + OVERLAP_HOURS ≤ now)', () => {
    // 07's end + 2 h = 10:00 ≤ 10:13; 08's = 11:00 > 10:13.
    expect(plan(config, state(to, { '2026-10-12T07': { final: false }, '2026-10-12T08': { final: false }, '2026-10-12T06': { final: true } })).early).toEqual([hr('07')])
  })

  it('clamps a watermark older than retention to the floor and records the gap', () => {
    expect(plan(config, state(to - 200 * H))).toMatchObject({ from: to - 144 * H, gap: [to - 200 * H, to - 144 * H] })
  })

  it('orders re-verify candidates round-robin from the cursor, within retention and since collection began', () => {
    const s = { first_from: hr('05'), collected_through: hr('09') }
    expect([
      verifyOrder(config, { ...s, verify_cursor: null }),
      verifyOrder(config, { ...s, verify_cursor: hr('07') }),
      verifyOrder(config, { ...s, verify_cursor: hr('09') }),
    ]).toEqual([
      [hr('05'), hr('06'), hr('07'), hr('08')],
      [hr('07'), hr('08'), hr('05'), hr('06')],
      [hr('05'), hr('06'), hr('07'), hr('08')],
    ])
  })

  it('reserves the counted rows\' pages, the empty last page and one page of margin', () => {
    expect([reservePages(0, 2000), reservePages(1999, 2000), reservePages(2000, 2000), reservePages(84_000, 2000)]).toEqual([2, 2, 3, 44])
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

describe('HourAccumulator', () => {
  it('dedupes ids within the drain, truncates times to the minute, keeps own requests and non-read actions out of the rows', () => {
    const acc = new HourAccumulator()
    const ev = (id: string, ts: string, action: string, key: string | null, own = false) => ({ id, timestamp: at(`2026-10-12T03:${ts}Z`), action, key, own })
    for (const e of [
      ev('1', '10:59.999', 'GetObject', 'k'), ev('2', '40:00', 'GetObject', 'k'), ev('2', '40:00', 'GetObject', 'k'),
      ev('3', '05:00', 'ListObjectsV2', null), ev('4', '06:00', 'ListObjectsV2', null, true), ev('5', '07:00', 'HeadObject', 'a'),
      ev('6', '08:00', 'PutObject', 'w'),
    ]) acc.add(e)
    expect(acc.finish('jc-taxes', hr('03'), 6, 1)).toEqual({
      schema_version: 4, bucket: 'jc-taxes', hour: '2026-10-12T03:00:00.000Z', events: 6, own: 1,
      actions: { GetObject: 2, HeadObject: 1, ListObjectsV2: 2, PutObject: 1 }, classes: { content_read: 2, list: 2, metadata_read: 1, write: 1 },
      sampling: sampling(6),
      rows: [
        ['a', 'HeadObject', 1, at('2026-10-12T03:07:00Z'), at('2026-10-12T03:07:00Z')],
        ['k', 'GetObject', 2, at('2026-10-12T03:10:00Z'), at('2026-10-12T03:40:00Z')],
      ],
    })
    expect(minute(at('2026-10-12T03:10:59.999Z'))).toBe(at('2026-10-12T03:10:00Z'))
  })
})

describe('collect', () => {
  it('drains each settled hour in turn, then publishes state before the success marker', async () => {
    const { env, order, read, archive } = setup()
    const fetcher = provider(rows)
    expect(await collect(time, env.HOURLY_CRON, env, fetcher, 'r1')).toEqual({ run_id: 'r1', buckets: 1, hours: 6, pages: 10, events: 5, changed: 6, waiting: 0 })
    const pages: [string, number][] = [['03', 3], ['04', 1], ['05', 2], ['06', 1], ['07', 2], ['08', 1]]
    expect(order).toEqual([
      ...pages.flatMap(([h, n]) => [...Array.from({ length: n }, (_, i) => page('r1', h, i)), agg(h, 'r1')]),
      'state.json', `${prefix('r1')}/_SUCCESS.json`,
    ])
    expect(pages.map(([h]) => read(agg(h, 'r1')))).toEqual([
      hourAgg('03', {
        events: 3, actions: { GetObject: 2, HeadObject: 1 }, classes: { content_read: 2, metadata_read: 1 },
        rows: [['data/a', 'GetObject', 2, at('2026-10-12T03:30:00Z'), at('2026-10-12T03:45:00Z')], ['data/a', 'HeadObject', 1, from, from]],
      }),
      quiet('04'),
      hourAgg('05', { events: 1, actions: { ListObjectsV2: 1 }, classes: { list: 1 }, rows: [] }),
      quiet('06'),
      hourAgg('07', { events: 1, actions: { GetObject: 1 }, classes: { content_read: 1 }, rows: [['data/b', 'GetObject', 1, at('2026-10-12T07:10:00Z'), at('2026-10-12T07:10:00Z')]] }),
      quiet('08'),
    ])
    const events: Record<string, number> = { '03': 3, '05': 1, '07': 1 }
    expect(read('state.json')).toEqual({
      schema_version: 2, account: env.ACCOUNT_ID, scheduled_time: time, run_id: 'r1',
      buckets: { 'jc-taxes': {
        collected_through: to, first_from: from, gaps: [], verify_cursor: null, frozen: {},
        hours: Object.fromEntries(await Promise.all(pages.map(async ([h, n]) => [`2026-10-12T${h}`, {
          agg: agg(h, 'r1'), digest: await sha(archive.get(agg(h, 'r1'))!.text), drained_at: time,
          // 08 ends at 09:00; drained at 10:13 < 09:00 + OVERLAP_HOURS: early, re-drained next run.
          final: h !== '08', events: events[h] ?? 0, own: 0, estimated: events[h] ?? 0, max_sample_interval: 1, capture: 'unsampled', pages: n,
        }]))),
      } },
    })
    expect(read(`${prefix('r1')}/_SUCCESS.json`)).toEqual({
      schema_version: 3, account: env.ACCOUNT_ID, scheduled_time: time, run_id: 'r1', provider_delivery: 'best_effort',
      budget: { pages: 50, pages_used: 10, hours: 48, hours_used: 6 },
      buckets: [{
        bucket: 'jc-taxes', gap: null, collected_through: to, waiting: 0, error: null,
        hours: pages.map(([h, n]) => ({
          hour: `2026-10-12T${h}:00:00.000Z`, phase: 'new', events: events[h] ?? 0, capture: 'unsampled', changed: true,
          pages: Array.from({ length: n }, (_, i) => page('r1', h, i)),
        })),
      }],
    })
    const bodies = fetcher.mock.calls.map(([, init]) => JSON.parse(String(init?.body)))
    // Per hour: the count first (it sizes the reservation), then the drain to an empty page.
    expect(bodies.slice(0, 4)).toEqual([
      {
        queryId: 'disky-r2-sampling', dry: true, view: 'calculations', timeframe: { from: from - 1, to: from + H },
        parameters: {
          datasets: ['r2'], filters: [{ key: 'bucket', operation: 'eq', type: 'string', value: 'jc-taxes' }], filterCombination: 'and',
          calculations: [{ operator: 'count', alias: 'events' }], groupBys: [{ type: 'string', value: 'action' }],
        },
      },
      ...[undefined, 'b', 'a'].map(offset => ({
        queryId: 'disky-r2-archive', dry: true, view: 'events', limit: 500, timeframe: { from: from - 1, to: from + H },
        parameters: { datasets: ['r2'], filters: [{ key: 'bucket', operation: 'eq', type: 'string', value: 'jc-taxes' }], filterCombination: 'and' },
        ...(offset ? { offset, offsetDirection: 'next' } : {}),
      })),
    ])
    expect(bodies.map(b => [b.view, b.timeframe.from + 1]).filter(([view]) => view === 'calculations')).toEqual(
      pages.map(([h]) => ['calculations', hr(h)]))
  })

  it('re-drains an early hour once settled, replacing its aggregate (late delivery), and leaves unchanged hours alone', async () => {
    const { env, order, read } = setup({ VERIFY_HOURS: '0' })
    const live = [...rows]
    await collect(time, env.HOURLY_CRON, env, provider(live), 'r1')
    // Delivered after r1 drained hour 08 early.
    live.push({ id: 'late', timestamp: at('2026-10-12T08:30:00Z'), action: 'HeadObject', key: 'data/c' })
    order.length = 0
    const next = time + H
    expect(await collect(next, env.HOURLY_CRON, env, provider(live), 'r2')).toEqual({ run_id: 'r2', buckets: 1, hours: 2, pages: 4, events: 2, changed: 2, waiting: 0 })
    expect(order).toEqual([
      page('r2', '09', 0, next), page('r2', '09', 1, next), agg('09', 'r2'),
      page('r2', '08', 0, next), page('r2', '08', 1, next), agg('08', 'r2'),
      'state.json', `${prefix('r2', next)}/_SUCCESS.json`, `delete:${agg('08', 'r1')}`,
    ])
    expect(read(agg('08', 'r2'))).toEqual(hourAgg('08', {
      events: 1, actions: { HeadObject: 1 }, classes: { metadata_read: 1 }, rows: [['data/c', 'HeadObject', 1, at('2026-10-12T08:30:00Z'), at('2026-10-12T08:30:00Z')]],
    }))
    const hours = read('state.json').buckets['jc-taxes'].hours
    expect([hours['2026-10-12T08'], hours['2026-10-12T09'].final, hours['2026-10-12T07'].agg, read('state.json').buckets['jc-taxes'].collected_through]).toEqual([
      expect.objectContaining({ agg: agg('08', 'r2'), drained_at: next, final: true, events: 1, pages: 2 }), false, agg('07', 'r1'), to + H,
    ])
    expect(read(`${prefix('r2', next)}/_SUCCESS.json`).buckets[0].hours.map((h: { hour: string, phase: string }) => [h.hour, h.phase])).toEqual([
      ['2026-10-12T09:00:00.000Z', 'new'], ['2026-10-12T08:00:00.000Z', 'early'],
    ])
  })

  it('a re-drain with the same result keeps the aggregate key and writes neither aggregate nor public file', async () => {
    const { env, order, read } = setup({ VERIFY_HOURS: '0', PUBLIC_BUCKETS: '["jc-taxes"]', PUBLIC_PREFIX: 'access/r2' })
    await collect(time, env.HOURLY_CRON, env, provider(rows), 'r1')
    order.length = 0
    const next = time + H
    expect(await collect(next, env.HOURLY_CRON, env, provider(rows), 'r2')).toMatchObject({ hours: 2, changed: 1 })
    // 09 is new (changed: its first aggregate + public file); 08's re-drain matches r1's.
    expect(order).toEqual([
      page('r2', '09', 0, next), page('r2', '09', 1, next), agg('09', 'r2'), 'public:access/r2/jc-taxes/2026-10-12/09.json',
      page('r2', '08', 0, next), 'state.json', `${prefix('r2', next)}/_SUCCESS.json`,
    ])
    expect(read('state.json').buckets['jc-taxes'].hours['2026-10-12T08']).toMatchObject({ agg: agg('08', 'r1'), drained_at: next, final: true })
  })

  it('re-verifies older hours round-robin, VERIFY_HOURS per invocation', async () => {
    const { env, read } = setup({ VERIFY_HOURS: '2' })
    const phases: [string, string][][] = []
    for (const [i, run] of ['r1', 'r2', 'r3', 'r4', 'r5'].entries()) {
      const when = time + i * H
      await collect(when, env.HOURLY_CRON, env, provider(rows), run)
      phases.push(read(`${prefix(run, when)}/_SUCCESS.json`).buckets[0].hours.map((h: { hour: string, phase: string }) => [h.hour.slice(11, 13), h.phase]))
    }
    expect(phases).toEqual([
      [['03', 'new'], ['04', 'new'], ['05', 'new'], ['06', 'new'], ['07', 'new'], ['08', 'new']],
      [['09', 'new'], ['08', 'early'], ['03', 'verify'], ['04', 'verify']],
      [['10', 'new'], ['09', 'early'], ['05', 'verify'], ['06', 'verify']],
      [['11', 'new'], ['10', 'early'], ['07', 'verify'], ['08', 'verify']],
      // 09 and 10 were just re-drained this run's predecessors, but only this run's own hours are skipped.
      [['12', 'new'], ['11', 'early'], ['09', 'verify'], ['10', 'verify']],
    ])
    expect(read('state.json').buckets['jc-taxes'].verify_cursor).toBe(hr('11'))
  })

  it('carries hours that don\'t fit the hour budget to the next invocation', async () => {
    const { env, read } = setup({ MAX_HOURS: '2', VERIFY_HOURS: '0' })
    expect(await collect(time, env.HOURLY_CRON, env, provider(rows), 'r1')).toMatchObject({ hours: 2, waiting: 4 })
    expect(read('state.json').buckets['jc-taxes'].collected_through).toBe(hr('05'))
    expect(await collect(time, env.HOURLY_CRON, env, provider(rows), 'r2')).toMatchObject({ hours: 2, waiting: 2 })
    expect(Object.keys(read('state.json').buckets['jc-taxes'].hours)).toEqual(['03', '04', '05', '06'].map(h => `2026-10-12T${h}`))
  })

  it('carries an hour whose reservation exceeds the pages left; its count is spent, nothing else', async () => {
    const { env, order, read } = setup({ MAX_PAGES: '4', VERIFY_HOURS: '0' })
    // 03 drains in 3 pages (2 + 1 + empty) under a reservation of 2; 04 then needs 2 > 1 left.
    expect(await collect(time, env.HOURLY_CRON, env, provider(rows), 'r1')).toMatchObject({ hours: 1, pages: 3, waiting: 5 })
    expect(order).toEqual([page('r1', '03', 0), page('r1', '03', 1), page('r1', '03', 2), agg('03', 'r1'), 'state.json', `${prefix('r1')}/_SUCCESS.json`])
    expect(read(`${prefix('r1')}/_SUCCESS.json`).budget).toEqual({ pages: 4, pages_used: 3, hours: 48, hours_used: 2 })
  })

  it('refuses an hour that can never fit one invocation (hour_over_cap): earlier hours publish, the watermark holds, the run fails', async () => {
    // 1,500 counted events reserve 5 pages > MAX_PAGES 4.
    const { env, order, read } = setup({ MAX_PAGES: '4', VERIFY_HOURS: '0' })
    const burst = Array.from({ length: 1500 }, (_, i) => ({ id: `x${i}`, timestamp: hr('05') + i, action: 'GetObject', key: 'k' }))
    await expect(collect(time, env.HOURLY_CRON, env, provider(burst), 'r1')).rejects.toThrow('r2-access: hour_over_cap')
    expect(order).toEqual([page('r1', '03', 0), agg('03', 'r1'), page('r1', '04', 0), agg('04', 'r1'), 'state.json', `${prefix('r1')}/_SUCCESS.json`])
    expect(read('state.json').buckets['jc-taxes'].collected_through).toBe(hr('05'))
    expect(read(`${prefix('r1')}/_SUCCESS.json`).buckets[0]).toMatchObject({ collected_through: hr('05'), waiting: 4, error: 'hour_over_cap' })
  })

  it('marks ingestion-sampled and inconsistent hours without inflating observed counts', async () => {
    const { env, read } = setup()
    await collect(time, env.HOURLY_CRON, env, provider([
      { id: 's', timestamp: at('2026-10-12T04:10:00Z'), action: 'ListObjectsV2', weight: 2 },
      { id: 'h', timestamp: at('2026-10-12T05:10:00Z'), action: 'GetObject', key: 'k', hidden: true },
    ]), 'r1')
    expect([read(agg('04', 'r1')).sampling, read(agg('05', 'r1')).sampling, read(agg('05', 'r1')).rows]).toEqual([
      sampling(1, 2, 2, 'sampled'), sampling(0, 1, 1, 'inconsistent'), [],
    ])
  })

  it('publishes each changed hour of a public bucket (per-key counts, minute times, capture) before state; own requests stay out', async () => {
    const { env, order, pub, read } = setup({ VERIFY_HOURS: '0', PUBLIC_BUCKETS: config.vars.PUBLIC_BUCKETS, PUBLIC_PREFIX: config.vars.PUBLIC_PREFIX })
    const scanner = { ...env, EXCLUDED_ACCESS_KEY_IDS: '["scanner-key"]', MAX_HOURS: '1' }
    await collect(time, env.HOURLY_CRON, scanner, provider([
      { id: 'g', timestamp: at('2026-10-12T03:05:30Z'), action: 'GetObject', key: 'data/2024.pqt', keyId: 'reader-key' },
      { id: 'g2', timestamp: at('2026-10-12T03:07:01Z'), action: 'GetObject', key: 'data/2024.pqt' },
      { id: 'l', timestamp: at('2026-10-12T03:20:00Z'), action: 'ListObjectsV2', keyId: 'scanner-key' },
      { id: 'h', timestamp: at('2026-10-12T03:40:00Z'), action: 'HeadObject', key: 'data/2023.pqt', keyId: 'scanner-key' },
      { id: 'w', timestamp: at('2026-10-12T03:50:00Z'), action: 'GetObject', key: 'data/2023.pqt', weight: 3 },
    ], 500), 'r1')
    expect(order).toEqual([page('r1', '03', 0), page('r1', '03', 1), agg('03', 'r1'), 'public:access/r2/jc-taxes/2026-10-12/03.json', 'state.json', `${prefix('r1')}/_SUCCESS.json`])
    expect(read(agg('03', 'r1'))).toMatchObject({ events: 5, own: 2, actions: { GetObject: 3, HeadObject: 1, ListObjectsV2: 1 } })
    expect(JSON.parse(pub.get('access/r2/jc-taxes/2026-10-12/03.json')!)).toEqual({
      schema_version: 1, bucket: 'jc-taxes', hour: '2026-10-12T03:00:00.000Z',
      // GetObject's weights 1, 1, 3: the mean interval is 5/3.
      capture: 'sampled', observed: 5, estimated: 7, max_sample_interval: 5 / 3, excluded: 2,
      rows: [
        { key: 'data/2023.pqt', action: 'GetObject', n: 1, last_ts: at('2026-10-12T03:50:00Z') },
        { key: 'data/2024.pqt', action: 'GetObject', n: 2, last_ts: at('2026-10-12T03:07:00Z') },
      ],
    })
  })

  it('drains a 100K-event hour in 51 pages, holding one hour at a time', async () => {
    const { env, read } = setup({ PAGE_SIZE: '2000', MAX_PAGES: '60', MAX_HOURS: '1', VERIFY_HOURS: '0' })
    const n = 100_000
    const big: Row[] = Array.from({ length: n }, (_, i) => ({
      id: `e${String(i).padStart(6, '0')}`, timestamp: hr('03') + Math.floor(i * (H / n)),
      action: i % 3 ? 'PutObject' : 'GetObject', key: `shard/${i % 20_000}`,
    }))
    const started = performance.now()
    expect(await collect(time, env.HOURLY_CRON, env, provider(big, 2000), 'r1')).toEqual({ run_id: 'r1', buckets: 1, hours: 1, pages: 51, events: n, changed: 1, waiting: 5 })
    const elapsed = performance.now() - started
    const a = read(agg('03', 'r1'))
    expect([a.events, a.actions, a.rows.length, a.rows.reduce((sum: number, r: [unknown, unknown, number]) => sum + r[2], 0), a.rows[0]]).toEqual([
      n, { GetObject: 33_334, PutObject: 66_666 }, 20_000, 33_334, ['shard/0', 'GetObject', 2, hr('03'), at('2026-10-12T03:36:00Z')],
    ])
    // Sanity, not a benchmark: the mock provider's JSON dominates; the collector's own work is a small fraction.
    expect(elapsed).toBeLessThan(30_000)
  }, 60_000)

  it('upgrades a version-1 state: watermark and gaps carry over, legacy days stay named', async () => {
    const { env, archive, read } = setup({ VERIFY_HOURS: '1' })
    archive.set('state.json', { etag: 'v1', text: JSON.stringify({
      schema_version: 1, account: env.ACCOUNT_ID, scheduled_time: 0, run_id: 'old',
      buckets: { 'jc-taxes': { collected_through: to - H, first_from: from - 2 * H, gaps: [[1, 2]], days: { '2026-10-12': 'agg/jc-taxes/2026-10-12/old.json' } } },
    }) })
    await collect(time, env.HOURLY_CRON, env, provider(rows), 'r1')
    const state = read('state.json').buckets['jc-taxes']
    expect([state.collected_through, state.first_from, state.gaps, state.legacy_days, Object.keys(state.hours), state.verify_cursor]).toEqual([
      to, from - 2 * H, [[1, 2]], { '2026-10-12': 'agg/jc-taxes/2026-10-12/old.json' }, ['2026-10-12T01', '2026-10-12T08'], hr('02'),
    ])
    expect(upgrade({ schema_version: 1, account: 'a', buckets: { b: { collected_through: 5, first_from: 1, gaps: [], days: {} } } })).toEqual({
      b: { collected_through: 5, first_from: 1, gaps: [], verify_cursor: null, hours: {}, frozen: {} },
    })
  })

  it('records a gap when the watermark fell out of retention, and freezes hours past it', async () => {
    const { env, read } = setup({ VERIFY_HOURS: '0', MAX_HOURS: '1' })
    await collect(time, env.HOURLY_CRON, env, provider([]), 'r1')
    const later = time + 200 * H
    await collect(later, env.HOURLY_CRON, env, provider([]), 'r2')
    const state = read('state.json').buckets['jc-taxes']
    expect([state.gaps, state.frozen, Object.keys(state.hours)]).toEqual([
      [[hr('04'), to + 200 * H - 144 * H]], { '2026-10-12T03': agg('03', 'r1') }, [hourKeyOf(to + 56 * H)],
    ])
  })

  it('refuses to publish over a concurrently advanced state', async () => {
    const { env, archive, order } = setup({ VERIFY_HOURS: '0' })
    await collect(time, env.HOURLY_CRON, env, provider(rows), 'r1')
    const fetcher = provider(rows)
    const racing = vi.fn<typeof fetch>(async (...args) => {
      archive.set('state.json', { ...archive.get('state.json')!, etag: 'other-run' })
      return fetcher(...args)
    })
    order.length = 0
    const next = time + H
    await expect(collect(next, env.HOURLY_CRON, env, racing, 'r2')).rejects.toThrow('r2-access: state_conflict')
    expect(order).toEqual([page('r2', '09', 0, next), page('r2', '09', 1, next), agg('09', 'r2'), page('r2', '08', 0, next)])
  })

  it("never follows a redirect (Workers' fetch has no `redirect: 'error'`): a 3xx fails the run before archiving", async () => {
    const { env, order } = setup()
    const fetcher = vi.fn<typeof fetch>(async () => new Response(null, { status: 302, headers: { location: 'https://example.org/' } }))
    await expect(collect(time, env.HOURLY_CRON, env, fetcher, 'r3')).rejects.toThrow('r2-access: provider_http_302')
    expect([fetcher.mock.calls.map(([, init]) => init?.redirect), order]).toEqual([['manual'], []])
  })

  it("ends an hour at a short page of events it already drained (the provider echoes a twice-delivered last event; marin-na 2026-10-17T16)", async () => {
    const { env, read } = setup()
    const twice = { id: 'a', timestamp: from, action: 'HeadObject', key: 'data/a' }
    expect(await collect(time, env.HOURLY_CRON, env, provider([...rows, twice]), 'r1')).toEqual({ run_id: 'r1', buckets: 1, hours: 6, pages: 10, events: 5, changed: 6, waiting: 0 })
    expect(read(agg('03', 'r1'))).toEqual(hourAgg('03', {
      events: 3, actions: { GetObject: 2, HeadObject: 1 }, classes: { content_read: 2, metadata_read: 1 },
      sampling: { ...sampling(3, 4, 1), capture: 'inconsistent' },
      rows: [['data/a', 'GetObject', 2, at('2026-10-12T03:30:00Z'), at('2026-10-12T03:45:00Z')], ['data/a', 'HeadObject', 1, from, from]],
    }))
  })

  it('rejects a full page that repeats a cursor before archiving it', async () => {
    const { env, order } = setup({ PAGE_SIZE: '2' })
    const repeated = eventsPage([rows[2], rows[1]])
    const fetcher = vi.fn<typeof fetch>(async (_url, init) =>
      new Response(JSON.parse(String(init?.body)).view === 'calculations' ? calculationPage([{ count: 1, sampleInterval: 1 }]) : repeated))
    await expect(collect(time, env.HOURLY_CRON, env, fetcher, 'r1')).rejects.toThrow('r2-access: cursor_stalled')
    expect(order).toEqual([page('r1', '03', 0)])
  })
})

function hourKeyOf(ms: number) {
  return new Date(ms).toISOString().slice(0, 13)
}

describe('publicHour', () => {
  it('carries per-key counts, minute last_ts and the capture status only', () => {
    const agg: HourAggregate = {
      schema_version: 4, bucket: 'jc-taxes', hour: '2026-10-12T03:00:00.000Z', events: 3, own: 1,
      actions: {}, classes: {}, sampling: { observed: 3, estimated: 3, max_sample_interval: 1, capture: 'unsampled' },
      rows: [['a', 'HeadObject', 1, 60_000, 120_000], ['k', 'GetObject', 1, 0, 0]],
    }
    expect(publicHour(agg)).toBe(
      '{"schema_version":1,"bucket":"jc-taxes","hour":"2026-10-12T03:00:00.000Z","capture":"unsampled","observed":3,"estimated":3,'
      + '"max_sample_interval":1,"excluded":1,"rows":[{"key":"a","action":"HeadObject","n":1,"last_ts":120000},{"key":"k","action":"GetObject","n":1,"last_ts":0}]}')
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
    expect(() => parsePage(eventsPage([rows[1]]), 'other-bucket', from, to)).toThrow('r2-access: invalid_event')
    expect(() => parsePage(eventsPage([rows[6]]), 'jc-taxes', from, to)).toThrow('r2-access: event_outside_window')
    expect(() => parsePage(eventsPage([rows[0]]), 'jc-taxes', from, to)).toThrow('r2-access: event_outside_window')
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
