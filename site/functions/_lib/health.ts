// /api/health (specs/done/health-page.md): the deployment's append-only stores read the way their readers read them —
// `INDEX_R2`'s `scans.json` and newest manifest per store (`manifests.ts`), each listed run's files on R2 — plus D1's
// per-scan path store (`index_schema`) and scan jobs (`scan_runs`), assembled by `src/healthModel.ts`. A store the
// deployment doesn't configure (`INTERVAL_STORE_GEN`, `STATIC_GEN`, the `INDEX_R2` binding) is left out.
import type { D1Database } from '@cloudflare/workers-types'
import {
  COMPACT_LEVEL, type CompactionRecord, type HealthDoc, healthDoc, type HealthRun, type MergeRecord, type StackRun, type StaticTiers, type StoreKind, type StoreRead,
  type UpkeepInputs,
} from '../../src/healthModel.js'
import { isScanProducer } from '../../src/scanRunsModel.js'
import { newestManifest } from './manifests.js'
import { loadScanRuns } from './scanRuns.js'
import { STATIC_PREFIX, staticGen, staticPrefix } from './staticNames.js'
import { ivPrefix, pathScans } from './index.js'
import { storeKey } from './stores.js'
import type { Env } from './auth.js'

/** The sorts an interval-store run serves (`interval_append.RUN_SORTS`' path sorts), each a data file and its footer
 *  groups: a run is whole on R2 when every one is there (`r2_publish` copies the manifest after all of them). */
export const IV_RUN_FILES = ['served/path.parquet', 'served/path.groups.parquet', 'served/bysize.parquet', 'served/bysize.groups.parquet']

type R2 = Pick<R2Bucket, 'get' | 'head' | 'list'>

const message = (e: unknown): string => (e instanceof Error ? e.message : String(e))

/** Whether `key` is on R2 (a `head`). */
const has = async (r2: R2, key: string): Promise<boolean> => (await r2.head(key)) !== null

