// POST /api/plan-sweep/dispatch — launch a plan-first deletion run on GCP
// Batch from /staged (specs/done/staged-delete.md). Admin only. Body: { plan_id,
// mode: 'dry' | 'real', date: <scan id>, items?: string[] }. The work is the
// `plan-sweep` executor behind `_lib/executor.ts` (shared with `/slack/actions`):
// the run acts on `items` (the rows checked on /staged; the whole plan when
// absent), never more (specs/delete-what-you-see.md).
// A dispatch is announced in the plan's Slack thread (specs/done/staged-slack.md).
import { type Ctx, type Env as AuthEnv, json, requireAdmin } from "../../_lib/auth.js"
import { dispatchBody, dispatchPlan, type ExecEnv, notifyDispatched, planFirstKind } from "../../_lib/executor.js"

type Env = AuthEnv & ExecEnv

export const onRequestPost = async (ctx: Ctx & { env: Env; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const gated = await requireAdmin(ctx)
  if (gated instanceof Response) return gated
  const body = (await ctx.request.json().catch(() => null)) as { plan_id?: number; mode?: string; date?: string; items?: string[] } | null
  const planId = body?.plan_id
  const mode = body?.mode
  if (!Number.isInteger(planId)) return json({ error: "plan_id required" }, 400)
  if (mode !== "dry" && mode !== "real") return json({ error: "mode must be 'dry' or 'real'" }, 400)
  const items = body?.items
  if (items !== undefined && (!Array.isArray(items) || items.some(i => typeof i !== "string"))) return json({ error: "items must be a list of staged prefixes" }, 400)
  const siteUrl = new URL(ctx.request.url).origin
  const actor = gated.email ?? gated.name ?? "admin"
  const r = await dispatchPlan(ctx.env, { planId: planId!, mode, date: body?.date, ...(items ? { items } : {}), actor, siteUrl }, planFirstKind(ctx.env))
  if (r.ok) {
    const p = notifyDispatched(ctx.env, r, "www", siteUrl)
    if (ctx.waitUntil) ctx.waitUntil(p)
    else await p
  }
  const { body: out, status } = dispatchBody(r)
  return json(out, status)
}
