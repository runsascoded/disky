// POST /api/sweep/dispatch — launch a deletion run on GCP Batch from /staged
// (specs/staged-delete.md; the "web dispatch bridge" of
// specs/sweep-executor.md).
//
// Body: { plan_id: number, mode: 'dry' | 'real', date: 'YYYY-MM-DD', buckets?: string[], machine?: keyof SWEEP_MACHINES }.
// Admin scope only. The work is the `sweep` executor (`_lib/sweepDispatch.ts`)
// behind `_lib/executor.ts` (shared with `/slack/actions`): a real run needs a
// finished dry-run of exactly the item set it would act on, on its scan. A
// dispatch is announced in the plan's Slack thread (specs/done/staged-slack.md).
import { ADMIN_SCOPE, type Env as AuthEnv, json, requireScope } from '../../_lib/auth.js'
import { dispatchBody, dispatchPlan, type ExecEnv, notifyDispatched } from '../../_lib/executor.js'
import { SWEEP_MACHINES, type SweepMachine } from '../../_lib/sweepMachines.js'

type Env = AuthEnv & ExecEnv

export const onRequestPost = async (ctx: { request: Request; env: Env; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const gated = await requireScope(ctx, ADMIN_SCOPE)
  if (gated instanceof Response) return gated
  const body = (await ctx.request.json().catch(() => null)) as
    | { mode?: string; date?: string; buckets?: string[]; plan_id?: number; machine?: string } | null
  const mode = body?.mode
  const planId = body?.plan_id
  if (!Number.isInteger(planId)) return json({ error: 'plan_id required (an integer)' }, 400)
  if (mode !== 'dry' && mode !== 'real') return json({ error: "mode must be 'dry' or 'real'" }, 400)
  const buckets = body?.buckets ?? []
  if (!Array.isArray(buckets) || buckets.some(b => typeof b !== 'string')) return json({ error: 'buckets must be a list of names' }, 400)
  const siteUrl = new URL(ctx.request.url).origin
  const actor = gated.email ?? 'sweep-console'
  const machine = body?.machine
  if (machine !== undefined && !(machine in SWEEP_MACHINES)) return json({ error: `machine must be one of ${Object.keys(SWEEP_MACHINES).join(', ')}` }, 400)
  const r = await dispatchPlan(ctx.env, { planId: planId!, mode, date: body?.date, actor, siteUrl, buckets, ...(machine ? { machine: machine as SweepMachine } : {}) }, 'sweep')
  if (r.ok) {
    const p = notifyDispatched(ctx.env, r, 'www', siteUrl)
    if (ctx.waitUntil) ctx.waitUntil(p)
    else await p
  }
  const { body: out, status } = dispatchBody(r)
  return json(out, status)
}
