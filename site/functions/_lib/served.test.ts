/**
 * The served seam (`index.ts` `servedStore`): with `SERVED_R2`, a page's scan list, header JSONs,
 * registry, same-day scan times, series' unindexed points and the extras sidecars read `INDEX_R2` and
 * D1 only — no request leaves for the store (`STORE_*`, GCS by default). Unset, the same answers come
 * through the store seam. The store is the real `S3Store` over a stubbed `fetch`, so every request it
 * makes is seen by host and key; `INDEX_R2` is an in-memory binding that logs its reads.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { LINEAGES, sqliteD1 } from './testD1'

describe.each(LINEAGES)('D1, %s lineage', lineage => {
  const A = '2026-10-01'
  const B = '2026-10-01T1200' // shares A's day: A's start is its pointer's (`index_schema.started`, D1)
  const OLD = '2026-08-01' // a snapshot dir D1 never indexed: series fills it from its meta
  const dirOf = (d: string) => `listing/${d}/index/g`
  const GCS = 'storage.googleapis.com'

  const enc = (s: string) => new TextEncoder().encode(s)
  /** Node-only, imported by a variable name so the Workers-typed `tsc -p functions` never resolves it. */
  const NODE_FS = 'node:fs'

  /** The provenance sidecar for `dir`: one block (every key's offset is 0). */
  function attrFiles(lines: string[]): Record<string, Uint8Array> {
    const body = lines.map(l => `${l}\n`).join('')
    const idx = { v: 1, n: lines.length, size: enc(body).byteLength, keys: [lines[0].split('\t')[0]], offsets: [0] }
    return { 'attr.tsv': enc(body), 'attr.tsv.idx.json': enc(JSON.stringify(idx)) }
  }

  /** The small served files, by key: what the job publishes. */
  function smallFiles(): Map<string, Uint8Array> {
    const meta = (d: string, extra: Record<string, unknown> = {}) => enc(JSON.stringify({ published: `${d.slice(0, 10)}T09:00:00Z`, total_bytes: 1000, total_objects: 10, users: [{ u: 'alice' }], ...extra }))
    const files = new Map<string, Uint8Array>([
      [`snapshots/${A}/meta.json`, meta(A, { started: `${A}T07:00:00Z` })],
      [`snapshots/${A}/lifecycle.json`, enc('{"bk":[]}')],
      [`snapshots/${B}/meta.json`, meta(B)],
      [`snapshots/${OLD}/meta.json`, meta(OLD, { total_bytes: 777, total_objects: 7 })],
      ['snapshots/rules.json', enc(JSON.stringify({ users: [{ u: 'alice', aliases: ['al'] }] }))],
    ])
    for (const [name, bytes] of Object.entries(attrFiles(['bk\talice\trule\tr1', 'bk/nest/a/z\talice\tmanual\t']))) files.set(`${dirOf(A)}/${name}`, bytes)
    return files
  }

  /** `INDEX_R2` over `files`: `get` (ranged) and `list` (delimited), each logged by key. */
  function r2Of(files: Map<string, Uint8Array>, log: string[]) {
    const uploaded = new Date('2026-10-01T10:00:00Z')
    return {
      async get(key: string, o?: { range?: { offset: number; length: number } }) {
        log.push(`get ${key}`)
        const all = files.get(key)
        if (!all) return null
        const bytes = o?.range ? all.slice(o.range.offset, o.range.offset + o.range.length) : all
        return { size: all.byteLength, uploaded, httpMetadata: {}, body: new Response(bytes).body!, arrayBuffer: async () => bytes.slice().buffer }
      },
      async list(o: { prefix?: string; delimiter?: string }) {
        log.push(`list ${o.prefix}`)
        const p = o.prefix ?? ''
        const dirs = new Set<string>()
        const objects: { key: string; size: number; uploaded: Date }[] = []
        for (const [k, v] of files) {
          if (!k.startsWith(p)) continue
          const cut = k.indexOf('/', p.length)
          if (cut >= 0) dirs.add(k.slice(0, cut + 1))
          else objects.push({ key: k, size: v.byteLength, uploaded })
        }
        return { objects, delimitedPrefixes: [...dirs].sort(), truncated: false }
      },
    }
  }

  /** The store's side of `fetch`: path-style GETs (ranged) and ListObjectsV2 over `files` and the
   * parquet fixtures (`testStore` `FILES`), every request logged as `<host> <op> <key>`. */
  function fetchOf(files: Map<string, Uint8Array>, fixtures: Map<string, string>, log: string[]) {
    return async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const req = new Request(input as RequestInfo, init)
      const u = new URL(req.url)
      const [, , ...rest] = u.pathname.split('/') // /<bucket>/<key…>
      const key = decodeURIComponent(rest.join('/'))
      if (u.searchParams.get('list-type') === '2') {
        const p = u.searchParams.get('prefix') ?? ''
        log.push(`${u.host} list ${p}`)
        const dirs = new Set<string>()
        for (const k of files.keys()) {
          if (!k.startsWith(p)) continue
          const cut = k.indexOf('/', p.length)
          if (cut >= 0) dirs.add(k.slice(0, cut + 1))
        }
        const xml = `<ListBucketResult><IsTruncated>false</IsTruncated>${[...dirs].sort().map(d => `<CommonPrefixes><Prefix>${d}</Prefix></CommonPrefixes>`).join('')}</ListBucketResult>`
        return new Response(xml, { status: 200 })
      }
      log.push(`${u.host} get ${key}`)
      let all = files.get(key)
      const local = fixtures.get(key)
      if (!all && local) all = new Uint8Array((await import(/* @vite-ignore */ NODE_FS) as { readFileSync(p: string): Uint8Array }).readFileSync(local))
      if (!all) return new Response('<Error><Code>NoSuchKey</Code></Error>', { status: 404 })
      const m = /^bytes=(\d+)-(\d+)$/.exec(req.headers.get('range') ?? '')
      if (!m) return new Response(all, { status: 200, headers: { 'Content-Length': String(all.byteLength), 'Content-Type': 'application/json' } })
      const [s, e] = [Number(m[1]), Math.min(Number(m[2]), all.byteLength - 1)]
      return new Response(all.slice(s, e + 1), { status: 206, headers: { 'Content-Range': `bytes ${s}-${e}/${all.byteLength}` } })
    }
  }

  interface World {
    env: Env
    /** Requests that left for the store (`<host> <op> <key>`). */
    store: string[]
    /** `INDEX_R2` reads (`<op> <key>`). */
    r2: string[]
    mods: {
      data: typeof import('../data/[[path]]')
      series: typeof import('../api/series')
      identity: typeof import('./identity')
      scanTimes: typeof import('./scanTimes')
      extras: typeof import('./extras')
      view: typeof import('./view')
      search: typeof import('./search')
    }
  }

  /** A fresh deployment (modules re-imported: their per-isolate memos start empty) with scans A and B
   * indexed in D1 and the small files in the store; with `served`, also in `INDEX_R2` under `SERVED_R2`. */
  async function world(served: boolean, extra: Partial<Env> = {}): Promise<World> {
    vi.resetModules()
    const { FILES, readJson, seedGeneration } = await import('./testStore')
    const { db, raw } = await sqliteD1(lineage)
    const fx = 'v2-lens' // owned rows (alice, bob): provenance has someone to explain
    const { path, bysize } = await readJson<Record<string, import('./testStore').D1Variant>>(`${fx}/d1.json`)
    const variants = { path, bysize }
    const files = { path: { parquet: `${fx}/path-index.parquet`, groups: `${fx}/path-index.groups.json` }, bysize: { parquet: `${fx}/path-index-bysize.parquet`, groups: `${fx}/path-index-bysize.groups.json` } }
    for (const d of [A, B]) seedGeneration(raw, { date: d, gen: 'g', dir: dirOf(d), variants, files })
    raw.exec(`UPDATE index_schema SET started = '${A}T07:00:00Z' WHERE date = '${A}' AND variant = 'path'`)
    const small = smallFiles()
    const store: string[] = []
    const r2: string[] = []
    vi.stubGlobal('fetch', fetchOf(small, FILES, store))
    const env = {
      DB: db, GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data', PUBLIC_READ: '1', BASE_SCOPE: 'gcs', ROOT_LABEL: 'root',
      ...(served ? { SERVED_R2: '1', INDEX_R2: r2Of(small, r2) as unknown as R2Bucket } : {}),
      ...extra,
    } as Env
    const mods = {
      data: await import('../data/[[path]]'),
      series: await import('../api/series'),
      identity: await import('./identity'),
      scanTimes: await import('./scanTimes'),
      extras: await import('./extras'),
      view: await import('./view'),
      search: await import('./search'),
    }
    return { env, store, r2, mods }
  }

  const get = async (w: World, rel: string): Promise<[number, unknown]> => {
    const r = await w.mods.data.onRequest({ request: new Request(`http://localhost/data/${rel}`), env: w.env })
    return [r.status, r.status === 200 ? await r.json() : await r.text()]
  }

  /** Every route this seam serves, in order; their answers. */
  async function readAll(w: World) {
    return {
      scans: await get(w, 'scans.json'),
      meta: await get(w, `${A}/meta.json`),
      lifecycle: await get(w, `${A}/lifecycle.json`),
      rules: await get(w, 'rules.json'),
      missing: await get(w, `${A}/age.json`),
      registry: await w.mods.identity.loadRegistry(w.env),
      times: await w.mods.scanTimes.scanTimes(w.env, [A, B]),
      starts: await get(w, 'scan-times.json'),
      provenance: (await w.mods.extras.extrasFor(w.env, A, 'bk'))?.provenance('bk/nest/a/z/q', 'alice'),
    }
  }

  /** The whole-bucket series: one point per scan, an unindexed one's from its meta. */
  async function series(w: World) {
    const r = await w.mods.series.onRequestGet({ request: new Request('http://localhost/api/series?path='), env: w.env })
    return [r.status, ((await r.json()) as { points: { date: string; b: number }[] }).points.map(p => [p.date, p.b])]
  }
  const SERIES = [200, [[OLD, 777], [A, 37229948], [B, 37229948]]]
  /** What a series read asks the store for: the indexed scans' per-scan `path` sorts (this fixture has
   * no interval store), never a snapshot. */
  const PATH_SORTS = [A, B].map(d => `${GCS} get ${dirOf(d)}/path-index.parquet`)

  const hosts = (log: string[]) => [...new Set(log.map(l => l.split(' ')[0]))].sort()
  const keys = (log: string[]) => [...new Set(log)].sort()

  beforeEach(() => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  })
  afterEach(() => { vi.unstubAllGlobals() })

  describe('`SERVED_R2`: the small served files from INDEX_R2', () => {
    it('every route answers from INDEX_R2 and D1; no request leaves for the store', async () => {
      const w = await world(true)
      const got = await readAll(w)
      expect(got).toEqual(EXPECTED)
      expect(hosts(w.store)).toEqual([])
      expect(keys(w.r2)).toEqual([
        `get ${dirOf(A)}/attr.tsv`,
        `get ${dirOf(A)}/attr.tsv.idx.json`,
        `get snapshots/${A}/age.json`,
        `get snapshots/${A}/lifecycle.json`,
        `get snapshots/${A}/meta.json`,
        'get snapshots/rules.json',
      ])
    })

    it('series: the unindexed scans’ LIST and metas from INDEX_R2', async () => {
      const w = await world(true)
      expect(await series(w)).toEqual(SERIES)
      expect([keys(w.store), keys(w.r2)]).toEqual([PATH_SORTS, ['get snapshots/2026-08-01/meta.json', 'list snapshots/']])
    })

    it('unset: the same answers through the store seam, and INDEX_R2 is never read', async () => {
      const w = await world(false, { INDEX_R2: r2Of(new Map(), []) as unknown as R2Bucket })
      const got = await readAll(w)
      expect(got).toEqual(EXPECTED)
      expect(hosts(w.store)).toEqual([GCS])
      expect(keys(w.store)).toEqual([
        `${GCS} get ${dirOf(A)}/attr.tsv`,
        `${GCS} get ${dirOf(A)}/attr.tsv.idx.json`,
        `${GCS} get snapshots/${A}/age.json`,
        `${GCS} get snapshots/${A}/lifecycle.json`,
        `${GCS} get snapshots/${A}/meta.json`,
        `${GCS} get snapshots/rules.json`,
      ])
      const s = await world(false, { INDEX_R2: r2Of(new Map(), []) as unknown as R2Bucket })
      expect(await series(s)).toEqual(SERIES)
      expect([keys(s.store), s.r2]).toEqual([[...PATH_SORTS, `${GCS} get snapshots/${OLD}/meta.json`, `${GCS} list snapshots/`], []])
    })

    it('series: a point that errors is named in `failed` and the response is not cached', async () => {
      const w = await world(true)
      const puts: string[] = []
      ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async (k: Request) => { puts.push(k.url) } } }
      const pass = globalThis.fetch
      vi.stubGlobal('fetch', async (req: RequestInfo, init?: RequestInit) =>
        (typeof req === 'string' ? req : req.url).includes(dirOf(B)) ? new Response('boom', { status: 403 }) : pass(req, init))
      const r = await w.mods.series.onRequestGet({ request: new Request('http://localhost/api/series?path='), env: w.env })
      const body = (await r.json()) as { points: { date: string; b: number }[]; failed?: string[] }
      expect([r.status, r.headers.get('cache-control'), body.points.map(p => [p.date, p.b]), body.failed, puts]).toEqual([
        200, 'no-store', [[OLD, 777], [A, 37229948]], [B], [],
      ])
    })

    it('`scans.json` is D1 alone: no store LIST, no INDEX_R2 read', async () => {
      for (const served of [true, false]) {
        const w = await world(served)
        expect(await get(w, 'scans.json')).toEqual([200, [B, A]])
        expect([w.store, w.r2]).toEqual([[], []])
      }
    })

    it('`scans.json` under another store’s snapshot dir is a 404', async () => {
      const w = await world(true)
      expect(await get(w, 'cw/scans.json')).toEqual([404, 'not found'])
      const r2 = await world(true, { SNAPSHOTS_SUBDIR: 'r2' })
      expect([await get(r2, 'r2/scans.json'), await get(r2, 'scans.json')]).toEqual([[200, [B, A]], [404, 'not found']])
    })

    it('`scan-times.json` is D1 alone (the pointers\' `started`), and another store\'s dir is a 404', async () => {
      for (const served of [true, false]) {
        const w = await world(served)
        expect([await get(w, 'scan-times.json'), await get(w, 'cw/scan-times.json')]).toEqual([[200, { [A]: `${A}T07:00:00Z` }], [404, 'not found']])
        expect([w.store, w.r2]).toEqual([[], []])
      }
    })

    it('a view still carries provenance, read from INDEX_R2; the store serves only the path sort', async () => {
      const w = await world(true)
      const v = await w.mods.view.buildView(w.env, { date: A, path: 'bk/nest', w: 800, h: 600, minArea: 0, atten: 1 })
      const pv: [string, unknown][] = []
      const walk = (n: { n: string; pv?: unknown; c?: unknown[] }, p: string) => {
        if (n.pv) pv.push([p, n.pv])
        for (const c of (n.c ?? []) as { n: string }[]) walk(c as never, `${p}/${c.n}`)
      }
      walk(v.tree as never, 'bk/nest')
      expect(pv).toEqual(EXPECTED_PV)
      expect(keys(w.store).map(k => k.replace(/ get .*\//, ' get '))).toEqual([`${GCS} get path-index.parquet`])
      expect(keys(w.r2)).toEqual([`get ${dirOf(A)}/attr.tsv`, `get ${dirOf(A)}/attr.tsv.idx.json`])
    })
  })

  describe('`SEARCH_SIDECARS`', () => {
    it('unset: no sidecar is probed', async () => {
      const w = await world(true)
      expect(await w.mods.search.openSearch(w.env, dirOf(A))).toBeNull()
      expect([w.store, w.r2]).toEqual([[], []])
    })
    it('set: the generation’s sidecars are opened', async () => {
      const w = await world(true, { SEARCH_SIDECARS: '1' })
      const { FILES, fixture } = await import('./testStore')
      FILES.set(`${dirOf(A)}/path-index.rows-search.parquet`, fixture('v2-search/path-index.rows-search.parquet'))
      const idx = await w.mods.search.openSearch(w.env, dirOf(A))
      expect([idx?.v, idx?.key]).toEqual([2, `${dirOf(A)}/path-index.rows-search.parquet`])
    })
  })

  const EXPECTED = {
    scans: [200, [B, A]],
    meta: [200, { published: `${A}T09:00:00Z`, started: `${A}T07:00:00Z`, total_bytes: 1000, total_objects: 10, users: [{ u: 'alice' }] }],
    lifecycle: [200, { bk: [] }],
    rules: [200, { users: [{ u: 'alice', aliases: ['al'] }] }],
    missing: [404, 'not found'],
    registry: { al: { u: 'alice', name: 'Alice' }, alice: { u: 'alice', name: 'Alice' } },
    times: { [A]: '2026-10-01T0700' },
    starts: [200, { [A]: `${A}T07:00:00Z` }],
    provenance: ['manual', null, 'bk/nest/a/z'],
  }

  const RULE = ['rule', 'r1', 'bk']
  const EXPECTED_PV: [string, unknown][] = [
    ['bk/nest', RULE],
    ['bk/nest/a', RULE],
    ['bk/nest/a/b', RULE],
    ['bk/nest/a/b/c0', RULE],
    ['bk/nest/a/b/c1', RULE],
    ['bk/nest/a/b/c2', RULE],
    ['bk/nest/a/b/c3', RULE],
    ['bk/nest/a/z', ['manual', null, 'bk/nest/a/z']],
  ]
})
