import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { type D1Variant, fixture, FILES, readJson, seedGeneration } from '../_lib/testStore'
import { searchKey } from '../_lib/search'
import { REJECT_MESSAGES } from '../_lib/indexedOnly'
import { drillSource, injectedStores, SuffixHits, type StaticFilterStore } from '../_lib/staticFilter'
import type { Blobs } from '../_lib/staticNames'
import { tiers } from '../_lib/staticRuns'
import { onRequestGet as subtree } from './subtree'
import { onRequestGet as diff } from './diff'
import { onRequestGet as filterScans } from './filter-scans'

vi.mock('@rdub/file-tree/stores/s3', async () => ({ S3Store: (await import('../_lib/testStore')).S3Store }))

// The gcs incident (2026-10-09): a manifest listed a run whose `catalog/meta.json` was not written yet, and every
// heavy literal on every scan was a 500 (`/api/subtree`, `/api/diff`). The map routes over a generation wired as
// the site wires it (`staticFilterStore`: the light tiers, the drill over their runs and catalog) — the base from
// the static-filter fixture's shards and the static-drill fixture's catalog and drill (scans A and B), plus one
// run for scan C, broken each way a publish can leave it: the base's scans answer byte for byte as the base alone
// does, C is `scan-not-indexed` for the literals its broken files would answer, and the broken key is logged.

const A = '2026-10-04'
const B = '2026-10-05'
const C = '2026-10-06'
const RUN = `deltas/${C}`
const MANIFEST = `manifests/${C}.json`
const dirOf = (date: string) => `cw-l2/${date}/index/g`
let base: Env
/** The base generation's files (keys under the generation root). */
const gen = new Map<string, ArrayBuffer>()
const GARBAGE = new Uint8Array(64).fill(0xab).buffer

async function readTree(dir: string, into: Map<string, ArrayBuffer>, rel = ''): Promise<void> {
  const fs = await import(/* @vite-ignore */ 'node:fs' as string) as { readdirSync(p: string, o: { withFileTypes: true }): { name: string; isDirectory(): boolean }[]; readFileSync(p: string): Uint8Array }
  for (const e of fs.readdirSync(fixture(dir), { withFileTypes: true })) {
    const k = rel ? `${rel}/${e.name}` : e.name
    if (e.isDirectory()) await readTree(`${dir}/${e.name}`, into, k)
    else { const b = fs.readFileSync(fixture(`${dir}/${e.name}`)); into.set(k, b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer) }
  }
}

const json = (o: unknown): ArrayBuffer => new TextEncoder().encode(JSON.stringify(o)).buffer as ArrayBuffer
/** The run's files as a publish would have them: the base's light files copied under the run (its answers are the
 *  base's; nothing here asks the run's catalog at the fleet root), its drill `meta.json` naming scan C. */
function runFiles(): Map<string, ArrayBuffer> {
  const m = new Map<string, ArrayBuffer>()
  for (const [k, v] of gen) if (k === 'shards.json' || k.startsWith('sx/') || k.startsWith('catalog/')) m.set(`${RUN}/${k}`, v)
  return m
}
function drillFiles(): Map<string, ArrayBuffer> {
  const m = new Map<string, ArrayBuffer>()
  for (const [k, v] of gen) if (k.startsWith('drill/')) m.set(`${RUN}/${k}`, v)
  const meta = JSON.parse(new TextDecoder().decode(gen.get('drill/meta.json'))) as Record<string, unknown>
  m.set(`${RUN}/drill/meta.json`, json({ ...meta, scans: [C] }))
  return m
}
const manifest = () => json({ gen: 'fixture', date: C, base_scans: 2, scans: [A, B, C], runs: [{ key: RUN, first: C, last: C, level: 0, scans: [C] }] })

