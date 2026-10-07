// POST /api/sweep/stop — ask a running sweep job to stop cleanly.
//
// Body: { job_id }. Drops `sweep/runs/<job_id>/STOP` in the data bucket; the
// executor polls that file every 10 s (`dt-cloud sweep stop`): roots already
// listing finish and log, the rest are left for a re-run, the job ends red.
// Admin-only, like dispatch.
import type { D1Database } from '@cloudflare/workers-types'
import { ADMIN_SCOPE, type Env as AuthEnv, json, requireScope } from '../../_lib/auth.js'
import { batchConfig, type BatchEnv, notConfigured } from '../../_lib/batchConfig.js'
import { gcpToken } from '../../_lib/gcp.js'
import { auditRunControl } from '../../_lib/plans.js'

type Env = AuthEnv & BatchEnv & { DB?: D1Database }

export const onRequestPost = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const gated = await requireScope(ctx, ADMIN_SCOPE)
  if (gated instanceof Response) return gated
  if (!ctx.env.GCP_SA_KEY) return json({ error: 'stop not configured (no GCP_SA_KEY)' }, 503)
  const cfg = batchConfig(ctx.env, ['DATA_BUCKET'])
  if ('missing' in cfg) return json({ error: notConfigured('stop', cfg.missing) }, 503)
  const body = (await ctx.request.json().catch(() => null)) as { job_id?: string } | null
  const jobId = body?.job_id ?? ''
  if (!/^gcs-sweep-(dry|real)-\d{8}-\d{6}z$/.test(jobId)) return json({ error: 'bad job_id' }, 400)
  const token = await gcpToken(ctx.env.GCP_SA_KEY)
  const name = encodeURIComponent(`sweep/runs/${jobId}/STOP`)
  const r = await fetch(`https://storage.googleapis.com/upload/storage/v1/b/${cfg.dataBucket}/o?uploadType=media&name=${name}`, {
    method: 'POST',
    headers: { authorization: `Bearer ${token}`, 'content-type': 'text/plain' },
    body: `${new Date().toISOString()} by ${gated.email ?? 'sweep-console'}\n`,
  })
  if (!r.ok) return json({ error: 'STOP write failed', status: r.status, detail: (await r.text()).slice(0, 300) }, 500)
  if (ctx.env.DB) await auditRunControl(ctx.env.DB, 'stop', jobId, gated.email ?? 'sweep-console', null)
  return json({ job_id: jobId, stopped_by: gated.email, note: 'the executor polls STOP every 10 s; roots already listing finish first' })
}
