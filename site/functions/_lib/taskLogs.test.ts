import { afterEach, describe, expect, it, vi } from 'vitest'
import { requireViewer } from './auth'
import { boundedJson, taskLogBody, taskLogEntries } from './taskLogs'
import { onRequestGet } from '../api/sweep/logs'

vi.mock('./auth', async importOriginal => ({ ...await importOriginal<typeof import('./auth')>(), requireViewer: vi.fn(async () => ({ name: 'viewer' })) }))
vi.mock('./gcp', async importOriginal => ({ ...await importOriginal<typeof import('./gcp')>(), gcpToken: vi.fn(async () => 'token') }))

const JOB = 'gcs-sweep-real-20261005-024600z'
const VIEW = 'projects/proj/locations/global/buckets/_Default/views/sweep-task-logs'
const CREATED = '2026-10-05T02:47:18.280290576Z'
const UNTIL = '2026-10-05T16:30:00.000Z'
const UID = 'gcs-sweep-real-202-e41f66a7-9ad5-46400'
const name = `projects/proj/locations/us-central1/jobs/${JOB}`
const env = { GCP_PROJECT: 'proj', GCP_SA_KEY: 'key', GCP_LOG_VIEW: VIEW }
const request = (query: string) => new Request(`https://gcs.example/api/sweep/logs?${query}`)
const get = async (query: string, overrides = {}) => { const r = await onRequestGet({ request: request(query), env: { ...env, ...overrides } }); return [r.status, await r.json()] }

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); vi.mocked(requireViewer).mockReset(); vi.mocked(requireViewer).mockResolvedValue({ name: 'viewer' } as never) })

describe('task log payloads', () => {
  it('uses one view, exact server-resolved UID and fixed bounds; paginates unchanged', () => {
    expect(taskLogBody(VIEW, UID, CREATED, UNTIL, 'next')).toEqual({
      resourceNames: [VIEW], filter: `log_id("batch_task_logs")\nlabels.job_uid="${UID}"\ntimestamp >= "${CREATED}"\ntimestamp <= "${UNTIL}"`,
      orderBy: 'timestamp desc', pageSize: 50, pageToken: 'next',
    })
  })
  it('normalizes text/JSON, distinguishes truncation, and rejects malformed entries', () => {
    expect(taskLogEntries([{ timestamp: 't', insertId: 'i', textPayload: '<script>not HTML</script>', severity: 'INFO' }, { timestamp: 'u', jsonPayload: { progress: 1 } }, { timestamp: 'v', textPayload: 'a'.repeat(16_385) }])).toEqual([
      { timestamp: 't', id: 'i', message: '<script>not HTML</script>', severity: 'INFO', truncated: false },
      { timestamp: 'u', id: 'u:1', message: '{"progress":1}', severity: 'DEFAULT', truncated: false },
      { timestamp: 'v', id: 'v:2', message: 'a'.repeat(16_384), severity: 'DEFAULT', truncated: true },
    ])
    expect(taskLogEntries(undefined)).toEqual([])
    expect(() => taskLogEntries({})).toThrow('invalid Logging entries')
    expect(() => taskLogEntries([{}])).toThrow('invalid Logging entry')
  })
  it('bounds streamed bodies without relying on Content-Length', async () => {
    expect(await boundedJson(new Response('{"n":1}'), 7)).toEqual({ n: 1 })
    const cancel = vi.fn()
    const body = new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode('12345678')) }, cancel })
    await expect(boundedJson(new Response(body), 7)).rejects.toThrow('upstream response exceeds size limit')
    expect(cancel).toHaveBeenCalledExactlyOnceWith(undefined)
  })
})

