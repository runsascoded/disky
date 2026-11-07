/** The size-over-time card (`?section=over-time`): the page's "Size over time" chart (`src/SizeOverTime.tsx`) as a
 * card, from the series the page reads (`/api/series`'s `seriesRead`), scoped as the page scopes it. */
import type { Env } from '../auth.js'
import { canonId, loadRegistry } from '../identity.js'
import { ownerKey, parseOwner } from '../scope.js'
import { scanTimes } from '../scanTimes.js'
import { seriesRead } from '../../api/series.js'
import { slotColor } from '../../../src/colors.js'
import { scanCmp, scanInstant, type ScanTimes } from '../../../src/scanSlug.js'
import { stackSeries } from '../../../src/series.js'
import { type CardData, type Chart, fmtB, type LegendItem } from './card.js'
import { resolveScan, type Site } from './data.js'
import { fmtChange } from './diff.js'
import type { OgTier } from './sign.js'

type Point = { date: string; b: number; o: number }
/** `/api/series`'s body, as the card reads it. */
export interface SeriesBody { points: Point[]; roots?: { path: string; points: Point[] }[] }

/** The `/api/series` query the page sends for a card view (`SizeOverTime`'s `scope`): a person in `o` → `lens=user:`
 * (canonical), a pool or `!` negation → `o`, the filter as `q` (+ `qs`), `cl` as is; the unscoped store root splits
 * by root (`split=roots`). Null for `o=me`: a card has no viewer to resolve. */
export function seriesQuery(params: Record<string, string>, canon: (u: string) => string = u => u): URLSearchParams | null {
  if (params.o === 'me') return null
  const sp = new URLSearchParams({ path: params.path ?? '' })
  const pool = params.o === undefined ? undefined : parseOwner(params.o)
  if (pool) sp.set('o', ownerKey(pool))
  else if (params.o) sp.set('lens', `user:${canon(params.o)}`)
  if (params.f) { sp.set('q', params.f); if (params.qs) sp.set('qs', params.qs) }
  if (params.cl) sp.set('cl', params.cl)
  if (!params.path && params.o === undefined && !params.f && !params.cl) sp.set('split', 'roots')
  return sp
}

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
const DAY_MS = 86_400_000
const YEAR_MS = 365 * DAY_MS
const fmtN = (n: number) => n.toLocaleString('en-US')
/** A UTC date: `2026-10-12`. */
const ymd = (x: number) => new Date(x).toISOString().slice(0, 10)

/** The card for a series `body`, drawn through `scan` (the view's selected scan; later points are left off, so a card
 * of an older scan stays that card). Pure. */
export function seriesCardData(
  site: Site,
  title: string,
  tier: OgTier,
  body: SeriesBody,
  times: ScanTimes,
  scan: string,
  publicLabels = false,
): CardData {
  const upTo = (ps: Point[]) => ps.filter(p => scanCmp(p.date, scan, times) <= 0)
  const xy = (ps: Point[]) => ps.map(p => ({ x: scanInstant(p.date, times), y: p.b })).sort((a, b) => a.x - b.x)
  const points = upTo(body.points)
  const base = { tier, site: site.name, title, tiles: [], ...(publicLabels ? { publicLabels: true } : {}) }
  if (points.length < 2) return { ...base, subtitle: '', total: points.length ? `${fmtB(points[0].b)} · ${fmtN(points[0].o)} objects` : '', empty: 'not enough scans yet' }
  const total = xy(points)
  const [first, last] = [points[0], points[points.length - 1]]
  const [x0, x1] = [total[0].x, total[total.length - 1].x]
  const subtitle = `${ymd(x0)} → ${ymd(x1)} · ${fmtN(points.length)} scans · ${fmtChange(last.b - first.b)}`
  const footer = `${fmtB(last.b)} · ${fmtN(last.o)} objects`
  if (total.every(p => p.y === 0)) return { ...base, subtitle, total: footer, empty: 'nothing here in any scan' }
  // The store root's buckets, as the page stacks them: slot by size at the latest scan (largest = slot 0).
  const roots = (body.roots ?? []).map(r => ({ path: r.path, points: upTo(r.points) })).filter(r => r.points.length)
  const latest = (r: { points: Point[] }) => { const p = r.points[r.points.length - 1]; return p.date === last.date ? p.b : -1 }
  const bySize = roots.length > 1 ? [...roots].sort((a, b) => latest(b) - latest(a)) : []
  const slot = new Map(bySize.map((r, i) => [r.path, i]))
  const bands = bySize.length ? stackSeries(roots.map(r => ({ key: r.path, points: xy(r.points) }))) : []
  const fmtX = x1 - x0 > YEAR_MS
    ? (x: number) => { const d = new Date(x); return `${MONTHS[d.getUTCMonth()]} ’${String(d.getUTCFullYear()).slice(2)}` }
    : x1 - x0 < 2 * DAY_MS
      ? (x: number) => { const d = new Date(x); return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()} ${new Date(x).toISOString().slice(11, 16)}` }
      : (x: number) => { const d = new Date(x); return `${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}` }
  const chart: Chart = {
    total: total.map(p => [p.x, p.y]),
    ...(bands.length ? { bands: bands.map(b => ({ color: slotColor(slot.get(b.key)!), points: b.points.map(p => [p.x, p.y0, p.y] as [number, number, number]) })) } : {}),
    fmtX,
  }
  const legend: LegendItem[] = bySize.map(r => ({ label: r.path, color: slotColor(slot.get(r.path)!), b: Math.max(0, latest(r)) }))
  return { ...base, subtitle, total: footer, chart, ...(legend.length ? { legend } : {}) }
}

/** The card for a series view (`routes.ts` params): the page's series through the view's scan. A refusal (a filter the
 * series can't sum, a hidden path) is drawn as such; a read error as "series unavailable", never cached. */
export async function seriesCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier, origin: string): Promise<CardData> {
  const base = { tier, site: site.name, title, subtitle: '', total: '', tiles: [] }
  const scan = await resolveScan(env, params.d, !!params.f)
  if (!scan) return { ...base, empty: 'no scans yet' }
  const reg = params.o ? await loadRegistry(env).catch(() => ({}) as Awaited<ReturnType<typeof loadRegistry>>) : {}
  const q = seriesQuery(params, u => canonId(u, reg))
  if (!q) return { ...base, empty: 'choose a named owner before sharing' }
  const r = await seriesRead({ request: new Request(`${origin}/api/series?${q}`), env }, async () => null)
  if (!r.ok) {
    const why = await r.text().catch(() => '')
    console.log(`series card: ${params.path ?? '/'} ${q}: ${r.status} ${why.slice(0, 200)}`)
    if (r.status === 404) return { ...base, empty: 'path not found' }
    if (r.status === 400) return { ...base, empty: params.f ? 'filter not charted over time' : 'series unavailable' }
    return { ...base, empty: 'series unavailable', failed: true }
  }
  const body = await r.json() as SeriesBody & { failed?: string[] }
  const times = await scanTimes(env, body.points.map(p => p.date))
  const card = seriesCardData(site, title, tier, body, times, scan, !!env.PUBLIC_READ)
  // Points lost to read errors: drawn (gaps beat no card), never cached.
  return body.failed?.length ? { ...card, failed: true } : card
}
