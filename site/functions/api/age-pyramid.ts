/** Multi-scale per-path created-time strata — the pyrmts pyramid backend for
 * `AgeChart` (specs/age-index.md, Phase B): a bin the planner picks for the
 * requested window + budget.
 *
 *   GET /api/age-pyramid?date=<scan>&path=<prefix>&from=<iso>&to=<iso>&bin_budget=<n>
 *
 * `planQuery` picks the finest tier (bin `1h|1d|1mo|1y`) whose bin count fits
 * `bin_budget`; we read that one complete path-major tier for `path`'s own rows
 * in `[from, to]` and return `{ records: [{ dt, b, o }], plan }` (the pyrmts
 * `usePyramid` shape — `dt` is the bin-start epoch-ms). `from`/`to` default to
 * the full range (the planner then picks the coarsest tier that fits), so the
 * FE should pass the chart's actual time domain to get useful granularity.
 */
import { type Env, requireViewer } from '../_lib/auth.js'
import { withStore } from '../_lib/stores.js'
import { num, openIndex, readPoint, storeReady } from '../_lib/index.js'
import { AGE_TIERS, planAge } from '../_lib/agePyramid.js'

const COLS = ['path', 'depth', 'binstart', 'b', 'o']

export const onRequestGet = async (ctx0: { request: Request; env: Env }): Promise<Response> => {
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  if (!storeReady(ctx.env)) {
    return new Response('age-pyramid API not configured (missing index store creds)', { status: 503 })
  }
  const url = new URL(ctx.request.url)
  const date = url.searchParams.get('date') ?? ''
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (!/^\d{4}-\d{2}-\d{2}(?:T\d{4})?$/.test(date)) return new Response('bad date', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
  const binBudget = Math.max(1, Number(url.searchParams.get('bin_budget')) || 512)
  const fromMs = Date.parse(url.searchParams.get('from') ?? '')
  const toMs = Date.parse(url.searchParams.get('to') ?? '')

  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated

  const depth = path === '' ? 0 : path.split('/').length
  const fine = AGE_TIERS[0].bin // finest produced tier ('1d')
  // Read the finest tier for the path: it gives the true extent and, in the
  // common case (day-granular history), the rows themselves — so the FE need
  // only pass `path` (+ optional bin_budget). from/to override the extent.
  let baseRows: Record<string, unknown>[]
  try {
    baseRows = await readPoint(await openIndex(ctx.env, date, `age-pyramid-${fine}`), depth, path, COLS)
  } catch (e) {
    if (/not synced/.test((e as Error).message)) return json({ records: [], plan: { outputBin: fine, tier: fine, binBudget } })
    throw e
  }
  if (!baseRows.length) return json({ records: [], plan: { outputBin: fine, tier: fine, binBudget } })
  const dts = baseRows.map(r => num(r.binstart))
  const from = new Date(Number.isNaN(fromMs) ? Math.min(...dts) : fromMs)
  const to = new Date(Number.isNaN(toMs) ? Math.max(...dts) : toMs)

  const plan = planAge(from, to, binBudget)
  const bin = plan.outputTier?.bin ?? plan.outputBin
  const meta = { outputBin: bin, tier: plan.outputTier?.name ?? null, binBudget }
  // Reuse the finest rows when the planner chose that tier; else read the coarser one.
  const rows = bin === fine ? baseRows : await readPoint(await openIndex(ctx.env, date, `age-pyramid-${bin}`), depth, path, COLS)
  const lo = from.getTime()
  const hi = to.getTime()
  const records = rows
    .map(r => ({ dt: num(r.binstart), b: num(r.b), o: num(r.o) }))
    .filter(r => r.dt >= lo && r.dt <= hi)
    .sort((a, b) => a.dt - b.dt)
  return json({ records, plan: meta })
}

const json = (body: unknown): Response =>
  new Response(JSON.stringify(body), {
    headers: { 'content-type': 'application/json', 'cache-control': 'private, max-age=86400' },
  })
