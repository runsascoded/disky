/** Re-apply the Slack digest to chosen messages of a month's thread, from `/admin/slack` (specs/digest-runway-marks.md
 * §4; D1 `digest_reapply`, migration `digest-settings`). Each run is a Batch job on the scan image running the digest
 * CLI with its selectors — `-o` (the OP), `-y A..B` (those days' posted replies), or no selector (the whole thread, as
 * the cron) — dry (`-n`: what it would edit, nothing posted) or for real, optionally under candidate settings (`-p`,
 * `-q`) instead of the live ones. The CLI's output lands at `gs://<data>/digest/reapply/<job>.txt`.
 *
 *   POST /api/digest-reapply {month, channel?, target, dry_run, params?, quota_marks?} → {run}             admin
 *   GET  /api/digest-reapply → {runs: [run…]} (newest first, 20)                                              admin
 *   GET  /api/digest-reapply?job=<id> → {run, status, output: string | null}                                 admin
 *
 * `target`: `thread` | `op` | `YYYY-MM-DD` | `YYYY-MM-DD..YYYY-MM-DD` (days of `month`). `channel` defaults to
 * `DIGEST_CHANNEL` (the deployment's digest channel); another (a DM, to try a format first) is any Slack channel id. */
import type { D1Database } from '@cloudflare/workers-types'
import { type Ctx, json, requireAdmin } from '../_lib/auth.js'
import { batchConfig, type BatchEnv, notConfigured } from '../_lib/batchConfig.js'
import { jobStamp, secretRef, submitBatch, sweepBatchSpec } from '../_lib/cwBatch.js'
import { batchJobsUrl, gcpToken } from '../_lib/gcp.js'
import { parseParams } from '../../src/digest/codec.js'
import { QUOTA_MARKS } from './digest-settings.js'

const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/
const DAY_RE = /^(\d{4}-\d{2})-(\d{2})$/
const CHANNEL_RE = /^[CDG][A-Z0-9]{8,12}$/
const JOB_RE = /^cw-digest-\d{8}-\d{6}z$/

export interface ReapplyReq { month: string; channel: string; target: string; dry_run: boolean; params: string | null; quota_marks: string | null }
export interface ReapplyRun extends ReapplyReq { job_id: string; actor: string; ts: number; out: string }

const inMonth = (d: string, month: string): boolean => {
  const m = DAY_RE.exec(d)
  return !!m && m[1] === month && !Number.isNaN(Date.parse(`${d}T00:00:00Z`)) && new Date(`${d}T00:00:00Z`).toISOString().startsWith(d)
}

/** The request (its channel defaulted), or why it's refused. */
export function parseReapply(b: unknown, defaultChannel: string | undefined): ReapplyReq | string {
  if (typeof b !== 'object' || b === null || Array.isArray(b)) return 'body: a JSON object'
  const { month, channel, target, dry_run, params, quota_marks } = b as Record<string, unknown>
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
  return { month, channel: ch, target, dry_run, params: (params as string | null | undefined) ?? null, quota_marks: (quota_marks as string | null | undefined) ?? null }
}

/** The digest CLI's arguments for a request (after `dt-cloud digest -T cw -C job/digest.yml -r <root>`). */
export function reapplyArgs(r: ReapplyReq): string[] {
  const sel = r.target === 'thread' ? [] : r.target === 'op' ? ['-o'] : ['-y', r.target.includes('..') ? r.target : `${r.target}..${r.target}`]
  return ['-m', r.month, '-c', r.channel, ...sel, ...(r.dry_run ? ['-n'] : []),
    ...(r.params !== null ? ['-p', r.params] : []), ...(r.quota_marks !== null ? ['-q', r.quota_marks] : [])]
}

