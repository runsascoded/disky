// /api/health (specs/health-page.md): the deployment's append-only stores read the way their readers read them —
// `INDEX_R2`'s `scans.json` and newest manifest per store (`manifests.ts`), each listed run's files on R2 — plus D1's
// per-scan path store (`index_schema`) and scan jobs (`scan_runs`), assembled by `src/healthModel.ts`. A store the
// deployment doesn't configure (`INTERVAL_STORE_GEN`, `STATIC_GEN`, the `INDEX_R2` binding) is left out.
import type { D1Database } from '@cloudflare/workers-types'
import { COMPACT_LEVEL, type HealthDoc, healthDoc, type HealthRun, type MergeRecord, type StackRun, type StaticTiers, type StoreRead } from '../../src/healthModel.js'
import { newestManifest } from './manifests.js'
import { loadScanRuns } from './scanRuns.js'
import { staticGen, staticPrefix } from './staticNames.js'
import { pathScans } from './index.js'
import { storeKey } from './stores.js'
import type { Env } from './auth.js'

/** The sorts an interval-store run serves (`interval_append.RUN_SORTS`' path sorts), each a data file and its footer
 *  groups: a run is whole on R2 when every one is there (`r2_publish` copies the manifest after all of them). */
export const IV_RUN_FILES = ['served/path.parquet', 'served/path.groups.parquet', 'served/bysize.parquet', 'served/bysize.groups.parquet']

type R2 = Pick<R2Bucket, 'get' | 'head' | 'list'>

const message = (e: unknown): string => (e instanceof Error ? e.message : String(e))

/** Whether `key` is on R2 (a `head`). */
const has = async (r2: R2, key: string): Promise<boolean> => (await r2.head(key)) !== null

/** The newest manifest under `<root>/manifests/` (name and upload time), null when there's none. */
async function newest(r2: R2, root: string): Promise<{ name: string; uploaded: number | null } | null> {
  const prefix = `${root}/manifests/`
  const up = new Map<string, number | null>()
  let cursor: string | undefined
  do {
    const page = await r2.list({ prefix, ...(cursor ? { cursor } : {}) })
    for (const o of page.objects) up.set(o.key.slice(prefix.length), o.uploaded ? Math.floor(new Date(o.uploaded).getTime() / 1000) : null)
    cursor = page.truncated ? page.cursor : undefined
  } while (cursor)
  const name = newestManifest(up.keys())
  return name === null ? null : { name, uploaded: up.get(name) ?? null }
}

async function getJson<T>(r2: R2, key: string): Promise<T> {
  const o = await r2.get(key)
  if (!o) throw new Error(`${key}: not on R2`)
  return o.json<T>()
}

/** The store's merge status record (`<root>/merging.json`, `append_runner.StatusMirror`), null when there's none or it
 *  doesn't parse as one: a merge's status never breaks the store's read. Read by key: no reader lists `<root>/`. */
export async function readMerging(r2: R2, root: string): Promise<MergeRecord | null> {
  try {
    const o = await r2.get(`${root}/merging.json`)
    if (!o) return null
    const m = await o.json<MergeRecord>()
    return m && typeof m.holder === 'string' && typeof m.started_ts === 'number' && typeof m.updated_ts === 'number' && Array.isArray(m.merges) ? m : null
  } catch {
    return null
  }
}

interface ManifestDoc { scans: string[]; runs: StackRun[]; rev?: number; revises?: string; compact_level?: number }

/** A store's base and newest manifest (`run` checks one listed run on R2), and its merge status record. */
async function readStack(r2: R2, kind: StoreRead['kind'], gen: string, root: string, run: (r: StackRun) => Promise<Omit<HealthRun, keyof StackRun>>,
  baseTiers?: () => Promise<StaticTiers>): Promise<StoreRead> {
  const [read, merging] = await Promise.all([readRuns(r2, kind, gen, root, run, baseTiers), readMerging(r2, root)])
  return { ...read, merging }
}

