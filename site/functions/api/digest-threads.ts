/** A digest thread's reply style per month (specs/digest-thread-variant.md; D1 `digest_threads`, migration
 * `digest-thread-variant`): an admin's switch on `/admin/slack`, which the job converges to over the thread's live style.
 *
 *   GET /api/digest-threads?channel=<id>&month=YYYY-MM → {thread: {channel, month, variant, deletes, plan_job, updated_by, updated_at} | null}   viewer
 *   PUT /api/digest-threads {channel, month, variant, plan_job} → {thread}                                                                    admin
 *
 * GET is viewer-gated: the job reads it with its grant. A path-restricted link is refused (`RESTRICTED_BLOCKED`): the row
 * names a channel and the digest's message ids. A PUT names the dry run it was reviewed from (`plan_job`, a
 * `digest_reapply` run: a whole-thread preview under the candidate `variant`), and the row's `deletes` come from that
 * run's switch plan (`gs://<data>/digest/reapply/<plan_job>.switch.json`), never from the request: a switch deletes
 * only replies an admin saw listed. Refused (409) when the plan is another thread's or style's, or older than the row
 * it would replace. Each PUT logs its old and new row to `admin_edits`. Where `SWEEP_DISPATCH` is `"0"` a PUT answers
 * 403: the next converge acts on it. A D1 without the recipe (`digest-thread-variant`) reads as no row (each thread keeps
 * its live style), and a PUT there is a 503 naming the recipe. */
import type { D1Database } from '@cloudflare/workers-types'
import { type Ctx, json, requireAdmin, requireViewer } from '../_lib/auth.js'
import { batchConfig, type BatchEnv, notConfigured } from '../_lib/batchConfig.js'
import { sweepsOff, SWEEPS_OFF } from '../_lib/dispatch.js'
import { gcpToken } from '../_lib/gcp.js'
import { TEMPLATES, VARIANTS } from '../../src/digest/templates.js'
import { hasThreadVariant, recipeMissing } from '../_lib/digestRecipe.js'

export const TABLE = 'digest_threads'
/** `digest_switch.PLAN_VERSION`: keep identical. */
export const PLAN_VERSION = 1

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/
const CHANNEL_RE = /^[CDG][A-Z0-9]{8,12}$/
const JOB_RE = /^[a-z0-9-]+-digest-\d{8}-\d{6}z$/
const STYLES = [...new Set(TEMPLATES.flatMap(t => VARIANTS[t]))]
const NO_STORE = { 'cache-control': 'no-store' }

export interface DigestThread { channel: string; month: string; variant: string; deletes: string[]; plan_job: string; updated_by: string; updated_at: number }
/** A switch plan (`digest_switch.switch_plan`), as the dry run wrote it. */
export interface SwitchPlan {
  digest_switch: number; month: string; channel: string; from: string; to: string; at: string
  post: { key: string; scan: string; headline: string }[]
  delete: { key: string; ts: string }[]
}

const missingTable = (e: unknown): boolean => /no such table/.test(String((e as Error)?.message))

/** A switch plan read back, or why it isn't one. */
export function parseSwitchPlan(raw: unknown): SwitchPlan | string {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return 'not a switch plan'
  const p = raw as Record<string, unknown>
  if (p.digest_switch !== PLAN_VERSION) return `not a switch plan (version ${PLAN_VERSION})`
  for (const k of ['month', 'channel', 'from', 'to', 'at']) if (typeof p[k] !== 'string') return `switch plan: ${k}`
  if (!Array.isArray(p.delete) || !p.delete.every(d => typeof d === 'object' && d !== null && typeof (d as { ts?: unknown }).ts === 'string')) return 'switch plan: delete'
  if (!Array.isArray(p.post)) return 'switch plan: post'
  return p as unknown as SwitchPlan
}

export async function getThread(db: D1Database, channel: string, month: string): Promise<DigestThread | null> {
  try {
    const r = await db.prepare(`SELECT channel, month, variant, deletes, plan_job, updated_by, updated_at FROM ${TABLE} WHERE channel = ? AND month = ?`)
      .bind(channel, month).first<Omit<DigestThread, 'deletes'> & { deletes: string }>()
    return r && { ...r, deletes: JSON.parse(r.deletes) as string[] }
  } catch (e) {
    if (missingTable(e)) return null
    throw e
  }
}

/** The PUT body, or why it's refused. */
export function parsePut(b: unknown): { channel: string; month: string; variant: string; plan_job: string } | string {
  if (typeof b !== 'object' || b === null || Array.isArray(b)) return 'body: a JSON object'
  const { channel, month, variant, plan_job } = b as Record<string, unknown>
  if (typeof channel !== 'string' || !CHANNEL_RE.test(channel)) return 'channel: a Slack channel id'
  if (typeof month !== 'string' || !MONTH_RE.test(month)) return 'month: YYYY-MM'
  if (typeof variant !== 'string' || !STYLES.includes(variant)) return `variant: ${STYLES.join(' | ')}`
  if (typeof plan_job !== 'string' || !JOB_RE.test(plan_job)) return 'plan_job: the reviewed dry run (<template>-digest-<stamp>z)'
  return { channel, month, variant, plan_job }
}

