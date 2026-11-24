// POST /api/scan-runs/month-op[?store=<key>] — a deployment's monthly admin thread's opening post (OP;
// specs/admin-monthly-thread.md), for the scan job (`dt_cloud.nominal`) to post in `#<deploy>-admin`:
//
//   {month: 'YYYY-MM', prev_url?, next_url?, color?, emoji?}
//     → {text, card_url}      viewer (the job's grant), as `/api/scan-runs/card-url`
//     400: a malformed body, or a month not started yet
//     503: no `SESSION_SECRET` (nothing to sign the card with), no D1, or no scan-run tables
//
// `text` is `@disk-tree/pages-kit/admin/monthOp`'s, over the store's scan runs (D1 `scan_runs`, as `/api/scan-runs`
// summarizes them) and the meta store's root series (`STORES_JSON` `meta`, read as `/api/series?store=meta` reads it,
// under the caller's own credential: a grant without the meta store's `scope` or `series_scope` gets its line's
// "size unavailable (HTTP 403)"). `prev_url` / `next_url` are the months either side's threads (https permalinks).
// `card_url` is the status card's signed URL (`deployCardUrl`, as `card-url` mints it, `color` its dot), or null for a
// closed month: a signed URL lasts `IMAGE_TTL_DAYS`, and the card draws the last runs as of now, not the month's.
// `emoji` overrides the outcomes' marks (`nominal.Config.emoji`: `{outcome: ':code:' | characters}`).
import { type Ctx, json, requireViewer } from '../../_lib/auth.js'
import { withPathStore } from '../../_lib/index.js'
import { colorParam, deployCardUrl, type DeployEnv } from '../../_lib/og/deployData.js'
import { deploymentKey, type OgEnv } from '../../_lib/og/serve.js'
import { loadScanRuns, scanRunsList } from '../../_lib/scanRuns.js'
import { secondaryStores, storeKey, StoreConfigError, withStore } from '../../_lib/stores.js'
import { seriesRead, summaryGate } from '../series.js'
import { scanTime } from '../../../src/scanSlug.js'
import { GRADES, type Grade, type MetaSeries, monthBounds, type MonthOpInput, monthOpText } from '@disk-tree/pages-kit/admin/monthOp'

const NO_STORE = { 'cache-control': 'no-store' }
const MONTH_RE = /^\d{4}-(0[1-9]|1[0-2])$/
/** A thread permalink: https, nothing a markdown link would end at. */
const URL_RE = /^https:\/\/[^\s()<>[\]]+$/
/** An outcome mark (`nominal.EMOJI_RE`): a Slack `:name:` code, or emoji characters. */
const MARK_RE = /^(:[a-z0-9_+'-]{1,100}:|[^\x00-\x7f]{1,16})$/u

/** The store whose size the OP's Metadata line gives: the "data behind this site" (`STORES_JSON` `meta`). */
export const META_STORE = 'meta'

export interface MonthOpRequest {
  month: string
  prevUrl: string | null
  nextUrl: string | null
  color: string | null
  marks: Partial<Record<Grade, string>>
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)

/** The request, or why it's refused. */
export function parseMonthOpRequest(b: unknown): MonthOpRequest | string {
  if (!isObj(b)) return 'body: a JSON object'
  const { month, prev_url, next_url, color, emoji } = b
  if (typeof month !== 'string' || !MONTH_RE.test(month)) return 'month: YYYY-MM'
  for (const [k, v] of [['prev_url', prev_url], ['next_url', next_url]] as const) {
    if (v != null && (typeof v !== 'string' || !URL_RE.test(v))) return `${k}: an https URL`
  }
  if (color != null && (typeof color !== 'string' || colorParam(color) === null)) return 'color: #rrggbb'
  if (emoji != null && (!isObj(emoji) || Object.entries(emoji).some(([k, v]) => !(GRADES as readonly string[]).includes(k) || typeof v !== 'string' || !MARK_RE.test(v)))) {
    return `emoji: {outcome: a Slack :name: code or emoji characters}, outcomes ${GRADES.join(', ')}`
  }
  return {
    month,
    prevUrl: (prev_url as string | null | undefined) ?? null,
    nextUrl: (next_url as string | null | undefined) ?? null,
    color: typeof color === 'string' ? colorParam(color) : null,
    marks: (emoji ?? {}) as Partial<Record<Grade, string>>,
  }
}

/** The meta store's root series, read as `GET /api/series?store=meta` reads it, with the caller's credential (its
 *  `authorization` and `cookie`); null when the deployment has no meta store. */
export async function metaSeries(ctx: Ctx): Promise<MonthOpInput['meta']> {
  try {
    if (!Object.hasOwn(secondaryStores(ctx.env), META_STORE)) return null
  } catch (e) {
    if (e instanceof StoreConfigError) return { store: META_STORE, series: { status: 500 } }
    throw e
  }
  const { origin } = new URL(ctx.request.url)
  const headers = new Headers()
  for (const h of ['authorization', 'cookie']) {
    const v = ctx.request.headers.get(h)
    if (v !== null) headers.set(h, v)
  }
  const sctx = withStore({ request: new Request(`${origin}/api/series?store=${META_STORE}`, { headers }), env: ctx.env, data: ctx.data })
  if (sctx instanceof Response) return { store: META_STORE, series: { status: sctx.status } }
  const r = await seriesRead(await withPathStore(sctx), summaryGate)
  if (!r.ok) return { store: META_STORE, series: { status: r.status } }
  const body = await r.json() as { points: { date: string; bytes: number | null }[] }
  const points: Extract<MetaSeries, { points: unknown }>['points'] = body.points.flatMap(p => {
    const t = scanTime(p.date)
    return Number.isFinite(t) && p.bytes != null ? [{ t: t / 1000, bytes: p.bytes }] : []
  })
  return { store: META_STORE, series: { points } }
}

export const onRequestPost = async (ctx0: Ctx & { env: OgEnv; waitUntil?: (p: Promise<unknown>) => void }): Promise<Response> => {
  const ctx = withStore(ctx0)
  if (ctx instanceof Response) return ctx
  const gated = await requireViewer(ctx)
  if (gated instanceof Response) return gated
  const keyP = deploymentKey(ctx.env)
  if (!keyP) return json({ error: 'card URLs not configured (SESSION_SECRET unset)' }, 503, NO_STORE)
  if (!ctx.env.DB) return json({ error: 'no D1 bound' }, 503, NO_STORE)
  const req = parseMonthOpRequest(await ctx.request.json().catch(() => null))
  if (typeof req === 'string') return json({ error: req }, 400, NO_STORE)
  const now = Math.floor(Date.now() / 1000)
  const { start, end } = monthBounds(req.month)
  if (now < start) return json({ error: `month ${req.month} hasn't started` }, 400, NO_STORE)
  const { env } = await withPathStore(ctx)
  const [rows, meta] = await Promise.all([loadScanRuns(env.DB!, storeKey(env)), metaSeries(ctx0 as Ctx)])
  if (!rows) return json({ error: 'scan runs are not set up' }, 503, NO_STORE)
  const { origin } = new URL(ctx.request.url)
  const store = storeKey(env)
  const text = monthOpText(
    { runs: scanRunsList(rows, now).runs, month: req.month, now, meta },
    { site: origin, store, prevUrl: req.prevUrl, nextUrl: req.nextUrl, marks: req.marks },
  )
  const card = now >= end ? null : (await deployCardUrl(await keyP, env as DeployEnv, origin, now, { c: req.color })).url
  return json({ text, card_url: card }, 200, NO_STORE)
}
