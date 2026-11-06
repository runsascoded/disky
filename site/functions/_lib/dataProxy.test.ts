import { beforeEach, describe, expect, it, vi } from 'vitest'
import { onRequest } from '../data/[[path]]'
import { type Env, gateFor } from './auth'
import { sqliteD1 } from './testD1'

const get = vi.hoisted(() => vi.fn(async (key: string) => ({ bytes: new TextEncoder().encode(JSON.stringify({ key })), contentType: 'application/json' })))
const servedStore = vi.hoisted(() => vi.fn((_env: Env, _prefixes: string[]) => ({ get })))
vi.mock('./index.js', () => ({ storeReady: () => true, servedStore, pathScans: vi.fn() }))

const env: Env = {
  BASE_SCOPE: 'oa-r2', PUBLIC_READ: '1', SNAPSHOTS_SUBDIR: 'r2',
  STORES_JSON: JSON.stringify({ meta: { scope: 'admin', vars: { SNAPSHOTS_SUBDIR: 'meta' } } }),
}
const request = async (path: string, override: Partial<Env> = {}) => {
  const response = await onRequest({ request: new Request(`https://example.test/data/${path}`), env: { ...env, ...override } })
  return [response.status, await response.text()]
}

beforeEach(() => vi.clearAllMocks())

describe('snapshot store scope', () => {
  it('serves the primary snapshot and its shared rules, but refuses a named secondary through the primary gate', async () => {
    expect(await request('r2/2026-10-13T1935/age.json')).toEqual([200, '{"key":"snapshots/r2/2026-10-13T1935/age.json"}'])
    expect(await request('rules.json')).toEqual([200, '{"key":"snapshots/rules.json"}'])
    expect(await request('meta/2026-10-13T1935/age.json')).toEqual([404, 'not found'])
    expect(await request('meta/2026-10-13T1935/age.json?store=meta')).toEqual([401, '{"error":"unauthenticated"}\n'])
    expect(get.mock.calls).toEqual([['snapshots/r2/2026-10-13T1935/age.json'], ['snapshots/rules.json']])
  })
  it('also refuses named secondary directories when the primary stores snapshots at the bucket root', async () => {
    expect(await request('2026-10-13T1935/age.json', { SNAPSHOTS_SUBDIR: '' })).toEqual([200, '{"key":"snapshots/2026-10-13T1935/age.json"}'])
    expect(await request('meta/2026-10-13T1935/age.json', { SNAPSHOTS_SUBDIR: '' })).toEqual([404, 'not found'])
    expect(get.mock.calls).toEqual([['snapshots/2026-10-13T1935/age.json']])
  })
  it.each(['r2/2026-02-30/age.json', 'r2/2026-10-13T2460/age.json', 'r2/private/age.json', 'r2-old/2026-10-13/age.json'])('refuses malformed and neighboring namespaces without reading storage: %s', async path => {
    expect(await request(path)).toEqual([404, 'not found'])
    expect(get.mock.calls).toEqual([])
  })
  it('allows date-only captures and scopes the storage seam to the precise namespace', async () => {
    expect(await request('r2/2026-10-13/age.json')).toEqual([200, '{"key":"snapshots/r2/2026-10-13/age.json"}'])
    expect(servedStore.mock.calls.map(([, prefixes]) => prefixes)).toEqual([['snapshots/r2/']])
    expect(await request('rules.json')).toEqual([200, '{"key":"snapshots/rules.json"}'])
    expect(servedStore.mock.calls.map(([, prefixes]) => prefixes)).toEqual([['snapshots/r2/'], ['snapshots/rules.json']])
  })
})

describe('optional files', () => {
  it('answers a missing optional file 204 and a missing required one 404', async () => {
    get.mockRejectedValueOnce(new Error('NoSuchKey')).mockRejectedValueOnce(new Error('NoSuchKey'))
    expect(await request('r2/2026-10-13T1935/non-live.json?optional=1')).toEqual([204, ''])
    expect(await request('r2/2026-10-13T1935/non-live.json')).toEqual([404, 'not found'])
    expect(await request('r2/2026-10-13T1935/non-live.json?optional=1')).toEqual([200, '{"key":"snapshots/r2/2026-10-13T1935/non-live.json"}'])
  })
})

// `_lib/restrict.ts`: `rules.json` is the identity roster (`users`) and the attribution rules (`prefix_owners`:
// a prefix, its owner, a note).
describe('D1', () => {
  describe('`rules.json`, as a path-restricted link', () => {
    const RULES = {
      users: [{ u: 'alice', aliases: ['al'] }],
      prefix_owners: [
        { prefix: 'gs://bk/open/', user: 'alice' },
        { prefix: 'gs://bk/secret/', user: 'alice', note: 'the hidden dir itself' },
        { prefix: 'gs://bk/secret/plans/q4/', user: 'alice', note: 'below it' },
        { prefix: 'gs://bk/secretive/', user: 'alice' },
      ],
    }
    async function rulesAs(scopes: string[]) {
      const { db } = await sqliteD1()
      const linkEnv = { BASE_SCOPE: 'gcs', SESSION_SECRET: 's'.repeat(32), DB: db } as Env
      const { token } = await gateFor(linkEnv)!.mint({ email: null, scopes, name: 'link', createdBy: 'test', expiresAt: null, maxRedeems: null })
      get.mockResolvedValueOnce({ bytes: new TextEncoder().encode(JSON.stringify(RULES)), contentType: 'application/json' })
      const res = await onRequest({ request: new Request('https://example.test/data/rules.json', { headers: { authorization: `Bearer ${token}` } }), env: linkEnv })
      return [res.status, res.headers.get('cache-control'), await res.json()]
    }
    it('the roster whole; no rule for a prefix strictly below a hidden path (the hidden dir’s own, and a lookalike’s, stay)', async () => {
      expect(await rulesAs(['gcs:read', 'gcs:hide:bk/secret'])).toEqual([200, 'private, max-age=300', {
        users: RULES.users,
        prefix_owners: [RULES.prefix_owners[0], RULES.prefix_owners[1], RULES.prefix_owners[3]],
      }])
    })
    it('an unrestricted link reads the file as published', async () => {
      expect(await rulesAs(['gcs:read'])).toEqual([200, 'private, max-age=300', RULES])
    })
  })
})
