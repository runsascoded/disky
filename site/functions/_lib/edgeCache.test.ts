import { beforeEach, describe, expect, it } from 'vitest'
import { CACHE_V, cacheEnvTag, cacheKeyFor, cacheMatch, cacheStore, cutReason, keepFor, PARTIAL_TTL, resetUpgrades, upgradeLog, upgradePartial } from './edgeCache'

// `cacheStore` keeps an answer in the colo cache and KV; a budget-cut answer (a filter view whose phase 2 its
// time budget cut short) in the colo cache only, for `PARTIAL_TTL` seconds; `false`: served, not kept.
describe('cacheStore', () => {
  const setup = () => {
    const colo: string[] = []
    const kv: string[] = []
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async (k: Request) => { colo.push(k.url) } } }
    const env = { CACHE_KV: { put: async (k: string) => { kv.push(k) } } as unknown as KVNamespace }
    return { colo, kv, env }
  }
  const key = cacheKeyFor('subtree', '', 'd/p?q=x')
  // The awaited put's duration varies: `awaited;dur=<ms>`.
  const seen = async (r: Response) => [r.headers.get('x-cache'), r.headers.get('x-cache-store')?.replace(/dur=\d+/, 'dur=<ms>'), r.headers.get('server-timing'), await r.text()]

  it('stores in both tiers by default', async () => {
    const { colo, kv, env } = setup()
    const r = await cacheStore(env, key, '{"a":1}', { 'server-timing': 'total;dur=5' })
    expect([await seen(r), colo, kv.length]).toEqual([['miss', 'awaited;dur=<ms>', 'total;dur=5', '{"a":1}'], [key.url], 1])
  })

  it('`store` false: served, in neither tier', async () => {
    const { colo, kv, env } = setup()
    const r = await cacheStore(env, key, '{"a":1}', { 'server-timing': 'total;dur=5' }, undefined, false)
    expect([await seen(r), colo, kv]).toEqual([['miss', 'skipped', 'total;dur=5', '{"a":1}'], [], []])
  })

  it('a budget-cut answer (`keepFor(interiors.late)`): colo only, `PARTIAL_TTL` s, flagged on the miss and on the hit', async () => {
    const stored: [string, string | null, string | null, string][] = []
    const held = new Map<string, Response>()
    ;(globalThis as unknown as { caches: unknown }).caches = { default: {
      match: async (k: Request) => held.get(k.url)?.clone(),
      put: async (k: Request, r: Response) => { stored.push([k.url, r.headers.get('cache-control'), r.headers.get('x-cache-partial'), await r.clone().text()]); held.set(k.url, r) },
    } }
    const kv: string[] = []
    const env = { CACHE_KV: { put: async (k: string) => { kv.push(k) }, get: async () => null } as unknown as KVNamespace }
    const keep = keepFor({ interiors: { late: 3 } })
    const miss = await cacheStore(env, key, '{"budgetCut":true}', {}, undefined, keep)
    const hit = (await cacheMatch(env, key))!
    const flags = async (r: Response) => [r.headers.get('x-cache'), r.headers.get('x-cache-store')?.replace(/dur=\d+/, 'dur=<ms>') ?? null, r.headers.get('x-cache-partial'), r.headers.get('cache-control'), await r.text()]
    expect([keep, PARTIAL_TTL, await flags(miss), await flags(hit), stored, kv]).toEqual([
      { partialTtl: 120 }, 120,
      ['miss', 'partial;ttl=120;awaited;dur=<ms>', 'ttl=120', 'no-store', '{"budgetCut":true}'],
      ['hit', null, 'ttl=120', 'no-store', '{"budgetCut":true}'],
      [[key.url, 'public, s-maxage=120', 'ttl=120', '{"budgetCut":true}']],
      [],
    ])
  })

  it('`keepFor`: whole answers (no interiors, or only read-budget skips) are kept in both tiers; a time cut briefly', () => {
    expect([keepFor({}), keepFor({ interiors: {} }), keepFor({ interiors: { late: 0 } }), keepFor({ interiors: { late: 1 } }), keepFor({ searchLate: true }), keepFor({ detailsLate: true }), keepFor({ lookups_late: true })]).toEqual([true, true, true, { partialTtl: PARTIAL_TTL }, { partialTtl: PARTIAL_TTL }, { partialTtl: PARTIAL_TTL }, { partialTtl: PARTIAL_TTL }])
  })
})

