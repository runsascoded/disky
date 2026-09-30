/**
 * The dispatch seam (specs/done/staged-slack.md): one code path per executor,
 * shared by the /staged console's HTTP routes (`api/plan-sweep/dispatch`,
 * `api/sweep/dispatch`) and `/slack/actions`. The deployment names its
 * executor with the `[vars]` `EXECUTOR` (= its `Store.executor` in
 * `src/stores.ts`): `plan-sweep` (cw's plan-first Batch bridge, the default)
 * or `sweep` (gcs's). An HTTP route passes its own kind; Slack uses the var.
 *
 * `dispatchPlan` owns what both executors share: request validation, the
 * digest of the item set the run acts on (recorded on the run), and the real
 * gate — a real run needs a finished dry-run of exactly that set, with no run
 * of the plan in flight, and runs on that dry-run's scan.
 */
import type { D1Database } from '@cloudflare/workers-types'
import { type DispatchErr, type DispatchReq, type ExecEnv, type Executor, refuse } from './dispatch.js'
import { planDigest, planRuns, realGate, type RunRow } from './plans.js'
import { announceFinished, notifyPlan, runEvent } from './stagedSlack.js'
import { laptop } from './laptopDispatch.js'
import { planSweep } from './planDispatch.js'
import { sweep } from './sweepDispatch.js'

export type { DispatchReq, ExecEnv }

export const EXECUTOR_KINDS = ['plan-sweep', 'sweep', 'laptop'] as const
export type ExecutorKind = typeof EXECUTOR_KINDS[number]

/** The deployment's executor: `EXECUTOR`, default `plan-sweep`. Anything else
 * is a misconfiguration, and throws. */
export function executorOf(env: { EXECUTOR?: string }): ExecutorKind {
  const e = env.EXECUTOR ?? 'plan-sweep'
  if (!(EXECUTOR_KINDS as readonly string[]).includes(e)) throw new Error(`unknown EXECUTOR ${JSON.stringify(e)} (expected ${EXECUTOR_KINDS.join(' | ')})`)
  return e as ExecutorKind
}

export const EXECUTORS: Record<ExecutorKind, Executor> = { 'plan-sweep': planSweep, sweep, laptop }

/** The plan-first executor an HTTP route under `/api/plan-sweep/` dispatches
 * to: the deployment's own (`laptop` on m3), never gcs's `sweep`. */
export const planFirstKind = (env: { EXECUTOR?: string }): ExecutorKind => {
  const k = executorOf(env)
  return k === 'sweep' ? 'plan-sweep' : k
}

export interface DispatchOk {
  ok: true
  job_id: string
  plan_id: number
  mode: 'dry' | 'real'
  date: string
  actor: string
  /** `planDigest` of the item set the run acts on. */
  digest: string
  /** Executor-specific response fields (`run`, `plan`, `region`, `buckets`). */
  extra: Record<string, unknown>
}
export type DispatchResult = DispatchOk | DispatchErr

/** Bring runs up to date and announce any that just finished. */
export async function refreshRuns(env: ExecEnv, db: D1Database, siteUrl: string, ex: Executor): Promise<void> {
  const done = await ex.refresh(env, db)
  if (done.length) await announceFinished(env, db, done, siteUrl)
}

export async function dispatchPlan(
  env: ExecEnv,
  req: DispatchReq,
  kind: ExecutorKind = executorOf(env),
  executors: Record<ExecutorKind, Executor> = EXECUTORS,
): Promise<DispatchResult> {
  if (!env.DB) return refuse(503, 'plans store not configured (no D1 binding)')
  // Each executor checks its own credentials in `prepare` (the GCP ones need
  // `GCP_SA_KEY`; `laptop` needs a live drainer).
  const ex = executors[kind]
  if (req.date !== undefined && !ex.dateRe.test(req.date)) return refuse(400, `date must be a scan id (${ex.dateHint})`)
  if (req.mode === 'dry' && req.date === undefined) return refuse(400, `date required for a dry run (${ex.dateHint})`)
  const db = env.DB

  const prep = await ex.prepare(env, db, req)
  if ('ok' in prep) return prep
  const digest = await planDigest(prep.prefixes)

  let date = req.date
  if (req.mode === 'real') {
    await refreshRuns(env, db, req.siteUrl, ex)
    const gate = realGate(await planRuns(db, req.planId), digest, prep.prefixes.length)
    if (!gate.ok) return refuse(409, `not deleting: ${gate.reason}`)
    if (date !== undefined && date !== gate.dry.scan) {
      return refuse(409, `not deleting: a real run uses its dry-run's scan (${gate.dry.scan}, ${gate.dry.run_id}), not ${date}`)
    }
    date = gate.dry.scan
  }
  if (date === undefined) throw new Error('unreachable: a dry run without a date was refused above')

  const launched = await prep.launch(date, digest)
  if ('ok' in launched) return launched
  return { ok: true, job_id: launched.job_id, plan_id: req.planId, mode: req.mode, date, actor: req.actor, digest, extra: launched.extra }
}

/** Announce a dispatch in the plan's thread (`via`: www / Slack). */
export async function notifyDispatched(env: ExecEnv, r: DispatchOk, via: string, siteUrl: string): Promise<void> {
  if (!env.DB) return
  const row: RunRow = {
    run_id: r.job_id, mode: r.mode, scan: r.date, actor: r.actor, started_ts: 0, finished_ts: null,
    deleted_bytes: 0, deleted_objects: 0, skipped_gone: 0, skipped_overwritten: 0, plan_digest: r.digest,
  }
  await notifyPlan(env, env.DB, r.plan_id, siteUrl, { text: runEvent(row, 'dispatched', via) })
}

/** An HTTP route's JSON for a dispatch result (the shape /staged reads). */
export function dispatchBody(r: DispatchResult): { body: Record<string, unknown>; status: number } {
  if (!r.ok) return { body: { error: r.error, ...r.extra }, status: r.status }
  return { body: { job_id: r.job_id, plan_id: r.plan_id, mode: r.mode, date: r.date, ...r.extra, by: r.actor }, status: 200 }
}
