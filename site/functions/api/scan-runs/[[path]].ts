// /api/scan-runs[/:run_id] — what each scan job did (specs/scan-runs-ui.md):
//
//   GET /api/scan-runs          { configured, runs: RunSummary[], project, meta }     viewer
//   GET /api/scan-runs/:run_id  { ...ScanRunDetail, project, meta }                    viewer
//
// Rows come from D1 (`dt-cloud scan-run record | backfill`), scoped to the
// request's store. `project` (`GCP_PROJECT`) builds the Batch / Logging links,
// `meta` (`META_TREE_URL`, the `/meta` store's base URL) the output links; either
// unset = no such links.
import type { D1Database } from '@cloudflare/workers-types'
import { type Env as AuthEnv, json, requireViewer } from '../../_lib/auth.js'
import { loadScanRuns, scanRunDetail, scanRunsList } from '../../_lib/scanRuns.js'
import { storeKey } from '../../_lib/stores.js'

type Env = AuthEnv & { DB?: D1Database; GCP_PROJECT?: string; META_TREE_URL?: string; STORE_KEY?: string }

export const onRequestGet = async (ctx: { request: Request; env: Env; params?: { path?: string | string[] } }): Promise<Response> => {
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const db = ctx.env.DB
  if (!db) return json({ error: 'no D1 bound' }, 503)
  const p = ctx.params?.path
  const id = decodeURIComponent((Array.isArray(p) ? p.join('/') : p) ?? '')
  const rows = await loadScanRuns(db, storeKey(ctx.env as Parameters<typeof storeKey>[0]))
  const now = Math.floor(Date.now() / 1000)
  const links = { project: ctx.env.GCP_PROJECT ?? null, meta: ctx.env.META_TREE_URL ?? null }
  const headers = { 'cache-control': 'private, max-age=15' }
  if (!id) return json({ ...scanRunsList(rows, now), ...links }, 200, headers)
  if (!rows) return json({ error: 'scan runs are not set up on this deployment (no scan_runs table)' }, 404)
  const d = scanRunDetail(rows, id, now)
  return d ? json({ ...d, ...links }, 200, headers) : json({ error: `no scan run ${id}` }, 404)
}
