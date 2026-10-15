import { describe, expect, it } from 'vitest'
import type { Env, Identity } from './auth'
import { gateFor } from './auth'
import { shareKeyGrant } from './og/tokens'
import { blockedForRestricted, dataAllowed, filterJson, hideScope, isHidden, restrictDiff, restrictGate, restrictionFor, restrictSubtree, restrictTree } from './restrict'
import { storeEnv } from './stores'
import { sqliteD1 } from './testD1'
import type { ViewNode } from './view'
import { onRequestGet as subtree } from '../api/subtree'
import { onRequestGet as diff } from '../api/diff'
import { onRequestGet as series } from '../api/series'
import { onRequestPost as prefixes } from '../api/prefixes'
import { onRequest as data } from '../data/[[path]]'
import { onRequestGet as agePyramid } from '../api/age-pyramid'
import { onRequestGet as largest } from '../api/largest'

const ENV = { BASE_SCOPE: 'laptop', SESSION_SECRET: 'test-secret', STORE_BUCKET: 'disk-tree', STORE_ENDPOINT: 'https://r2.example.test', STORE_ACCESS_KEY_ID: 'k', STORE_SECRET_ACCESS_KEY: 's' } as Env
const HIDE = 'Users/ryan/Downloads/2025 tax returns'
const id = (via: Identity['via'], scopes: string[]): Identity => ({ email: null, name: null, scopes, admin: false, via, subject: null })

describe('restrictionFor', () => {
  it('a grant’s `<base>:hide:` scopes on this store, decoded, normalized and deduped', () => {
    expect(restrictionFor(id('grant', ['laptop', hideScope('laptop', HIDE), `laptop:hide:/${encodeURI(HIDE)}/`, 'laptop:hide:a']), ENV)).toEqual({ hide: [HIDE, 'a'] })
  })
  it('a scope holds no space or comma (auth splits stored scopes on both)', () => {
    expect([hideScope('laptop', HIDE), hideScope('nas', '/x, y/100%/')]).toEqual([
      'laptop:hide:Users/ryan/Downloads/2025%20tax%20returns',
      'nas:hide:x%2C%20y/100%25',
    ])
  })
  it('none for an unrestricted grant, an email session, or another store’s tag', () => {
    expect([
      restrictionFor(id('grant', ['laptop']), ENV),
      restrictionFor(id('session', ['laptop', hideScope('laptop', HIDE)]), ENV),
      restrictionFor(id('grant', ['laptop', 'nas:hide:mnt/user/backups']), ENV),
      restrictionFor(null, ENV),
    ]).toEqual([null, null, null, null])
  })
  it('a secondary store’s tag is its key', () => {
    const nas = storeEnv({ ...ENV, STORES_JSON: '{"nas": {}}' } as Env, 'nas', {})
    expect(restrictionFor(id('grant', ['laptop', hideScope('laptop', HIDE), 'nas:hide:mnt/user/backups']), nas)).toEqual({ hide: ['mnt/user/backups'] })
  })
})

describe('isHidden', () => {
  const r = { hide: [HIDE] }
  it('strictly below a hidden dir only: the dir itself, siblings and name-prefix lookalikes stay visible', () => {
    expect([
      `${HIDE}/W2.pdf`, `${HIDE}/a/b`, HIDE, `${HIDE} (1)`, 'Users/ryan/Downloads', '',
    ].map(p => isHidden(p, r))).toEqual([true, true, false, false, false, false])
  })
})

const leaf = (n: string, b: number): ViewNode => ({ n, k: 'file', b, o: 1 })
const TREE: ViewNode = {
  n: 'Downloads', k: 'dir', b: 160, o: 5, f: 2, c: [
    { n: '2025 tax returns', k: 'dir', b: 100, o: 3, f: 3, nc: 3, nd: 0, c: [leaf('1040.pdf', 60), leaf('W2.pdf', 30), leaf('1099.pdf', 10)] },
    leaf('big.zip', 50),
    leaf('notes.txt', 10),
  ],
}

