/** `/api/name-summary`'s gates and validation, with the static name index (`staticSummary`) stubbed: which requests
 *  reach it, with which params, and which are refused before any read. The index's answers are tested beside it
 *  (`staticCatalog.test.ts`, `staticRuns.test.ts`, …). */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { requireViewer, type Identity } from '../_lib/auth.js'
import type { NameSummaryEnv } from '../_lib/nameSummary.js'
import { staticSummary } from '../_lib/nameSummaryStatic.js'
import { onRequest } from './name-summary.js'

vi.mock('../_lib/auth.js', async original => ({ ...await original<typeof import('../_lib/auth.js')>(), requireViewer: vi.fn() }))
vi.mock('../_lib/nameSummaryStatic.js', async original => ({ ...await original<typeof import('../_lib/nameSummaryStatic.js')>(), staticSummary: vi.fn() }))
const viewer: Identity = { email: null, name: null, scopes: ['gcs'], admin: false, via: 'session', subject: null }
const env: NameSummaryEnv = { BASE_SCOPE: 'gcs', NAME_SUMMARY_STATIC: '1', INDEX_R2: {} as R2Bucket }
const query = 'date=2026-10-05&name=DATAKIT'
const request = (search = query, vars: NameSummaryEnv = env, method = 'GET') => onRequest({ request: new Request(`https://dev.example/api/name-summary?${search}`, { method }), env: vars })
/** The params each call to the static index carried. */
const sent = () => vi.mocked(staticSummary).mock.calls.map(([, params]) => params.toString())
async function refused(response: Response, status: number, error: string) {
  expect([response.status, await response.json(), response.headers.get('cache-control')]).toEqual([status, { error }, 'private, no-store'])
}
beforeEach(() => {
  vi.mocked(requireViewer).mockReset().mockResolvedValue(viewer)
  vi.mocked(staticSummary).mockReset().mockImplementation(async () => new Response('{}'))
})
afterEach(() => vi.unstubAllGlobals())

describe('private, explicitly enabled summary route', () => {
  it('authenticates before the feature flag, validation or index access', async () => {
    vi.mocked(requireViewer).mockResolvedValue(new Response('{"error":"Unauthorized"}', { status: 401 }))
    await refused(await request('name=%FF', {}), 401, 'Unauthorized')
    expect([sent(), vi.mocked(requireViewer).mock.calls]).toEqual([[], [[{ request: expect.any(Request), env: { PUBLIC_READ: undefined } }]]])
  })
  it.each([
    ['no flag', { NAME_SUMMARY_STATIC: undefined }],
    ['flag 0', { NAME_SUMMARY_STATIC: '0' }],
    ['no R2 binding', { INDEX_R2: undefined }],
  ])('is off with %s', async (_, override) => {
    await refused(await request(query, { ...env, ...override }), 404, 'Name summaries are not enabled.')
    expect(sent()).toEqual([])
  })
  it.each([{ PUBLIC_READ: '1' }, { STORE_KEY: '' }, { STORE_SCOPE: 'secondary' }])('refuses public/secondary deployment %j', async override => {
    await refused(await request(query, { ...env, ...override }), override.PUBLIC_READ ? 403 : 409,
      override.PUBLIC_READ ? 'Name summaries require a private viewer-protected deployment.' : 'Name summaries serve the primary frozen index only.')
    expect(sent()).toEqual([])
  })
  it('refuses anonymous identity and non-GET', async () => {
    vi.mocked(requireViewer).mockResolvedValue({ ...viewer, via: 'public' })
    await refused(await request(), 403, 'Name summaries require a private viewer-protected deployment.')
    vi.mocked(requireViewer).mockResolvedValue(viewer)
    const response = await request(query, env, 'POST')
    expect(response.headers.get('allow')).toBe('GET')
    await refused(response, 405, 'Only GET is supported.')
    expect(sent()).toEqual([])
  })
  it('real auth cannot inherit the anonymous PUBLIC_READ bypass', async () => {
    const actual = await vi.importActual<typeof import('../_lib/auth.js')>('../_lib/auth.js'); vi.mocked(requireViewer).mockImplementation(actual.requireViewer)
    const response = await request(query, { ...env, PUBLIC_READ: '1' })
    expect([response.status, response.headers.get('cache-control'), sent()]).toEqual([401, 'private, no-store', []])
  })
  it.each([
    ['date=2026-10-05&name=x&path=', 'Use date, name and optional from only.'],
    ['date=2026-10-05&name=x&owner=y', 'Use date, name and optional from only.'],
    ['date=2026-10-05&name=x&%6eame=y', 'Duplicate query parameter.'],
    ['date=2026-10-05&name=%FF', 'Invalid query parameters.'],
    ['date=2026-02-30&name=x', 'A valid scan date is required.'],
    ['date=2026-10-05&name=a/b', 'Use one nonempty NUL/slash-free literal of at most 512 characters.'],
    ['date=2026-10-05&name=%00', 'Use one nonempty NUL/slash-free literal of at most 512 characters.'],
    ['date=2026-10-05&name=x&from=2026-10-05', 'from must be a valid earlier scan date.'],
  ])('refuses invalid %s before IO', async (search, error) => { await refused(await request(search), 400, error); expect(sent()).toEqual([]) })
  it('asks the static index for any scan and baseline, with the params as given', async () => {
    const statuses = [(await request()).status, (await request('date=2026-10-09T1802&name=Datakit&from=2026-10-08')).status]
    expect([statuses, sent()]).toEqual([[200, 200], ['date=2026-10-05&name=DATAKIT', 'date=2026-10-09T1802&name=Datakit&from=2026-10-08']])
  })
})

