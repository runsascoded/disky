import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from '../../_lib/auth'
import { sqliteD1 } from '../../_lib/testD1'
import { fixture } from '../../_lib/testStore'

// `/api/diff/level`'s fixed phase (`shared.ahead`): the gate runs beside the lookups that don't depend on who asks
// (the scans' checks, the cache key's generations, the cache lookups, the first round's handles and plan). Each is
// wrapped here with a latency and a call log; the answers are the real ones, over `fixtures/iv-churn/`.

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../../_lib/testStore')).S3Store }))

const lag = { auth: 0, scan: 0, gens: 0, match: 0, prep: 0 }
const calls: string[] = []
/** The wrapped lookups that settled, in order. */
const settled: string[] = []
/** The wrapped lookups' settles and `INDEX_R2`'s reads (`r2 <key>`), in order. */
const timeline: string[] = []
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))
const slowed = <A extends unknown[], R>(name: keyof typeof lag, f: (...a: A) => Promise<R>) => async (...a: A): Promise<R> => {
  calls.push(name)
  await sleep(lag[name])
  try {
    return await f(...a)
  } finally {
    settled.push(name)
    timeline.push(name)
  }
}

/** `credentials: false`: the request presents none, so `ahead` starts nothing before the gate (a test's baseline). */
const knobs = vi.hoisted(() => ({ credentials: true }))

vi.mock('../../_lib/auth.js', async orig => {
  const m = await orig<typeof import('../../_lib/auth.js')>()
  // `x-deny`: the gate refuses (a session it can't find), after its latency.
  return { ...m, presentsCredentials: (ctx: Parameters<typeof m.presentsCredentials>[0]) => knobs.credentials && m.presentsCredentials(ctx), requireViewer: slowed('auth', async (ctx: Parameters<typeof m.requireViewer>[0]) => (ctx.request.headers.has('x-deny') ? m.json({ error: 'unauthenticated' }, 401) : m.requireViewer(ctx))) }
})
vi.mock('../../_lib/scanArg.js', async orig => {
  const m = await orig<typeof import('../../_lib/scanArg.js')>()
  return { ...m, scanArg: slowed('scan', m.scanArg) }
})
vi.mock('../../_lib/index.js', async orig => {
  const m = await orig<typeof import('../../_lib/index.js')>()
  return { ...m, pathGens: slowed('gens', m.pathGens) }
})
vi.mock('../../_lib/edgeCache.js', async orig => {
  const m = await orig<typeof import('../../_lib/edgeCache.js')>()
  return { ...m, cacheMatch: slowed('match', m.cacheMatch) }
})
vi.mock('../../_lib/diffLevel.js', async orig => {
  const m = await orig<typeof import('../../_lib/diffLevel.js')>()
  return { ...m, prepareLevels: slowed('prep', m.prepareLevels) }
})

const { onRequestGet: level } = await import('./level')
const { buildLevels } = await import('../../_lib/diffLevel')
const { clearIsolate, isolateSnapshot, loadScopes } = await import('../../_lib/shared')
const { coloPutsSettled } = await import('../../_lib/index')
await loadScopes()

/** `INDEX_R2` over a fixture dir (`diffLevel.test.ts`'s, without the read log). */
function r2(dir: string): R2Bucket {
  type Fs = { existsSync(p: string): boolean; readFileSync(p: string): Uint8Array; readdirSync(p: string, o: { recursive: true }): string[]; statSync(p: string): { isFile(): boolean } }
  const nodeFs = async () => {
    const mod = 'node:fs'
    return (await import(/* @vite-ignore */ mod)) as Fs
  }
  const path = (key: string) => fixture(`${dir}/${key}`)
  const list = async ({ prefix }: { prefix: string }) => {
    const fs = await nodeFs()
    const d = prefix.slice(0, prefix.lastIndexOf('/') + 1)
    const root = path(d)
    const keys = fs.existsSync(root) ? fs.readdirSync(root, { recursive: true }).map(f => `${d}${f}`).filter(k => k.startsWith(prefix) && fs.statSync(path(k)).isFile()).sort() : []
    return { objects: keys.map(key => ({ key })), truncated: false }
  }
  const get = async (key: string, o?: { range?: { offset: number; length: number } }) => {
    timeline.push(`r2 ${key}`)
    const fs = await nodeFs()
    if (!fs.existsSync(path(key))) return null
    const buf = fs.readFileSync(path(key))
    const { offset = 0, length = buf.byteLength - offset } = o?.range ?? {}
    const part = buf.slice(offset, offset + length)
    return {
      size: buf.byteLength,
      arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength),
      json: async () => JSON.parse(new TextDecoder().decode(part)),
    }
  }
  const head = async (key: string) => ((await nodeFs()).existsSync(path(key)) ? { key } : null)
  return { get, list, head } as unknown as R2Bucket
}

