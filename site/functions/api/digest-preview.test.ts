// `/api/digest-preview` (specs/slack-preview-page.md): admins only; lists the served store's preview bundles newest
// month first, serves one by key; other keys and versions left out.
import { beforeAll, describe, expect, it } from 'vitest'
import { gateFor } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { BUNDLE_PARAM, onRequestGet } from './digest-preview'

/** An `R2Bucket`'s `list` / `get` as `R2Store` uses them: `delimiter: '/'` rolls deeper keys up into
 *  `delimitedPrefixes` (the bundles sit three levels under the prefix), pages of 2. */
function fakeR2(files: Record<string, string>) {
  return {
    async list({ prefix, cursor, limit, delimiter }: { prefix: string; cursor?: string; limit: number; delimiter?: string }) {
      const under = Object.keys(files).filter(k => k.startsWith(prefix)).sort()
      const items = [...new Set(under.map(k => {
        const i = delimiter ? k.indexOf(delimiter, prefix.length) : -1
        return i < 0 ? k : k.slice(0, i + 1)
      }))]
      const at = cursor ? Number(cursor) : 0
      const page = items.slice(at, at + Math.min(limit, 2))
      const more = at + page.length < items.length
      return {
        objects: page.filter(k => !k.endsWith('/')).map(key => ({ key, size: files[key].length, uploaded: new Date(0) })),
        delimitedPrefixes: page.filter(k => k.endsWith('/')), truncated: more, cursor: more ? String(at + page.length) : undefined,
      }
    },
    async get(key: string) {
      const v = files[key]
      return v === undefined ? null : { size: v.length, arrayBuffer: async () => new TextEncoder().encode(v).buffer }
    },
  }
}

const bundle = (month: string, version = 1) => JSON.stringify({ version, month, op: `OP ${month}` })
let env: Record<string, unknown>
let admin: string
let viewer: string

beforeAll(async () => {
  const { db } = await sqliteD1()
  env = {
    DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'gcs', SERVED_R2: '1',
    INDEX_R2: fakeR2({
      'digest/preview/C1/sender/2026-10.json': bundle('2026-10'),
      'digest/preview/C1/sender/2026-11.json': bundle('2026-11'),
      'digest/preview/C1/body/2026-11.json': bundle('2026-11'),
      'digest/preview/C1/sender/2026-09.json': bundle('2026-09', 2),
      'digest/preview/C1/sender/notes.txt': 'x',
      'digest/C1/sender/2026-11.json': '{}',
    }),
  }
  const gate = gateFor(env as never)!
  const tok = async (email: string, scopes: string[]) =>
    (await gate.mint({ email, scopes, name: email, createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  admin = await tok('ops@example.test', ['gcs', 'admin'])
  viewer = await tok('ann@example.test', ['gcs'])
})

const get = async (qs: string, token?: string) => {
  const res = await onRequestGet({ request: new Request(`https://site.example.test/api/digest-preview${qs}`, { headers: token ? { authorization: `Bearer ${token}` } : {} }), env } as never)
  return [res.status, await res.json()] as const
}

describe('/api/digest-preview', () => {
  it('lists the bundles, newest month first, then channel and variant', async () => {
    expect(await get('', admin)).toEqual([200, { bundles: [
      { key: 'digest/preview/C1/body/2026-11.json', channel: 'C1', variant: 'body', month: '2026-11' },
      { key: 'digest/preview/C1/sender/2026-11.json', channel: 'C1', variant: 'sender', month: '2026-11' },
      { key: 'digest/preview/C1/sender/2026-10.json', channel: 'C1', variant: 'sender', month: '2026-10' },
      { key: 'digest/preview/C1/sender/2026-09.json', channel: 'C1', variant: 'sender', month: '2026-09' },
    ] }])
  })
  it('serves a bundle by key; another version or a missing one is a 404, a bad key a 400', async () => {
    expect(await get('?b=digest/preview/C1/sender/2026-11.json', admin)).toEqual([200, { version: 1, month: '2026-11', op: 'OP 2026-11' }])
    expect(await get('?b=digest/preview/C1/sender/2026-09.json', admin)).toEqual([404, { error: 'no preview bundle digest/preview/C1/sender/2026-09.json' }])
    expect(await get('?b=digest/preview/C1/sender/2026-08.json', admin)).toEqual([404, { error: 'no preview bundle digest/preview/C1/sender/2026-08.json' }])
    expect(await get('?b=digest/C1/sender/2026-11.json', admin)).toEqual([400, { error: 'b: digest/preview/<channel>/<variant>/<YYYY-MM>.json' }])
  })
  it("serves a bundle to a signed-in browser (a session cookie, not a token): its query never reads as a share link", async () => {
    // the gate takes a `?key=` as a presented grant token, which would 401 the session (hf.oa.dev, 2026-11-02)
    const e = { ...env, STAFF_DOMAIN: 'example.test' }
    const cookie = (await gateFor(e as never)!.signIn('ops@example.test', new Request('https://site.example.test/signin')))!.cookie.split(';')[0]
    const res = await onRequestGet({ request: new Request(`https://site.example.test/api/digest-preview?${BUNDLE_PARAM}=digest/preview/C1/sender/2026-11.json`, { headers: { cookie } }), env: e } as never)
    expect([res.status, await res.json()]).toEqual([200, { version: 1, month: '2026-11', op: 'OP 2026-11' }])
  })
  it('is admin-only', async () => {
    expect([(await get('', viewer))[0], (await get(''))[0]]).toEqual([403, 401])
  })
})
