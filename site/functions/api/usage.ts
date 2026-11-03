/**
 * GET /api/usage[?days=<1..92>] → { days: UsageSnapshot[] }   admin
 *
 * The deployment's daily Cloudflare usage documents (specs/usage-metrics.md), oldest first: the newest `days`
 * (default 30) of `snapshots/usage/<date>.json`, which the job's `cf-usage` step writes (`dt-cloud cf-usage -o`) to
 * the data store and R2. Read through the served seam (`servedStore`: `INDEX_R2` under `SERVED_R2`), as the other
 * snapshot files are; `/data/*` serves only a scan's. A document of another version is left out.
 */
import { type Ctx, json, requireAdmin } from '../_lib/auth.js'
import { servedStore } from '../_lib/index.js'
import type { UsageSnapshot } from '../../src/usageModel.js'

export const USAGE_PREFIX = 'snapshots/usage/'
export const USAGE_DAYS_MAX = 92
const KEY_RE = /^snapshots\/usage\/(\d{4}-\d{2}-\d{2})\.json$/

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const gated = await requireAdmin(ctx)
  if (gated instanceof Response) return gated
  const n = Number(new URL(ctx.request.url).searchParams.get('days') ?? 30)
  if (!Number.isInteger(n) || n < 1 || n > USAGE_DAYS_MAX) return json({ error: `days: an integer 1..${USAGE_DAYS_MAX}` }, 400)
  const store = servedStore(ctx.env, [USAGE_PREFIX])
  const keys: string[] = []
  let cursor: string | undefined
  do {
    const page = await store.list(USAGE_PREFIX, { cursor })
    for (const e of page.entries) if (!e.isDir && KEY_RE.test(e.key)) keys.push(e.key)
    cursor = page.cursor
  } while (cursor)
  keys.sort()
  const dec = new TextDecoder()
  const docs = await Promise.all(keys.slice(-n).map(async k => JSON.parse(dec.decode((await store.get(k)).bytes)) as UsageSnapshot))
  return json({ days: docs.filter(d => d.version === 1) }, 200, { 'cache-control': 'private, max-age=300' })
}
