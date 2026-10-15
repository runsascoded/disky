import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { type D1Variant, FILES, fixture, readJson, seedGeneration } from '../_lib/testStore'
import { SEARCH_FILES, searchKey } from '../_lib/search'
import { overCapReason } from '../_lib/cover'
import { onRequestGet as filterCover } from './filter-cover'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// The `v2-search` fixture's `ttl` matches: `bk/fill/zz-TTL-b` (an object), `bk/fill/zz-ttl-a` (a folder of one
// object), `bk/iris/TTL-misc`, `bk/tmp/ttl=14d`, `bk/tmp/ttl=7d`, and `zz/Checkpoints/ttl` — the one object
// under `zz/Checkpoints`, which therefore collapses to it. `bk/tmp` also holds `scratch` (no match): it stays
// per root; so do `bk/fill` and `bk/iris`.

describe('D1', () => {
  const A = '2026-10-01T0001'
  let base: Env

  beforeAll(async () => {
    ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
    const { db, raw } = await sqliteD1()
    const dir = 'v2-search'
    const v = await readJson<Record<string, D1Variant>>(`${dir}/d1.json`)
    const files = { path: { parquet: `${dir}/path-index.parquet`, groups: `${dir}/path-index.groups.json` }, bysize: { parquet: `${dir}/path-index-bysize.parquet`, groups: `${dir}/path-index-bysize.groups.json` } }
    seedGeneration(raw, { date: A, gen: 'g', dir: `cw-l2/${A}/index/g`, variants: v, files })
    // The search sidecars: every match listed, exact.
    for (const role of ['rows', 'trigrams', 'rowsSearch'] as const) FILES.set(searchKey(`cw-l2/${A}/index/g`, role), fixture(`v2-search/${SEARCH_FILES[role]}`))
    base = { DB: db, GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', SEARCH_SIDECARS: '1', STORE_BUCKET: 'my-data', DEV_EMAIL: 'dev@example.test' } as Env
  })

  const cover = async (qs: string) => {
    const r = await filterCover({ request: new Request(`http://localhost/api/filter-cover?${qs}`), env: base })
    return [r.status, r.headers.get('content-type')?.startsWith('application/json') ? await r.json() : await r.text()]
  }

  describe('/api/filter-cover', () => {
    it('the fleet root: folders whose every object matches collapse, the rest stay one per match root', async () => {
      expect(await cover(`date=${A}&path=&q=ttl`)).toEqual([200, {
        // Looked up: `bk/fill`, `bk/iris`, `bk/tmp`, `zz/Checkpoints` (depth ≥ 2; never a whole bucket).
        date: A, path: '', q: 'ttl', complete: true, looked: 4, unchecked: 0,
        roots: { n: 6, b: 7000 + 5000 + 500 + 5242880 + 2097152 + 10, o: 8 },
        items: [
          { path: 'bk/fill/zz-TTL-b', kind: 'file', b: 7000, o: 1, roots: 1 },
          { path: 'bk/fill/zz-ttl-a', kind: 'dir', b: 5000, o: 1, roots: 1 },
          { path: 'bk/iris/TTL-misc', kind: 'dir', b: 500, o: 2, roots: 1 },
          { path: 'bk/tmp/ttl=14d', kind: 'dir', b: 5242880, o: 2, roots: 1 },
          { path: 'bk/tmp/ttl=7d', kind: 'dir', b: 2097152, o: 1, roots: 1 },
          { path: 'zz/Checkpoints', kind: 'dir', b: 10, o: 1, roots: 1 },
        ],
      }])
    })
    it('a drilled view: only its matches, and never a folder above it', async () => {
      expect(await cover(`date=${A}&path=zz/Checkpoints&q=ttl`)).toEqual([200, {
        date: A, path: 'zz/Checkpoints', q: 'ttl', complete: true, looked: 1, unchecked: 0,
        roots: { n: 1, b: 10, o: 1 },
        items: [{ path: 'zz/Checkpoints', kind: 'dir', b: 10, o: 1, roots: 1 }],
      }])
      expect(await cover(`date=${A}&path=bk/tmp&q=ttl`)).toEqual([200, {
        date: A, path: 'bk/tmp', q: 'ttl', complete: true, looked: 1, unchecked: 0,
        roots: { n: 2, b: 5242880 + 2097152, o: 3 },
        items: [
          { path: 'bk/tmp/ttl=14d', kind: 'dir', b: 5242880, o: 2, roots: 1 },
          { path: 'bk/tmp/ttl=7d', kind: 'dir', b: 2097152, o: 1, roots: 1 },
        ],
      }])
    })
    it('refuses what isn\'t a set of exact prefixes, and a scan the store doesn\'t have', async () => {
      expect(await cover(`date=${A}&path=&q=ttl -misc`)).toEqual([400, { error: 'A filter with exclusions can’t be turned into whole folders or files to act on; drop the exclusion to act on the matches.', code: 'cover-exclusions' }])
      expect(await cover(`date=${A}&path=`)).toEqual([400, { error: 'q= (a filter) is required' }])
      expect(await cover(`date=2026-10-02&path=&q=ttl`)).toEqual([404, { error: 'no scan matches date=2026-10-02' }])
    })

    // Over the cap by its lower bound (`coverFloor`): the over-cap answer, without the cover's walk. `ttl`'s six
    // roots sit in `bk/fill` (2), `bk/iris` (1), `bk/tmp` (2) and `zz/Checkpoints` (1, which collapses).
    const ROOTS = { n: 6, b: 7000 + 5000 + 500 + 5242880 + 2097152 + 10, o: 8 }
    /** The answer, and the phases its `Server-Timing` names (`floor`: the bound's lookups; `cover`: the walk). */
    const phased = async (qs: string, env: { COVER_ITEMS_MAX?: string } = {}) => {
      const r = await filterCover({ request: new Request(`http://localhost/api/filter-cover?${qs}`), env: { ...base, ...env } as Env })
      const phases = (r.headers.get('server-timing') ?? '').split(', ').map(p => p.split(';')[0])
      return [r.status, await r.json(), phases.filter(p => p === 'floor' || p === 'cover')]
    }
    it('over the cap before any lookup: four floor folders > 3 — no lookup (`looked: 0`), no walk', async () => {
      expect(await phased(`date=${A}&path=&q=ttl`, { COVER_ITEMS_MAX: '3' })).toEqual([200, {
        date: A, path: '', q: 'ttl', items: [], roots: ROOTS, complete: false,
        reason: overCapReason(4, true), looked: 0, unchecked: 0,
      }, ['floor']])
    })
    it('over the cap after one lookup of the four floor folders: exactly six items > 5, no walk', async () => {
      expect(await phased(`date=${A}&path=&q=ttl`, { COVER_ITEMS_MAX: '5' })).toEqual([200, {
        date: A, path: '', q: 'ttl', items: [], roots: ROOTS, complete: false,
        reason: overCapReason(6), looked: 4, unchecked: 0,
      }, ['floor']])
    })
    it('at the cap by roots: no bound, the cover as usual', async () => {
      const [status, body, phases] = await phased(`date=${A}&path=&q=ttl`, { COVER_ITEMS_MAX: '6' })
      expect([status, (body as { complete: boolean }).complete, (body as { items: unknown[] }).items.length, phases]).toEqual([200, true, 6, ['cover']])
    })
  })
})
