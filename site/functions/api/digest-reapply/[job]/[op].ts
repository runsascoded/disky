/** Review and apply a digest re-apply's dry run from `/admin/slack` (specs/done/digest-rewrite-review.md): the run's thrds
 * bulk-rewrite plan (`gs://<data>/digest/reapply/<job>.plan.json`, written by `dt-cloud digest -n -j`), served to the
 * page's `PlanView`, and a selection of its edits checked or applied in place, through thrds' `rewriteHandler`.
 *
 *   GET  /api/digest-reapply/<job>/plan → {path, plan}                                                       admin
 *   POST /api/digest-reapply/<job>/check {ids} → {outcomes}   (a dry run: each message re-read, nothing written) admin
 *   POST /api/digest-reapply/<job>/apply {ids} → {outcomes}   (`chat.update`, 1.2 s apart)                   admin
 *
 * At most 10 ids a POST (`postRewrite` batches), JSON and same-origin. Each id is re-read first and refused when it
 * changed since the plan (`stale`); `already` when it shows the planned content. Outcomes of a real apply land in D1
 * (`thrds_rewrite_receipts`, keyed by the job id: recipe `rewrite-receipts`), so a later batch or a re-run skips what's
 * done. Where `SWEEP_DISPATCH` is `"0"` an apply answers 403 (`sweepsOff`: it edits the real digest); the plan and a
 * check only read. The page's apply leaves the digest's converge state alone (spec, "Digest state"). */
import type { D1Database } from '@cloudflare/workers-types'
import { rewriteHandler, SlackClient, verifyPlan } from '@open-athena/thrds'
import { d1Receipts } from '@open-athena/thrds/d1'
import { type Ctx, json, requireAdmin } from '../../../_lib/auth.js'
import { batchConfig, type BatchEnv, notConfigured } from '../../../_lib/batchConfig.js'
import { sweepsOff, SWEEPS_OFF } from '../../../_lib/dispatch.js'
import { gcpToken } from '../../../_lib/gcp.js'
import { JOB_RE, plans, runKey } from '../../digest-reapply.js'

type Env = BatchEnv & { DB?: D1Database; SLACK_BOT_TOKEN?: string }

const OPS = ['plan', 'check', 'apply'] as const
type Op = typeof OPS[number]

const NO_STORE = { 'cache-control': 'no-store' }

/** The run's id and the op, from `/api/digest-reapply/<job>/<op>`, or null. */
export function route(url: string): { job: string; op: Op } | null {
  const m = /^\/api\/digest-reapply\/([^/]+)\/([^/]+)$/.exec(new URL(url).pathname)
  return m && JOB_RE.test(m[1]) && (OPS as readonly string[]).includes(m[2]) ? { job: m[1], op: m[2] as Op } : null
}

export const onRequest = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const r = route(ctx.request.url)
  if (!r) return json({ error: 'not found: /api/digest-reapply/<template>-digest-<stamp>z/{plan,check,apply}' }, 404)
  const writes = ctx.request.method !== 'GET'
  if (r.op === 'apply' && sweepsOff(ctx.env)) return json({ error: SWEEPS_OFF }, 403)
  const env = ctx.env as unknown as Env
  if (!env.DB) return json({ error: 'not configured (no D1 binding)' }, 503)
  const run = await env.DB.prepare('SELECT month, channel, target, dry_run FROM digest_reapply WHERE job_id = ?').bind(r.job)
    .first<{ month: string; channel: string; target: string; dry_run: number }>()
  if (!run) return json({ error: `no run ${r.job}` }, 404)
  if (!plans({ target: run.target, dry_run: !!run.dry_run })) {
    return json({ error: `${r.job} has no plan: only a dry run of the OP or of days writes one` }, 404)
  }
  if (writes && !env.SLACK_BOT_TOKEN) return json({ error: 'not configured (SLACK_BOT_TOKEN)' }, 503)
  const cfg = batchConfig(env, ['DATA_BUCKET'])
  if ('missing' in cfg) return json({ error: notConfigured('digest re-apply review', cfg.missing) }, 503)
  if (!env.GCP_SA_KEY) return json({ error: notConfigured('digest re-apply review', 'GCP_SA_KEY') }, 503)

  const token = await gcpToken(env.GCP_SA_KEY)
  const got = await fetch(`https://storage.googleapis.com/storage/v1/b/${cfg.dataBucket}/o/${encodeURIComponent(runKey(r.job, 'plan.json'))}?alt=media`,
    { headers: { authorization: `Bearer ${token}` } })
  if (got.status === 404) return json({ error: `${r.job} wrote no plan (still running, failed, or from before plans)` }, 404, NO_STORE)
  if (!got.ok) return json({ error: `plan read failed (${got.status})` }, 502, NO_STORE)
  let plan: Awaited<ReturnType<typeof verifyPlan>>
  try {
    plan = await verifyPlan(await got.json())
  } catch (e) {
    return json({ error: `${r.job}'s plan: ${(e as Error).message}` }, 500, NO_STORE)
  }
  const res = await rewriteHandler(ctx.request, {
    plan,
    client: () => new SlackClient({ token: env.SLACK_BOT_TOKEN!, channel: run.channel }),
    receipts: d1Receipts(env.DB, r.job),
    name: `${run.month} · ${run.target} · ${run.channel}`,
  })
  const out = new Response(res.body, res)
  out.headers.set('cache-control', 'no-store')
  return out
}
