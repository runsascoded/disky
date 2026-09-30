/**
 * The executor contract behind the dispatch seam (`_lib/executor.ts`,
 * specs/done/staged-slack.md). Each implementation (`planDispatch.planSweep`,
 * `sweepDispatch.sweep`) depends only on this module, so the seam can import
 * them without a cycle.
 */
import type { D1Database } from '@cloudflare/workers-types'
import type { FinishedRun } from './plans.js'
import type { NotifyEnv } from './stagedSlack.js'

export type ExecEnv = NotifyEnv & {
  DB?: D1Database
  GCP_SA_KEY?: string
  EXECUTOR?: string
  STORE_SCHEME?: string
  STORE_BUCKETS?: string
}

export interface DispatchReq {
  planId: number
  mode: 'dry' | 'real'
  /** The scan to run against. Optional for `real`: the matching dry-run's. */
  date?: string
  actor: string
  siteUrl: string
  /** gcs only: a `-b` cut of the plan's buckets (empty/absent = all). */
  buckets?: string[]
}

export interface DispatchErr { ok: false; status: number; error: string; extra?: Record<string, unknown> }

export const refuse = (status: number, error: string, extra?: Record<string, unknown>): DispatchErr =>
  ({ ok: false, status, error, ...(extra ? { extra } : {}) })

export interface Launched { job_id: string; extra: Record<string, unknown> }

/** A dispatch an executor has validated against the plan, ready to launch. */
export interface Prepared {
  /** The canonical prefixes the run acts on (the digest's input). */
  prefixes: string[]
  launch(date: string, digest: string): Promise<Launched | DispatchErr>
}

export interface Executor {
  /** The scan ids this executor accepts, and how to say so. */
  dateRe: RegExp
  dateHint: string
  prepare(env: ExecEnv, db: D1Database, req: DispatchReq): Promise<Prepared | DispatchErr>
  /** Reflect finished runs into D1 (totals, digests); the runs this call closed. */
  refresh(env: ExecEnv, db: D1Database): Promise<FinishedRun[]>
}
