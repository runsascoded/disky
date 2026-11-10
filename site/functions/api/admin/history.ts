import { type Ctx, json, requireAdmin } from '../../_lib/auth.js'
import { submitBatch } from '../../_lib/cwBatch.js'
import { batchJobsUrl, gcpToken } from '../../_lib/gcp.js'
import { historySourceScans } from '../../_lib/historySource.js'
import {
  checkedHistoryPlan, historyBatchSpec, historyConfig, HistoryError, type HistoryEnv, type HistoryRequest,
  historyJobId, historyJobs, historyJson, historyKey, historyRequestBody, type HistoryStatus, parseHistoryAction, reconcileHistoryJobs, requiredHistoryPins, unfinishedHistoryJob,
} from '../../_lib/historyAdmin.js'

const NO_STORE = { 'cache-control': 'no-store' }
type HistoryCtx = Omit<Ctx, 'env'> & { env: HistoryEnv }
const JSON_OPTIONS = { httpMetadata: { contentType: 'application/json' } }

export const onRequestGet = async (ctx: HistoryCtx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const cfg = historyConfig(ctx.env)
  if (!ctx.env.INDEX_R2) return json({ enabled: false, reason: 'History jobs require INDEX_R2', jobs: [] }, 200, NO_STORE)
  try {
    const [jobs, source_scans, required_pins] = await Promise.all([
      historyJobs(ctx.env.INDEX_R2, new URL(ctx.request.url).searchParams.get('cursor') ?? undefined),
      historySourceScans(ctx.env),
      ctx.env.DB ? requiredHistoryPins(ctx.env.DB, Date.now()) : Promise.resolve([]),
    ])
    if (!('reason' in cfg) && jobs.jobs.some(unfinishedHistoryJob)) {
      const token = await gcpToken(ctx.env.GCP_SA_KEY!)
      jobs.jobs = await reconcileHistoryJobs(ctx.env.INDEX_R2, jobs.jobs, async jobId => {
        const response = await fetch(`${batchJobsUrl(cfg.batch, cfg.batch.region)}/${jobId}`, { headers: { authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(10000) })
        if (!response.ok) throw new Error(`Batch status could not be checked (${response.status})`)
        const body = await response.json() as { status?: { state?: string } }
        if (!body.status?.state) throw new Error('Batch status response did not name a state')
        return body.status.state
      })
    }
    return json({ enabled: !('reason' in cfg), ...('reason' in cfg ? { reason: cfg.reason } : {}), source_scans, required_pins, ...jobs }, 200, NO_STORE)
  } catch (e) {
    if (e instanceof HistoryError) return json({ error: e.message }, e.status, NO_STORE)
    throw e
  }
}

export const onRequestPost = async (ctx: HistoryCtx): Promise<Response> => {
  const actor = await requireAdmin(ctx)
  if (actor instanceof Response) return actor
  const cfg = historyConfig(ctx.env), bucket = ctx.env.INDEX_R2
  if ('reason' in cfg) return json({ error: cfg.reason }, 503, NO_STORE)
  if (!bucket || !ctx.env.DB) return json({ error: 'History jobs require INDEX_R2 and DB' }, 503, NO_STORE)
  try {
    const input = parseHistoryAction(await historyRequestBody(ctx.request)), now = Date.now()
    const required_pins = await requiredHistoryPins(ctx.env.DB, now)
    const plan = input.action === 'build' ? checkedHistoryPlan(await historyJson<HistoryStatus>(bucket, historyKey(input.plan_id, 'status')), required_pins) : undefined
    const id = historyJobId(now, crypto.randomUUID())
    const request: HistoryRequest = {
      version: 1, id, action: input.action, created_at: new Date(now).toISOString(), actor: actor.email ?? actor.grantId ?? actor.name ?? 'admin',
      ...(input.action === 'plan' ? { policy: input.policy, pinned: input.pinned } : { plan_id: input.plan_id, plan }),
      required_pins, data_store: cfg.dataStore, serving_store: cfg.servingStore,
      batch_url: `https://console.cloud.google.com/batch/jobsDetail/regions/${cfg.batch.region}/jobs/${id}?project=${encodeURIComponent(cfg.batch.project)}`,
    }
    const created = await bucket.put(historyKey(id, 'request'), JSON.stringify(request), { ...JSON_OPTIONS, onlyIf: { etagDoesNotMatch: '*' } })
    if (!created) throw new HistoryError('Job ID already exists; try again', 409)
    await bucket.put(historyKey(id, 'status'), JSON.stringify({ state: 'queued', updated_at: request.created_at }), JSON_OPTIONS)
    let got: Awaited<ReturnType<typeof submitBatch>>
    try {
      const token = await gcpToken(ctx.env.GCP_SA_KEY!)
      got = await submitBatch(cfg.batch, token, id, historyBatchSpec(cfg, id, input.action))
    } catch (e) {
      await bucket.put(historyKey(id, 'status'), JSON.stringify({ state: 'dispatch-unknown', error: 'Could not confirm Batch submission. Check the Batch job link before retrying.', updated_at: new Date().toISOString() }), JSON_OPTIONS)
      throw e
    }
    if (!got.ok) {
      const error = `Batch rejected the job (${got.status}): ${got.text.slice(0, 2000)}`
      await bucket.put(historyKey(id, 'status'), JSON.stringify({ state: 'failed', error, updated_at: new Date().toISOString() }), JSON_OPTIONS)
      return json({ error, id, batch_url: request.batch_url }, 502, NO_STORE)
    }
    return json({ id, request, status: { state: 'queued' } }, 202, NO_STORE)
  } catch (e) {
    if (e instanceof HistoryError) return json({ error: e.message }, e.status, NO_STORE)
    throw e
  }
}