describe('restrictTree / restrictSubtree / restrictDiff', () => {
  const r = { hide: [HIDE] }
  it('a hidden dir keeps its sizes and loses its children and child counts', () => {
    expect(restrictTree(TREE, 'Users/ryan/Downloads', r)).toEqual({
      n: 'Downloads', k: 'dir', b: 160, o: 5, f: 2, c: [
        { n: '2025 tax returns', k: 'dir', b: 100, o: 3 },
        leaf('big.zip', 50),
        leaf('notes.txt', 10),
      ],
    })
  })
  it('a tree rooted above: the hidden dir is a leaf wherever it sits', () => {
    const ryan: ViewNode = { n: 'ryan', k: 'dir', b: 160, o: 5, c: [TREE] }
    expect(restrictTree(ryan, 'Users/ryan', r).c![0].c![0]).toEqual({ n: '2025 tax returns', k: 'dir', b: 100, o: 3 })
  })
  it('search fields drop hidden paths', () => {
    const body = {
      path: 'Users/ryan/Downloads', tree: TREE, q: 'pdf',
      matches: [`${HIDE}/1040.pdf`, 'Users/ryan/Downloads/notes.txt'],
      matched: [{ path: `${HIDE}/1040.pdf`, b: 60, o: 1 }, { path: 'Users/ryan/Downloads/notes.txt', b: 10, o: 1 }],
      excluded: [`${HIDE}/W2.pdf`],
    }
    const out = restrictSubtree(body, r)
    expect([out.matches, out.matched, out.excluded]).toEqual([
      ['Users/ryan/Downloads/notes.txt'],
      [{ path: 'Users/ryan/Downloads/notes.txt', b: 10, o: 1 }],
      [],
    ])
  })
  it('search totals that count hidden hits (`matchCount`) are dropped; the rest of the body is kept as is', () => {
    const body = { path: 'Users/ryan/Downloads', tree: TREE, q: 'pdf', matches: [], matched: [], matchCount: { n: 3, b: 70, o: 3 }, firstPaint: true }
    expect(restrictSubtree(body, r)).toEqual({ path: 'Users/ryan/Downloads', tree: restrictTree(TREE, 'Users/ryan/Downloads', r), q: 'pdf', matches: [], matched: [], firstPaint: true })
  })
  it('diff rows are relative to the diff’s path', () => {
    const rows = [
      { p: '2025 tax returns', s: 'added' },
      { p: '2025 tax returns/1040.pdf', s: 'added' },
      { p: 'big.zip', s: 'changed' },
    ]
    expect(restrictDiff({ path: 'Users/ryan/Downloads', rows }, r).rows).toEqual([rows[0], rows[2]])
  })
  it('filterJson re-emits an OK body through the filter, keeps status/headers, passes errors through', async () => {
    const ok = new Response(JSON.stringify({ path: 'Users/ryan/Downloads', tree: TREE }), { headers: { 'content-type': 'application/json', 'cache-control': 'private, max-age=300' } })
    const out = await filterJson(ok, (b: Parameters<typeof restrictSubtree>[0]) => restrictSubtree(b, r))
    expect([out.status, out.headers.get('cache-control'), ((await out.json()) as { tree: ViewNode }).tree.c![0]]).toEqual([200, 'private, max-age=300', { n: '2025 tax returns', k: 'dir', b: 100, o: 3 }])
    const bad = new Response('nope', { status: 503 })
    expect(await filterJson(bad, () => { throw new Error('not called') })).toBe(bad)
  })
})

describe('blockedForRestricted', () => {
  it('blocks the unfilterable name-search, bench, cover, health and scan-run routes, by path segment', () => {
    expect([
      '/api/name-summary', '/api/name-summary-registry', '/api/static-bench', '/api/filter-cover', '/api/health', '/api/scan-runs', '/api/scan-runs/x',
      '/api/name-summary-extra', '/api/filter-covering', '/api/healthcheck', '/api/scan-runs-extra',
      '/api/largest', '/api/filter-scans', '/api/filter-caps', '/api/me', '/api/whoami', '/api/session-log',
    ].map(blockedForRestricted)).toEqual([
      true, true, true, true, true, true, true,
      false, false, false, false,
      false, false, false, false, false, false,
    ])
  })
  it('raw-bytes and D1-list routes, by path segment', () => {
    expect([
      '/api/path-index', '/v1/files/listing/x', '/v1', '/api/plans/run', '/api/db/actions', '/api/og/mint',
      '/api/subtree', '/api/diff', '/api/series', '/data/laptop/scans.json', '/data/laptop/scan-times.json', '/api/ownersx', '/v10',
    ].map(blockedForRestricted)).toEqual([true, true, true, true, true, true, false, false, false, false, false, false, false])
  })
})

