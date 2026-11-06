import { afterEach, describe, expect, it, vi } from 'vitest'
import type { D1Database } from '@cloudflare/workers-types'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { CACHE_V } from '../_lib/edgeCache'
import { classKey, parseClasses } from '../_lib/scope'
import { FILTER_VIEW_V, SCOPED_V } from '../_lib/view'

// The scoped read's answer version (`SCOPED_V`, `&sv=`) is in a view's and a diff's edge-cache key only where the
// answer is a `scopedRead`: an owner pool or a class scope over the interval store (and a filter under a lens). Every
// other key (the plain map's and diff's, a lens's, a per-scan store's) is, to the byte, what it was without it.

// Classes this deployment has, as a key spells them: another's letters are no scope at all.
const LETTERS = [...'abcdefghijklmnopqrstuvwxyz'].filter(l => parseClasses(l))
const ONE = classKey(parseClasses(LETTERS[0]))
const TWO = classKey(parseClasses(LETTERS.slice(0, 2).join('')))

const keys: string[] = []
vi.mock('../_lib/edgeCache.js', async orig => ({
  ...await orig<typeof import('../_lib/edgeCache.js')>(),
  // Record the key, answer as a hit: the handler returns before any read.
  cacheMatch: vi.fn(async (_env: unknown, key: Request) => { keys.push(key.url); return new Response('{}') }),
}))
vi.mock('../_lib/identity.js', async orig => ({ ...await orig<typeof import('../_lib/identity.js')>(), loadRegistry: vi.fn(async () => ({})) }))
vi.mock('../_lib/ledger.js', async orig => ({
  ...await orig<typeof import('../_lib/ledger.js')>(),
  ledgerHead: vi.fn(async () => 7),
  hasLedger: vi.fn(async () => true),
}))
vi.mock('../_lib/extras.js', async orig => ({ ...await orig<typeof import('../_lib/extras.js')>(), hasExtras: vi.fn(async () => false) }))
vi.mock('../_lib/index.js', async orig => ({
  ...await orig<typeof import('../_lib/index.js')>(),
  pathGens: vi.fn(async () => 'g1'),
  storeHeldScans: vi.fn(async () => ['2026-09-01', '2026-10-01']),
}))

const { onRequestGet: subtree } = await import('./subtree')
const { onRequestGet: diff } = await import('./diff')

afterEach(() => { keys.length = 0 })

