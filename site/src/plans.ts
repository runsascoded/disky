import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { DEFAULT_STORE } from './stores'
import { type DeletionRun, EXEC_CAPS, type ExecCaps, type ExecJob } from './runs'

export type { DeletionRun, ExecJob } from './runs'
export { LIVE_STATES } from './runs'

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
/** A stage batch with no items left, and what became of what it staged
 *  (`functions/_lib/plans.ts` `emptiedBatches`). */
export interface EmptiedBatch extends StageBatch {
  staged: number
  covered: number
  absorbed: { into: number; n: number }[]
  unstaged: number
}
export interface StagedPlan { plan: PlanSummary | null; items: StagedItem[]; batches: StageBatch[]; emptied?: EmptiedBatch[]; runs: DeletionRun[] }
/** A plan as `GET /api/plans` lists it. */
export interface PlanListing extends PlanSummary { items: number; runs: number }

/** The deployment's executor routes: cw's plan-first Batch bridge or gcs's. */
export const EXEC_API = `/api/${DEFAULT_STORE.executor}`
/** What the deployment's executor can do (stop / undo / purge / bucket cut /
 *  run files): the console keys every control on these, never on a name. */
export const CAPS: ExecCaps = EXEC_CAPS[DEFAULT_STORE.executor]

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

/** The plan /staged shows (polled while a run is live): the shared open plan
 * (GET /api/plans/staged), or plan `id` (GET /api/plans/:id — a closed one
 * keeps its runs and their undo windows reachable). */
export function useStagedPlan(live = false, id: number | null = null) {
  return useQuery<StagedPlan, Error>({
    queryKey: ['plans', id ?? 'staged'],
    queryFn: () => call<StagedPlan>(id == null ? '/api/plans/staged' : `/api/plans/${id}`),
    refetchInterval: live ? 20_000 : false,
  })
}

/** Every plan, newest first, with its item and run counts (GET /api/plans). */
export function usePlanList() {
  return useQuery<PlanListing[], Error>({
    queryKey: ['plans', 'list'],
    queryFn: async () => (await call<{ plans: PlanListing[] }>('/api/plans')).plans,
    staleTime: 60_000,
  })
}

/** Close a plan (admin; PATCH /api/plans/:id): the next trash gesture opens a
 * fresh one. Its runs stay reachable through the plan picker. */
export function useClosePlan() {
  const qc = useQueryClient()
  return useMutation<unknown, Error, number>({
    mutationFn: id => call(`/api/plans/${id}`, 'PATCH', { state: 'closed' }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['plans'] }) },
  })
}

export interface RunBand { prefix: string; bytes: number; objects: number; gone: number; overwritten: number; drift_new_objects: number; undone_objects: number }

/** One run's D1 record and its per-band rows (GET /api/plans/run?id=). */
export function useRunDetail(runId: string, enabled: boolean, live = false) {
  return useQuery<{ run: DeletionRun; bands: RunBand[] }, Error>({
    queryKey: ['run-detail', runId],
    queryFn: () => call(`/api/plans/run?id=${encodeURIComponent(runId)}`),
    enabled,
    refetchInterval: live ? 30_000 : false,
  })
}

/** A file in the deployment's files proxy as JSON; null when it isn't there. */
export async function filesJson<T>(rel: string): Promise<T | null> {
  const r = await fetch(`/v1/files/get?path=${encodeURIComponent(rel)}`, { credentials: 'include' })
  if (r.status === 404) return null
  if (!r.ok) throw new Error(`${rel}: ${r.status}`)
  return r.json() as Promise<T>
}

/** Run-dir JSON files (`CAPS.runFiles`), each polled every `poll` ms while
 * `live(rel)` — only until it appears, with `untilPresent` (a manifest step's
 * summary lands minutes in, then never changes). */
export function useRunFiles<T>(rels: readonly string[], live: boolean | ((rel: string) => boolean), { poll = 30_000, untilPresent = false } = {}) {
  const isLive = typeof live === 'function' ? live : () => live
  return useQueries({
    queries: rels.map(rel => ({
      queryKey: ['run-file', rel],
      queryFn: () => filesJson<T>(rel),
      enabled: CAPS.runFiles,
      retry: false,
      staleTime: isLive(rel) ? 20_000 : Infinity,
      refetchInterval: (q: { state: { data?: T | null } }) => (isLive(rel) && !(untilPresent && q.state.data) ? poll : false),
    })),
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
  return useMutation<{ job_id: string }, Error, { mode: 'dry' | 'real'; date: string; buckets?: string[]; machine?: string }>({
    mutationFn: ({ mode, date, buckets, machine }) => {
      if (planId == null) throw new Error('nothing staged')
      return call(`${EXEC_API}/dispatch`, 'POST', { plan_id: planId, mode, date, ...(buckets ? { buckets } : {}), ...(machine ? { machine } : {}) })
    },
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['plans'] }); void qc.invalidateQueries({ queryKey: ['sweep-jobs'] }) },
  })
}

/** A run control: stop (the run's Batch job) or undo / purge (the run). */
export type RunAction = { action: 'stop'; job_id: string } | { action: 'undo' | 'purge'; run_id: string }

export function useRunAction() {
  const qc = useQueryClient()
  return useMutation<unknown, Error, RunAction>({
    mutationFn: a => call(`${EXEC_API}/${a.action}`, 'POST', a.action === 'stop' ? { job_id: a.job_id } : { run_id: a.run_id }),
    onSuccess: () => { void qc.invalidateQueries({ queryKey: ['plans'] }); void qc.invalidateQueries({ queryKey: ['sweep-jobs'] }) },
  })
}

/** The executor's recent jobs (live state from Batch); `configured` false =
 * the deployment has no dispatch credentials (recorded runs only). */
export function useExecJobs(live = false) {
  return useQuery<{ jobs: ExecJob[]; configured: boolean }, Error>({
    queryKey: ['sweep-jobs'],
    queryFn: async () => {
      const d = await call<{ jobs: ExecJob[]; configured?: boolean }>(`${EXEC_API}/jobs`)
      return { jobs: d.jobs, configured: d.configured !== false }
    },
    refetchInterval: live ? 20_000 : false,
    retry: false,
  })
}