// `upgradePartial`: after a partial answer is served, the full one computed in the background replaces it.
describe('upgradePartial', () => {
  const key = cacheKeyFor('subtree', '', 'd/p?q=heavy')
  const setup = () => {
    const held = new Map<string, Response>()
    ;(globalThis as unknown as { caches: unknown }).caches = { default: {
      match: async (k: Request) => held.get(k.url)?.clone(),
      put: async (k: Request, r: Response) => { held.set(k.url, r) },
    } }
    const kv: string[] = []
    const env = { CACHE_KV: { put: async (_k: string, v: string) => { kv.push(v) }, get: async () => null } as unknown as KVNamespace }
    const bg: Promise<unknown>[] = []
    const waitUntil = (p: Promise<unknown>) => { bg.push(p) }
    /** The colo entry: its cache-control, partial flag and body. */
    const entry = async () => {
      const r = held.get(key.url)
      return r ? [r.headers.get('cache-control'), r.headers.get('x-cache-partial'), await r.clone().text()] : null
    }
    return { env, kv, bg, waitUntil, entry }
  }
  const PARTIAL = '{"budgetCut":true}', FULL = '{"tree":"all"}'
  beforeEach(() => resetUpgrades())

  it('the full answer replaces the partial in the colo cache at the normal TTL (and goes to KV)', async () => {
    const { env, kv, bg, waitUntil, entry } = setup()
    await cacheStore(env, key, PARTIAL, {}, undefined, keepFor({ interiors: { late: 2 } }))
    const before = await entry()
    const runs: number[] = []
    const got = upgradePartial(env, key, async () => { runs.push(1); return { body: FULL, keep: true } }, waitUntil)
    await Promise.all(bg)
    const hit = (await cacheMatch(env, key))!
    expect([before, got, runs.length, await entry(), kv, hit.headers.get('x-cache-partial'), await hit.text()]).toEqual([
      ['public, s-maxage=120', 'ttl=120', PARTIAL],
      'scheduled', 1,
      ['public, s-maxage=86400', null, FULL],
      [FULL],
      null, FULL,
    ])
  })

  it('dedupe: concurrent partial hits while a run is in flight schedule nothing more; after it, a new one may', async () => {
    const { env, bg, waitUntil } = setup()
    let release!: () => void
    const gate = new Promise<void>(r => { release = r })
    let runs = 0
    const compute = async () => { runs++; await gate; return { body: FULL, keep: true as const } }
    const first = [upgradePartial(env, key, compute, waitUntil), upgradePartial(env, key, compute, waitUntil), upgradePartial(env, key, compute, waitUntil)]
    const other = upgradePartial(env, cacheKeyFor('subtree', '', 'd/p?q=other'), async () => ({ body: FULL, keep: true }), waitUntil)
    release()
    await Promise.all(bg)
    const after = upgradePartial(env, key, compute, waitUntil)
    await Promise.all(bg)
    expect([first, other, after, runs]).toEqual([['scheduled', 'in-flight', 'in-flight'], 'scheduled', 'scheduled', 2])
  })

  it('a background run cut short too: the partial stays, and no rerun until it would have expired', async () => {
    const { env, bg, waitUntil, entry } = setup()
    await cacheStore(env, key, PARTIAL, {}, undefined, keepFor({ interiors: { late: 2 } }))
    let runs = 0
    const cut = async () => { runs++; return { body: '{"budgetCut":true,"more":1}', keep: keepFor({ interiors: { late: 1 } }) } }
    const t0 = 1_000_000
    const got = upgradePartial(env, key, cut, waitUntil, t0)
    await Promise.all(bg)
    const again = [upgradePartial(env, key, cut, waitUntil, t0 + 1000), upgradePartial(env, key, cut, waitUntil, t0 + PARTIAL_TTL * 1000 - 1)]
    const expired = upgradePartial(env, key, cut, waitUntil, t0 + PARTIAL_TTL * 1000)
    await Promise.all(bg)
    expect([got, again, expired, runs, await entry()]).toEqual([
      'scheduled', ['gave-up', 'gave-up'], 'scheduled', 2, ['public, s-maxage=120', 'ttl=120', PARTIAL],
    ])
  })

  it('every run logs its start and exactly one end: done, cut (which budgets) or failed (the message), with its decoded groups', async () => {
    const { env, bg, waitUntil } = setup()
    const lines: string[] = []
    const prev = upgradeLog.line
    upgradeLog.line = s => { lines.push(s) }
    try {
      const k2 = cacheKeyFor('subtree', '', 'd/p?q=cut'), k3 = cacheKeyFor('subtree', '', 'd/p?q=boom')
      upgradePartial(env, key, async trace => { trace('group', 3); trace('fetch', 9); trace('group', 4); return { body: FULL, keep: true } }, waitUntil)
      upgradePartial(env, k2, async trace => { trace('group', 1); return { body: PARTIAL, keep: keepFor({ interiors: { late: 1 }, searchLate: true }), cut: cutReason({ interiors: { late: 1 }, searchLate: true }) } }, waitUntil)
      upgradePartial(env, k3, async trace => { trace('group', 1); throw new Error('Memory limit would be exceeded') }, waitUntil)
      // Skipped (in flight): no line.
      upgradePartial(env, key, async () => ({ body: FULL, keep: true }), waitUntil)
      await Promise.all(bg)
    } finally { upgradeLog.line = prev }
    // The ends settle in whatever order their awaits do: sorted.
    expect(lines.map(l => l.replace(/ \d+ (\d+)$/, ' <ms> $1')).sort()).toEqual([
      `upgrade: subtree.cache/v${CACHE_V}/d/p?q=boom failed Memory_limit_would_be_exceeded <ms> 1`,
      `upgrade: subtree.cache/v${CACHE_V}/d/p?q=boom started - <ms> 0`,
      `upgrade: subtree.cache/v${CACHE_V}/d/p?q=cut cut phase2+search <ms> 1`,
      `upgrade: subtree.cache/v${CACHE_V}/d/p?q=cut started - <ms> 0`,
      `upgrade: subtree.cache/v${CACHE_V}/d/p?q=heavy done - <ms> 2`,
      `upgrade: subtree.cache/v${CACHE_V}/d/p?q=heavy started - <ms> 0`,
    ])
  })

  it('`cutReason`: each budget that cut an answer, or null for a whole one', () => {
    expect([
      cutReason({}), cutReason({ interiors: { late: 0 } }), cutReason({ interiors: { late: 2 } }), cutReason({ detailsLate: true }),
      cutReason({ lookups_late: true, searchLate: true }), cutReason({ interiors: { late: 1 }, searchLate: true, detailsLate: true, lookups_late: true }),
    ]).toEqual([null, null, 'phase2', 'details', 'search+lookups', 'phase2+search+details+lookups'])
  })

  it('a background run that throws: the partial stays, no unhandled rejection, treated as cut short', async () => {
    const { env, bg, waitUntil, entry } = setup()
    await cacheStore(env, key, PARTIAL, {}, undefined, keepFor({ interiors: { late: 2 } }))
    const got = upgradePartial(env, key, async () => { throw new Error('D1_ERROR') }, waitUntil, 0)
    await Promise.all(bg)
    expect([got, upgradePartial(env, key, async () => ({ body: FULL, keep: true }), waitUntil, 1), await entry()]).toEqual([
      'scheduled', 'gave-up', ['public, s-maxage=120', 'ttl=120', PARTIAL],
    ])
  })

  it('without `waitUntil` nothing is scheduled', () => {
    const { env } = setup()
    let runs = 0
    expect([upgradePartial(env, key, async () => { runs++; return { body: FULL, keep: true } }, undefined), runs]).toEqual(['no-waituntil', 0])
  })
})

