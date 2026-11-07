/**
 * GET /api/cost/<cloud>[?days=<1..92>] → { days: CostSnapshot[] }   admin
 *
 * A cloud's daily cost documents, oldest first: the newest `days` (default 30) of `snapshots/cost/<cloud>/<date>.json`,
 * which the job writes per cloud (`gcp`: the `gcp-cost` step, `dt-cloud cost -o`) to the data store and R2. Read
 * through the served seam (`servedStore`), as `/api/usage` reads Cloudflare's. A cloud outside `COST_CLOUDS` is a 404;
 * a document of a version its cloud doesn't name is left out. A deployment that writes none answers `{ days: [] }`.
 */
import { type Ctx, json, requireAdmin } from '../../_lib/auth.js'
import { servedStore } from '../../_lib/index.js'
import { COST_CLOUDS, type CostSnapshot } from '../../../src/costModel.js'

export const COST_PREFIX = 'snapshots/cost/'
export const COST_DAYS_MAX = 92
const DATE_RE = /^(\d{4}-\d{2}-\d{2})\.json$/

type C = Ctx & { params?: { cloud?: string | string[] } }

export const onRequestGet = async (ctx: C): Promise<Response> => {
  const gated = await requireAdmin(ctx)
  if (gated instanceof Response) return gated
  const cloud = String(ctx.params?.cloud ?? '')
  const version = Object.keys(COST_CLOUDS).includes(cloud) ? COST_CLOUDS[cloud].version : undefined
  if (version === undefined) return json({ error: `unknown cloud: ${cloud} (known: ${Object.keys(COST_CLOUDS).join(', ')})` }, 404)
  const n = Number(new URL(ctx.request.url).searchParams.get('days') ?? 30)
  if (!Number.isInteger(n) || n < 1 || n > COST_DAYS_MAX) return json({ error: `days: an integer 1..${COST_DAYS_MAX}` }, 400)
  const prefix = `${COST_PREFIX}${cloud}/`
  const store = servedStore(ctx.env, [prefix])
  const keys: string[] = []
  let cursor: string | undefined
  do {
    const page = await store.list(prefix, { cursor })
    for (const e of page.entries) if (!e.isDir && e.key.startsWith(prefix) && DATE_RE.test(e.key.slice(prefix.length))) keys.push(e.key)
    cursor = page.cursor
  } while (cursor)
  keys.sort()
  const dec = new TextDecoder()
  const docs = await Promise.all(keys.slice(-n).map(async k => JSON.parse(dec.decode((await store.get(k)).bytes)) as CostSnapshot))
  return json({ days: docs.filter(d => d.version === version) }, 200, { 'cache-control': 'private, max-age=300' })
}
