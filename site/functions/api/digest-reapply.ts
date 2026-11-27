/** Re-apply the Slack digest to chosen messages of a month's thread, from `/admin/slack` (specs/digest-runway-marks.md
 * §4; D1 `digest_reapply`, migration `digest-settings`). Each run is a Batch job on the scan image running the digest
 * CLI with its selectors — `-o` (the OP), `-y A..B` (those days' posted replies), or no selector (the whole thread, as
 * the cron) — dry (`-n`: what it would edit, nothing posted) or for real, optionally under candidate settings (`-p`,
 * `-q`) instead of the live ones. The CLI's output lands at `gs://<data>/digest/reapply/<job>.txt`; a dry run of the OP
 * or of days also writes its edits as a thrds bulk-rewrite plan (`-j`) beside it, `<job>.plan.json`, which `/admin/slack`
 * reviews and applies a selection of (`digest-reapply/[job]/[op].ts`; specs/done/digest-rewrite-review.md).
 *
 *   POST /api/digest-reapply {month, channel?, target, dry_run, params?, quota_marks?, variant?} → {run}   admin
 *   GET  /api/digest-reapply → {runs: [run…]} (newest first, 20)                                              admin
 *   GET  /api/digest-reapply?job=<id> → {run, status, output: string | null, switch: plan | null}            admin
 *
 * `target`: `thread` | `op` | `YYYY-MM-DD` | `YYYY-MM-DD..YYYY-MM-DD` (days of `month`). `channel` defaults to
 * `DIGEST_CHANNEL` (the deployment's digest channel); another (a DM, to try a format first) is any Slack channel id.
 * `variant`: a candidate reply style (`-V`). A whole-thread dry run naming another style than the thread's live one is a
 * switch's preview: it writes the switch plan (`<job>.switch.json`, `-j`), which `PUT /api/digest-threads` stores
 * with the admin's switch (specs/digest-thread-variant.md). The job runs as `DT_JOB_NAME` / `DT_JOB_REGION`, so the
 * thread lease it holds names a job the next taker can see has ended.
 *
 * `DIGEST_REAPPLY` (JSON, `ReapplyConfig`) names what's the deployment's: unset, a POST answers 503. The job is its own
 * (`reapplySpec`): its image, the batch config's `REAPPLY_NEEDS` and exactly its secrets, so a deployment can run it
 * with no plan-sweep executor configured. Where `SWEEP_DISPATCH` is `"0"` a POST answers 403 (`sweepsOff`): the job
 * edits the real digest; the GETs only read. */
import type { D1Database } from '@cloudflare/workers-types'
import { type Ctx, json, requireAdmin } from '../_lib/auth.js'
import { batchConfig, type BatchConfig, type BatchEnv, notConfigured } from '../_lib/batchConfig.js'
import { batchTaskSpec, jobStamp, secretRef, submitBatch } from '../_lib/cwBatch.js'
import { sweepsOff, SWEEPS_OFF } from '../_lib/dispatch.js'
import { batchJobsUrl, gcpToken } from '../_lib/gcp.js'
import { parseParams } from '../../src/digest/codec.js'
import { canonicalTemplate, TEMPLATES, VARIANTS } from '../../src/digest/templates.js'
import { QUOTA_MARKS } from './digest-settings.js'
import { parseSwitchPlan } from './digest-threads.js'

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/
const DAY_RE = /^(\d{4}-\d{2})-(\d{2})$/
const CHANNEL_RE = /^[CDG][A-Z0-9]{8,12}$/
/** A run's job id, `<template>-digest-<stamp>z`: runs from before the templates' rename carry their old name (`cw-…`). */
export const JOB_RE = /^[a-z0-9-]+-digest-\d{8}-\d{6}z$/
const NAME_RE = /^[\w./{}$-]+$/
/** A `root` that names its store: used as given (an R2 deployment's thread state and snapshots aren't on the data bucket). */
const ROOT_URL_RE = /^(gs|r2|s3):\/\/[\w./{}$-]+$/
/** An image reference: `<registry>/<path>` by tag or digest. */
const IMAGE_RE = /^[a-z0-9.-]+(:\d+)?\/[\w./-]+(:[\w.-]+|@sha256:[0-9a-f]{64})$/

/** What a re-apply job needs of the batch config. Not `SWEEP_IMAGE` / `SWEEP_S3_ENDPOINT`: enabling re-apply never
 *  configures plan-sweep dispatch (`PLAN_SWEEP_NEEDS`). */