const UNSET_SLASH = { error: 'Use one nonempty NUL/slash-free literal of at most 512 characters.' }
describe('an indexed-only deployment (`FILTER_INDEXED_ONLY=1`)', () => {
  it('refuses a name with `/` with its reason code; unset, the validator\'s own message (no code)', async () => {
    const flagged = await request('date=2026-10-05&name=a%2Fb', { ...env, FILTER_INDEXED_ONLY: '1' } as NameSummaryEnv)
    expect([flagged.status, await flagged.json(), sent()]).toEqual([400, { error: 'Only plain text search (a substring of a file or folder name) is supported here; a term can’t contain “/” (it matches within one name).', code: 'unsupported-slash' }, []])
    const unset = await request('date=2026-10-05&name=a%2Fb')
    expect([unset.status, await unset.json()]).toEqual([400, UNSET_SLASH])
  })
})

describe('an anchored name (`^q`, `q$`): /names reads literals only', () => {
  const ANCHOR = { error: 'Starts-with (^) and ends-with ($) search isn’t indexed on this deployment yet; search for a plain substring instead.', code: 'anchor-not-indexed' }
  it('is `anchor-not-indexed` (flag set or not), never read; a lone `^`/`$` or an inner one stays a literal', async () => {
    const got = await Promise.all(['^qwen', 'qwen$', '^qwen$'].flatMap(name => [env, { ...env, FILTER_INDEXED_ONLY: '1' } as NameSummaryEnv].map(async vars => {
      const r = await request(new URLSearchParams({ date: '2026-10-05', name }).toString(), vars)
      return [r.status, await r.json()]
    })))
    expect([got, sent()]).toEqual([Array(6).fill([400, ANCHOR]), []])
    const literal = []
    for (const name of ['^', '$', 'a^b$c']) literal.push((await request(new URLSearchParams({ date: '2026-10-05', name }).toString())).status)
    expect([literal, sent().length]).toEqual([[200, 200, 200], 3])
  })
  it('`\\^q`, `q\\$`, `\\\\` and a quoted `"^q"` are literals: read unescaped, answered; a bare `^foo` / `foo$` still refused', async () => {
    const got = []
    for (const name of ['foo\\$', '\\^foo', '"^foo"', '"foo$"', '^foo\\$', 'a\\\\b', '^foo', 'foo$', '^$']) {
      const before = vi.mocked(staticSummary).mock.calls.length
      const r = await request(new URLSearchParams({ date: '2026-10-05', name }).toString())
      const names = vi.mocked(staticSummary).mock.calls.slice(before).map(([, params]) => params.get('name'))
      got.push([name, r.status, r.status === 200 ? names : await r.json()])
    }
    expect(got).toEqual([
      ['foo\\$', 200, ['foo$']],
      ['\\^foo', 200, ['^foo']],
      ['"^foo"', 200, ['^foo']],
      ['"foo$"', 200, ['foo$']],
      ['^foo\\$', 400, ANCHOR],
      ['a\\\\b', 200, ['a\\b']],
      ['^foo', 400, ANCHOR],
      ['foo$', 400, ANCHOR],
      ['^$', 200, ['^$']],
    ])
  })
})
