/**
 * POST /slack/actions — Slack interactivity for the staged-deletion thread
 * (specs/done/staged-slack.md). Buttons on the plan's parent message: Dry-run,
 * Delete for real; on each stage reply: Reject (every batch the reply covers —
 * a coalesced reply covers a stager's recent batches; per-batch rejection is
 * www's). ("Open in www" is a URL
 * button; Slack still calls here, and it's acked.)
 *
 * Authority is the site's, not Slack's: the request is verified with the
 * app's signing secret, the clicking user is mapped to their email
 * (`users.info`), and the same rules as www apply — dispatch needs `admin`
 * (staff domain or an `admin_emails` row); rejecting needs admin or being the one
 * who staged the batches. Dispatch goes through the deployment's
 * executor (`EXECUTOR`, `_lib/executor.ts`) — the same code path as /staged,
 * so a real deletion needs a finished dry-run of exactly the current item
 * set and runs against that dry-run's scan. Slack wants an answer within 3 s,
 * so this acks at once and does the work after the response; outcomes go to
 * the thread (everyone) or back to the clicker ephemerally (refusals, errors).
 * Unconfigured (no signing secret or D1) it answers 503 and does nothing.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { type Env as AuthEnv, isAdmin } from '../_lib/auth.js'
import { dispatchPlan, type ExecEnv, executorOf, notifyDispatched } from '../_lib/executor.js'
import { audit } from '../_lib/plans.js'
import { slackUserEmail, verifySlackSignature } from '../_lib/slack.js'
import { notifyPlan, planGate, slackNames, updateReplies } from '../_lib/stagedSlack.js'

type Env = AuthEnv & ExecEnv
interface PagesCtx { request: Request; env: Env; waitUntil: (p: Promise<unknown>) => void }

interface BlockActions {
  type: string
  user: { id: string }
  response_url?: string
  actions: { action_id: string; value?: string }[]
}

async function tell(url: string | undefined, text: string): Promise<void> {
  if (!url) return
  await fetch(url, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ response_type: 'ephemeral', replace_original: false, text }) })
    .catch(() => undefined)
}

async function latestScan(db: D1Database): Promise<string | null> {
  const r = await db.prepare("SELECT max(date) AS d FROM index_schema WHERE variant = 'path'").first<{ d: string | null }>()
  return r?.d ?? null
}

async function handle(env: Env, p: BlockActions, siteUrl: string): Promise<void> {
  const db = env.DB!
  const a = p.actions[0]
  const email = await slackUserEmail(env, p.user.id)
  if (!email) return tell(p.response_url, "Couldn't resolve your Slack account to an email (the app needs `users:read.email`).")
  const admin = await isAdmin(env, email)

  if (a.action_id === 'staged_reject') {
    // `<plan>:r<reply>` rejects every batch a coalesced stage reply covers;
    // `<plan>:<batch>` (replies posted before coalescing) one batch.
    const [planS, ref = ''] = (a.value ?? '').split(':')
    const planId = Number(planS)
    const replyId = ref.startsWith('r') ? Number(ref.slice(1)) : null
    const batchId = replyId == null ? Number(ref) : null
    if (!Number.isInteger(planId) || !Number.isInteger(replyId ?? batchId) || !ref) return tell(p.response_url, 'Bad button value.')
    const batches = (replyId != null
      ? await db.prepare('SELECT id, created_by, reply_id FROM stage_batches WHERE reply_id = ? AND plan_id = ? ORDER BY id').bind(replyId, planId).all<{ id: number; created_by: string; reply_id: number | null }>()
      : await db.prepare('SELECT id, created_by, reply_id FROM stage_batches WHERE id = ? AND plan_id = ?').bind(batchId, planId).all<{ id: number; created_by: string; reply_id: number | null }>()).results
    const what = replyId != null ? 'These batches' : `Batch #${batchId}`
    if (!batches.length) return tell(p.response_url, `${what} no longer ${replyId != null ? 'exist' : 'exists'}.`)
    if (!admin && batches.some(b => b.created_by.toLowerCase() !== email)) return tell(p.response_url, 'Only an admin or the person who staged a batch can reject it.')
    const plan = await db.prepare('SELECT state FROM plans WHERE id = ?').bind(planId).first<{ state: string }>()
    if (plan?.state !== 'open') return tell(p.response_url, 'That plan is closed.')
    let n = 0, k = 0
    for (const b of batches) {
      const items = (await db.prepare('SELECT prefix FROM plan_items WHERE plan_id = ? AND batch_id = ?').bind(planId, b.id).all<{ prefix: string }>()).results
      if (!items.length) continue
      await db.prepare('DELETE FROM plan_items WHERE plan_id = ? AND batch_id = ?').bind(planId, b.id).run()
      await audit(db, 'plan_items', String(planId), 'delete', email, { batch_id: b.id, prefixes: items.map(i => i.prefix), via: 'slack' }, null)
      n += items.length; k++
    }
    if (!n) return tell(p.response_url, `${what} ${replyId != null ? 'have' : 'has'} nothing left staged.`)
    const name = (await slackNames(env, [email]))[email] ?? email.replace(/@.*$/, '')
    await notifyPlan(env, db, planId, siteUrl, { text: `:no_entry_sign: ${name} rejected ${k} ${k === 1 ? 'batch' : 'batches'} (${n.toLocaleString('en-US')} ${n === 1 ? 'path' : 'paths'} unstaged)` })
    const rid = replyId ?? batches[0].reply_id
    if (rid != null) await updateReplies(env, db, planId, siteUrl, { replyIds: [rid] })
    return
  }

  if (a.action_id !== 'staged_dry' && a.action_id !== 'staged_real') return
  if (!env.GCP_SA_KEY) return tell(p.response_url, 'Dispatch is not configured for this deployment; use www.')
  if (!admin) return tell(p.response_url, 'Only admins can dispatch runs.')
  const kind = executorOf(env)
  const [planId, clickedDigest] = (a.value ?? '').split(':')
  const id = Number(planId)
  if (!Number.isInteger(id)) return tell(p.response_url, 'Bad button value.')
  const g = await planGate(db, id)
  if (!g) return tell(p.response_url, 'That plan no longer exists.')
  if (g.closed) return tell(p.response_url, 'That plan is closed.')

  let mode: 'dry' | 'real'
  let date: string | undefined
  if (a.action_id === 'staged_dry') {
    mode = 'dry'
    date = (await latestScan(db)) ?? undefined
    if (!date) return tell(p.response_url, 'No indexed scan to run against.')
  } else {
    mode = 'real'
    // The gate itself (and the dry-run's scan) is the executor seam's; this
    // only refuses a button drawn for an older item set.
    if (clickedDigest !== g.digest) return tell(p.response_url, 'Not deleting: the plan changed after that button was drawn. Check the updated message.')
  }
  const r = await dispatchPlan(env, { planId: id, mode, date, actor: email, siteUrl }, kind)
  if (!r.ok) return tell(p.response_url, `Dispatch refused: ${r.error}`)
  await notifyDispatched(env, r, 'Slack', siteUrl)
}

export const onRequestPost = async (ctx: PagesCtx): Promise<Response> => {
  const { env, request } = ctx
  if (!env.SLACK_SIGNING_SECRET || !env.DB) return new Response('slack actions not configured', { status: 503 })
  const body = await request.text()
  const ok = await verifySlackSignature(env.SLACK_SIGNING_SECRET, request.headers.get('x-slack-request-timestamp'), request.headers.get('x-slack-signature'), body)
  if (!ok) return new Response('bad signature', { status: 401 })
  const raw = new URLSearchParams(body).get('payload')
  let p: BlockActions
  try { p = JSON.parse(raw ?? '') as BlockActions } catch { return new Response('bad payload', { status: 400 }) }
  if (p.type !== 'block_actions' || !p.actions?.length) return new Response('', { status: 200 })
  if (p.actions[0].action_id === 'staged_open') return new Response('', { status: 200 })
  const siteUrl = new URL(request.url).origin
  ctx.waitUntil(handle(env, p, siteUrl).catch(e => tell(p.response_url, `Something went wrong: ${(e as Error).message}`)))
  return new Response('', { status: 200 })
}
