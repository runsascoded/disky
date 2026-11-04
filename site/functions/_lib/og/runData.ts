/** A deletion run's card data: the row and newest sample's time from D1 (every fetch), then the samples, plan summary and
 * progress files once per version per colo, shared by the page's description and the image so they agree. */
import { S3Store } from '@rdub/file-tree/stores/s3'
import type { Env } from '../auth.js'
import { storeCreds, storePrefixes, storeReady, storeTarget } from '../index.js'
import { runSamples } from '../runProgress.js'
import { BUILD } from '../build.js'
import { plannedOf, runFilesRel, type PlanSummaryFile, type ProgressFile } from '../../../src/runs.js'
import { MAX_FILES, runBuckets, runMaxAge, runVersion, shapeRun, validRunId, type RunRow, type RunShape } from './run.js'

export interface RunAt { id: string; run: RunRow; version: string }

/** The run and its card's current version; null for a malformed or unknown id, or a deployment without the tables. */
export async function runAt(env: Pick<Env, 'DB'>, id: string, now: number): Promise<RunAt | null> {
  if (!env.DB || !validRunId(id)) return null
  try {
    const run = await env.DB.prepare('SELECT mode, scan, started_ts, finished_ts, deleted_objects, buckets, log_dir FROM deletion_runs WHERE run_id = ?').bind(id).first<RunRow>()
    if (!run) return null
    const newest = await env.DB.prepare('SELECT MAX(ts) AS ts FROM deletion_progress WHERE run_id = ?').bind(id).first<{ ts: number | null }>()
    return { id, run, version: runVersion(run, newest?.ts ?? null, now) }
  } catch {
    return null
  }
}

/** The run's shape at its version, from the colo's cache or its reads. `complete` false: a store read failed, so it's
 * drawn from what it has and not cached. */
export async function runShapeAt(env: Env, origin: string, at: RunAt, now: number): Promise<{ shape: RunShape; complete: boolean }> {
  const key = new Request(`${origin}/og/run.json?id=${encodeURIComponent(at.id)}&v=${at.version}&bld=${BUILD.sha}`)
  const hit = await caches.default.match(key)
  if (hit) return { shape: await hit.json<RunShape>(), complete: true }
  let complete = true
  const store = storeReady(env) ? S3Store({ ...storeTarget(env), prefixes: storePrefixes(env, ['sweep/']), ...storeCreds(env) }) : null
  const read = async <T>(rel: string): Promise<T | null> => {
    if (!store) return null
    try {
      return JSON.parse(new TextDecoder().decode((await store.get(rel)).bytes)) as T
    } catch (e) {
      // Absent is an answer (no manifest or progress yet); anything else is a failed read.
      if ((e as Error).name !== 'NotFoundError') { complete = false; console.log(`run card: ${(e as Error).message}`) }
      return null
    }
  }
  const dir = runFilesRel(at.run.log_dir)
  const [samples, plan] = await Promise.all([runSamples(env.DB!, at.id), read<PlanSummaryFile>(`${dir}plan-summary.json`)])
  const planBuckets = plan ? Object.keys(plan.buckets) : []
  const named = runBuckets(at.run.buckets)
  const targets = at.run.finished_ts != null ? [] : (named.length ? named : planBuckets).slice(0, MAX_FILES)
  const progress = await Promise.all(targets.map(b => read<ProgressFile>(`${dir}progress/${b}.json`)))
  const files = new Map(targets.flatMap((b, i) => progress[i] ? [[b, progress[i]] as const] : []))
  const shape = shapeRun(at.run, plan ? plannedOf(plan).objects : null, files, samples, planBuckets, now)
  if (complete) await caches.default.put(key, new Response(JSON.stringify(shape), { headers: { 'content-type': 'application/json', 'cache-control': `public, max-age=${runMaxAge(at.run)}` } }))
  return { shape, complete }
}