/** Why `plan` (the dry run `run`'s) can't back a switch of (`channel`, `month`) to `variant`, or null. `prev`: the row it
 *  would replace. */
export function planRefusal(req: { channel: string; month: string; variant: string }, run: { month: string; channel: string; target: string; dry_run: number; variant: string | null; ts: number } | null,
  plan: SwitchPlan | string | null, prev: DigestThread | null): string | null {
  if (!run) return 'no such dry run'
  if (!run.dry_run || run.target !== 'thread' || run.variant !== req.variant) return `that run isn't a whole-thread preview under ${req.variant}`
  if (run.channel !== req.channel || run.month !== req.month) return `that run previewed ${run.month} in ${run.channel}`
  if (plan === null) return 'that run wrote no switch plan (still running, failed, or the thread is already that style)'
  if (typeof plan === 'string') return plan
  if (plan.channel !== req.channel || plan.month !== req.month || plan.to !== req.variant) return `its plan switches ${plan.month} in ${plan.channel} to ${plan.to}`
  if (prev && prev.updated_at > run.ts) return `the thread's style was set after that preview (by ${prev.updated_by}); preview again`
  return null
}

export async function putThread(db: D1Database, row: DigestThread): Promise<DigestThread> {
  const old = await getThread(db, row.channel, row.month)
  await db.batch([
    db.prepare(`INSERT INTO ${TABLE} (channel, month, variant, deletes, plan_job, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (channel, month) DO UPDATE SET variant = excluded.variant, deletes = excluded.deletes, plan_job = excluded.plan_job,
                  updated_by = excluded.updated_by, updated_at = excluded.updated_at`)
      .bind(row.channel, row.month, row.variant, JSON.stringify(row.deletes), row.plan_job, row.updated_by, row.updated_at),
    db.prepare('INSERT INTO admin_edits (tbl, pk, action, who, ts, old_json, new_json) VALUES (?, ?, ?, ?, ?, ?, ?)')
      .bind(TABLE, `${row.channel}/${row.month}`, old ? 'update' : 'insert', row.updated_by, row.updated_at, old ? JSON.stringify(old) : null, JSON.stringify(row)),
  ])
  return row
}

type Env = BatchEnv & { DB?: D1Database }

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const q = new URL(ctx.request.url).searchParams
  const [channel, month] = [q.get('channel') ?? '', q.get('month') ?? '']
  if (!CHANNEL_RE.test(channel)) return json({ error: 'channel: a Slack channel id' }, 400)
  if (!MONTH_RE.test(month)) return json({ error: 'month: YYYY-MM' }, 400)
  const db = (ctx.env as unknown as Env).DB
  return json({ thread: db ? await getThread(db, channel, month) : null }, 200, NO_STORE)
}

export const onRequestPut = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  if (sweepsOff(ctx.env)) return json({ error: SWEEPS_OFF }, 403)
  const req = parsePut(await ctx.request.json().catch(() => null))
  if (typeof req === 'string') return json({ error: req }, 400)
  const env = ctx.env as unknown as Env
  if (!env.DB) return json({ error: 'not configured (no D1 binding)' }, 503)
  if (!(await hasThreadVariant(env.DB))) return recipeMissing()
  const cfg = batchConfig(env, ['DATA_BUCKET'])
  if ('missing' in cfg) return json({ error: notConfigured('digest thread switch', cfg.missing) }, 503)
  if (!env.GCP_SA_KEY) return json({ error: notConfigured('digest thread switch', 'GCP_SA_KEY') }, 503)
  const run = await env.DB.prepare('SELECT month, channel, target, dry_run, variant, ts FROM digest_reapply WHERE job_id = ?').bind(req.plan_job)
    .first<{ month: string; channel: string; target: string; dry_run: number; variant: string | null; ts: number }>()
  let plan: SwitchPlan | string | null = null
  if (run) {
    const token = await gcpToken(env.GCP_SA_KEY)
    const got = await fetch(`https://storage.googleapis.com/storage/v1/b/${cfg.dataBucket}/o/${encodeURIComponent(`digest/reapply/${req.plan_job}.switch.json`)}?alt=media`,
      { headers: { authorization: `Bearer ${token}` } })
    if (got.ok) plan = parseSwitchPlan(await got.json().catch(() => null))
    else if (got.status !== 404) return json({ error: `switch plan read failed (${got.status})` }, 502, NO_STORE)
  }
  const prev = await getThread(env.DB, req.channel, req.month)
  const why = planRefusal(req, run, plan, prev)
  if (why) return json({ error: why }, run ? 409 : 404, NO_STORE)
  const row: DigestThread = { ...req, deletes: (plan as SwitchPlan).delete.map(d => d.ts), updated_by: id.email ?? id.name ?? 'admin', updated_at: Math.floor(Date.now() / 1000) }
  return json({ thread: await putThread(env.DB, row) }, 200, NO_STORE)
}