async function db(): Promise<D1Database> {
  const { db } = await sqliteD1()
  // the scans the requests name are indexed (an unindexed one is a 404: `scanArg`)
  for (const date of ['2026-09-01', '2026-10-01']) await db.prepare("INSERT INTO index_schema (date, variant, version, schema_json) VALUES (?, 'path', 2, '[]')").bind(date).run()
  return db
}
const STORE = { GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data' }
/** A deployment on per-scan sorts, and one on the interval store (a pinned generation: no pointer read). */
const perScan = async (): Promise<Env> => ({ ...STORE, DB: await db(), DEV_EMAIL: 'alan@example.test', PROD_HOST: 'localhost' }) as Env
const intervals = async (): Promise<Env> => ({ ...await perScan(), PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g8', INDEX_R2: {} as R2Bucket }) as Env
/** The keys a handler looked up for each of `qs` (appended to `base`), in turn: one per request. */
const asked = async (handler: (ctx: never) => Promise<Response>, env: Env, base: string, qs: string[]): Promise<string[]> => {
  for (const q of qs) await handler({ request: new Request(`http://localhost${base}${q}`), env } as never)
  return [...keys]
}

const SV = `&sv=${SCOPED_V}`

describe('/api/subtree', () => {
  const S = '/api/subtree?date=2026-10-01&path=marin-a&w=1200&h=700'
  /** The key before `SCOPED_V`, by its parts that vary here. */
  const key = (o: { l?: string; o?: string; b?: string; cl?: string; q?: string; head?: number }) =>
    `https://subtree.cache/v${CACHE_V}/2026-10-01/marin-a?w=1280&h=768&a=12&t=2&l=${o.l ?? ''}&o=${o.o ?? ''}&b=${o.b ?? ''}&D=&cl=${o.cl ?? ''}&x=0&F=${o.q ? 0 : 1}&qs=${o.q ? 'simple' : ''}&q=${o.q ?? ''}&head=${o.head ?? 0}&g=g1&st=${o.q ? `&fv=${FILTER_VIEW_V}` : ''}`

  it('the plain view: its key as it was', async () => {
    expect(await asked(subtree, await intervals(), S, [''])).toEqual([
      `https://subtree.cache/v${CACHE_V}/2026-10-01/marin-a?w=1280&h=768&a=12&t=2&l=&o=&b=&D=&cl=&x=0&F=1&qs=&q=&head=0&g=g1&st=`,
    ])
  })
  it('an owner pool, a class scope, both: the version, after the key as it was', async () => {
    expect(await asked(subtree, await intervals(), S, ['&o=*', '&o', '&o=!alan', `&cl=${TWO}`, `&o=*&cl=${ONE}`])).toEqual([
      key({ o: '*', head: 7 }) + SV,
      key({ head: 7 }) + SV,
      key({ o: '!alan', head: 7 }) + SV,
      key({ cl: TWO }) + SV,
      key({ o: '*', cl: ONE, head: 7 }) + SV,
    ])
  })
  it('a lens: its key as it was, scoped or not; a filter under it reads as a scope does', async () => {
    expect(await asked(subtree, await intervals(), S, ['&lens=user:grace', `&lens=user:grace&cl=${TWO}`, '&lens=user:grace&by=alan', '&lens=user:grace&q=ckpt', '&by=alan', '&q=ckpt'])).toEqual([
      key({ l: 'user:grace', head: 7 }),
      key({ l: 'user:grace', cl: TWO, head: 7 }),
      key({ l: 'user:grace', b: 'alan', head: 7 }),
      key({ l: 'user:grace', q: 'ckpt', head: 7 }) + SV,
      key({ b: 'alan' }) + SV,
      key({ q: 'ckpt' }),
    ])
  })
  it('a per-scan store: every key as it was', async () => {
    expect(await asked(subtree, await perScan(), S, ['', '&o=*', `&cl=${TWO}`, '&lens=user:grace&q=ckpt'])).toEqual([
      key({}),
      key({ o: '*', head: 7 }),
      key({ cl: TWO }),
      key({ l: 'user:grace', q: 'ckpt', head: 7 }),
    ])
  })
})

describe('/api/diff', () => {
  const D = '/api/diff?from=2026-09-01&to=2026-10-01&path=marin-a'
  /** The key before `SCOPED_V`, by its parts that vary here. */
  const key = (o: { l?: string; o?: string; cl?: string; q?: string; head?: number; s?: 1 }) =>
    `https://diff.cache/v${CACHE_V}/2026-09-01/2026-10-01/marin-a?w=1280&h=896&a=12&t=2&n=500&l=${o.l ?? ''}&o=${o.o ?? ''}&cl=${o.cl ?? ''}&qs=${o.q ? 'simple' : ''}&q=${o.q ?? ''}&head=${o.head ?? 0}&s=${o.s ?? 0}&D=&g=g1&st=${o.q ? `&fv=${FILTER_VIEW_V}` : ''}${o.s ? '' : ``}`

  it('the plain diff: its key as it was', async () => {
    expect(await asked(diff, await intervals(), D, [''])).toEqual([
      `https://diff.cache/v${CACHE_V}/2026-09-01/2026-10-01/marin-a?w=1280&h=896&a=12&t=2&n=500&l=&o=&cl=&qs=&q=&head=0&s=0&D=&g=g1&st=`,
    ])
  })
  it('an owner pool, a class scope, both, a scoped summary: the version, after the key as it was', async () => {
    expect(await asked(diff, await intervals(), D, ['&o=*', '&o', `&cl=${TWO}`, `&o=*&cl=${ONE}`, `&cl=${TWO}&summary=1`])).toEqual([
      key({ o: '*', head: 7 }) + SV,
      key({ head: 7 }) + SV,
      key({ cl: TWO }) + SV,
      key({ o: '*', cl: ONE, head: 7 }) + SV,
      key({ cl: TWO, s: 1 }) + SV,
    ])
  })
  it('a lens: its key as it was, scoped or not; a filter under it reads as a scope does', async () => {
    expect(await asked(diff, await intervals(), D, ['&lens=user:grace', `&lens=user:grace&cl=${TWO}`, '&lens=user:grace&q=ckpt', '&q=ckpt', '&summary=1'])).toEqual([
      key({ l: 'user:grace', head: 7 }),
      key({ l: 'user:grace', cl: TWO, head: 7 }),
      key({ l: 'user:grace', q: 'ckpt', head: 7 }) + SV,
      key({ q: 'ckpt' }),
      key({ s: 1 }),
    ])
  })
  it('a per-scan store: every key as it was', async () => {
    expect(await asked(diff, await perScan(), D, ['', '&o=*', `&cl=${TWO}`, '&lens=user:grace&q=ckpt'])).toEqual([
      key({}),
      key({ o: '*', head: 7 }),
      key({ cl: TWO }),
      key({ l: 'user:grace', q: 'ckpt', head: 7 }),
    ])
  })
})
