// POST /api/sweep/undo — undo a real gcs run on Batch (specs/staged-runs.md).
//
// Body: { run_id } (`<scan>-p<plan>/<stamp>`, as /staged lists it). Admin only.
// Allowed only for a finished real run inside its undo window, not already
// undone; the job (`gcs-undo-<stamp>z`) runs `dt-cloud sweep undo` on the
// dispatch's spec and records the outcome on the run row itself
// (`_lib/sweepUndo.ts`).
import { ADMIN_SCOPE, type Env as AuthEnv, json, requireScope } from '../../_lib/auth.js'
import type { ExecEnv } from '../../_lib/dispatch.js'
import { undoSweepRun } from '../../_lib/sweepUndo.js'

type Env = AuthEnv & ExecEnv

export const onRequestPost = async (ctx: { request: Request; env: Env }): Promise<Response> => {
  const gated = await requireScope(ctx, ADMIN_SCOPE)
  if (gated instanceof Response) return gated
  const body = (await ctx.request.json().catch(() => null)) as { run_id?: unknown } | null
  const runId = typeof body?.run_id === 'string' ? body.run_id : ''
  const r = await undoSweepRun(ctx.env, runId, gated.email ?? 'sweep-console', new URL(ctx.request.url).origin)
  if (!r.ok) return json({ error: r.error, ...r.extra }, r.status)
  return json({ job_id: r.job_id, target: r.target, region: r.region, by: gated.email })
}