async function readRuns(r2: R2, kind: StoreRead['kind'], gen: string, root: string, run: (r: StackRun) => Promise<Omit<HealthRun, keyof StackRun>>,
  baseTiers?: () => Promise<StaticTiers>): Promise<StoreRead> {
  const empty: StoreRead = { kind, gen, manifest: null, rev: 0, revises: null, manifest_ts: null, compact_level: COMPACT_LEVEL, base: [], runs: [] }
  try {
    const [doc, m, tiers] = await Promise.all([getJson<{ scans: { id: string }[] }>(r2, `${root}/scans.json`), newest(r2, root), baseTiers?.()])
    const base = doc.scans.map(s => s.id).sort()
    if (!m) return { ...empty, base, ...(tiers ? { base_tiers: tiers } : {}) }
    const md = await getJson<ManifestDoc>(r2, `${root}/manifests/${m.name}`)
    if (!Array.isArray(md.runs)) throw new Error(`${root}/manifests/${m.name}: no runs`)
    const runs = await Promise.all(md.runs.map(async r => ({ key: r.key, first: r.first, last: r.last, level: r.level, scans: r.scans, ...(r.rows != null ? { rows: r.rows } : {}), ...(r.bytes != null ? { bytes: r.bytes } : {}), ...await run(r) })))
    return {
      ...empty, base, runs, manifest: m.name, manifest_ts: m.uploaded, rev: md.rev ?? 0, revises: md.revises ?? null,
      compact_level: md.compact_level ?? COMPACT_LEVEL, ...(tiers ? { base_tiers: tiers } : {}),
    }
  } catch (e) {
    return { ...empty, error: message(e) }
  }
}

/** The interval store (`interval-store/<INTERVAL_STORE_GEN>/`), or null when the deployment has none. */
export async function readInterval(env: Env): Promise<StoreRead | null> {
  const r2 = env.INDEX_R2 as R2 | undefined, gen = env.INTERVAL_STORE_GEN?.trim()
  if (!r2 || !gen) return null
  const root = `interval-store/${gen}`
  return readStack(r2, 'interval', gen, root, async r => ({ r2: (await Promise.all(IV_RUN_FILES.map(f => has(r2, `${root}/${r.key}/${f}`)))).every(Boolean) }))
}

/** A static tier dir's markers (`static_runner.has_tier`): `start` whether the base carries the starts-with catalog. */
async function staticTiers(r2: R2, dir: string, start: boolean | null): Promise<StaticTiers & { start: boolean }> {
  const [light, drill, anchors, st] = await Promise.all(['catalog/meta.json', 'drill/meta.json', 'anchors/meta.json', 'anchors/start/meta.json'].map(f => has(r2, `${dir}/${f}`)))
  return { light, drill, anchors: anchors && (start === false || start === null || st), start: st }
}

/** The static name index (`static-names/<STATIC_GEN>/`), or null when the deployment has none. */
export async function readStatic(env: Env): Promise<StoreRead | null> {
  const r2 = env.INDEX_R2 as R2 | undefined
  if (!r2 || !env.STATIC_GEN?.trim()) return null
  const gen = staticGen(env), root = staticPrefix(gen)
  let base: Promise<StaticTiers & { start: boolean }> | undefined
  const baseOf = () => (base ??= staticTiers(r2, root, null))
  return readStack(r2, 'static', gen, root, async r => {
    const t = await staticTiers(r2, `${root}/${r.key}`, (await baseOf()).start)
    return { r2: t.light, tiers: { light: t.light, drill: t.drill, anchors: t.anchors } }
  }, async () => { const { light, drill, anchors } = await baseOf(); return { light, drill, anchors } })
}

/** The /health document: the stores, D1's per-scan path store and scan jobs (each optional), at `now` (epoch s). */
export async function health(env: Env & { DB?: D1Database }, now: number): Promise<HealthDoc> {
  const db = env.DB
  const [iv, st, perScan, jobs] = await Promise.all([
    readInterval(env),
    readStatic(env),
    db ? pathScans(env, false).then(r => r.results.map(x => x.date), () => [] as string[]) : Promise.resolve([] as string[]),
    db ? loadScanRuns(db, storeKey(env)).then(r => r?.runs ?? [], () => []) : Promise.resolve([]),
  ])
  return healthDoc([iv, st].filter((x): x is StoreRead => x !== null), perScan, jobs, now)
}
