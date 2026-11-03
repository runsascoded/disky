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
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))
const slowed = <A extends unknown[], R>(name: keyof typeof lag, f: (...a: A) => Promise<R>) => async (...a: A): Promise<R> => {
  calls.push(name)
  await sleep(lag[name])
  return f(...a)
}

vi.mock('../../_lib/auth.js', async orig => {
  const m = await orig<typeof import('../../_lib/auth.js')>()
  // `x-deny`: the gate refuses (a session it can't find), after its latency.
  return { ...m, requireViewer: slowed('auth', async (ctx: Parameters<typeof m.requireViewer>[0]) => (ctx.request.headers.has('x-deny') ? m.json({ error: 'unauthenticated' }, 401) : m.requireViewer(ctx))) }
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
beforeEach(async () => {
  // A fresh colo per test: every request below starts cold at the edge.
  const held = new Map<string, ArrayBuffer>()
  ;(globalThis as unknown as { caches: unknown }).caches = {
    default: {
      match: async (r: Request) => (held.has(r.url) ? new Response(held.get(r.url)!.slice(0)) : undefined),
      put: async (r: Request, res: Response) => { held.set(r.url, await res.arrayBuffer()) },
    },
  }
  const { db } = await sqliteD1()
  env = { DB: db, ROOT_LABEL: 'all buckets', PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g', INDEX_R2: r2('iv-churn'), GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'b' } as Env
  calls.length = 0
  Object.assign(lag, { auth: 0, scan: 0, gens: 0, match: 0, prep: 0 })
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
