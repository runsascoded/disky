/**
 * The executor contract behind the dispatch seam (`_lib/executor.ts`,
 * specs/done/staged-slack.md). Each implementation (`planDispatch.planSweep`,
 * `sweepDispatch.sweep`) depends only on this module, so the seam can import
 * them without a cycle.
 */
import type { D1Database } from '@cloudflare/workers-types'
import type { BatchEnv } from './batchConfig.js'
import type { FinishedRun, RunRow } from './plans.js'
import type { NotifyEnv } from './stagedSlack.js'

export type ExecEnv = NotifyEnv & BatchEnv & {
  DB?: D1Database
  GCP_SA_KEY?: string
  /** The service account a dispatched Batch job runs as (a `[vars]` entry;
   *  the dispatcher behind `GCP_SA_KEY` must be able to act as it). */
  JOB_SA?: string
  EXECUTOR?: string
  STORE_SCHEME?: string
  STORE_BUCKETS?: string
}

export interface DispatchReq {
  planId: number
  mode: 'dry' | 'real'
  /** The scan to list from (each item is held to its own staging scan, `plan_items.as_of`). Optional for `real` when
   *  a dry run of exactly `items` exists: its scan. */
  date?: string
  /** The items the run acts on (`digestLines`: a prefix, or `=<key>` for an object): the rows checked on /staged.
   *  Absent: the whole plan (the Slack thread's buttons). Never more than named (specs/delete-what-you-see.md). */
  items?: string[]
  actor: string
  siteUrl: string
  /** gcs only: a `-b` cut of the plan's buckets (empty/absent = all). */
  buckets?: string[]
  /** gcs only: the executor's machine (`SWEEP_MACHINES`; absent = the default). */
  machine?: 'n2-highmem-8' | 'n2-highmem-32'
}

export interface DispatchErr { ok: false; status: number; error: string; extra?: Record<string, unknown> }

export const refuse = (status: number, error: string, extra?: Record<string, unknown>): DispatchErr =>
  ({ ok: false, status, error, ...(extra ? { extra } : {}) })

export interface Launched { job_id: string; extra: Record<string, unknown> }

/** A dispatch an executor has validated against the plan, ready to launch. */
export interface Prepared {
  /** The items the run acts on, as `digestLines` (canonical prefixes; an
   * exact object as `=<key>`): the digest's input. */
  prefixes: string[]
  launch(date: string, digest: string, reviewed?: RunRow): Promise<Launched | DispatchErr>
}

export interface Executor {
  /** The scan ids this executor accepts, and how to say so. */
  dateRe: RegExp
  dateHint: string
  prepare(env: ExecEnv, db: D1Database, req: DispatchReq): Promise<Prepared | DispatchErr>
  /** Reflect finished runs into D1 (totals, digests); the runs this call closed. */
  refresh(env: ExecEnv, db: D1Database): Promise<FinishedRun[]>
}
