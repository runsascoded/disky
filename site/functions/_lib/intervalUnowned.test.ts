import { beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from './auth'
import { openIndex, slices } from './index'
import type { OwnerScope } from './scope'
import { LEDGER_KIND, sqliteD1 } from './testD1'
import { type D1Variant, fixture, GETS, readJson, seedGeneration } from './testStore'
import { buildDiff, buildView, readRootAgg, readRootHistory } from './view'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('./testStore')).S3Store }))

// A generation without the owner axis (specs/interval-store.md §2.1): `fixtures/iv/interval-store/g7` (`fixtures/iv/gen.py
// g7`) is three scans of the per-scan fixtures with no `usr` column (`v2`, `v2-search`, `v2`), built by the single-machine
// build, whose `scans.json` says `"slices": false` and which has no slice sorts. A sliced read there (a user lens, an
// owner pool, owner totals) has no owners: it reads the folded sorts, and every answer equals the per-scan reader's over
// the same files, with nothing read per-scan. (Its own file: the per-scan handles of `intervalSlices.test.ts`' fixtures
// are memoized per isolate by date, and these are the same dates.)

const [S1, S2, S3] = ['2026-09-30T0007', '2026-09-30T0008', '2026-09-30T0009']
const base = { w: 1280, h: 800, minArea: 12, atten: 1 }
const PATHS = ['', 'bk', 'bk/flat', 'bk/nest', 'bk/models', 'bk/tmp']
const SCOPES: { lens?: { key: string }; owner?: OwnerScope }[] = [
  { lens: { key: 'alice' } }, { lens: { key: 'bob' } }, { lens: { key: 'carol' } },
  { owner: 'owned' }, { owner: 'unowned' }, { owner: { not: ['alice'] } },
]
/** The ledger: alice and bob assigned prefixes on S2. */
const LEDGER = `
  INSERT INTO actions (id, actor, ts, scan, pattern, set_owner, owner) VALUES
    (1, 'ops', 1, '${S2}', 'gs://bk/nest/', 1, 'alice'), (2, 'ops', 2, '${S2}', 'gs://bk/tmp/', 1, 'bob');
  INSERT INTO owner_prefixes (action_id, prefix, owner, ts) VALUES (1, 'gs://bk/nest/', 'alice', 1), (2, 'gs://bk/tmp/', 'bob', 2);
`
const r2Reads: string[] = []

/** `INDEX_R2` over `fixtures/iv/`. */
function r2(): R2Bucket {
  const fs = async () => (await import(/* @vite-ignore */ 'node:fs' as string)) as {
    existsSync(p: string): boolean; readFileSync(p: string): Uint8Array; readdirSync(p: string, o: { recursive: true }): string[]; statSync(p: string): { isFile(): boolean }
  }
  const get = async (key: string, o?: { range?: { offset: number; length: number } | { suffix: number } }) => {
    const f = await fs()
    const file = fixture(`iv/${key}`)
    if (!f.existsSync(file)) return null
    r2Reads.push(key)
    const buf = f.readFileSync(file)
    const r = o?.range
    const { offset = 0, length = buf.byteLength - offset } = r && 'suffix' in r ? { offset: Math.max(0, buf.byteLength - r.suffix) } : r ?? {}
    const part = buf.slice(offset, offset + length)
    return { size: buf.byteLength, arrayBuffer: async () => part.buffer.slice(part.byteOffset, part.byteOffset + part.byteLength), json: async () => JSON.parse(new TextDecoder().decode(part)) }
  }
  const list = async ({ prefix, cursor }: { prefix: string; cursor?: string }) => {
    const f = await fs()
    const dir = prefix.slice(0, prefix.lastIndexOf('/') + 1)
    const root = fixture(`iv/${dir}`)
    const keys = f.existsSync(root) ? f.readdirSync(root, { recursive: true }).map(x => `${dir}${x}`).filter(k => k.startsWith(prefix) && f.statSync(fixture(`iv/${k}`)).isFile()).sort() : []
    return { objects: keys.map(key => ({ key })), truncated: false, cursor }
  }
  const head = async (key: string) => ((await fs()).existsSync(fixture(`iv/${key}`)) ? { key } : null)
  return { get, list, head } as unknown as R2Bucket
}

