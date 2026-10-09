import { afterEach, describe, expect, it, vi } from 'vitest'
import type { D1Database } from '@cloudflare/workers-types'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { ownerIdFor, resolveUser } from '../_lib/me'
import { CACHE_V } from '../_lib/edgeCache'

// `me` resolution (`_lib/me.ts`) and its cache keys: a `lens=user:me` read
// must key the shared edge cache on the caller's resolved id — the same key
// as naming the id outright — never on the literal `me`.

const keys: string[] = []
vi.mock('../_lib/edgeCache.js', async orig => ({
  ...await orig<typeof import('../_lib/edgeCache.js')>(),
  // Record the key, answer as a hit: the handler returns before any read.
  cacheMatch: vi.fn(async (_env: unknown, key: Request) => { keys.push(key.url); return new Response('{}') }),
}))
vi.mock('../_lib/identity.js', async orig => ({
  ...await orig<typeof import('../_lib/identity.js')>(),
  // An empty registry (no store read): an email resolves to its sanitized handle.
  loadRegistry: vi.fn(async () => ({})),
}))
vi.mock('../_lib/ledger.js', async orig => ({
  ...await orig<typeof import('../_lib/ledger.js')>(),
  ledgerHead: vi.fn(async () => 7),
  hasLedger: vi.fn(async () => true),
}))
vi.mock('../_lib/extras.js', async orig => ({ ...await orig<typeof import('../_lib/extras.js')>(), hasExtras: vi.fn(async () => false) }))
vi.mock('../_lib/index.js', async orig => ({
  ...await orig<typeof import('../_lib/index.js')>(),
  pathGens: vi.fn(async () => 'g1'),
  pathScans: vi.fn(async () => ({ results: [{ date: '2026-10-01' }] })),
}))

vi.mock('../_lib/ownerTotals.js', () => ({
  ownerTotals: vi.fn(async () => ({ head: 3, users: { 'alan-turing': { b: 5, mix: { '1': 5 } }, grace: { b: 2, mix: {} } }, assignments: [] })),
}))

const { onRequestGet: subtree, warmSubtree } = await import('./subtree')
const { onRequestGet: diff } = await import('./diff')
const { onRequestGet: series } = await import('./series')
const { onRequestGet: me } = await import('./me')
const { onRequestGet: estate } = await import('./estate')

afterEach(() => { keys.length = 0 })

/** A D1 with gcs's `user_emails` table (`cloud` carries only the cw lineage)
 * and one row (alan's sign-in → `alan-turing`). */
