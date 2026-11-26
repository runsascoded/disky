// /api/scan-runs[/:run_id] — what each scan job did (specs/done/scan-runs-ui.md):
//
//   GET /api/scan-runs[?store=<key>]          { configured, runs: RunSummary[], project, meta, dispatch }     viewer
//   GET /api/scan-runs/:run_id[?store=<key>]  { ...ScanRunDetail, dag, cost, project, meta }         viewer
//
// `cost`: the run's compute report by job (`snapshots/cost/runs/<scan>/<run>.json`, the `run-cost` step's, read through
// the served store; specs/scan-cost-by-stage.md), or null where none was written. Compute only: public where the
// deployment's served copy is (sizes and per-scan cost may be). Under `?store=`, the requested store's own copy (`withStore`
// rebinds the env): a store whose runs write no report shows none.
//
// `emoji=1` (the scan-health thread's read, `dt_cloud.nominal`): each manual run's starter's Slack emoji
// (`trigger_emoji`), asked of OA's broker (`OA_LOGIN_*`, the deployment's own client) and never stored; nothing for a
// public viewer, or without the broker's client. Off by default: the page never asks, and the broker rate-limits.
//
// A manual run's starter (`trigger_by`: an address) reaches signed-in viewers and grants only, never a public one.
//
// Rows come from D1 (`dt-cloud scan-run record | backfill`), scoped to the
// request's store (`withStore`: no `store=` = the primary; a secondary store's
// runs need its scope too). `project` (`GCP_PROJECT`) builds the Batch / Logging
// links, `meta` (`META_TREE_URL`, the `/meta` store's base URL) the output links;
// either unset = no such links. `dispatch` (the primary store's list, where `SCAN_SUBMIT_JOB` and `GCP_SA_KEY` are set:
// "Run a scan", `POST /api/scan-runs/dispatch`) is what would refuse one now: `{ running: <run id> | null,
// pending_since: <ts> | null }`; null where there is no such action (unset, another store, or a D1 without the
// `scan_dispatch` table).
import type { D1Database } from '@cloudflare/workers-types'
import { type Env as AuthEnv, json, requireViewer } from '../../_lib/auth.js'
import { dispatchState, missingTable, STORE, submitJob } from '../../_lib/scanDispatch.js'
import { slackEmojiClient } from '@open-athena/auth/oidc'
import { loadDag, loadScanRuns, scanRunDetail, scanRunsList, withoutStarters, withStarterEmoji } from '../../_lib/scanRuns.js'
import { storeKey, withStore } from '../../_lib/stores.js'
import { servedStore } from '../../_lib/index.js'
import { RUN_COST_PREFIX, type RunCostReport, runCostKey } from '../../../src/runCost.js'

/** One broker emoji client per isolate (it caches answers 10 min), per issuer + client. */
const emojiClients = new Map<string, ReturnType<typeof slackEmojiClient>>()
const emojiClient = (env: AuthEnv): ReturnType<typeof slackEmojiClient> | null => {
  const { OA_LOGIN_ISSUER: issuer, OA_LOGIN_CLIENT_ID: clientId, OA_LOGIN_CLIENT_SECRET: clientSecret } = env
  if (!issuer || !clientId || !clientSecret) return null
  const key = `${issuer}|${clientId}`
  let c = emojiClients.get(key)
  if (!c) emojiClients.set(key, c = slackEmojiClient({ issuer, clientId, clientSecret }))
  return c
}

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
  const loaded = withoutStarters(await loadScanRuns(db, storeKey(ctx.env)), gated)
  const emoji = gated.via !== 'public' && new URL(ctx.request.url).searchParams.get('emoji') === '1' ? emojiClient(ctx.env) : null
  const rows = emoji ? await withStarterEmoji(loaded, emoji.getMany) : loaded
  const now = Math.floor(Date.now() / 1000)
  const links = { project: ctx.env.GCP_PROJECT ?? null, meta: ctx.env.META_TREE_URL ?? null }
  const headers = { 'cache-control': 'private, max-age=15' }
  if (!id) {
    const dispatch: DispatchBlock | null = rows && submitJob(ctx.env.SCAN_SUBMIT_JOB) && ctx.env.GCP_SA_KEY && storeKey(ctx.env) === STORE
      ? await dispatchState(db, now).then(
        s => ({ running: s.running?.run_id ?? null, pending_since: s.pending?.ts ?? null }),
        e => { if (missingTable(e)) return null; throw e },
      )
      : null
    return json({ ...scanRunsList(rows, now), ...links, dispatch }, 200, headers)
  }
  if (!rows) return json({ error: 'scan runs are not set up on this deployment (no scan_runs table)' }, 404)
  const d = scanRunDetail(rows, id, now)
  if (!d) return json({ error: `no scan run ${id}` }, 404)
  return json({ ...d, dag: await loadDag(db, id), cost: await loadRunCost(ctx.env, d.run.scan, d.run.run_id), ...links }, 200, headers)
}

/** The run's saved cost report, or null: none written (the step is off, or hasn't run), or no store to read it from. */
export async function loadRunCost(env: Env, scan: string, runId: string): Promise<RunCostReport | null> {
  const key = runCostKey(scan, runId)
  if (!key) return null
  try {
    const { bytes } = await servedStore(env as Parameters<typeof servedStore>[0], [RUN_COST_PREFIX]).get(key)
    return JSON.parse(new TextDecoder().decode(bytes)) as RunCostReport
  } catch (e) {
    if ((e as Error).name === 'NotFoundError') return null
    console.warn(`scan-runs: ${key}: ${(e as Error).message}`)
    return null
  }
}