const files = (dir: string) => ({
  path: { parquet: `${dir}/path-index.parquet`, groups: `${dir}/path-index.groups.json` },
  bysize: { parquet: `${dir}/path-index-bysize.parquet`, groups: `${dir}/path-index-bysize.groups.json` },
})

/** The per-scan stores (`v2` on S1 and S3, `v2-search` on S2) and the interval store `g7` over the same scans, on one D1
 *  with the gcs ledger's tables and `ledger` (SQL) in them. */
async function seed(ledger = LEDGER): Promise<{ perScan: Env; iv: Env }> {
  const { db, raw } = await sqliteD1()
  const v2 = await readJson<Record<string, D1Variant>>('v2/d1.json')
  const search = await readJson<Record<string, D1Variant>>('v2-search/d1.json')
  for (const date of [S1, S3]) seedGeneration(raw, { date, gen: 'g', dir: `listing/${date}/index/g`, variants: v2, files: files('v2') })
  seedGeneration(raw, { date: S2, gen: 'g', dir: `listing/${S2}/index/g`, variants: search, files: files('v2-search') })
  raw.exec(`
    CREATE TABLE actions (id INTEGER PRIMARY KEY, actor TEXT NOT NULL, ts INTEGER NOT NULL, scan TEXT NOT NULL, pattern TEXT NOT NULL, set_owner INTEGER NOT NULL DEFAULT 0, owner TEXT);
    CREATE TABLE owner_prefixes (action_id INTEGER NOT NULL REFERENCES actions (id), prefix TEXT NOT NULL, owner TEXT, ts INTEGER NOT NULL, tombstoned TEXT, PRIMARY KEY (prefix, action_id));
    ${LEDGER_KIND}
    CREATE TABLE owner_totals (scan TEXT NOT NULL, head INTEGER NOT NULL, body TEXT NOT NULL, claims TEXT NOT NULL, computed_ts INTEGER, ms INTEGER, PRIMARY KEY (scan, head));
    ${ledger}
  `)
  const perScan = { DB: db, ROOT_LABEL: 'root', BASE_SCOPE: 'gcs', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's' } as Env
  return { perScan, iv: { ...perScan, PATH_STORE: 'intervals', INTERVAL_STORE_GEN: 'g7', INDEX_R2: r2() } as Env }
}

/** What a read did per-scan: the store's `listing/` keys it fetched. */
async function perScanGets<T>(f: () => Promise<T>): Promise<[T, string[]]> {
  const n = GETS.length
  const v = await f()
  return [v, [...new Set(GETS.slice(n).map(g => g.key))]]
}

const err = (e: unknown) => `${(e as Error).name}: ${(e as Error).message}`
let envs: { perScan: Env; iv: Env }

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  envs = await seed()
})