// A Pages preview (the dev stack) binds its prod's `CACHE_KV` and shares its zone's colo cache: its keys carry
// a `~<env>/` tag, prod's are exactly what they were (no flush).
describe('the deployment-environment tag', () => {
  const PROD = { PROD_HOST: 'gcs.oa.dev' }
  const req = (host: string) => new Request(`https://${host}/api/subtree?date=d&path=p`)
  const sha = async (s: string) => [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s)))].map(b => b.toString(16).padStart(2, '0')).join('')

  it('`cacheEnvTag`: prod\'s host is `\'\'`; any other host (the dev host, a `*.pages.dev` alias, a preview env without `PROD_HOST`) is itself; `CACHE_NS` wins', () => {
    expect([
      cacheEnvTag(PROD, req('gcs.oa.dev')),
      cacheEnvTag(PROD, req('dev.gcs.oa.dev')),
      cacheEnvTag(PROD, req('oa-gcs-usage.pages.dev')),
      cacheEnvTag({}, req('dev.gcs.oa.dev')),
      cacheEnvTag({}, req('gcs.oa.dev')),
      cacheEnvTag({ CACHE_NS: '' }, req('r2.rbw.sh')),
      cacheEnvTag({ ...PROD, CACHE_NS: 'staging' }, req('gcs.oa.dev')),
    ]).toEqual(['', 'dev.gcs.oa.dev', 'oa-gcs-usage.pages.dev', 'dev.gcs.oa.dev', 'gcs.oa.dev', '', 'staging'])
  })

  it('prod\'s keys (colo URL and KV hash) are unchanged; a preview\'s get a `~<tag>/` segment, before a store\'s', () => {
    const prod = cacheKeyFor('subtree', cacheEnvTag(PROD, req('gcs.oa.dev')), 'd/p?w=1')
    const dev = cacheKeyFor('subtree', cacheEnvTag({}, req('dev.gcs.oa.dev')), 'd/p?w=1')
    expect([
      prod.url,
      dev.url,
      cacheKeyFor('subtree', 'dev.gcs.oa.dev', 'd/p?w=1', 'meta').url,
      cacheKeyFor('subtree', 'a/b', 'd/p?w=1').url,
    ]).toEqual([
      `https://subtree.cache/v${CACHE_V}/d/p?w=1`,
      `https://subtree.cache/v${CACHE_V}/~dev.gcs.oa.dev/d/p?w=1`,
      `https://subtree.cache/v${CACHE_V}/~dev.gcs.oa.dev/@meta/d/p?w=1`,
      `https://subtree.cache/v${CACHE_V}/~a%2Fb/d/p?w=1`,
    ])
  })

  // A PR preview (`pr-N.<project>.pages.dev`) runs in the dev stack's environment, `CACHE_NS` included, but its own code.
  const HF_PREVIEW = { PROD_HOST: 'hf.oa.dev', CACHE_NS: 'dev.hf.oa.dev', PAGES_SUBDOMAIN: 'oa-hf-usage.pages.dev' }
  it('`cacheEnvTag`: a preview host (one label under `PAGES_SUBDOMAIN`) is itself, before `CACHE_NS`; dev\'s custom host keeps it', () => {
    expect([
      'dev.hf.oa.dev', 'pr-12.oa-hf-usage.pages.dev', 'pr-13.oa-hf-usage.pages.dev', 'dev.oa-hf-usage.pages.dev',
      'oa-hf-usage.pages.dev', 'a.b.oa-hf-usage.pages.dev', 'pr-12.oa-hf-usage.pages.dev.evil.com',
    ].map(h => cacheEnvTag(HF_PREVIEW, req(h)))).toEqual([
      'dev.hf.oa.dev', 'pr-12.oa-hf-usage.pages.dev', 'pr-13.oa-hf-usage.pages.dev', 'dev.oa-hf-usage.pages.dev',
      'dev.hf.oa.dev', 'dev.hf.oa.dev', 'dev.hf.oa.dev',
    ])
    expect(cacheEnvTag({ CACHE_NS: 'dev.hf.oa.dev' }, req('pr-12.oa-hf-usage.pages.dev'))).toEqual('dev.hf.oa.dev')
  })

  it('a preview host never reads or writes dev\'s keys, nor another preview\'s (colo and KV)', async () => {
    const colo = new Map<string, Response>()
    const kv = new Map<string, string>()
    ;(globalThis as unknown as { caches: unknown }).caches = { default: {
      match: async (k: Request) => colo.get(k.url)?.clone(),
      put: async (k: Request, r: Response) => { colo.set(k.url, r) },
    } }
    const env = { ...HF_PREVIEW, CACHE_KV: { get: async (k: string) => kv.get(k) ?? null, put: async (k: string, v: string) => { kv.set(k, v) } } as unknown as KVNamespace }
    const key = (host: string) => cacheKeyFor('subtree', cacheEnvTag(env, req(host)), 'd/p?w=1')
    await cacheStore(env, key('dev.hf.oa.dev'), '{"dev":1}')
    const previewMiss = await cacheMatch(env, key('pr-12.oa-hf-usage.pages.dev'))
    await cacheStore(env, key('pr-12.oa-hf-usage.pages.dev'), '{"pr":12}')
    colo.clear()
    const otherMiss = await cacheMatch(env, key('pr-13.oa-hf-usage.pages.dev'))
    const devHit = await cacheMatch(env, key('dev.hf.oa.dev'))
    const prHit = await cacheMatch(env, key('pr-12.oa-hf-usage.pages.dev'))
    expect([previewMiss, otherMiss, await devHit!.text(), await prHit!.text(), [...kv.keys()].sort()]).toEqual([
      null, null, '{"dev":1}', '{"pr":12}',
      [await sha(`https://subtree.cache/v${CACHE_V}/~dev.hf.oa.dev/d/p?w=1`), await sha(`https://subtree.cache/v${CACHE_V}/~pr-12.oa-hf-usage.pages.dev/d/p?w=1`)].sort(),
    ])
  })

  it('an answer stored on the dev host is a miss on prod (colo and KV), and vice versa', async () => {
    const colo = new Map<string, Response>()
    const kv = new Map<string, string>()
    ;(globalThis as unknown as { caches: unknown }).caches = { default: {
      match: async (k: Request) => colo.get(k.url)?.clone(),
      put: async (k: Request, r: Response) => { colo.set(k.url, r) },
    } }
    const env = { ...PROD, CACHE_KV: { get: async (k: string) => kv.get(k) ?? null, put: async (k: string, v: string) => { kv.set(k, v) } } as unknown as KVNamespace }
    const key = (host: string) => cacheKeyFor('diff', cacheEnvTag(env, req(host)), 'a/b/p?w=1')
    await cacheStore(env, key('dev.gcs.oa.dev'), '{"dev":1}')
    const prodMiss = await cacheMatch(env, key('gcs.oa.dev'))
    colo.clear()
    const prodKvMiss = await cacheMatch(env, key('gcs.oa.dev'))
    await cacheStore(env, key('gcs.oa.dev'), '{"prod":1}')
    colo.clear()
    const devHit = await cacheMatch(env, key('dev.gcs.oa.dev'))
    const prodHit = await cacheMatch(env, key('gcs.oa.dev'))
    expect([prodMiss, prodKvMiss, await devHit!.text(), await prodHit!.text(), [...kv.keys()].sort()]).toEqual([
      null, null, '{"dev":1}', '{"prod":1}',
      [await sha(`https://diff.cache/v${CACHE_V}/~dev.gcs.oa.dev/a/b/p?w=1`), await sha(`https://diff.cache/v${CACHE_V}/a/b/p?w=1`)].sort(),
    ])
  })
})