function blobsOf(files: Map<string, ArrayBuffer>): Blobs {
  const bytes = (k: string) => { const b = files.get(k); if (!b) throw new Error(`static names: static-names/fixture/${k} is missing`); return b }
  return {
    async range(key, offset, length) { const b = bytes(key); return b.slice(offset, length == null ? b.byteLength : offset + length) },
    async suffix(key, n) { const b = bytes(key); return { buf: b.slice(Math.max(0, b.byteLength - n)), size: b.byteLength } },
    async json(key) { return JSON.parse(new TextDecoder().decode(bytes(key))) },
    async list(prefix) { return [...files.keys()].filter(k => k.startsWith(prefix)).sort() },
  }
}

/** The store `staticFilterStore` builds, over `files`. */
function storeOver(files: Map<string, ArrayBuffer>): StaticFilterStore {
  const blobs = blobsOf(files)
  const t = tiers(blobs)
  const runs = async () => (await t.tiers.state()).tiers.flatMap(x => x.dir ? [x.dir] : [])
  const heavy = drillSource(blobs, undefined, { runs, catalog: t.catalog })
  return { source: new SuffixHits(t.names, { heavy }), scans: t.scans, gen: 'fixture' }
}
const envOf = (s: StaticFilterStore): Env => {
  const env = { ...base, FILTER_INDEXED_ONLY: '1' } as Env
  injectedStores.set(env, s)
  return env
}

let logged: string[] = []
beforeAll(async () => {
  ;(globalThis as unknown as { caches: unknown }).caches = { default: { match: async () => undefined, put: async () => {} } }
  vi.spyOn(console, 'error').mockImplementation((m: unknown) => { if (String(m).startsWith('static ')) logged.push(String(m)) })
  await readTree('static-drill', gen)
  gen.delete('expected.json'); gen.delete('gen.py')
  const light = new Map<string, ArrayBuffer>()
  await readTree('static-filter', light)
  for (const [k, v] of light) if (k === 'shards.json' || k === 'scans.json' || k.startsWith('sx/')) gen.set(k, v)
  const { db, raw } = await sqliteD1('cw')
  // Scan C's path store is B's (the static index is what's under test).
  for (const [date, from] of [[A, A], [B, B], [C, B]]) {
    const v = await readJson<Record<string, D1Variant>>(`static-filter/${from}/d1.json`)
    const files = {
      path: { parquet: `static-filter/${from}/path-index.parquet`, groups: `static-filter/${from}/path-index.groups.json` },
      bysize: { parquet: `static-filter/${from}/path-index-bysize.parquet`, groups: `static-filter/${from}/path-index-bysize.groups.json` },
    }
    seedGeneration(raw, { date, gen: 'g', dir: dirOf(date), variants: v, files })
    for (const [role, file] of [['rows', 'rows'], ['trigrams', 'trigrams'], ['rowsSearch', 'rows-search']] as const) {
      FILES.set(searchKey(dirOf(date), role), fixture(`static-filter/${from}/path-index.${file}.parquet`))
    }
  }
  base = { DB: db, ROOT_LABEL: 'root', GCS_HMAC_KEY_ID: 'k', GCS_HMAC_SECRET: 's', STORE_BUCKET: 'my-data' } as Env
})
afterEach(() => { logged = [] })
afterAll(() => { vi.restoreAllMocks() })

type Route = typeof subtree
const call = async (route: Route, qs: string, env: Env): Promise<[number, string]> => {
  const r = await route({ request: new Request(`http://localhost/api/x?${qs}`), env })
  return [r.status, await r.text()]
}
const notIndexed: [number, string] = [400, JSON.stringify({ error: REJECT_MESSAGES['scan-not-indexed'], code: 'scan-not-indexed' })]
/** A light literal (its suffix range is read whole) and a heavy one (one character: the drill's). */
const LIGHT = 'path=bk&q=tomat'
const HEAVY = 'path=bk/fill&q=0'
/** The requests on the base's scans, and on C. */
const onBase = (env: Env) => Promise.all([
  call(subtree, `date=${A}&${LIGHT}`, env), call(subtree, `date=${B}&${LIGHT}`, env),
  call(subtree, `date=${A}&${HEAVY}`, env), call(subtree, `date=${B}&${HEAVY}`, env),
  call(diff, `from=${A}&to=${B}&${LIGHT}`, env), call(diff, `from=${A}&to=${B}&${HEAVY}`, env),
])
const onC = (env: Env) => Promise.all([
  call(subtree, `date=${C}&${LIGHT}`, env), call(subtree, `date=${C}&${HEAVY}`, env),
  call(diff, `from=${B}&to=${C}&${LIGHT}`, env), call(diff, `from=${B}&to=${C}&${HEAVY}`, env),
])
const broke = (dir: string, error: string, who = 'tiers') => `static ${who}: ${dir} is broken; its scans and every later tier's are not indexed until it loads: ${error}`
const scansOf = async (env: Env) => (await (await filterScans({ request: new Request('http://localhost/api/filter-scans'), env } as never)).json() as { scans: string[] }).scans