export const REAPPLY_NEEDS = ['GCP_PROJECT', 'BATCH_REGION', 'DATA_BUCKET'] as const

/** `DIGEST_REAPPLY`: the digest CLI's `template` (an old name, `cw` / `gcs`, read as its name), `config` and `root` (a
 *  `gs://` / `r2://` / `s3://` URL as given, else a path under the data bucket), the job's `image` (optional: else
 *  `SWEEP_IMAGE`, where the sweep executor runs the scan image), the `env_script` the task sources for the scan steps' environment, the R2 bucket its preview bundle
 *  goes to, and env var -> Secret Manager name. */
export interface ReapplyConfig { template: string; config: string; root: string; env_script: string; r2_bucket: string; image?: string; secrets: Record<string, string> }

/** The deployment's config, or why there is none. */
export function reapplyConfig(raw: string | undefined): ReapplyConfig | string {
  if (!raw) return 'DIGEST_REAPPLY'
  let c: Partial<ReapplyConfig>
  try {
    c = JSON.parse(raw) as Partial<ReapplyConfig>
  } catch {
    return 'DIGEST_REAPPLY: not JSON'
  }
  for (const k of ['template', 'config', 'root', 'env_script', 'r2_bucket'] as const) {
    const v = c[k]
    if (typeof v !== 'string' || !(NAME_RE.test(v) || (k === 'root' && ROOT_URL_RE.test(v)))) return `DIGEST_REAPPLY.${k}`
  }
  if (c.image !== undefined && (typeof c.image !== 'string' || !IMAGE_RE.test(c.image))) return 'DIGEST_REAPPLY.image'
  const template = canonicalTemplate(c.template)
  if (!template) return 'DIGEST_REAPPLY.template'
  const secrets = c.secrets
  if (typeof secrets !== 'object' || secrets === null || Object.entries(secrets).some(([k, v]) => !/^[A-Z][A-Z0-9_]*$/.test(k) || typeof v !== 'string' || !/^[\w-]+$/.test(v))) return 'DIGEST_REAPPLY.secrets'
  return { ...c, template } as ReapplyConfig
}

export interface ReapplyReq { month: string; channel: string; target: string; dry_run: boolean; params: string | null; quota_marks: string | null; variant: string | null }
export interface ReapplyRun extends ReapplyReq { job_id: string; actor: string; ts: number; out: string }

const inMonth = (d: string, month: string): boolean => {
  const m = DAY_RE.exec(d)
  return !!m && m[1] === month && !Number.isNaN(Date.parse(`${d}T00:00:00Z`)) && new Date(`${d}T00:00:00Z`).toISOString().startsWith(d)
}

/** The request (its channel defaulted), or why it's refused. */
export function parseReapply(b: unknown, defaultChannel: string | undefined): ReapplyReq | string {
  if (typeof b !== 'object' || b === null || Array.isArray(b)) return 'body: a JSON object'
  const { month, channel, target, dry_run, params, quota_marks, variant } = b as Record<string, unknown>
  if (typeof month !== 'string' || !MONTH_RE.test(month)) return 'month: YYYY-MM'
  const ch = channel ?? defaultChannel
  if (typeof ch !== 'string' || !CHANNEL_RE.test(ch)) return 'channel: a Slack channel id (no DIGEST_CHANNEL default here)'
  if (typeof target !== 'string') return 'target: thread | op | YYYY-MM-DD | YYYY-MM-DD..YYYY-MM-DD'
  if (target !== 'thread' && target !== 'op') {
    const [a, z, ...rest] = target.split('..')
    if (rest.length || !inMonth(a, month) || (z !== undefined && (!inMonth(z, month) || z < a))) {
      return `target: thread | op | a day of ${month} | an ordered range of its days (A..B)`
    }
  }
  if (typeof dry_run !== 'boolean') return 'dry_run: a boolean'
  if (params !== undefined && params !== null) {
    if (typeof params !== 'string') return 'params: a string'
    try {
      parseParams(params)
    } catch (e) {
      return `params: ${(e as Error).message}`
    }
  }
  if (quota_marks !== undefined && quota_marks !== null && (typeof quota_marks !== 'string' || !QUOTA_MARKS.includes(quota_marks))) {
    return `quota_marks: ${QUOTA_MARKS.join(' | ')}`
  }
  const styles = [...new Set(TEMPLATES.flatMap(t => VARIANTS[t]))]
  if (variant !== undefined && variant !== null && (typeof variant !== 'string' || !styles.includes(variant))) return `variant: ${styles.join(' | ')}`
  return {
    month, channel: ch, target, dry_run, params: (params as string | null | undefined) ?? null, quota_marks: (quota_marks as string | null | undefined) ?? null,
    variant: (variant as string | null | undefined) ?? null,
  }
}

