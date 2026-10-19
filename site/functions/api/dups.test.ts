import { beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import type { DupGroup } from '../_lib/dups'
import { sqliteD1 } from '../_lib/testD1'
import { FILES, GETS, fixture } from '../_lib/testStore'
import { onRequestGet as dups } from './dups'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// The report `fixtures/dups/gen.py` cuts (7 three-row row groups), in file order:
//   dc67fb4e7e33 moved east/run0..6/part-0.parquet  suspect, 7 copies   12 GiB reclaimable
//   b7a8285fb072 tree  {east,eu,west}/ds/web         keep west          8 GiB
//   8118b0d1d6a4 moved east/tok/{en,fr}/data         keep en            5 GiB
//   75509602fe9b moved east/p/q, west/p/q (nested), eu/q                4 GiB
//   a45cfb33347a tree  {east,west}/p                                    3 GiB
//   50c65943d14b tree  {east,west}/m/model.bin       objects            1.5 GiB
const G = 2 ** 30
const SCAN = '2026-10-18T0701'
const OLD = '2026-10-17T0701'
const DIR = `listing/${SCAN}/index/g1`

let env: Env
let raw: Awaited<ReturnType<typeof sqliteD1>>['raw']

beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  const d = await sqliteD1()
  raw = d.raw
  for (const [date, dir] of [[SCAN, DIR], [OLD, `listing/${OLD}/index/g0`]]) {
    raw.exec(`INSERT INTO index_schema (date, variant, version, schema_json, gen, dir) VALUES ('${date}', 'path', 2, '[]', 'g', '${dir}')`)
  }
  FILES.set(`${DIR}/dups.json`, fixture('dups/dups.json'))
  FILES.set(`${DIR}/dups.parquet`, fixture('dups/dups.parquet'))
  raw.exec("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'open', 'open', 'a', 0), (2, 'old', 'closed', 'a', 0)")
  raw.exec(`INSERT INTO plan_items (plan_id, prefix, added_by, added_ts, kind) VALUES
    (1, 'gs://eu/ds/', 'a', 0, NULL), (1, 'gs://west/m/model.bin', 'a', 0, 'object'), (2, 'gs://east/ds/web/', 'a', 0, NULL)`)
  env = { DB: d.db, DUPS: '1', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'dvx', STORE_SCHEME: 'gs://' } as Env
})

beforeEach(() => { GETS.length = 0 })

const get = async (qs: string, e: Env = env, host = 'localhost') => {
  const r = await dups({ request: new Request(`http://${host}/api/dups?${qs}`), env: e } as never)
  return { status: r.status, body: await r.json() as { date: string; summary: Record<string, unknown>; groups: DupGroup[]; more: boolean; rows: number; row_groups: number; error?: string } }
}
/** Each group as `<tier> <rel>: <copy>[*keep][^nested][ =check][ @plan]…`. */
const shape = (gs: DupGroup[]) => gs.map(g => `${g.tier} ${g.rel}: ` + g.items.map(c =>
  c.path + (c.keep ? '*' : '') + (c.nested ? '^' : '') + (c.check ? ` =${c.check}` : '') + (c.staged != null ? ` @${c.staged}` : '')).join(', '))

describe('/api/dups', () => {
  it('serves the newest report, suspect and all-nested groups left out, with live staged state', async () => {
    const { status, body } = await get('')
    expect(status).toBe(200)
    expect([body.date, body.summary.scan, body.summary.groups, body.more, body.rows, body.row_groups]).toEqual([SCAN, SCAN, 6, false, 19, 7])
    expect(shape(body.groups)).toEqual([
      'tree ds/web: east/ds/web =equal, eu/ds/web =equal @1, west/ds/web*',
      'moved data: east/tok/en/data*, east/tok/fr/data =equal',
      'moved q: east/p/q*^, eu/q =differs, west/p/q^ =equal',
      'tree p: east/p*, west/p =equal',
      'tree m/model.bin: east/m/model.bin*, west/m/model.bin =equal @1',
    ])
    const ds = body.groups[0]
    expect({ ...ds, items: ds.items.map(c => [c.bucket, c.size, c.n_files, c.created, c.last_read, c.cb]) }).toEqual({
      group: 'b7a8285fb072', tier: 'tree', rel: 'ds/web', kind: 'dir', size: 4 * G, n_files: 2, copies: 3, reclaimable: 8 * G, n_buckets: 3, suspect: false,
      items: [['east', 4 * G, 2, 1_760_000_000, 20_300, { 3: G }], ['eu', 4 * G, 2, 1_759_913_600, null, {}], ['west', 4 * G, 2, 1_760_086_400, 20_370, {}]],
    })
  })

  it('filters by tier, min, bucket pair and text, and includes suspect groups on request', async () => {
    const rels = async (qs: string) => (await get(qs)).body.groups.map(g => `${g.tier} ${g.rel}`)
    expect(await rels(`date=${SCAN}&tier=moved&suspect=1`)).toEqual(['moved part-0.parquet', 'moved data', 'moved q'])
    expect(await rels(`date=${SCAN}&min=${4 * G}`)).toEqual(['tree ds/web', 'moved data', 'moved q'])
    expect(await rels(`date=${SCAN}&buckets=west,east`)).toEqual(['tree ds/web', 'moved q', 'tree p', 'tree m/model.bin'])
    expect(await rels(`date=${SCAN}&buckets=east,east&suspect=1`)).toEqual(['moved part-0.parquet', 'moved data'])
    expect(await rels(`date=${SCAN}&q=MODEL`)).toEqual(['tree m/model.bin'])
  })

  it('stops at min: the rows past it are never read', async () => {
    const { body } = await get(`date=${SCAN}&min=${6 * G}`)
    expect([shape(body.groups).length, body.rows, body.row_groups]).toEqual([1, 15, 5])
  })

  it('pages: n groups, more when another matches', async () => {
    const a = await get(`date=${SCAN}&n=2`)
    expect([a.body.groups.map(g => g.rel), a.body.more]).toEqual([['ds/web', 'data'], true])
    const b = await get(`date=${SCAN}&n=5`)
    expect([b.body.groups.length, b.body.more]).toEqual([5, false])
  })

  it('refuses what it cannot answer', async () => {
    expect((await get(`date=${OLD}`)).status).toBe(404)
    expect((await get(`date=${OLD}`)).body.error).toBe(`no dups report for ${OLD}`)
    expect((await get('tier=exact')).body.error).toBe('bad tier (want tree|moved)')
    expect((await get('min=1e9')).body.error).toBe('bad min (bytes)')
    expect((await get('buckets=a,b,c')).body.error).toBe('bad buckets (one, or a pair)')
    expect((await get('n=0')).body.error).toBe('bad n (want 1..500)')
    expect((await get('', { ...env, DUPS: undefined })).status).toBe(404)
    expect((await get('', env, 'gcs.example.org')).status).toBe(401)
  })
})
