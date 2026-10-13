/** `/api/name-summary-registry`'s gates, with the static name index (`staticRegistry`) stubbed. */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { requireViewer, type Identity } from '../_lib/auth.js'
import type { NameSummaryEnv } from '../_lib/nameSummary.js'
import { staticRegistry } from '../_lib/nameSummaryStatic.js'
import { onRequest as registry } from './name-summary-registry.js'

vi.mock('../_lib/auth.js', async original => ({ ...await original<typeof import('../_lib/auth.js')>(), requireViewer: vi.fn() }))
vi.mock('../_lib/nameSummaryStatic.js', async original => ({ ...await original<typeof import('../_lib/nameSummaryStatic.js')>(), staticRegistry: vi.fn() }))
const viewer: Identity = { email: null, name: null, scopes: ['gcs'], admin: false, via: 'session', subject: null }
const env: NameSummaryEnv = { BASE_SCOPE: 'gcs', NAME_SUMMARY_STATIC: '1', INDEX_R2: {} as R2Bucket }
const scans = (vars: NameSummaryEnv = env, search = '', method = 'GET') => registry({ request: new Request(`https://dev.example/api/name-summary-registry${search}`, { method }), env: vars })
async function refused(response: Response, status: number, error: string) {
  expect([response.status, await response.json(), response.headers.get('cache-control')]).toEqual([status, { error }, 'private, no-store'])
}
beforeEach(() => {
  vi.mocked(requireViewer).mockReset().mockResolvedValue(viewer)
  vi.mocked(staticRegistry).mockReset().mockImplementation(async () => new Response('{"schema":"static-name-registry-v1"}'))
})

describe('private scan registry', () => {
  it('authenticates before flags, query validation and index access', async () => {
    vi.mocked(requireViewer).mockResolvedValue(new Response('{"error":"Unauthorized"}', { status: 401 }))
    await refused(await scans({}, '?x=y'), 401, 'Unauthorized')
    expect([vi.mocked(staticRegistry).mock.calls, vi.mocked(requireViewer).mock.calls]).toEqual([[], [[{ request: expect.any(Request), env: { PUBLIC_READ: undefined } }]]])
  })
  it.each([
    [{ NAME_SUMMARY_STATIC: '0' }, '', 'GET', 404, 'Name summaries are not enabled.'],
    [{ INDEX_R2: undefined }, '', 'GET', 404, 'Name summaries are not enabled.'],
    [{ PUBLIC_READ: '1' }, '', 'GET', 403, 'Name summaries require a private viewer-protected deployment.'],
    [{ STORE_KEY: 'secondary' }, '', 'GET', 409, 'Name summaries serve the primary frozen index only.'],
    [{}, '?date=2026-10-06', 'GET', 400, 'The name-summary scan registry accepts no query parameters.'],
    [{}, '', 'POST', 405, 'Only GET is supported.'],
  ] as const)('rejects unsafe/unsupported registry request %j %s %s', async (override, search, method, status, error) => {
    await refused(await scans({ ...env, ...override }, search, method), status, error)
    expect(vi.mocked(staticRegistry).mock.calls).toEqual([])
  })
  it('answers from the static index', async () => {
    const response = await scans()
    expect([response.status, await response.text(), vi.mocked(staticRegistry).mock.calls.length]).toEqual([200, '{"schema":"static-name-registry-v1"}', 1])
  })
})
