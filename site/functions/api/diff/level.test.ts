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
  // `x-deny`: the gate refuses (a session it can't find), after its latency (and `x-gate-ms` more).
  return {
    ...m,
    presentsCredentials: (ctx: Parameters<typeof m.presentsCredentials>[0]) => knobs.credentials && m.presentsCredentials(ctx),
    requireViewer: slowed('auth', async (ctx: Parameters<typeof m.requireViewer>[0]) => {
      await sleep(Number(ctx.request.headers.get('x-gate-ms') ?? 0))
      return ctx.request.headers.has('x-deny') ? m.json({ error: 'unauthenticated' }, 401) : m.requireViewer(ctx)
    }),
  }
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

/** Each `INDEX_R2` read waits this long first (a slow store: a request's lookups stay in flight). */
const r2Lag = { ms: 0 }
/** `INDEX_R2` over a fixture dir (`diffLevel.test.ts`'s, without the read log); `gets`: its `get`s counted. */
function r2(dir: string, gets = { n: 0 }): R2Bucket {
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
    gets.n++
    timeline.push(`r2 ${key}`)
    await sleep(r2Lag.ms)
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
  env = { DB: db, SESSION_SECRET: SECRET, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g', INDEX_R2: r2('iv-churn'), GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'b' } as Env
  await cold()
  calls.length = 0
  settled.length = 0
  timeline.length = 0
  Object.assign(lag, { auth: 0, scan: 0, gens: 0, match: 0, prep: 0 })
  r2Lag.ms = 0
  knobs.credentials = true
})

const { signSession } = await import('@open-athena/auth')
/** The gate's secret (`env.SESSION_SECRET`). */
const SECRET = 'test-secret'
/** A session cookie for `sub`, signed under `secret`, minted `ago` ms before now with a `ttl` (s). */
const session = async (sub: string, { secret = SECRET, ago = 0, ttl }: { secret?: string; ago?: number; ttl?: number } = {}) =>
  `oa_auth=${await signSession(sub, secret, Date.now() - ago, ttl)}`
/** A session cookie that verifies (signed under the gate's secret, unexpired) for an email the allowlist doesn't hold
 *  (a user since removed): its lookups start beside the gate, which then refuses it. */
const REFUSED_COOKIE = { cookie: await session('e:removed@example.test') }

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
  it('a refused session (it verifies; its user is no longer allowed) leaves the isolate, the colo and KV exactly as they were, every lookup having finished first', async () => {
    // A warm isolate: one authorized request's memos, colo and KV entries.
    expect((await askAt('localhost', '2026-08-02', '2026-08-04', '')).status).toBe(200)
    await quiesce()
    const before = state()
    expect(Object.keys(before.isolate).length).toBeGreaterThan(0)
    calls.length = 0
    settled.length = 0
    lag.auth = 300
    const r = await askAt('example.test', '2026-08-04', '2026-08-06', 'b1', REFUSED_COOKIE)
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

describe('which requests start their lookups beside the gate (`presentsCredentials`)', () => {
  /** What a refused request at a non-local host started: `auth` alone, or every lookup too. */
  const started = async (headers: Record<string, string>, qs = '') => {
    calls.length = 0
    lag.auth = 20
    const r = await level({ request: new Request(`http://example.test/api/diff/level?from=2026-08-03&to=2026-08-05&p=${qs}`, { headers: { 'x-deny': '1', ...headers } }), env })
    await r.text()
    return [r.status, [...new Set(calls)].sort().join(',')]
  }
  const NONE = [401, 'auth']
  const ALL = [401, 'auth,gens,match,prep,scan']
  const TOKEN = 'A'.repeat(31) + '_'

  it('none for anything but a session cookie that verifies: other cookies, a malformed, forged or expired one, a token', async () => {
    const [forged, expired, unshaped] = await Promise.all([
      session('e:removed@example.test', { secret: 'not-the-secret' }),
      session('e:removed@example.test', { ago: 2 * 3600_000, ttl: 3600 }),
      // A forged signature in the cookie's shape, the claims' own.
      session('e:removed@example.test').then(c => `${c.slice(0, c.indexOf('.'))}.c2lnbmF0dXJlLW5vdC1yZWFs`),
    ])
    expect(await Promise.all([
      started({}),
      started({ cookie: '__cf_bm=abc.def; _ga=GA1.2.345.678' }),
      started({ cookie: 'oa_oidc=nonce.signature; oa_google_hint=x' }),
      started({ cookie: 'oa_auth=junk' }),
      started({ cookie: 'oa_auth=a.b.c' }),
      started({ cookie: forged }),
      started({ cookie: expired }),
      started({ cookie: unshaped }),
      started({ authorization: 'Basic dXNlcjpwYXNz' }),
      started({ authorization: 'Bearer short' }),
      started({ authorization: `Bearer ${TOKEN}` }),
      started({}, `&key=${TOKEN}`),
      // The gate reads a presented token first: it is the credential, whatever cookie comes with it.
      started({ authorization: `Bearer ${TOKEN}`, ...REFUSED_COOKIE }),
      started(REFUSED_COOKIE, `&key=${TOKEN}`),
    ])).toEqual(Array(14).fill(NONE))
  })

  it('every lookup for a session cookie that verifies, alone or among other cookies', async () => {
    expect(await Promise.all([
      started(REFUSED_COOKIE),
      started({ cookie: `_ga=GA1.2.3; ${REFUSED_COOKIE.cookie}; __cf_bm=x` }),
    ])).toEqual(Array(2).fill(ALL))
  })
})

describe('a refused request\'s lookups are its own: an authorized request racing it never joins them', () => {
  it('the authorized request reads as it would alone, with no join, unaffected by the refused one\'s kill', async () => {
    const [from, to] = ['2026-08-02', '2026-08-06']
    /** An authorized request (its gate passing at once), against an `INDEX_R2` of its own, counted. */
    const authed = async () => {
      const gets = { n: 0 }
      const r = await level({ request: new Request(`http://localhost/api/diff/level?from=${from}&to=${to}&p=`), env: { ...env, INDEX_R2: r2('iv-churn', gets) } })
      return { status: r.status, body: await r.text(), gets: gets.n, joins: Object.keys(timing(r)).filter(k => k.startsWith('join')) }
    }
    r2Lag.ms = 10
    const alone = await authed()
    await quiesce()
    await cold()
    calls.length = 0
    // A refused session, its gate refusing at 60 ms: its lookups (the same keys) are in flight from the start, and
    // killed (`done()`) at the refusal, while the authorized request (started 5 ms in) is still reading.
    const junkGets = { n: 0 }
    const junk = level({ request: new Request(`http://example.test/api/diff/level?from=${from}&to=${to}&p=`, { headers: { ...REFUSED_COOKIE, 'x-deny': '1', 'x-gate-ms': '60' } }), env: { ...env, INDEX_R2: r2('iv-churn', junkGets) } })
    await sleep(5)
    const raced = await authed()
    const refused = await junk
    expect([refused.status, raced.status, raced.body === alone.body, raced.gets, raced.joins, junkGets.n > 0])
      .toEqual([401, 200, true, alone.gets, [], true])
    // Each request ran each lookup once: none restarted.
    expect(['auth', 'gens', 'match', 'prep', 'scan'].map(c => [c, calls.filter(x => x === c).length]))
      .toEqual([['auth', 2], ['gens', 2], ['match', 2], ['prep', 2], ['scan', 4]])
  })
})
