// POST /api/scan-runs/dispatch — "Run a scan" on /scans: start the primary store's scan now, through the deployment's
// cron submitter (`_lib/scanDispatch.ts`).
//
//   POST /api/scan-runs/dispatch → 200 { execution, operation }                                                   admin
//     409 { error, running } / { error, pending }: a scan is running, or an earlier dispatch hasn't recorded its run
//     500 { error, detail }: Cloud Run refused (the claim is released)
//     503: not configured (`SCAN_SUBMIT_JOB`, `GCP_SA_KEY`, `DB`)
//
// No body. `execution` is the Cloud Run execution's resource name (null if the operation didn't carry one); the scan
// shows on /scans once its head records the run, a few minutes on.
import type { D1Database } from '@cloudflare/workers-types'
import { type Ctx, json, requireAdmin } from '../../_lib/auth.js'
import { gcpToken } from '../../_lib/gcp.js'
import { claim, dispatchState, executionOf, refusal, runRequest, settle, submitJob } from '../../_lib/scanDispatch.js'

type Env = { DB?: D1Database; GCP_SA_KEY?: string; SCAN_SUBMIT_JOB?: string }

const NO_STORE = { 'cache-control': 'no-store' }

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const env = ctx.env as unknown as Env
  const job = submitJob(env.SCAN_SUBMIT_JOB)
  if (!job) return json({ error: 'scan dispatch not configured (SCAN_SUBMIT_JOB unset or not projects/<p>/locations/<r>/jobs/<job>)' }, 503)
  if (!env.GCP_SA_KEY) return json({ error: 'scan dispatch not configured (GCP_SA_KEY unset)' }, 503)
  const db = env.DB
  if (!db) return json({ error: 'scan dispatch not configured (no D1 bound)' }, 503)
  const now = Math.floor(Date.now() / 1000)
  const who = id.email ?? id.name ?? 'admin'
  let state
  try {
    state = await dispatchState(db, now)
  } catch (e) {
    if (/no such table/i.test(String((e as Error).message ?? e))) return json({ error: 'scan runs are not set up on this deployment (no scan_runs table)' }, 503)
    throw e
  }
  const why = refusal(state, now)
  if (why) return json({ error: why, ...(state.running ? { running: state.running } : { pending: state.pending }) }, 409, NO_STORE)
  const key = crypto.randomUUID()
  if (!(await claim(db, key, who, now, job))) {
    const later = await dispatchState(db, now)
    return json({ error: refusal(later, now) ?? 'another dispatch just claimed the scan', pending: later.pending }, 409, NO_STORE)
  }
  const { url, body } = runRequest(job)
  let r: Response
  try {
    const token = await gcpToken(env.GCP_SA_KEY)
    r = await fetch(url, { method: 'POST', headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: JSON.stringify(body) })
  } catch (e) {
    const error = (e as Error).message ?? String(e)
    await settle(db, key, who, Math.floor(Date.now() / 1000), 'failed', { error })
    return json({ error: 'scan dispatch failed', detail: error.slice(0, 1000) }, 500, NO_STORE)
  }
  const text = await r.text()
  if (!r.ok) {
    console.error('scan dispatch failed', r.status, text.slice(0, 2000))
    await settle(db, key, who, Math.floor(Date.now() / 1000), 'failed', { status: r.status, error: text.slice(0, 1000) })
    // 500, not 502: Cloudflare replaces an origin 502 with its own page, losing the detail.
    return json({ error: `scan dispatch failed (Cloud Run ${r.status})`, detail: text.slice(0, 1000) }, 500, NO_STORE)
  }
  let op: { name?: string } | null = null
  try { op = JSON.parse(text) as { name?: string } } catch { op = null }
  const out = { execution: executionOf(op), operation: op?.name ?? null }
  await settle(db, key, who, Math.floor(Date.now() / 1000), 'dispatched', out)
  return json(out, 200, NO_STORE)
}