/** Every manifest under `<root>/manifests/` (file name → upload time, epoch s), and the newest (null when there's none). */
async function manifests(r2: R2, root: string): Promise<{ uploads: Map<string, number | null>; newest: { name: string; uploaded: number | null } | null }> {
  const prefix = `${root}/manifests/`
  const up = new Map<string, number | null>()
  let cursor: string | undefined
  do {
    const page = await r2.list({ prefix, ...(cursor ? { cursor } : {}) })
    for (const o of page.objects) up.set(o.key.slice(prefix.length), o.uploaded ? Math.floor(new Date(o.uploaded).getTime() / 1000) : null)
    cursor = page.truncated ? page.cursor : undefined
  } while (cursor)
  const name = newestManifest(up.keys())
  return { uploads: up, newest: name === null ? null : { name, uploaded: up.get(name) ?? null } }
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

/** The generation's `compaction.json` (the fold that made it) and its upload, null when there's none (a built
 *  generation) or it doesn't parse as one: like the merge record, never a break in the store's read. */
export async function readCompaction(r2: R2, root: string): Promise<UpkeepInputs['compaction']> {
  try {
    const o = await r2.get(`${root}/compaction.json`)
    if (!o) return null
    const doc = await o.json<CompactionRecord>()
    return doc && typeof doc.from === 'string' ? { doc, ts: o.uploaded ? Math.floor(new Date(o.uploaded).getTime() / 1000) : null } : null
  } catch {
    return null
  }
}

interface ManifestDoc { scans: string[]; runs: StackRun[]; rev?: number; revises?: string; compact_level?: number }

/** A store as /health reads it: its stack, and what its upkeep reads beyond it (the manifests' uploads, `compaction.json`). */
export interface StoreReading { read: StoreRead; upkeep: Pick<UpkeepInputs, 'uploads' | 'compaction'> }

/** A store's base and newest manifest (`run` checks one listed run on R2), its merge status record, and its
 *  `compaction.json`. */
async function readStack(r2: R2, kind: StoreKind, prefix: string, gen: string, run: (r: StackRun) => Promise<Omit<HealthRun, keyof StackRun>>,
  baseTiers?: () => Promise<StaticTiers>): Promise<StoreReading> {
  const root = `${prefix}/${gen}`
  const [{ read, uploads }, merging, compaction] = await Promise.all([readRuns(r2, kind, prefix, gen, run, baseTiers), readMerging(r2, root), readCompaction(r2, root)])
  return { read: { ...read, merging }, upkeep: { uploads, compaction } }
}

async function readRuns(r2: R2, kind: StoreKind, prefix: string, gen: string, run: (r: StackRun) => Promise<Omit<HealthRun, keyof StackRun>>,
  baseTiers?: () => Promise<StaticTiers>): Promise<{ read: StoreRead; uploads: Map<string, number | null> }> {
  const root = `${prefix}/${gen}`
  const empty: StoreRead = { kind, prefix, gen, manifest: null, rev: 0, revises: null, manifest_ts: null, compact_level: COMPACT_LEVEL, base: [], runs: [] }
  let uploads = new Map<string, number | null>()
  try {
    const [doc, ms, tiers] = await Promise.all([getJson<{ scans: { id: string }[] }>(r2, `${root}/scans.json`), manifests(r2, root), baseTiers?.()])
    uploads = ms.uploads
    const base = doc.scans.map(s => s.id).sort(), m = ms.newest
    if (!m) return { read: { ...empty, base, ...(tiers ? { base_tiers: tiers } : {}) }, uploads }
    const md = await getJson<ManifestDoc>(r2, `${root}/manifests/${m.name}`)
    if (!Array.isArray(md.runs)) throw new Error(`${root}/manifests/${m.name}: no runs`)
    const runs = await Promise.all(md.runs.map(async r => ({ key: r.key, first: r.first, last: r.last, level: r.level, scans: r.scans, ...(r.rows != null ? { rows: r.rows } : {}), ...(r.bytes != null ? { bytes: r.bytes } : {}), ...await run(r) })))
    return {
      read: {
        ...empty, base, runs, manifest: m.name, manifest_ts: m.uploaded, rev: md.rev ?? 0, revises: md.revises ?? null,
        compact_level: md.compact_level ?? COMPACT_LEVEL, ...(tiers ? { base_tiers: tiers } : {}),
      },
      uploads,
    }
  } catch (e) {
    return { read: { ...empty, error: message(e) }, uploads }
  }
}

/** The interval store (`interval-store/<INTERVAL_STORE_GEN>/`: the pin, or the current generation `/api/health`
 *  resolved), or null when the deployment has none. */
export async function readInterval(env: Env): Promise<StoreReading | null> {
  const r2 = env.INDEX_R2 as R2 | undefined, gen = env.INTERVAL_STORE_GEN?.trim()
  if (!r2 || !gen) return null
  const root = `${ivPrefix(env)}/${gen}`
  return readStack(r2, 'interval', ivPrefix(env), gen, async r => ({ r2: (await Promise.all(IV_RUN_FILES.map(f => has(r2, `${root}/${r.key}/${f}`)))).every(Boolean) }))
}

/** A static tier dir's markers (`static_runner.has_tier`): `start` whether the base carries the starts-with catalog. */
async function staticTiers(r2: R2, dir: string, start: boolean | null): Promise<StaticTiers & { start: boolean }> {
  const [light, drill, anchors, st] = await Promise.all(['catalog/meta.json', 'drill/meta.json', 'anchors/meta.json', 'anchors/start/meta.json'].map(f => has(r2, `${dir}/${f}`)))
  return { light, drill, anchors: anchors && (start === false || start === null || st), start: st }
}

/** The static name index (`static-names/<STATIC_GEN>/`), or null when the deployment has none. */
export async function readStatic(env: Env): Promise<StoreReading | null> {
  const r2 = env.INDEX_R2 as R2 | undefined
  if (!r2 || !env.STATIC_GEN?.trim()) return null
  const gen = staticGen(env), root = staticPrefix(gen)
  let base: Promise<StaticTiers & { start: boolean }> | undefined
  const baseOf = () => (base ??= staticTiers(r2, root, null))
  return readStack(r2, 'static', STATIC_PREFIX, gen, async r => {
    const t = await staticTiers(r2, `${root}/${r.key}`, (await baseOf()).start)
    return { r2: t.light, tiers: { light: t.light, drill: t.drill, anchors: t.anchors } }
  }, async () => { const { light, drill, anchors } = await baseOf(); return { light, drill, anchors } })
}

/** The /health document: the stores, D1's per-scan path store and scan jobs (each optional), at `now` (epoch s). The
 *  D1 store's runs are read once: the scan jobs (coverage, freshness) and the maintenance runs (each store's upkeep). */
export async function health(env: Env & { DB?: D1Database }, now: number): Promise<HealthDoc> {
  const db = env.DB
  const [iv, st, perScan, rows] = await Promise.all([
    readInterval(env),
    readStatic(env),
    db ? pathScans(env, false).then(r => r.results.map(x => x.date), () => [] as string[]) : Promise.resolve([] as string[]),
    db ? loadScanRuns(db, storeKey(env)).then(r => r ?? { runs: [], phases: [] }, () => ({ runs: [], phases: [] })) : Promise.resolve({ runs: [], phases: [] }),
  ])
  const stores = [iv, st].filter((x): x is StoreReading => x !== null)
  // scan jobs only: a running compaction or merge names the scan it follows, which isn't a scan in flight
  const jobs = rows.runs.filter(j => isScanProducer(j.kind))
  return healthDoc(stores.map(s => s.read), perScan, jobs, now, {
    runs: rows.runs, phases: rows.phases, stores: Object.fromEntries(stores.map(s => [s.read.kind, s.upkeep])),
  })
}
