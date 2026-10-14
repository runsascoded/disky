// The footer LRU (`_lib/footerCache.ts`, specs/done/footer-lru.md). Admin only.
// GET  /api/footer-cache        — the entries by state, and the budget.
// POST /api/footer-cache?dry=1  — one eviction pass (`evictFooters`; `dry=1` reports without writing). A pass
//      demotes past the budget and deletes what was demoted at least `EVICT_GRACE_S` ago: call again after
//      that to finish (or run `dt-cloud footer-evict`, which waits).
import { type Ctx, json, requireAdmin } from '../_lib/auth.js'
import { evictFooters, footerBudget } from '../_lib/footerCache.js'

const NO_STORE = { 'cache-control': 'no-store' }

export const onRequestGet = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  if (!ctx.env.DB) return json({ error: 'no D1' }, 503)
  const { results } = await ctx.env.DB.prepare('SELECT state, COUNT(*) AS entries, SUM(rows) AS rows, SUM(bytes) AS bytes FROM footer_cache GROUP BY state ORDER BY state').all<{ state: string; entries: number; rows: number; bytes: number }>()
  return json({ budget: footerBudget(ctx.env), states: results }, 200, NO_STORE)
}

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  if (!ctx.env.DB) return json({ error: 'no D1' }, 503)
  const dryRun = new URL(ctx.request.url).searchParams.get('dry') === '1'
  return json(await evictFooters(ctx.env.DB, footerBudget(ctx.env), { dryRun }), 200, NO_STORE)
}