describe('D1', () => {
  /** A D1 with the auth tables and a live share link carrying `scopes`; the env
   *  authenticates its key as a Bearer credential, as a link holder's would. */
  async function linkEnv(scopes: string[]): Promise<{ env: Env; key: string }> {
    const { db } = await sqliteD1()
    const env = { ...ENV, DB: db } as Env
    const { token } = await gateFor(env)!.mint({ email: null, scopes, name: 'test link', createdBy: 'ryan@example.test', expiresAt: null, maxRedeems: null })
    return { env, key: token }
  }
  const bearer = (url: string, key: string, init?: RequestInit) =>
    new Request(`https://disk.example.test${url}`, { ...init, headers: { ...init?.headers, authorization: `Bearer ${key}` } })

  describe('routes, as a restricted link', () => {
    const scopes = ['laptop', hideScope('laptop', HIDE)]
    it('a hidden path is absent from subtree, diff, series and largest (before any store read)', async () => {
      const { env, key } = await linkEnv(scopes)
      const p = encodeURIComponent(`${HIDE}/W2.pdf`)
      const got = await Promise.all([
        subtree({ request: bearer(`/api/subtree?date=2026-10-09&path=${p}`, key), env } as never),
        diff({ request: bearer(`/api/diff?from=2026-10-08&to=2026-10-09&path=${p}`, key), env } as never),
        series({ request: bearer(`/api/series?paths=Users/ryan/Documents&paths=${p}`, key), env } as never),
        largest({ request: bearer(`/api/largest?date=2026-10-09&path=${p}`, key), env } as never),
      ].map(async r => { const res = await r; return [res.status, await res.text()] }))
      expect(got).toEqual([[404, 'path not found'], [404, 'path not found'], [404, 'path not found'], [404, 'path not found']])
    })
    it('prefixes (caller-supplied probes) and the unfilterable routes are 403', async () => {
      const { env, key } = await linkEnv(scopes)
      const body = '{"error":"forbidden for a path-restricted link"}\n'
      const pre = await prefixes({ request: bearer('/api/prefixes', key, { method: 'POST', body: '{}' }), env } as never)
      const mw = await Promise.all(['/api/path-index?date=x', '/v1/files/listing/', '/api/plans/run?id=1'].map(u => restrictGate(bearer(u, key), env)))
      expect([[pre.status, await pre.text()], ...await Promise.all(mw.map(async r => [r!.status, await r!.text()]))]).toEqual([
        [403, body], [403, body], [403, body], [403, body],
      ])
    })
    it('age-pyramid: a hidden dir itself is absent too (its pyramid breaks down its contents)', async () => {
      const { env, key } = await linkEnv(scopes)
      const res = await agePyramid({ request: bearer(`/api/age-pyramid?date=2026-10-09&path=${encodeURIComponent(HIDE)}`, key), env } as never)
      expect([res.status, await res.text()]).toEqual([404, 'path not found'])
    })
    it('the middleware refuses every newly blocked route to a restricted link, and to it alone', async () => {
      const restricted = await linkEnv(scopes)
      const open = await linkEnv(['laptop'])
      const urls = ['/api/name-summary?q=x', '/api/name-summary-registry', '/api/static-bench?q=x', '/api/filter-cover?q=x', '/api/health', '/api/scan-runs/r1']
      const got = await Promise.all(urls.map(async u => {
        const r = await restrictGate(bearer(u, restricted.key), restricted.env)
        return [r!.status, await r!.text()]
      }))
      expect(got).toEqual(urls.map(() => [403, '{"error":"forbidden for a path-restricted link"}\n']))
      expect(await Promise.all(urls.map(u => restrictGate(bearer(u, open.key), open.env)))).toEqual(urls.map(() => null))
    })
    it('`/data`: a restricted link is refused lifecycle.json and other snapshot files, before any store read', async () => {
      const { env, key } = await linkEnv(scopes)
      const get = async (rel: string) => {
        const r = await data({ request: bearer(`/data/${rel}`, key), env } as never)
        return [r.status, await r.text()]
      }
      const forbidden = [403, '{"error":"forbidden for a path-restricted link"}\n']
      expect([await get('2026-10-09/lifecycle.json'), await get('2026-10-09/listing.parquet')]).toEqual([forbidden, forbidden])
    })
    it('`dataAllowed`: meta always; age only while no hide prefix is top-level; nothing else', () => {
      const deep = { hide: [HIDE] }
      const top = { hide: ['Users', HIDE] }
      const keys = ['snapshots/2026-10-09/meta.json', 'snapshots/2026-10-09/age.json', 'snapshots/2026-10-09/lifecycle.json', 'snapshots/2026-10-09/x/meta.json', 'snapshots/nas/2026-10-09/meta.json']
      expect([keys.map(k => dataAllowed(k, 'snapshots/', deep)), keys.map(k => dataAllowed(k, 'snapshots/', top))]).toEqual([
        [true, true, false, false, false],
        [true, false, false, false, false],
      ])
      expect(dataAllowed('snapshots/nas/2026-10-09/meta.json', 'snapshots/nas/', deep)).toBe(true)
    })
    it('an unrestricted link, and an unblocked route, pass the gate', async () => {
      const open = await linkEnv(['laptop'])
      const restricted = await linkEnv(scopes)
      expect([
        await restrictGate(bearer('/api/path-index?date=x', open.key), open.env),
        await restrictGate(bearer('/api/subtree?date=x', restricted.key), restricted.env),
      ]).toEqual([null, null])
    })
    it('a restricted link never earns a full OG card; an unrestricted one does', async () => {
      const restricted = await linkEnv(scopes)
      const open = await linkEnv(['laptop'])
      const now = Math.floor(Date.now() / 1000)
      expect([
        await shareKeyGrant(restricted.env.DB, restricted.key, ['laptop'], now),
        typeof await shareKeyGrant(open.env.DB, open.key, ['laptop'], now),
      ]).toEqual([null, 'string'])
    })
  })
})
