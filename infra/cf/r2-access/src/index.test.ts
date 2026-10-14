import { afterEach, describe, expect, it, vi } from 'vitest'
import config from '../wrangler.json'
import worker, { boundedText, collect, parsePage, settings } from './index'

const time = Date.parse('2026-10-12T10:13:00Z')
const to = Date.parse('2026-10-12T08:00:00Z')
const from = Date.parse('2026-10-12T02:00:00Z')
const prefix = `runs/${time}/test-run`

function fixture(ids: string[], extra: Record<string, unknown> = {}) {
  return JSON.stringify({
    success: true,
    result: {
      run: { status: 'COMPLETED' }, statistics: { abr_level: 1 },
      events: { count: ids.length, events: ids.map(id => ({
        dataset: 'r2', $metadata: { id }, source: { bucket: 'jc-taxes', action: 'HeadObject' },
      })) }, ...extra,
    },
  })
}

function setup(vars: Partial<typeof config.vars> = {}) {
  const archive = new Map<string, string>()
  const put = vi.fn<R2Bucket['put']>(async (key, value) => {
    if (typeof value !== 'string') throw new Error('collector must archive JSON text')
    archive.set(key, value)
    return { key, version: 'fixture', size: value.length, etag: 'fixture', httpEtag: '"fixture"',
      checksums: { toJSON: () => ({}) }, uploaded: new Date(time), storageClass: 'Standard', writeHttpMetadata() {} }
  })
  return { archive, env: { ...config.vars, ...vars, CF_OBSERVABILITY_TOKEN: 'test-secret', ARCHIVE: { put } } }
}

function fetchPages(pages: string[]) {
  let i = 0
  return vi.fn<typeof fetch>(async () => {
    if (i >= pages.length) throw new Error('unexpected extra query')
    return new Response(pages[i++])
  })
}

afterEach(() => vi.restoreAllMocks())

describe('bounded collector', () => {
  it('uses delayed, overlapping hourly and daily windows within retention', () => {
    const { env } = setup()
    expect(settings(env, env.HOURLY_CRON, time)).toEqual({ buckets: ['jc-taxes'], from, to, pageSize: 500, maxPages: 50, maxBytes: 4194304 })
    expect(settings(env, env.DAILY_CRON, time)).toEqual({ buckets: ['jc-taxes'], from: to - 144 * 3600000, to, pageSize: 500, maxPages: 50, maxBytes: 4194304 })
  })

  it.each([
    { SOURCE_BUCKETS: '["disky-r2-access-logs"]' },
    { SOURCE_BUCKETS: '["jc-taxes","jc-taxes"]' },
    { MAX_PAGES: '101' },
    { MAX_PAGE_BYTES: '999999999' },
    { LAG_HOURS: '24', REPLAY_HOURS: '144' },
  ])('rejects unsafe configuration %j', vars => {
    const { env } = setup(vars)
    expect(() => settings(env, env.DAILY_CRON, time)).toThrow('r2-access: invalid_config')
  })

  it('rejects an unexpected cron', () => {
    expect(() => settings(setup().env, 'unwired', time)).toThrow('r2-access: unwired_cron')
  })

  it('archives complete pages before an exact final publication marker', async () => {
    const { env, archive } = setup()
    const pages = [fixture(['a']), fixture(['b']), fixture([])]
    const fetcher = fetchPages(pages)
    expect(await collect(time, env.HOURLY_CRON, env, fetcher, 'test-run')).toEqual({ run_id: 'test-run', buckets: 1, pages: 3, events: 2 })
    expect([...archive.keys()]).toEqual([`${prefix}/jc-taxes/0000.json`, `${prefix}/jc-taxes/0001.json`, `${prefix}/jc-taxes/0002.json`, `${prefix}/_SUCCESS.json`])
    expect([...archive.values()].slice(0, 3)).toEqual(pages)
    expect(JSON.parse(archive.get(`${prefix}/_SUCCESS.json`)!)).toEqual({
      schema_version: 1, account: env.ACCOUNT_ID, scheduled_time: time, run_id: 'test-run',
      provider_delivery: 'best_effort', capture_sampling: 'unknown',
      windows: [{ bucket: 'jc-taxes', from, to, events: 2, actions: { HeadObject: 2 },
        pages: [`${prefix}/jc-taxes/0000.json`, `${prefix}/jc-taxes/0001.json`, `${prefix}/jc-taxes/0002.json`], query_exhausted: true }],
    })
    expect(fetcher.mock.calls.map(([url, init]) => ({ url, body: JSON.parse(String(init?.body)), redirect: init?.redirect }))).toEqual(
      [undefined, 'a', 'b'].map(offset => ({
        url: `https://api.cloudflare.com/client/v4/accounts/${env.ACCOUNT_ID}/workers/observability/telemetry/query`, redirect: 'error',
        body: { queryId: 'disky-r2-archive', dry: true, view: 'events', limit: 500, timeframe: { from, to },
          parameters: { datasets: ['r2'], filters: [{ key: 'bucket', operation: 'eq', type: 'string', value: 'jc-taxes' }], filterCombination: 'and' },
          ...(offset ? { offset, offsetDirection: 'next' } : {}),
        },
      })),
    )
  })

  it('deduplicates IDs at page overlaps without miscounting actions', async () => {
    const { env, archive } = setup()
    expect(await collect(time, env.HOURLY_CRON, env, fetchPages([fixture(['a', 'b']), fixture(['b', 'c']), fixture([])]), 'test-run')).toEqual({ run_id: 'test-run', buckets: 1, pages: 3, events: 3 })
    expect(JSON.parse(archive.get(`${prefix}/_SUCCESS.json`)!).windows[0].actions).toEqual({ HeadObject: 3 })
  })

  it('fails without publishing success when the page cap is exhausted', async () => {
    const { env, archive } = setup({ MAX_PAGES: '1' })
    await expect(collect(time, env.HOURLY_CRON, env, fetchPages([fixture(['a'])]), 'test-run')).rejects.toThrow('r2-access: page_cap_exceeded')
    expect([...archive.keys()]).toEqual([`${prefix}/jc-taxes/0000.json`])
  })

  it('rejects repeated cursors before archiving the repeated page', async () => {
    const { env, archive } = setup()
    await expect(collect(time, env.HOURLY_CRON, env, fetchPages([fixture(['a']), fixture(['a'])]), 'test-run')).rejects.toThrow('r2-access: cursor_stalled')
    expect([...archive.keys()]).toEqual([`${prefix}/jc-taxes/0000.json`])
  })

  it.each([
    [{ run: { status: 'RUNNING' } }, 'query_incomplete'],
    [{ statistics: { abr_level: 2 } }, 'query_sampled'],
    [{ events: { count: 1, events: [] } }, 'page_count_mismatch'],
  ])('rejects incomplete/sampled/mismatched pages %j', (extra, code) => {
    expect(() => parsePage(fixture([], extra), 'jc-taxes')).toThrow(`r2-access: ${code}`)
  })

  it('rejects responses outside the requested bucket', () => {
    expect(() => parsePage(fixture(['a']), 'other-bucket')).toThrow('r2-access: invalid_event')
  })

  it('bounds streamed response bytes and cancels over-cap bodies', async () => {
    const cancel = vi.fn()
    const response = new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('12345')) }, cancel }))
    await expect(boundedText(response, 4)).rejects.toThrow('r2-access: page_bytes_exceeded')
    expect(cancel.mock.calls).toEqual([[undefined]])
    expect(await boundedText(new Response('1234'), 4)).toBe('1234')
  })

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