describe('interval store g7: no owner axis', () => {
  it('opens the folded sorts for a sliced read, and refuses a held date\'s v1 `user` sort', async () => {
    const got = []
    for (const variant of ['path', 'bysize', 'bysize-user']) {
      const h = await openIndex(slices(envs.iv), S1, variant)
      got.push([variant, h.mode, h.variant, h.asOf, h.gen, h.dataKey, h.columns?.includes('usr')])
    }
    expect(got).toEqual([
      ['path', 'pq', 'path', 1790726820, 'iv:g7', 'interval-store/g7/served/path.parquet', false],
      ['bysize', 'pq', 'bysize', 1790726820, 'iv:g7', 'interval-store/g7/served/bysize.parquet', false],
      ['bysize-user', 'pq', 'bysize-user', 1790726820, 'iv:g7', 'interval-store/g7/served/bysize.parquet', false],
    ])
    await expect(openIndex(slices(envs.iv), S1, 'user')).rejects.toThrow(`index variant 'user' not synced for ${S1}`)
  })

  it('serves every lens view and owner pool of every scan as the per-scan reader draws it, reading nothing per-scan', async () => {
    let drawn = 0
    for (const date of [S1, S2, S3]) {
      for (const path of PATHS) {
        for (const scope of SCOPES) {
          const o = { ...base, date, path, ...scope }
          const want = await buildView(envs.perScan, o).catch(err)
          const [got, gets] = await perScanGets(() => buildView(envs.iv, o).catch(err))
          const tree = (v: typeof want) => (typeof v === 'string' ? v : v.tree)
          expect({ o, tree: tree(got), gets }).toEqual({ o, tree: tree(want), gets: [] })
          if (typeof got !== 'string' && typeof want !== 'string') {
            // `none`: nothing in scope, drawn without a read.
            expect([got.index, want.index === 'none']).toEqual([want.index === 'none' ? 'none' : 'iv:g7', want.index === 'none'])
            drawn += got.index === 'none' ? 0 : 1
          }
        }
      }
    }
    // The rest are `none` (the ledger names alice and bob alone), or a path the scan lacks (refused on both sides).
    expect(drawn).toBe(30)
  }, 60_000)

  it('totals a root by owner as the per-scan reader does, a date at a time and as a history', async () => {
    for (const scope of SCOPES) {
      const want = []
      for (const date of [S1, S2, S3]) {
        for (const path of PATHS) {
          const o = { date, path, ...scope }
          expect({ o, agg: await readRootAgg(envs.iv, o).catch(err) }).toEqual({ o, agg: await readRootAgg(envs.perScan, o).catch(err) })
        }
        want.push([date, await readRootAgg(envs.iv, { date, path: '', ...scope })])
      }
      expect([...(await readRootHistory(envs.iv, [S1, S2, S3], scope))!]).toEqual(want)
    }
  })

  it('diffs a lens and a pool across scans as the per-scan reader does', async () => {
    for (const [from, to] of [[S1, S2], [S2, S3], [S1, S3]]) {
      for (const scope of SCOPES) {
        const o = { ...base, from, to, path: '', top: 500, ...scope }
        const shape = (d: Awaited<ReturnType<typeof buildDiff>> | string) => typeof d === 'string' ? d : { rows: d.rows, totals: [d.total_a, d.total_b, d.objects_a, d.objects_b] }
        const [got, gets] = await perScanGets(() => buildDiff(envs.iv, o).catch(err))
        const want = await buildDiff(envs.perScan, o).catch(err)
        expect({ o, got: shape(got), gets }).toEqual({ o, got: shape(want), gets: [] })
      }
    }
  })

  it('folds owner totals on every scan from its history, as the per-scan reader folds them', async () => {
    type Totals = Awaited<ReturnType<typeof import('./ownerTotals').ownerTotals>>
    const fold = ({ computed: _, ...body }: Totals) => body
    const folds = async (env: 'perScan' | 'iv'): Promise<Totals[]> => {
      // A fresh module and D1 each: `ownerTotals` memoizes per (scan, head) in the isolate, and caches in D1.
      vi.resetModules()
      const { ownerTotals } = await import('./ownerTotals')
      const e = (await seed())[env]
      const out = []
      for (const date of [S1, S2, S3]) out.push(await ownerTotals(e, date))
      return out
    }
    const [iv, per] = [await folds('iv'), await folds('perScan')]
    expect(iv.map(t => [t.scan, t.computed.hist, fold(t)])).toEqual(per.map(t => [t.scan, true, fold(t)]))
  })

  it('reads the folded sorts, never a slice sort, for owner-scoped reads', () => {
    expect([...new Set(r2Reads.filter(k => k.startsWith('interval-store/g7/served/')))].sort()).toEqual([
      'interval-store/g7/served/bysize.groups.parquet', 'interval-store/g7/served/bysize.parquet',
      'interval-store/g7/served/path.groups.parquet', 'interval-store/g7/served/path.parquet',
    ])
  })
})
