/** Multi-scale per-path created-time strata — the pyrmts pyramid backend for
 * `AgeChart` (specs/done/age-index.md, Phase B): a bin the planner picks for the
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
import { hiddenNotFound, isHidden, isHideRoot, restrictionFor } from '../_lib/restrict.js'
import { type Env, requireViewer } from '../_lib/auth.js'
import { withStore } from '../_lib/stores.js'
import { num, openIndex, readPoint, storeReady } from '../_lib/index.js'
import { AGE_TIERS, planAge } from '../_lib/agePyramid.js'
import { isScanId } from '../../src/scanSlug.js'
import { scanArg } from '../_lib/scanArg.js'
import { memberOf, memberRequest, resolveUnion, unionFor, vectorOf } from '../_lib/union.js'
import type { UnionView } from '../_lib/stores.js'

const COLS = ['path', 'depth', 'binstart', 'b', 'o']

type AgeCtx = { request: Request; env: Env }

export const onRequestGet = async (ctx0: AgeCtx): Promise<Response> => {
  // A union view (`STORES_JSON` `union`, `_lib/union.ts`): its members' rows side by side; a deployment without
  // one never leaves `ageOf`.
  const u = unionFor(ctx0.request, ctx0.env)
  if (u instanceof Response) return u
  return u ? unionAge(ctx0, u) : ageOf(ctx0)
}

type Plan = { outputBin: string; tier: string | null; binBudget: number }
type Rec = { dt: number; b: number; o: number }

/**
 * `/api/age-pyramid` on a union view. The root: each member's own rows at its resolved scan, concatenated —
 * each record qualified by its store (`src`), its absolute bin untouched (never shifted to a scan's time; the
 * chart sums a bin's records). One bin for all: members asked without a window plan their own extents, so when
 * one's bin isn't the one the union's extent plans, all are asked again over that extent (`from`/`to`), which
 * plans that one tier for every member. A member unknown at the cutoff has no rows. Below the root: the path's member answers.
 */
async function unionAge(ctx0: AgeCtx, view: UnionView): Promise<Response> {
  const url = new URL(ctx0.request.url)
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
  const got = await resolveUnion(ctx0, view, true)
  if (got instanceof Response) return got
  const { resolved } = got
  if (path !== '') {
    const m = await memberOf(resolved, path)
    if (!m) return new Response('path not found', { status: 404 })
    return ageOf({ ...ctx0, request: memberRequest(ctx0.request, m.store, { date: m.date! }, ['d']) })
  }
  const ready = resolved.members.filter(m => m.date !== null)
  const ask = async (window: Record<string, string>) => {
    const reads = await Promise.all(ready.map(m => ageOf({ ...ctx0, request: memberRequest(ctx0.request, m.store, { date: m.date!, ...window }, ['d']) })))
    const bad = reads.find(r => !r.ok)
    return bad ?? Promise.all(reads.map(r => r.json() as Promise<{ records: Rec[]; plan: Plan }>))
  }
  let bodies = await ask({})
  if (bodies instanceof Response) return bodies
  // The union's extent plans its own bin; a member that planned another (its own extent was narrower, or wider)
  // is asked again over the union's window, as every member then is.
  const dts = bodies.flatMap(b => b.records.map(r => r.dt))
  const [lo, hi] = [Math.min(...dts), Math.max(...dts)]
  if (dts.length && lo < hi) {
    const window = { from: url.searchParams.get('from') ?? new Date(lo).toISOString(), to: url.searchParams.get('to') ?? new Date(hi).toISOString() }
    const p = planAge(new Date(window.from), new Date(window.to), Math.max(1, Number(url.searchParams.get('bin_budget')) || 512))
    const bin = p.outputTier?.bin ?? p.outputBin
    if (bodies.some(b => b.records.length && b.plan.outputBin !== bin)) {
      bodies = await ask(window)
      if (bodies instanceof Response) return bodies
    }
  }
  const plan = (bodies.find(b => b.records.length) ?? bodies[0])?.plan ?? null
  const records = bodies.flatMap((b, i) => b.records.map(r => ({ ...r, src: ready[i].store }))).sort((a, b) => a.dt - b.dt)
  return json({ records, plan, union: vectorOf(resolved) })
}

async function ageOf(ctx0: AgeCtx): Promise<Response> {
  // `store=<key>`: a secondary store's env overlay (none = the primary, as is).
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  if (!storeReady(ctx.env)) {
    return new Response('age-pyramid API not configured (missing index store creds)', { status: 503 })
  }
  const url = new URL(ctx.request.url)
  const path = (url.searchParams.get('path') ?? '').replace(/\/+$/, '')
  if (!isScanId(url.searchParams.get('date')) && !url.searchParams.has('d')) return new Response('bad date', { status: 400 })
  if (path.includes('..') || path.startsWith('/')) return new Response('bad path', { status: 400 })
  const binBudget = Math.max(1, Number(url.searchParams.get('bin_budget')) || 512)
  const fromMs = Date.parse(url.searchParams.get('from') ?? '')
  const toMs = Date.parse(url.searchParams.get('to') ?? '')

  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const restriction = restrictionFor(gated, ctx.env)
  // …and a hidden dir's own pyramid breaks down its contents by age (more than its one total).
  if (restriction && (isHidden(path, restriction) || isHideRoot(path, restriction))) return hiddenNotFound()
  // An unindexed id, or a `d=` slug matching no scan: a 404, never another scan.
  const scan = await scanArg(ctx.env, url.searchParams)
  if (scan instanceof Response) return scan
  const date = scan

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
  if (from > to) return new Response('bad range: `from` is after `to`', { status: 400 })

  // An extent of one instant (every row in one bin, or `from` = `to`) is that bin at the finest tier: the planner
  // takes only a non-empty range.
  const plan = from < to ? planAge(from, to, binBudget) : null
  const bin = plan ? plan.outputTier?.bin ?? plan.outputBin : fine
  const meta = { outputBin: bin, tier: plan ? plan.outputTier?.name ?? null : fine, binBudget }
  // Reuse the finest rows when the planner chose that tier; else read the coarser one.
  const rows = bin === fine ? baseRows : await readPoint(await openIndex(ctx.env, date, `age-pyramid-${bin}`), depth, path, COLS)
  // Planner extents select a tier; only caller-supplied bounds trim its bins.
  const lo = Number.isNaN(fromMs) ? -Infinity : fromMs
  const hi = Number.isNaN(toMs) ? Infinity : toMs
  const records = rows
    .map(r => ({ dt: num(r.binstart), b: num(r.b), o: num(r.o) }))
    .filter(r => r.dt >= lo && r.dt <= hi)
    .sort((a, b) => a.dt - b.dt)
  return json({ records, plan: meta })
}

const json = (body: unknown): Response =>
  new Response(JSON.stringify(body), {
    headers: { 'content-type': 'application/json', 'cache-control': 'private, no-cache' },
  })