/** A run's object key under the data bucket: its CLI output (`txt`), its rewrite plan (`plan.json`), or its switch plan
 *  (`switch.json`). */
export const runKey = (job: string, ext: 'txt' | 'plan.json' | 'switch.json'): string => `digest/reapply/${job}.${ext}`

/** Whether a run writes a rewrite plan: a dry run with a selector (a whole-thread run converges, which also posts). */
export const plans = (r: Pick<ReapplyReq, 'target' | 'dry_run'>): boolean => r.dry_run && r.target !== 'thread'

/** Whether a run may write a switch plan: a whole-thread dry run under a candidate variant (it writes one when the
 *  thread's live variant is another). */
export const switches = (r: Pick<ReapplyReq, 'target' | 'dry_run' | 'variant'>): boolean => r.dry_run && r.target === 'thread' && r.variant !== null

/** The digest CLI's arguments for a request (after `reapplyScript`'s `dt-cloud digest -T … -C … -r …`); `plan`: where a
 *  run that `plans` writes its rewrite plan, `switchPlan` where one that `switches` writes its switch plan. */
export function reapplyArgs(r: ReapplyReq, plan?: string, switchPlan?: string): string[] {
  const sel = r.target === 'thread' ? [] : r.target === 'op' ? ['-o'] : ['-y', r.target.includes('..') ? r.target : `${r.target}..${r.target}`]
  const out = plan !== undefined && plans(r) ? ['-j', plan] : switchPlan !== undefined && switches(r) ? ['-j', switchPlan] : []
  return ['-m', r.month, '-c', r.channel, ...sel, ...(r.dry_run ? ['-n'] : []), ...out,
    ...(r.params !== null ? ['-p', r.params] : []), ...(r.quota_marks !== null ? ['-q', r.quota_marks] : []), ...(r.variant !== null ? ['-V', r.variant] : [])]
}