const sq = (s: string): string => `'${s.replace(/'/g, `'\\''`)}'`

/** The Batch task's script: the scan steps' environment (`job/cw-env.sh`: secrets stripped, R2 profile), then the
 *  digest CLI, its output teed to the run's file on the data bucket's mount. */
export const reapplyScript = (args: string[], outMnt: string): string => [
  'set -euo pipefail',
  'cd "${APP_DIR:-/app}"',
  '. job/cw-env.sh',
  '{ set +x; } 2>/dev/null',
  `OUT=${sq(outMnt)}`,
  'mkdir -p "$(dirname "$OUT")"',
  `dt-cloud digest -T cw -C job/digest.yml -r "gs://$DATA_BUCKET/snapshots/cw" ${args.map(sq).join(' ')} 2>&1 | tee "$OUT"`,
].join('\n')

type Env = BatchEnv & { DB?: D1Database; JOB_SA?: string; DIGEST_CHANNEL?: string; SITE_URL?: string }

const COLS = 'job_id, month, channel, target, dry_run, params, quota_marks, actor, ts, out'
const row = (r: Record<string, unknown>): ReapplyRun => ({ ...(r as unknown as ReapplyRun), dry_run: !!r.dry_run })

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const env = ctx.env as unknown as Env
  if (!env.DB) return json({ error: 'not configured (no D1 binding)' }, 503)
  if (!env.GCP_SA_KEY || !env.JOB_SA) return json({ error: 'not configured (GCP_SA_KEY / JOB_SA)' }, 503)
  const cfg = batchConfig(env, ['GCP_PROJECT', 'DATA_BUCKET', 'SWEEP_IMAGE', 'SWEEP_S3_ENDPOINT'])
  if ('missing' in cfg) return json({ error: notConfigured('digest re-apply', cfg.missing) }, 503)
  const req = parseReapply(await ctx.request.json().catch(() => null), env.DIGEST_CHANNEL)
  if (typeof req === 'string') return json({ error: req }, 400)

  const jobId = `cw-digest-${jobStamp()}z`
  const out = `gs://${cfg.dataBucket}/digest/reapply/${jobId}.txt`
  const script = reapplyScript(reapplyArgs(req), `/gcs/${cfg.dataBucket}/digest/reapply/${jobId}.txt`)
  const spec = sweepBatchSpec(cfg, env.JOB_SA, script, "", {
    JOB_ID: jobId, SITE_URL: env.SITE_URL ?? new URL(ctx.request.url).origin, ...(cfg.cfAccountId ? { CLOUDFLARE_ACCOUNT_ID: cfg.cfAccountId } : {}),
  }, {
    SITE_TOKEN: secretRef(cfg, 'cw-s3-job-grant'),
    SLACK_BOT_TOKEN: secretRef(cfg, 'cw-s3-slack-bot-token'),
    CLOUDFLARE_API_TOKEN: secretRef(cfg, 'cw-s3-cf-pages-token'),
    R2_ENDPOINT: secretRef(cfg, 'cw-s3-r2-endpoint'),
    R2_ACCESS_KEY_ID: secretRef(cfg, 'cw-s3-isolated-r2-access-key-id'),
    R2_SECRET_ACCESS_KEY: secretRef(cfg, 'cw-s3-isolated-r2-secret-access-key'),
  }, 'digest')
  const token = await gcpToken(env.GCP_SA_KEY)
  const sub = await submitBatch(cfg, token, jobId, spec)
  if (!sub.ok) return json({ error: `batch submit failed (${sub.status})`, detail: sub.text.slice(0, 1000) }, 500)
  const run: ReapplyRun = { ...req, job_id: jobId, actor: id.email ?? id.name ?? 'admin', ts: Math.floor(Date.now() / 1000), out }
  await env.DB.prepare(`INSERT INTO digest_reapply (${COLS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`)
    .bind(run.job_id, run.month, run.channel, run.target, run.dry_run ? 1 : 0, run.params, run.quota_marks, run.actor, run.ts, run.out).run()
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
  if (!JOB_RE.test(job)) return json({ error: 'job: cw-digest-<stamp>z' }, 400)
  const r = await env.DB.prepare(`SELECT ${COLS} FROM digest_reapply WHERE job_id = ?`).bind(job).first()
  if (!r) return json({ error: `no run ${job}` }, 404)
  const cfg = batchConfig(env, ['GCP_PROJECT', 'DATA_BUCKET'])
  if ('missing' in cfg || !env.GCP_SA_KEY) return json({ run: row(r), status: null, output: null }, 200, { 'cache-control': 'no-store' })
  const token = await gcpToken(env.GCP_SA_KEY)
  const auth = { authorization: `Bearer ${token}` }
  const [jr, or] = await Promise.all([
    fetch(`${batchJobsUrl(cfg, cfg.region)}/${job}`, { headers: auth }),
    fetch(`https://storage.googleapis.com/storage/v1/b/${cfg.dataBucket}/o/${encodeURIComponent(`digest/reapply/${job}.txt`)}?alt=media`, { headers: auth }),
  ])
  const status = jr.ok ? ((await jr.json()) as { status?: { state?: string } }).status?.state ?? null : null
  return json({ run: row(r), status, output: or.ok ? await or.text() : null }, 200, { 'cache-control': 'no-store' })
}
