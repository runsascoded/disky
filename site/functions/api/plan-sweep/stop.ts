// POST /api/sweep/stop — cancel a running sweep job. Body: { job_id }. Admin only.
//
// This cancels the Batch job outright (`jobs:cancel`). A cleaner drain — drop a
// `STOP` file the executor polls so roots already listing finish and log — is a
// follow-up once the executor watches for it (specs/cw-sweep.md, Slice 2
// follow-ups). Cancelling mid-run is safe: partial deletes are recorded in the
// run's log part-files and are recoverable (delete markers), and a re-dispatch
// re-lists and skips already-deleted keys.
import { type Ctx, type Env as AuthEnv, json, requireAdmin } from "../../_lib/auth.js"
import { BATCH_REGION, batchJobsUrl, gcpToken } from "../../_lib/gcp.js"

type Env = AuthEnv & { GCP_SA_KEY?: string }

const JOB_RE = /^cw-sweep-(dry|real)-\d{8}-\d{6}z$/

export const onRequestPost = async (ctx: Ctx & { env: Env }): Promise<Response> => {
  const gated = await requireAdmin(ctx)
  if (gated instanceof Response) return gated
  if (!ctx.env.GCP_SA_KEY) return json({ error: "stop not configured (no GCP_SA_KEY)" }, 503)

  const body = (await ctx.request.json().catch(() => null)) as { job_id?: string } | null
  const jobId = body?.job_id ?? ""
  if (!JOB_RE.test(jobId)) return json({ error: "bad job_id" }, 400)

  const token = await gcpToken(ctx.env.GCP_SA_KEY)
  const r = await fetch(`${batchJobsUrl(BATCH_REGION)}/${jobId}:cancel`, {
    method: "POST",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    body: JSON.stringify({ reason: `cancelled by ${gated.email}` }),
  })
  if (!r.ok) return json({ error: "cancel failed", status: r.status, detail: (await r.text()).slice(0, 300) }, 500)
  return json({ job_id: jobId, cancelled_by: gated.email })
}
