// /api/scan-runs[/:run_id] — what each scan job did (specs/done/scan-runs-ui.md):
//
//   GET /api/scan-runs[?store=<key>]          { configured, runs: RunSummary[], project, meta, dispatch }     viewer
//   GET /api/scan-runs/:run_id[?store=<key>]  { ...ScanRunDetail, dag, project, meta }               viewer
//
// Rows come from D1 (`dt-cloud scan-run record | backfill`), scoped to the
// request's store (`withStore`: no `store=` = the primary; a secondary store's
// runs need its scope too). `project` (`GCP_PROJECT`) builds the Batch / Logging
// links, `meta` (`META_TREE_URL`, the `/meta` store's base URL) the output links;
// either unset = no such links. `dispatch` (the primary store's list, where `SCAN_SUBMIT_JOB` and `GCP_SA_KEY` are set:
// "Run a scan", `POST /api/scan-runs/dispatch`) is what would refuse one now: `{ running: <run id> | null,
// pending_since: <ts> | null }`; null where there is no such action.
import type { D1Database } from '@cloudflare/workers-types'
import { type Env as AuthEnv, json, requireViewer } from '../../_lib/auth.js'
import { dispatchState, STORE, submitJob } from '../../_lib/scanDispatch.js'
import { loadDag, loadScanRuns, scanRunDetail, scanRunsList } from '../../_lib/scanRuns.js'
import { storeKey, withStore } from '../../_lib/stores.js'

type Env = AuthEnv & { DB?: D1Database; GCP_PROJECT?: string; META_TREE_URL?: string; GCP_SA_KEY?: string; SCAN_SUBMIT_JOB?: string }

/** The list's `dispatch`: what blocks "Run a scan" now, or null where the deployment (or this store) has none. */
export interface DispatchBlock { running: string | null; pending_since: number | null }

export const onRequestGet = async (ctx0: { request: Request; env: Env; params?: { path?: string | string[] } }): Promise<Response> => {
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const db = ctx.env.DB
  if (!db) return json({ error: 'no D1 bound' }, 503)
  const p = ctx.params?.path
  const id = decodeURIComponent((Array.isArray(p) ? p.join('/') : p) ?? '')
  const rows = await loadScanRuns(db, storeKey(ctx.env))
  const now = Math.floor(Date.now() / 1000)
  const links = { project: ctx.env.GCP_PROJECT ?? null, meta: ctx.env.META_TREE_URL ?? null }
  const headers = { 'cache-control': 'private, max-age=15' }
  if (!id) {
    const dispatch: DispatchBlock | null = rows && submitJob(ctx.env.SCAN_SUBMIT_JOB) && ctx.env.GCP_SA_KEY && storeKey(ctx.env) === STORE
      ? await dispatchState(db, now).then(s => ({ running: s.running?.run_id ?? null, pending_since: s.pending?.ts ?? null }))
      : null
    return json({ ...scanRunsList(rows, now), ...links, dispatch }, 200, headers)
  }
  if (!rows) return json({ error: 'scan runs are not set up on this deployment (no scan_runs table)' }, 404)
  const d = scanRunDetail(rows, id, now)
  if (!d) return json({ error: `no scan run ${id}` }, 404)
  return json({ ...d, dag: await loadDag(db, id), ...links }, 200, headers)
}
