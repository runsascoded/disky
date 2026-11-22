// POST /api/scan-runs/cancel — "Cancel" on a running scan's /scans page: cancel its head Batch job and every Batch job
// its pipeline submitted, in every region, then mark the run cancelled (`_lib/scanCancel.ts`).
//
//   POST /api/scan-runs/cancel { run_id } → 200 { head, tasks: { <job>: outcome }, tasks_error?, recorded }      admin
//     400: no run_id;  404: no such run;  409 { error }: not running, or not a scan run
//     500 { error }: no token (nothing was cancelled)
//     503: not configured (`GCP_SA_KEY`, `GCP_PROJECT`, `DISKY_LABELS` with a `deployment`, `DB`)
//
// Each outcome is `cancelled`, `already` (a cancel was in progress, or the job had ended), `gone`, or `{ error }`.
// `tasks_error`: the listing of the scan's jobs failed, so no task was tried (the head's outcome stands). `recorded`:
// the head stopped and the run's row, still `running`, is now `cancelled` (or `failed`, when a step had already failed
// on its own before the cancel), error `cancelled from /scans`; null when a head whose cancel failed leaves the row
// running, or the head recorded its own end first.
import type { D1Database } from '@cloudflare/workers-types'
import { type Ctx, json, requireAdmin } from '../../_lib/auth.js'
import { parseLabels } from '../../_lib/costLabels.js'
import { gcpToken } from '../../_lib/gcp.js'
import { cancelScan, loadRun, refusal } from '../../_lib/scanCancel.js'

type Env = { DB?: D1Database; GCP_SA_KEY?: string; GCP_PROJECT?: string; DISKY_LABELS?: string }

const NO_STORE = { 'cache-control': 'no-store' }

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const env = ctx.env as unknown as Env
  const deployment = parseLabels(env.DISKY_LABELS).deployment
  const missing = !env.GCP_SA_KEY ? 'GCP_SA_KEY' : !env.GCP_PROJECT ? 'GCP_PROJECT' : !deployment ? 'DISKY_LABELS deployment' : !env.DB ? 'DB' : null
  if (missing) return json({ error: `scan cancel not configured (${missing} unset)` }, 503)
  const body = (await ctx.request.json().catch(() => null)) as { run_id?: unknown } | null
  const runId = typeof body?.run_id === 'string' ? body.run_id : ''
  if (!runId) return json({ error: 'run_id required' }, 400)
  const run = await loadRun(env.DB!, runId)
  if (!run) return json({ error: `no run ${runId}` }, 404)
  const why = refusal(run)
  if (why) return json({ error: why }, 409, NO_STORE)
  try {
    const out = await cancelScan({
      db: env.DB!, fetch: (u, i) => fetch(u, i), token: await gcpToken(env.GCP_SA_KEY!), project: env.GCP_PROJECT!,
      deployment: deployment!, run, now: () => Math.floor(Date.now() / 1000),
    })
    return json(out, 200, NO_STORE)
  } catch (e) {
    // 500, not 502: Cloudflare replaces an origin 502 with its own page, losing the detail.
    return json({ error: (e as Error).message ?? String(e) }, 500, NO_STORE)
  }
}
