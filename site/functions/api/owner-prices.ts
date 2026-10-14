// POST /api/owner-prices?n=64 — fill the owner prefix history (`_lib/ownerHist.ts`, specs/owner-prefix-history.md):
// price up to `n` of the ledger's prefixes not yet priced through the interval store's newest scan, in one read.
// Admin only. Call until `remaining` is 0: the first fill, and after scans are appended (until the scan job does it).
import { type Ctx, json, requireAdmin } from '../_lib/auth.js'
import { fillOwnerHist } from '../_lib/ownerTotals.js'

/** The most a call prices: one history read's row-group budget grows with it. */
const MAX_N = 128

export const onRequestPost = async (ctx: Ctx): Promise<Response> => {
  const id = await requireAdmin(ctx)
  if (id instanceof Response) return id
  const n = Math.min(MAX_N, Math.max(1, Number(new URL(ctx.request.url).searchParams.get('n') ?? 64) || 64))
  const t0 = Date.now()
  const got = await fillOwnerHist(ctx.env, n)
  if (!got) return json({ error: 'no interval store serves this deployment' }, 409)
  return json({ ...got, ms: Date.now() - t0 }, 200, { 'cache-control': 'no-store' })
}
