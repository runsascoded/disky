// POST /api/plan-sweep/dispatch — launch a plan-first deletion run on GCP
// Batch from /staged (specs/done/staged-delete.md). Admin only. Body: { plan_id,
// mode: 'dry' | 'real', date: <scan id>, items?: string[] }. The work is the
// `plan-sweep` executor behind `_lib/executor.ts` (shared with `/slack/actions`):
// the run acts on `items` (the rows checked on /staged; the whole plan when
// absent), never more (specs/done/delete-what-you-see.md).
// A dispatch is announced in the plan's Slack thread (specs/done/staged-slack.md).
import { MAX_MIN_SOFT_DELETE_DAYS } from "../../../src/softDelete.js"
import { parseExpected } from "../../_lib/dispatch.js"
import { withPathStore } from "../../_lib/index.js"
import { type Ctx, type Env as AuthEnv, json, requireAdmin } from "../../_lib/auth.js"
import { noteActivity } from "../../_lib/activityHook.js"
import { dispatchBody, dispatchPlan, type ExecEnv, notifyDispatched, planFirstKind } from "../../_lib/executor.js"

type Env = AuthEnv & ExecEnv

export const onRequestPost = async (ctx: Ctx & { env: Env; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const gated = await requireAdmin(ctx)
  if (gated instanceof Response) return gated
  const body = (await ctx.request.json().catch(() => null)) as { plan_id?: number; mode?: string; date?: string; items?: string[]; expected?: unknown; shown_min_soft_delete_days?: number } | null
  const planId = body?.plan_id
  const mode = body?.mode
  if (!Number.isInteger(planId)) return json({ error: "plan_id required" }, 400)
  if (mode !== "dry" && mode !== "real") return json({ error: "mode must be 'dry' or 'real'" }, 400)
  const items = body?.items
  if (items !== undefined && (!Array.isArray(items) || items.some(i => typeof i !== "string"))) return json({ error: "items must be a list of staged prefixes" }, 400)
  const shown = body?.shown_min_soft_delete_days
  if (shown !== undefined && (!Number.isInteger(shown) || shown < 0 || shown > MAX_MIN_SOFT_DELETE_DAYS)) return json({ error: `shown_min_soft_delete_days: an integer 0..${MAX_MIN_SOFT_DELETE_DAYS}` }, 400)
  const siteUrl = new URL(ctx.request.url).origin
  const actor = gated.email ?? gated.name ?? "admin"
  const r = await dispatchPlan(ctx.env, { planId: planId!, mode, date: body?.date, ...(items ? { items } : {}), ...(shown !== undefined ? { shownMinSoftDeleteDays: shown } : {}), actor, siteUrl, expected: parseExpected(body?.expected) }, planFirstKind(ctx.env))
  if (r.ok) {
    // The activity feed's line names the plan and links its Slack thread (`highlights`), not just the route.
    const thread = ctx.env.DB
      ? await ctx.env.DB.prepare("SELECT slack_channel, slack_ts FROM plans WHERE id = ?").bind(r.plan_id).first<{ slack_channel: string | null; slack_ts: string | null }>().catch(() => null)
      : null
    noteActivity(ctx, {
      line: `${r.mode === "real" ? "real deletion" : "dry run"} of plan #${r.plan_id} dispatched (${r.job_id})`,
      plan: r.plan_id, job: r.job_id, mode: r.mode,
      ...(thread?.slack_channel && thread.slack_ts ? { thread: { channel: thread.slack_channel, ts: thread.slack_ts } } : {}),
    })
    // Sized from the interval store for the dates it holds (`withPathStore`), as the plans routes size them.
    // The job has launched: an announcement sized from the raw env beats none, and the response still says it's running.
    const p = withPathStore(ctx).catch(e => { console.log(`dispatch: withPathStore failed: ${(e as Error).message}`); return ctx }).then(c => notifyDispatched(c.env, r, "www", siteUrl))
    if (ctx.waitUntil) ctx.waitUntil(p)
    else await p
  }
  const { body: out, status } = dispatchBody(r)
  return json(out, status)
}