let env: Env
/** The colo cache's and KV's entries (key -> bytes / text), fresh per test. */
let held: Map<string, ArrayBuffer>
let kv: Map<string, string>
/** Fresh colo and KV, and an empty isolate: every request below starts cold. */
async function cold(): Promise<void> {
  const h = held = new Map<string, ArrayBuffer>()
  ;(globalThis as unknown as { caches: unknown }).caches = {
    default: {
      match: async (r: Request) => (h.has(r.url) ? new Response(h.get(r.url)!.slice(0)) : undefined),
      put: async (r: Request, res: Response) => { h.set(r.url, await res.arrayBuffer()) },
    },
  }
  const k = kv = new Map<string, string>()
  env = { ...env, CACHE_KV: { get: async (key: string) => k.get(key) ?? null, put: async (key: string, v: string) => { k.set(key, v) } } } as unknown as Env
  clearIsolate()
}
beforeEach(async () => {
  const { db } = await sqliteD1()
  env = { DB: db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g', INDEX_R2: r2('iv-churn'), GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'b' } as Env
  await cold()
  calls.length = 0
  settled.length = 0
  timeline.length = 0
  Object.assign(lag, { auth: 0, scan: 0, gens: 0, match: 0, prep: 0 })
  knobs.credentials = true
})

/** A level request for the root between two scans; `localhost`: the gate's dev identity (and credentials presented). */
const ask = (from: string, to: string, o: { host?: string; headers?: Record<string, string> } = {}) =>
  level({ request: new Request(`http://${o.host ?? 'localhost'}/api/diff/level?from=${from}&to=${to}&p=`, { headers: o.headers }), env })

const timing = (r: Response): Record<string, number> =>
  Object.fromEntries([...(r.headers.get('server-timing') ?? '').matchAll(/(?:^|, )([\w-]+);dur=(\d+)/g)].map(([, k, d]) => [k, Number(d)]))

describe('a level request\'s fixed phase', () => {
  it('answers as the levels read alone, cold and then from the edge, byte for byte', async () => {
    const [from, to] = ['2026-08-02', '2026-08-06']
    const built = await buildLevels(env, { from, to, dirs: [''], k: 32, m: 'churn' })
    const want = JSON.stringify({ from, to, k: 32, m: 'churn', levels: [...built.values()] })
    const cold = await ask(from, to)
    const warm = await ask(from, to)
    expect([cold.status, cold.headers.get('x-cache'), await cold.text(), warm.status, warm.headers.get('x-cache'), await warm.text()])
      .toEqual([200, 'miss', want, 200, 'hit', want])
  })

  it('a refused gate answers its 401 and nothing else, though its lookups had started', async () => {
    lag.auth = 50
    const r = await ask('2026-08-03', '2026-08-05', { headers: { 'x-deny': '1' } })
    expect([r.status, await r.text(), [...new Set(calls)].sort()])
      .toEqual([401, '{"error":"unauthenticated"}\n', ['auth', 'gens', 'match', 'prep', 'scan']])
    // The next request (the dropped lookups' entries evicted, not left for it to join) answers whole.
    const ok = await ask('2026-08-03', '2026-08-05')
    expect([ok.status, ok.headers.get('x-cache')]).toEqual([200, 'miss'])
  })

  it('a request presenting no credentials starts nothing before its gate', async () => {
    lag.auth = 20
    const r = await ask('2026-08-03', '2026-08-05', { host: 'example.test', headers: { 'x-deny': '1' } })
    expect([r.status, calls]).toEqual([401, ['auth']])
  })

  it('the fixed phase takes about the longest chain of its lookups, not their sum', async () => {
    // Sequential, the gate, the scans' checks, the generations, the cache lookup and the handles took 5 × 100 ms
    // before the first read; overlapped, the longest chain is the generations then the lookup keyed by them (200 ms).
    Object.assign(lag, { auth: 100, scan: 100, gens: 100, match: 100, prep: 100 })
    const r = await ask('2026-08-04', '2026-08-06')
    const t = timing(r)
    expect(r.status).toBe(200)
    expect(t.fixed).toBeGreaterThanOrEqual(195)
    expect(t.fixed).toBeLessThan(330)
  })

  it('a generation with the level sort opens it beside the gate, and not the path sort', async () => {
    // `interval-store-level/`: the same store, every tier carrying the level sort.
    env = { ...env, INTERVAL_STORE_PREFIX: 'interval-store-level' } as Env
    const [from, to] = ['2026-08-02', '2026-08-06']
    const built = await buildLevels(env, { from, to, dirs: [''], k: 32, m: 'churn' })
    const want = JSON.stringify({ from, to, k: 32, m: 'churn', levels: [...built.values()] })
    await cold()
    timeline.length = 0
    calls.length = 0
    lag.auth = 200
    const r = await ask(from, to)
    // Before the gate settled: the store's state and the level sort's footers of the three tiers the range touches;
    // no `path` sort handle or plan (`prepareLevels` never runs).
    const early = timeline.slice(0, timeline.indexOf('auth')).filter(e => e.startsWith('r2 ')).map(e => e.slice('r2 interval-store-level/g/'.length))
    expect([r.status, await r.text(), timing(r)['level-index'], [...new Set(calls)].sort(), early]).toEqual([
      200, want, 1, ['auth', 'gens', 'match', 'scan'],
      ['scans.json', 'manifests/2026-08-06.m001.json', 'served/level.groups.parquet', 'deltas/2026-08-04_2026-08-05/served/level.groups.parquet', 'deltas/2026-08-06/served/level.groups.parquet'],
    ])
  })

  it('a path-restricted identity asking a hidden dir is 404, though an unrestricted request left its level cached', async () => {
    const { gateFor } = await import('../../_lib/auth')
    const { hideScope } = await import('../../_lib/restrict')
    // One cache namespace for both hosts: the unrestricted request's entry is the one the restricted request would hit.
    env = { ...env, BASE_SCOPE: 'laptop', SESSION_SECRET: 'test-secret', CACHE_NS: '' } as Env
    const { token } = await gateFor(env)!.mint({ email: null, scopes: ['laptop', hideScope('laptop', 'b1')], name: 'restricted link', createdBy: 'ryan@example.test', expiresAt: null, maxRedeems: null })
    const at = (host: string, headers?: Record<string, string>) =>
      level({ request: new Request(`http://${host}/api/diff/level?from=2026-08-02&to=2026-08-06&p=b1`, { headers }), env })
    const primed = await at('localhost')
    const again = await at('localhost')
    const hidden = await at('example.test', { authorization: `Bearer ${token}` })
    expect([primed.status, primed.headers.get('x-cache'), again.headers.get('x-cache'), hidden.status, await hidden.text()])
      .toEqual([200, 'miss', 'hit', 404, 'path not found'])
  })
})

describe('a bench\'s cache-key nonce (`nc=`)', () => {
  const at = (host: string, nc: string | null, headers?: Record<string, string>) =>
    level({ request: new Request(`http://${host}/api/diff/level?from=2026-08-02&to=2026-08-06&p=b1${nc == null ? '' : `&nc=${nc}`}`, { headers }), env })
  const cached = async (r: Response) => [r.status, r.headers.get('x-cache')]
  beforeEach(async () => {
    // What earlier tests left landing (their deferred puts), then cold again.
    await quiesce()
    await cold()
    // One cache namespace for both hosts: localhost's admin and a viewer's token share keys but for the nonce.
    env = { ...env, BASE_SCOPE: 'laptop', SESSION_SECRET: 'test-secret', CACHE_NS: '' } as Env
  })

  it('an admin\'s nonce is its own cache key: a miss per new nonce, a hit on its repeat', async () => {
    // `localhost`: the dev identity, an admin.
    const got = []
    for (const nc of [null, 'a', 'a', 'b', null]) got.push(await cached(await at('localhost', nc)))
    expect(got).toEqual([[200, 'miss'], [200, 'miss'], [200, 'hit'], [200, 'miss'], [200, 'hit']])
  })

  it('anyone else\'s nonce is ignored: the same key, and answer, as without it', async () => {
    const { gateFor } = await import('../../_lib/auth')
    const { token } = await gateFor(env)!.mint({ email: null, scopes: ['laptop'], name: 'viewer link', createdBy: 'ryan@example.test', expiresAt: null, maxRedeems: null })
    const viewer = { authorization: `Bearer ${token}` }
    const primed = await at('localhost', null)
    const want = await primed.text()
    const rs = [await at('example.test', 'x1', viewer), await at('example.test', 'x2', viewer)]
    expect([primed.headers.get('x-cache'), ...await Promise.all(rs.map(async r => [...await cached(r), await r.text() === want]))])
      .toEqual(['miss', [200, 'hit', true], [200, 'hit', true]])
    // The admin's nonce was never written by them: still a miss.
    expect(await cached(await at('localhost', 'x1'))).toEqual([200, 'miss'])
  })
})

/** Everything a request can leave behind: the isolate's memos (`isolateSnapshot`: in order, objects by identity), the
 *  colo cache and KV (keys in order, bytes). */
const state = () => ({
  isolate: isolateSnapshot(),
  colo: [...held].map(([k, b]) => [k, new Uint8Array(b).join(',')]),
  kv: [...kv],
})
/** `state()`'s keys alone, sorted (two runs' timestamps and object identities differ). */
const keys = () => {
  const { isolate, colo, kv } = state()
  return {
    isolate: Object.fromEntries(Object.entries(isolate).map(([n, es]) => [n, es.map(([k]) => String(k)).sort()]).sort()),
    colo: colo.map(([k]) => k).sort(),
    kv: kv.map(([k]) => k).sort(),
  }
}
/** A level request of `p` between two scans, at `host`. */
const askAt = (host: string, from: string, to: string, p: string, headers?: Record<string, string>) =>
  level({ request: new Request(`http://${host}/api/diff/level?from=${from}&to=${to}&p=${p}`, { headers }), env })
/** Let every put and continuation a request started land. */
const quiesce = async () => {
  await coloPutsSettled()
  await sleep(50)
  await coloPutsSettled()
}

describe('what a level request starts beside its gate (`ahead`) lands only once the gate passes', () => {
  it('a refused junk credential leaves the isolate, the colo and KV exactly as they were, every lookup having finished first', async () => {
    // A warm isolate: one authorized request's memos, colo and KV entries.
    expect((await askAt('localhost', '2026-08-02', '2026-08-04', '')).status).toBe(200)
    await quiesce()
    const before = state()
    expect(Object.keys(before.isolate).length).toBeGreaterThan(0)
    calls.length = 0
    settled.length = 0
    lag.auth = 300
    const r = await askAt('example.test', '2026-08-04', '2026-08-06', 'b1', { cookie: 'oa_auth=junk' })
    // Every lookup ran, and settled, before the gate refused (its own `auth` last).
    expect([r.status, [...new Set(calls)].sort(), settled.at(-1), [...new Set(settled)].sort()])
      .toEqual([401, ['auth', 'gens', 'match', 'prep', 'scan'], 'auth', ['auth', 'gens', 'match', 'prep', 'scan']])
    await quiesce()
    expect(state()).toEqual(before)
  })

  it('a passed gate commits them: what the request leaves is what the same lookups started after the gate leave', async () => {
    const run = async (eager: boolean) => {
      await cold()
      knobs.credentials = true
      expect((await askAt('localhost', '2026-08-02', '2026-08-04', '')).status).toBe(200)
      await quiesce()
      const warm = keys()
      knobs.credentials = eager
      settled.length = 0
      lag.auth = 100
      const r = await askAt('localhost', '2026-08-04', '2026-08-06', 'b1')
      const body = await r.text()
      await quiesce()
      return { status: r.status, body, warm, after: keys(), early: settled[0] !== 'auth' }
    }
    const strict = await run(true)
    const after = await run(false)
    // Started beside the gate, and not after it.
    expect([strict.early, after.early]).toEqual([true, false])
    expect([strict.status, strict.body]).toEqual([200, after.body])
    expect(strict.after).toEqual(after.after)
    expect(strict.after).not.toEqual(strict.warm)
  })
})