async function db(): Promise<D1Database> {
  const { db, raw } = await sqliteD1('cw')
  raw.exec('CREATE TABLE user_emails (email TEXT PRIMARY KEY, user TEXT NOT NULL, who TEXT NOT NULL, ts INTEGER NOT NULL)')
  await db.prepare('INSERT INTO user_emails (email, user, who, ts) VALUES (?, ?, ?, ?)').bind('alan@example.test', 'alan-turing', 'admin', 0).run()
  return db
}
const STORE = { GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data' }
/** A localhost request: `identify` is the dev identity, signed in as `DEV_EMAIL`. */
const envAs = async (email: string): Promise<Env> => ({ ...STORE, DB: await db(), DEV_EMAIL: email }) as Env
const at = (env: Env, path: string) => ({ request: new Request(`http://localhost${path}`), env })

describe('ownerIdFor: the `@me` / `me` rule', () => {
  it('a `user_emails` row wins (matched case-insensitively); else the registry id for the email\'s handle', async () => {
    const env = { DB: await db() } as Env
    expect(await Promise.all([
      ownerIdFor(env, 'alan@example.test'),
      ownerIdFor(env, 'Alan@Example.test'),
      ownerIdFor(env, 'grace.hopper@example.test'),
    ])).toEqual(['alan-turing', 'alan-turing', 'grace-hopper'])
  })
  it('a lineage without `user_emails` (cw), or no DB at all: the registry rule alone', async () => {
    const { db: cw } = await sqliteD1('cw')
    expect(await Promise.all([
      ownerIdFor({ DB: cw } as Env, 'alan@example.test'),
      ownerIdFor({} as Env, 'alan@example.test'),
    ])).toEqual(['alan', 'alan'])
  })
})

describe('resolveUser', () => {
  const id = (email: string | null) => ({ email, name: null, scopes: ['gcs'], admin: false, via: 'session' as const, subject: null })
  it('`me` → the caller\'s id; another id passes through; `me` without an email → null', async () => {
    const env = { DB: await db() } as Env
    expect(await Promise.all([
      resolveUser(env, id('alan@example.test'), 'me'),
      resolveUser(env, id('alan@example.test'), 'grace-hopper'),
      resolveUser(env, id(null), 'me'),
      resolveUser(env, null, 'me'),
    ])).toEqual(['alan-turing', 'grace-hopper', null, null])
  })
})

describe('GET /api/me', () => {
  it('reports the caller\'s canonical owner id', async () => {
    const r = await me(at(await envAs('alan@example.test'), '/api/me'))
    expect([r.status, r.headers.get('cache-control'), await r.json()]).toEqual([200, 'private, no-store', {
      email: 'alan@example.test',
      user: 'alan-turing',
      scopes: ['gcs', 'cw', 'admin', 'requests', 'gcs:assign'],
      admin: true,
      via: 'session',
    }])
  })
})

describe('GET /api/estate?user=me', () => {
  it('is the caller\'s estate, under their resolved id', async () => {
    const r = await estate(at(await envAs('alan@example.test'), '/api/estate?date=2026-10-01&user=me'))
    expect([r.status, await r.json()]).toEqual([200, { user: 'alan-turing', date: '2026-10-01', head: 3, bytes: 5, objects: 0, mix: { '1': 5 }, assignments: [] }])
  })
})

describe('`lens=user:me` keys the cache on the resolved id', () => {
  const S = '/api/subtree?date=2026-10-01&path=marin-a&w=1200&h=700'
  const D = '/api/diff?from=2026-09-01&to=2026-10-01&path=marin-a'
  const T = '/api/series?path=marin-a'
  it('/api/subtree: `me` = the named id for the same caller; another caller\'s `me` is their own key', async () => {
    const alan = await envAs('alan@example.test')
    await subtree(at(alan, `${S}&lens=user:me`))
    await subtree(at(alan, `${S}&lens=user:alan-turing`))
    await subtree(at(await envAs('grace@example.test'), `${S}&lens=user:me`))
    const key = (l: string) => `https://subtree.cache/v${CACHE_V}/2026-10-01/marin-a?w=1280&h=768&a=12&t=2&l=${l}&o=&b=&D=&cl=&x=0&F=1&qs=&q=&head=7&g=g1`
    expect(keys).toEqual([key('user:alan-turing'), key('user:alan-turing'), key('user:grace')])
  })
  it('/api/diff', async () => {
    const alan = await envAs('alan@example.test')
    await diff(at(alan, `${D}&lens=user:me`))
    await diff(at(alan, `${D}&lens=user:alan-turing`))
    const key = `https://diff.cache/v${CACHE_V}/2026-09-01/2026-10-01/marin-a?w=1280&h=896&a=12&t=2&n=500&l=user:alan-turing&o=&cl=&qs=&q=&head=7&s=0&D=&g=g1`
    expect(keys).toEqual([key, key])
  })
  it('/api/series', async () => {
    const alan = await envAs('alan@example.test')
    await series(at(alan, `${T}&lens=user:me`))
    await series(at(alan, `${T}&lens=user:alan-turing`))
    const key = `https://series.cache/v${CACHE_V}/marin-a?P=&l=user:alan-turing&o=&cl=&s=&d=2026-10-01&x=&head=7&g=g1`
    expect(keys).toEqual([key, key])
  })
  it('`me` with nobody to resolve (an identity-less cache warm) is a 400, never a cache read', async () => {
    const r = await warmSubtree(await envAs('alan@example.test'), `https://gcs.example.test${S}&lens=user:me`)
    expect([r.status, await r.text(), keys]).toEqual([400, "'me' needs a signed-in identity with an email", []])
  })
})
