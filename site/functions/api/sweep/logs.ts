// GET one known sweep job's task logs through its least-privilege Logging view.
import { type Env as AuthEnv, json, requireViewer } from '../../_lib/auth.js'
import { type BatchEnv, batchConfig } from '../../_lib/batchConfig.js'
import { batchJobsUrl, batchRegions, gcpToken } from '../../_lib/gcp.js'
import { boundedJson, taskLogBody, taskLogEntries } from '../../_lib/taskLogs.js'

export const onRequestGet = async (ctx: { request: Request; env: AuthEnv & BatchEnv }): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const cfg = batchConfig(ctx.env, ['GCP_PROJECT'])
  if ('missing' in cfg || !ctx.env.GCP_SA_KEY || !ctx.env.GCP_LOG_VIEW) return json({ error: 'Task logs not configured (GCP_SA_KEY / GCP_LOG_VIEW)' }, 503)
  const view = ctx.env.GCP_LOG_VIEW
  if (!view.startsWith(`projects/${cfg.project}/locations/`) || !/^projects\/[^/]+\/locations\/[^/]+\/buckets\/[^/]+\/views\/[^/]+$/.test(view)) return json({ error: 'Invalid task-log view configuration' }, 503)
  const p = new URL(ctx.request.url).searchParams
  const job = p.get('job') ?? '', region = p.get('region') ?? cfg.region
  if (!/^gcs-sweep-(dry|real)-[a-z0-9-]{1,50}$/.test(job) || !batchRegions(cfg).includes(region)) return json({ error: 'Invalid sweep job or region' }, 400)
  const cursor = p.get('cursor'), end = p.get('until')
  if ((cursor?.length ?? 0) > 4096 || cursor && !end || end && (!Number.isFinite(Date.parse(end)) || Date.parse(end) > Date.now())) return json({ error: 'Invalid log cursor or upper time bound' }, 400)
  const until = end ? new Date(end).toISOString() : new Date().toISOString()
  const headers = { authorization: `Bearer ${await gcpToken(ctx.env.GCP_SA_KEY)}` }
  try {
    const name = `projects/${cfg.project}/locations/${region}/jobs/${job}`
    const r = await fetch(`${batchJobsUrl(cfg, region)}/${job}`, { headers, signal: AbortSignal.timeout(15_000) })
    if (!r.ok) return json({ error: `Batch job lookup failed (HTTP ${r.status})` }, r.status === 404 ? 404 : 502)
    const j = await boundedJson(r, 1_048_576)
    if (!j || typeof j !== 'object' || !('name' in j) || j.name !== name || !('uid' in j) || typeof j.uid !== 'string' || !/^gcs-sweep-[a-zA-Z0-9_-]+$/.test(j.uid) || !('createTime' in j) || typeof j.createTime !== 'string' || !Number.isFinite(Date.parse(j.createTime))) throw new Error('invalid Batch job response')
    if (Date.parse(until) < Date.parse(j.createTime)) return json({ error: 'Log upper time bound precedes job creation' }, 400)
    const logs = await fetch('https://logging.googleapis.com/v2/entries:list', {
      method: 'POST', headers: { ...headers, 'content-type': 'application/json' }, signal: AbortSignal.timeout(15_000),
      body: JSON.stringify(taskLogBody(view, j.uid, j.createTime, until, cursor)),
    })
    if (!logs.ok) return json({ error: `Task logs unavailable (Logging HTTP ${logs.status})` }, 502)
    const data = await boundedJson(logs, 16_777_216)
    if (!data || typeof data !== 'object') throw new Error('invalid Logging response')
    const entries = taskLogEntries('entries' in data ? data.entries : undefined)
    const nextPageToken = 'nextPageToken' in data && typeof data.nextPageToken === 'string' ? data.nextPageToken : null
    return json({ entries, nextPageToken, until }, 200, { 'cache-control': 'private, max-age=10' })
  } catch (e) {
    console.error('Task-log read failed', e instanceof Error ? e.message : 'unknown error')
    return json({ error: 'Task-log read failed; refresh to retry' }, 502)
  }
}
