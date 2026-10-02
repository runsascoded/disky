import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { parseQuery } from './scope'
import { sqliteD1 } from './testD1'
import { type D1Variant, FILES, GETS, seedGeneration } from './testStore'
import { buildView } from './view'

// An opt-in latency model of a filtered view over a real store generation
// (specs/path-store-search.md §3.2): `SEARCH_BENCH_DIR` names a local copy of
// one generation dir (the `path` + `bysize` sorts with their `.groups.json`,
// `d1.json` as `index_footer.extract` writes it, and the search sidecars).
// Every store GET and colo-cache call takes one of 6 connection slots (a
// Worker's simultaneous-connection limit) for `LAT_MS` + bytes at `MBPS`, so
// the wall time tracks the request's round trips; the CPU time is this
// process's own. Skipped without the env var (CI never has the data).

declare const process: { env: Record<string, string | undefined>; cpuUsage(prev?: { user: number; system: number }): { user: number; system: number } }
const DIR = process.env.SEARCH_BENCH_DIR
const LAT_MS = Number(process.env.SEARCH_BENCH_LAT_MS ?? 60)
const CACHE_MS = Number(process.env.SEARCH_BENCH_CACHE_MS ?? 5)
const MBPS = Number(process.env.SEARCH_BENCH_MBPS ?? 50)
const SLOTS = 6

let free = SLOTS
const waiting: (() => void)[] = []
async function slot<T>(ms: number, f: () => Promise<T>): Promise<T> {
  if (free > 0) free--
  else await new Promise<void>(r => waiting.push(r))
  try {
    await new Promise(r => setTimeout(r, ms))
    return await f()
  } finally {
    const next = waiting.shift()
    if (next) next()
    else free++
  }
}

vi.mock('@rdub/file-tree/stores/s3', async () => {
  const { FILES, GETS } = await import('./testStore')
  const { readFileSync } = (await import(/* @vite-ignore */ 'node:' + 'fs')) as { readFileSync(p: string, enc?: string): Uint8Array & string }
  const loaded = new Map<string, Uint8Array>()
  return {
    S3Store: () => ({
      get: (key: string, range?: { offset: number; length: number }) => slot(LAT_MS + (range ? range.length : 0) / (MBPS * 1e3), async () => {
        const path = FILES.get(key)
        if (!path) throw Object.assign(new Error(`no such key: ${key}`), { name: 'NotFoundError' })
        GETS.push({ key, ...range })
        let all = loaded.get(path)
        if (!all) loaded.set(path, (all = new Uint8Array(readFileSync(path))))
        return { bytes: range ? all.slice(range.offset, range.offset + range.length) : all, totalSize: all.byteLength }
      }),
    }),
  }
})

const DATE = '2026-10-02'
const env = { ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' } as Env

describe.skipIf(!DIR)('search bench (latency model)', () => {
  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: () => slot(CACHE_MS, async () => undefined), put: () => slot(CACHE_MS, async () => {}) } }
    const { readFileSync } = (await import(/* @vite-ignore */ 'node:' + 'fs')) as { readFileSync(p: string, enc?: string): Uint8Array & string }
    const { db, raw } = await sqliteD1('cw')
    const v = JSON.parse(readFileSync(`${DIR}/d1.json`, 'utf8')) as Record<string, D1Variant>
    const files = { path: { parquet: `${DIR}/path-index.parquet`, groups: `${DIR}/path-index.groups.json` }, bysize: { parquet: `${DIR}/path-index-bysize.parquet`, groups: `${DIR}/path-index-bysize.groups.json` } }
    seedGeneration(raw, { date: DATE, gen: 'g', dir: 'listing/b/index/g', variants: v, files })
    // `seedGeneration` resolves under `fixtures/`; these are absolute.
    for (const [k, f] of FILES) if (k.startsWith('listing/b/')) FILES.set(k, f.replace(/^.*\/fixtures\//, ''))
    for (const role of ['names', 'trigrams', 'search', 'rows', 'rows-search']) {
      const f = `${DIR}/path-index.${role}.parquet`
      try { readFileSync(f); FILES.set(`listing/b/index/g/path-index.${role}.parquet`, f) } catch { /* absent: an older layout */ }
    }
    env.DB = db
  })

  for (const q of (process.env.SEARCH_BENCH_Q ?? '2019').split(',')) {
    it(`q=${q}`, async () => {
      const timing = new Map<string, number>()
      const descs = new Map<string, string>()
      GETS.length = 0
      const cpu0 = process.cpuUsage()
      const t0 = performance.now()
      const v = await buildView(env, {
        date: DATE, path: '', w: 1408, h: 896, minArea: 19098, atten: 2, query: parseQuery(q)!,
        trace: (k, ms, d) => { timing.set(k, (timing.get(k) ?? 0) + ms); if (d) descs.set(k, d) },
      })
      const wall = performance.now() - t0
      const cpu = process.cpuUsage(cpu0)
      const byKey = new Map<string, { n: number; bytes: number }>()
      for (const g of GETS) {
        const k = g.key.replace(/^.*\//, '')
        const e = byKey.get(k) ?? { n: 0, bytes: 0 }
        e.n++
        e.bytes += g.length ?? 0
        byKey.set(k, e)
      }
      console.log(JSON.stringify({
        q, matches: v.matches?.length, partial: v.partialReason ?? null, wall: Math.round(wall), cpu: Math.round((cpu.user + cpu.system) / 1000),
        timing: Object.fromEntries([...timing].map(([k, ms]) => [k, Math.round(ms)])), descs: Object.fromEntries(descs),
        gets: Object.fromEntries([...byKey].map(([k, e]) => [k, `${e.n} reads, ${(e.bytes / 1048576).toFixed(1)} MiB`])),
      }, null, 1))
      expect(v.matches?.length).toBeGreaterThan(0)
    }, 600_000)
  }
})
