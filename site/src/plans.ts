import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { DEFAULT_STORE } from './stores'

// Client for the deletion-plan API (specs/staged-delete.md; the OA build plan
// `sweep-plan-union.md`). The opt-in trash model: a trash gesture *stages*
// prefixes (any signed-in full viewer may) into a shared open plan; an admin
// approves + dispatches from /staged. Nothing is deleted by inaction.

export interface StageResult {
  plan_id: number
  batch_id: number
  /** What this gesture added (a re-staged prefix counts). */
  staged: string[]
  /** Skipped: a staged ancestor already names them. */
  covered: string[]
  /** Removed: staged descendants a new prefix now names. */
  absorbed: string[]
}

/** One trash gesture: the prefixes it stages and an optional shared memo (the
 *  reason for the deletion, stored once on the batch — not copied per path). */
export interface StageArgs {
  prefixes: string[]
  note?: string
}

export interface PlanSummary {
  id: number
  name: string
  note: string | null
  state: 'open' | 'closed'
  created_by: string
  created_ts: number
  closed_ts: number | null
}
export interface StagedItem { prefix: string; note: string | null; added_by: string; added_ts: number; batch_id: number | null }
export interface StageBatch { id: number; plan_id: number; note: string | null; created_by: string; created_ts: number }
/** A deletion run as the deployment's executor records it (cw's columns are a
 *  superset of gcs's; the console reads the common ones). */
export interface DeletionRun {
  run_id: string
  plan_id: number | null
  mode: 'dry' | 'real'
  scan?: string
  actor?: string
  started_ts: number
  finished_ts: number | null
  deleted_bytes: number
  deleted_objects: number
  /** A dry run's measured reclaim — what the set as a whole would actually
   *  free (clone/hardlink-shared bytes don't count); null = not measured. */
  freed_bytes?: number | null
  skipped_gone: number
  skipped_overwritten?: number
  undo_deadline: number | null
  undo_state: string
  purge_state?: string
  log_dir?: string
}
export interface StagedPlan { plan: PlanSummary | null; items: StagedItem[]; batches: StageBatch[]; runs: DeletionRun[] }

/** The deployment's executor routes: cw's plan-first Batch bridge or gcs's. */
export const EXEC_API = `/api/${DEFAULT_STORE.executor}`

async function call<T>(url: string, method = 'GET', body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    credentials: 'include',
    headers: body ? { 'content-type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  })
  // CF returns HTML on a 5xx: read text, then try JSON.
  const text = await r.text()
  let data: unknown = null
  try { data = JSON.parse(text) } catch { data = null }
  if (!r.ok) {
    const msg = data && typeof data === 'object' && 'error' in data ? String((data as { error: unknown }).error) : `${r.status} ${text.slice(0, 200)}`
    throw new Error(msg)
  }
  return data as T
}

/** Stage prefixes for deletion (POST /api/plans/stage). Pass canonical
 *  `<scheme>bucket/…/` prefixes (trailing slash) and, optionally, one memo for
 *  the whole gesture. Invalidates the plans query so /staged reflects it. */
export function useStage() {
  const qc = useQueryClient()
  return useMutation<StageResult, Error, StageArgs>({
    mutationFn: ({ prefixes, note }: StageArgs) => call('/api/plans/stage', 'POST', { prefixes, note: note?.trim() || undefined }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['plans'] }) },
  })
}

/** The shared open plan (GET /api/plans/staged), polled while a run is live. */
export function useStagedPlan(live = false) {
  return useQuery<StagedPlan, Error>({
    queryKey: ['plans', 'staged'],
    queryFn: () => call<StagedPlan>('/api/plans/staged'),
    refetchInterval: live ? 20_000 : false,
  })
}

/** Take prefixes back out of a plan (DELETE /api/plans/:id/items) — an admin
 *  any of them, a stager only their own. */
export function useUnstage(planId: number | null) {
  const qc = useQueryClient()
  return useMutation<{ removed: string[] }, Error, string[]>({
    mutationFn: prefixes => {
      if (planId == null) throw new Error('nothing staged')
      return call(`/api/plans/${planId}/items`, 'DELETE', { prefixes })
    },
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['plans'] }) },
  })
}

/** Dispatch the plan to the deployment's executor (admin): a dry run reports
 *  what a real run would delete; a real run deletes, recoverably. */
export function useDispatch(planId: number | null) {
  const qc = useQueryClient()
  return useMutation<{ job_id: string }, Error, { mode: 'dry' | 'real'; date: string }>({
    mutationFn: ({ mode, date }) => {
      if (planId == null) throw new Error('nothing staged')
      return call(`${EXEC_API}/dispatch`, 'POST', { plan_id: planId, mode, date })
    },
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['plans'] }); void qc.invalidateQueries({ queryKey: ['sweep-jobs'] }) },
  })
}

/** A run control (stop / undo / purge) on the plan-first executor. */
export function useRunAction() {
  const qc = useQueryClient()
  return useMutation<unknown, Error, { action: 'stop' | 'undo' | 'purge'; run_id: string }>({
    mutationFn: ({ action, run_id }) => call(`${EXEC_API}/${action}`, 'POST', action === 'stop' ? { job_id: run_id } : { run_id }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['plans'] }); void qc.invalidateQueries({ queryKey: ['sweep-jobs'] }) },
  })
}

export interface ExecJob {
  job_id: string
  state: string
  mode?: string
  logs?: string
  last_event?: string | null
}

/** The executor's recent jobs, by run id (live state from Batch). */
export function useExecJobs(live = false) {
  return useQuery<Record<string, ExecJob>, Error>({
    queryKey: ['sweep-jobs'],
    queryFn: async () => {
      const d = await call<{ jobs: ExecJob[] }>(`${EXEC_API}/jobs`)
      return Object.fromEntries(d.jobs.map(j => [j.job_id, j]))
    },
    refetchInterval: live ? 20_000 : false,
    retry: false,
  })
}

export const LIVE_STATES = new Set(['QUEUED', 'SCHEDULED', 'RUNNING'])
