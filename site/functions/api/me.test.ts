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
vi.mock('../_lib/extras.js', async orig => ({ ...await orig<typeof import('../_lib/extras.js')>(), hasExtras: vi.fn(async () => false) }))
vi.mock('../_lib/index.js', async orig => ({
  ...await orig<typeof import('../_lib/index.js')>(),
  pathGens: vi.fn(async () => 'g1'),
  pathScans: vi.fn(async () => ({ results: [{ date: '2026-10-01' }] })),
}))

const { onRequestGet: subtree, warmSubtree } = await import('./subtree')
const { onRequestGet: diff } = await import('./diff')
const { onRequestGet: series } = await import('./series')
const { onRequestGet: me } = await import('./me')

afterEach(() => { keys.length = 0 })

describe('D1', () => {
  /** A D1 with a `user_emails` table (gcs's schema has it; elsewhere it is
   * built ad hoc, as gcs has it) and one row (alan's sign-in → `alan-turing`). */
  async function db(): Promise<D1Database> {
    const { db, raw } = await sqliteD1()
    raw.exec('CREATE TABLE IF NOT EXISTS user_emails (email TEXT PRIMARY KEY, user TEXT NOT NULL, who TEXT NOT NULL, ts INTEGER NOT NULL)')
    await db.prepare('INSERT INTO user_emails (email, user, who, ts) VALUES (?, ?, ?, ?)').bind('alan@example.test', 'alan-turing', 'admin', 0).run()
    // the scans the requests name are indexed (an unindexed one is a 404: `scanArg`)
    for (const date of ['2026-09-01', '2026-10-01']) await db.prepare("INSERT INTO index_schema (date, variant, version, schema_json) VALUES (?, 'path', 2, '[]')").bind(date).run()
    return db
  }
  const STORE = { GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data' }
  /** A localhost request: `identify` is the dev identity, signed in as `DEV_EMAIL`. */
  const envAs = async (email: string): Promise<Env> => ({ ...STORE, DB: await db(), DEV_EMAIL: email, PROD_HOST: 'localhost' }) as Env
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
    it('a D1 without `user_emails` (cloud’s), or no DB at all: the registry rule alone', async () => {
      const { db: bare, raw } = await sqliteD1()
      raw.exec('DROP TABLE IF EXISTS user_emails')
      expect(await Promise.all([
        ownerIdFor({ DB: bare } as Env, 'alan@example.test'),
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

  describe('`lens=user:me` keys the cache on the resolved id', () => {
    const S = '/api/subtree?date=2026-10-01&path=marin-a&w=1200&h=700'
    const D = '/api/diff?from=2026-09-01&to=2026-10-01&path=marin-a'
    const T = '/api/series?path=marin-a'
    it('/api/subtree: `me` = the named id for the same caller; another caller\'s `me` is their own key', async () => {
      const alan = await envAs('alan@example.test')
      await subtree(at(alan, `${S}&lens=user:me`))
      await subtree(at(alan, `${S}&lens=user:alan-turing`))
      await subtree(at(await envAs('grace@example.test'), `${S}&lens=user:me`))
      const key = (l: string) => `https://subtree.cache/v${CACHE_V}/2026-10-01/marin-a?w=1280&h=768&a=12&t=2&l=${l}&o=&b=&D=&cl=&x=0&F=1&qs=&q=&head=0&g=g1&st=`
      expect(keys).toEqual([key('user:alan-turing'), key('user:alan-turing'), key('user:grace')])
    })
    it('/api/diff', async () => {
      const alan = await envAs('alan@example.test')
      await diff(at(alan, `${D}&lens=user:me`))
      await diff(at(alan, `${D}&lens=user:alan-turing`))
      const key = `https://diff.cache/v${CACHE_V}/2026-09-01/2026-10-01/marin-a?w=1280&h=896&a=12&t=2&n=500&l=user:alan-turing&o=&cl=&qs=&q=&head=0&s=0&D=&g=g1&st=`
      expect(keys).toEqual([key, key])
    })
    it('/api/series', async () => {
      const alan = await envAs('alan@example.test')
      await series(at(alan, `${T}&lens=user:me`))
      await series(at(alan, `${T}&lens=user:alan-turing`))
      const key = `https://series.cache/v${CACHE_V}/marin-a?P=&l=user:alan-turing&o=&cl=&s=&qs=&q=&st=&d=2026-10-01&x=&head=0&g=g1&oh=0`
      expect(keys).toEqual([key, key])
    })
    it('`me` with nobody to resolve (an identity-less cache warm) is a 400, never a cache read', async () => {
      const r = await warmSubtree(await envAs('alan@example.test'), `https://gcs.example.test${S}&lens=user:me`)
      expect([r.status, await r.text(), keys]).toEqual([400, "'me' needs a signed-in identity with an email", []])
    })
  })
})