const sq = (s: string): string => `'${s.replace(/'/g, `'\\''`)}'`

/** The Batch task's script: the scan steps' environment (`env_script`), then the digest CLI over `root` (a URL as given,
 *  else under the data bucket), its output teed to the run's file on the data bucket's mount. */
export const reapplyScript = (args: string[], outMnt: string, c: ReapplyConfig): string => [
  'set -euo pipefail',
  'cd "${APP_DIR:-/app}"',
  `. ${sq(c.env_script)}`,
  '{ set +x; } 2>/dev/null',
  `OUT=${sq(outMnt)}`,
  'mkdir -p "$(dirname "$OUT")"',
  `dt-cloud digest -T ${sq(c.template)} -C ${sq(c.config)} -r ${ROOT_URL_RE.test(c.root) ? '' : '"gs://$DATA_BUCKET/"'}${sq(c.root)} ${args.map(sq).join(' ')} 2>&1 | tee "$OUT"`,
].join('\n')

/** A run's Batch job (`batchTaskSpec`): `script` on `image` as `jobSa`, with the run's identity, the R2 bucket its
 *  preview bundle goes to (`R2_BUCKET`: the env script sets up R2 only with it) and exactly `DIGEST_REAPPLY.secrets`, no
 *  other store credentials. */
export function reapplySpec(cfg: BatchConfig, jobSa: string, image: string, script: string, jobId: string, siteUrl: string, c: ReapplyConfig): unknown {
  return batchTaskSpec(cfg, jobSa, image, script, {
    JOB_ID: jobId, DT_JOB_NAME: jobId, DT_JOB_REGION: cfg.region, SITE_URL: siteUrl, R2_BUCKET: c.r2_bucket,
    ...(cfg.cfAccountId ? { CLOUDFLARE_ACCOUNT_ID: cfg.cfAccountId } : {}),
  }, Object.fromEntries(Object.entries(c.secrets).map(([k, v]) => [k, secretRef(cfg, v)])), 'digest')
}

type Env = BatchEnv & { DB?: D1Database; JOB_SA?: string; DIGEST_CHANNEL?: string; DIGEST_REAPPLY?: string }

const COLS = 'job_id, month, channel, target, dry_run, params, quota_marks, variant, actor, ts, out'
const row = (r: Record<string, unknown>): ReapplyRun => ({ ...(r as unknown as ReapplyRun), dry_run: !!r.dry_run })

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  if (sweepsOff(ctx.env)) return json({ error: SWEEPS_OFF }, 403)
  const env = ctx.env as unknown as Env
  if (!env.DB) return json({ error: 'not configured (no D1 binding)' }, 503)
  if (!env.GCP_SA_KEY || !env.JOB_SA) return json({ error: 'not configured (GCP_SA_KEY / JOB_SA)' }, 503)
  const cfg = batchConfig(env, REAPPLY_NEEDS)
  if ('missing' in cfg) return json({ error: notConfigured('digest re-apply', cfg.missing) }, 503)
  const c = reapplyConfig(env.DIGEST_REAPPLY)
  if (typeof c === 'string') return json({ error: notConfigured('digest re-apply', c) }, 503)
  const image = c.image ?? env.SWEEP_IMAGE
  if (!image) return json({ error: notConfigured('digest re-apply', 'DIGEST_REAPPLY.image') }, 503)
  const req = parseReapply(await ctx.request.json().catch(() => null), env.DIGEST_CHANNEL)
  if (typeof req === 'string') return json({ error: req }, 400)

  const jobId = `${c.template}-digest-${jobStamp()}z`
  const out = `gs://${cfg.dataBucket}/${runKey(jobId, 'txt')}`
  const mnt = (ext: 'txt' | 'plan.json' | 'switch.json') => `/gcs/${cfg.dataBucket}/${runKey(jobId, ext)}`
  const script = reapplyScript(reapplyArgs(req, mnt('plan.json'), mnt('switch.json')), mnt('txt'), c)
  const spec = reapplySpec(cfg, env.JOB_SA, image, script, jobId, new URL(ctx.request.url).origin, c)
  const token = await gcpToken(env.GCP_SA_KEY)
  const sub = await submitBatch(cfg, token, jobId, spec)
  if (!sub.ok) return json({ error: `batch submit failed (${sub.status})`, detail: sub.text.slice(0, 1000) }, 500)
  const run: ReapplyRun = { ...req, job_id: jobId, actor: id.email ?? id.name ?? 'admin', ts: Math.floor(Date.now() / 1000), out }
  await env.DB.prepare(`INSERT INTO digest_reapply (${COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .bind(run.job_id, run.month, run.channel, run.target, run.dry_run ? 1 : 0, run.params, run.quota_marks, run.variant, run.actor, run.ts, run.out).run()
  return json({ run }, 200, { 'cache-control': 'no-store' })
}

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const env = ctx.env as unknown as Env
  if (!env.DB) return json({ runs: [] }, 200, { 'cache-control': 'no-store' })
  const job = new URL(ctx.request.url).searchParams.get('job')
  if (job === null) {
    const rs = await env.DB.prepare(`SELECT ${COLS} FROM digest_reapply ORDER BY ts DESC LIMIT 20`).all()
    return json({ runs: (rs.results ?? []).map(row) }, 200, { 'cache-control': 'no-store' })
  }
  if (!JOB_RE.test(job)) return json({ error: 'job: <template>-digest-<stamp>z' }, 400)
  const r = await env.DB.prepare(`SELECT ${COLS} FROM digest_reapply WHERE job_id = ?`).bind(job).first()
  if (!r) return json({ error: `no run ${job}` }, 404)
  const cfg = batchConfig(env, ['GCP_PROJECT', 'DATA_BUCKET'])
  const run = row(r)
  if ('missing' in cfg || !env.GCP_SA_KEY) return json({ run, status: null, output: null, switch: null }, 200, { 'cache-control': 'no-store' })
  const token = await gcpToken(env.GCP_SA_KEY)
  const auth = { authorization: `Bearer ${token}` }
  const obj = (ext: 'txt' | 'switch.json') => fetch(`https://storage.googleapis.com/storage/v1/b/${cfg.dataBucket}/o/${encodeURIComponent(runKey(job, ext))}?alt=media`, { headers: auth })
  const [jr, or, sr] = await Promise.all([
    fetch(`${batchJobsUrl(cfg, cfg.region)}/${job}`, { headers: auth }),
    obj('txt'),
    switches(run) ? obj('switch.json') : Promise.resolve(null),
  ])
  const status = jr.ok ? ((await jr.json()) as { status?: { state?: string } }).status?.state ?? null : null
  // a switch preview whose thread is already the candidate's style writes no plan (a plain dry run): `switch` null
  const plan = sr?.ok ? parseSwitchPlan(await sr.json().catch(() => null)) : null
  return json({ run, status, output: or.ok ? await or.text() : null, switch: typeof plan === 'string' ? null : plan }, 200, { 'cache-control': 'no-store' })
}