describe('authenticated task-log endpoint', () => {
  it('refuses unauthenticated callers before fetching anything', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    vi.mocked(requireViewer).mockResolvedValue(new Response('{"error":"unauthenticated"}', { status: 401 }))
    expect(await get(`job=${JOB}`)).toEqual([401, { error: 'unauthenticated' }])
    expect(fetch).not.toHaveBeenCalled()
  })
  it('rejects missing configuration, arbitrary jobs/regions and invalid cursor bounds', async () => {
    vi.useFakeTimers({ now: Date.parse(UNTIL), toFake: ['Date'] })
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch)
    expect(await Promise.all([
      get(`job=${JOB}`, { GCP_LOG_VIEW: undefined }),
      get(`job=${JOB}`, { GCP_LOG_VIEW: VIEW.replace('proj', 'other') }),
      get('job=unrelated-job'), get(`job=${JOB}&region=other`), get(`job=${JOB}&cursor=x`),
      get(`job=${JOB}&until=bad`), get(`job=${JOB}&until=2099-01-01`),
    ])).toEqual([
      [503, { error: 'Task logs not configured (GCP_SA_KEY / GCP_LOG_VIEW)' }],
      [503, { error: 'Invalid task-log view configuration' }],
      [400, { error: 'Invalid sweep job or region' }], [400, { error: 'Invalid sweep job or region' }],
      [400, { error: 'Invalid log cursor or upper time bound' }], [400, { error: 'Invalid log cursor or upper time bound' }], [400, { error: 'Invalid log cursor or upper time bound' }],
    ])
    expect(fetch).not.toHaveBeenCalled()
  })
  it('resolves the job UID from Batch and reads only its task logs', async () => {
    vi.useFakeTimers({ now: Date.parse(UNTIL), toFake: ['Date'] })
    const calls: { url: string; body: unknown; authorization: string | null }[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : null, authorization: new Headers(init?.headers).get('authorization') })
      return new Response(JSON.stringify(url.startsWith('https://batch.') ? { name, uid: UID, createTime: CREATED } : { entries: [{ timestamp: UNTIL, insertId: 'i', textPayload: 'done', severity: 'INFO' }], nextPageToken: 'next' }))
    }))
    expect(await get(`job=${JOB}`)).toEqual([200, { entries: [{ timestamp: UNTIL, id: 'i', message: 'done', severity: 'INFO', truncated: false }], nextPageToken: 'next', until: UNTIL }])
    expect(calls).toEqual([
      { url: `https://batch.googleapis.com/v1/${name}`, body: null, authorization: 'Bearer token' },
      { url: 'https://logging.googleapis.com/v2/entries:list', body: taskLogBody(VIEW, UID, CREATED, UNTIL, null), authorization: 'Bearer token' },
    ])
  })
  it('reports denied Logging reads without forwarding upstream payloads', async () => {
    vi.useFakeTimers({ now: Date.parse(UNTIL), toFake: ['Date'] })
    vi.stubGlobal('fetch', vi.fn(async (url: string) => url.startsWith('https://batch.') ? new Response(JSON.stringify({ name, uid: UID, createTime: CREATED })) : new Response('private upstream details', { status: 403 })))
    expect(await get(`job=${JOB}`)).toEqual([502, { error: 'Task logs unavailable (Logging HTTP 403)' }])
  })
  it('keeps pagination bounds and token fixed and exposes empty search pages', async () => {
    vi.useFakeTimers({ now: Date.parse(UNTIL), toFake: ['Date'] })
    const bodies: unknown[] = []
    vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
      if (url.startsWith('https://batch.')) return new Response(JSON.stringify({ name, uid: UID, createTime: CREATED }))
      bodies.push(JSON.parse(String(init?.body)))
      return new Response('{"nextPageToken":"continue-search"}')
    }))
    expect(await get(`job=${JOB}&until=${encodeURIComponent(UNTIL)}&cursor=page-2`)).toEqual([200, { entries: [], until: UNTIL, nextPageToken: 'continue-search' }])
    expect(bodies).toEqual([taskLogBody(VIEW, UID, CREATED, UNTIL, 'page-2')])
  })
  it('does not query Logging after a missing or malformed Batch job response', async () => {
    vi.useFakeTimers({ now: Date.parse(UNTIL), toFake: ['Date'] })
    const fetch = vi.fn(async () => new Response('not found', { status: 404 })); vi.stubGlobal('fetch', fetch)
    expect(await get(`job=${JOB}`)).toEqual([404, { error: 'Batch job lookup failed (HTTP 404)' }])
    expect(fetch).toHaveBeenCalledTimes(1)
    fetch.mockResolvedValue(new Response(JSON.stringify({ name: `${name}-other`, uid: UID, createTime: CREATED })))
    const error = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(await get(`job=${JOB}`)).toEqual([502, { error: 'Task-log read failed; refresh to retry' }])
    expect(fetch).toHaveBeenCalledTimes(2)
    expect(error).toHaveBeenCalledExactlyOnceWith('Task-log read failed', 'invalid Batch job response')
    error.mockRestore()
  })
})