describe('the map routes over a generation with a broken run', () => {
  /** The base alone (no manifest): what the base's scans must answer, byte for byte. */
  let want: [number, string][]
  beforeAll(async () => { want = await onBase(envOf(storeOver(gen))) })

  it('the base alone answers both literals on A and B (the reference)', () => {
    expect(want.map(([status]) => status)).toEqual([200, 200, 200, 200, 200, 200])
  })

  it('the incident: the run listed before its `catalog/meta.json` was written — A and B as the base alone, C not indexed', async () => {
    const files = new Map([...gen, ...runFiles(), [MANIFEST, manifest()]])
    files.delete(`${RUN}/catalog/meta.json`)
    const env = envOf(storeOver(files))
    // The fleet root too, where a heavy literal reads the catalog's per-bucket cells (the incident's own read).
    const root = (e: Env) => Promise.all([call(subtree, `date=${A}&path=&q=0`, e), call(diff, `from=${A}&to=${B}&path=&q=0`, e), call(subtree, `date=${C}&path=&q=0`, e)])
    const [rootA, rootDiff] = await root(envOf(storeOver(gen)))
    expect([rootA[0], rootDiff[0]]).toEqual([200, 200])
    expect([await onBase(env), await onC(env), await root(env), await scansOf(env)]).toEqual([want, [notIndexed, notIndexed, notIndexed, notIndexed], [rootA, rootDiff, notIndexed], [A, B]])
    expect(logged).toEqual([broke(RUN, `static names: static-names/fixture/${RUN}/catalog/meta.json is missing`)])
  })

  it('the run\'s suffix shards corrupt: found by the first light read, then C is not indexed', async () => {
    const files = new Map([...gen, ...runFiles(), [MANIFEST, manifest()], [`${RUN}/sx/s0000.parquet`, GARBAGE], [`${RUN}/sx/s0001.parquet`, GARBAGE]])
    const env = envOf(storeOver(files))
    expect([await onBase(env), await onC(env), await scansOf(env)]).toEqual([want, [notIndexed, notIndexed, notIndexed, notIndexed], [A, B]])
    expect(logged).toEqual([broke(RUN, 'Start offset -2880154483 is outside the bounds of the buffer')])
  })

  it.each([
    ['missing', (m: Map<string, ArrayBuffer>) => { for (const k of [...m.keys()]) if (/-index(\.top)?\.parquet$/.test(k)) m.delete(k) }, `static names: static-names/fixture/${RUN}/drill/short-roots-index.top.parquet is missing`],
    ['corrupt', (m: Map<string, ArrayBuffer>) => { for (const k of [...m.keys()]) if (/-index(\.top)?\.parquet$/.test(k)) m.set(k, GARBAGE) }, 'parquet file invalid (footer != PAR1)'],
  ])('the run\'s drill index %s (its `meta.json` there): the heavy literal is not indexed on C, the light one answers', async (_, damage, error) => {
    const drill = drillFiles()
    damage(drill)
    const env = envOf(storeOver(new Map([...gen, ...runFiles(), ...drill, [MANIFEST, manifest()]])))
    const [lightC, heavyC, lightDiff, heavyDiff] = await onC(env)
    expect([await onBase(env), [lightC[0], heavyC, lightDiff[0], heavyDiff], await scansOf(env)]).toEqual([want, [200, notIndexed, 200, notIndexed], [A, B, C]])
    expect(logged).toEqual([broke(RUN, error, 'drill')])
  })
})
