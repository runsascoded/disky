/** Card data for the non-map pages (specs/done/dogi.md, phase 2): `/staged`
 * (m3 has no owner pages). Same rule as the map: the data read is the page's; the tier
 * only decides what is labelled. Failures become a card that says so. */
import type { Env } from '../auth.js'
import { canonId, loadRegistry } from '../identity.js'
import { openPlanId } from '../plans.js'
import { type PrefixStat } from '../prefixes.js'
import { stagedStats } from '../stagedSlack.js'
import { filterStaged } from '../../../src/stagedFilter.js'
import { type CardData, type CardTile, fmtB } from './card.js'
import { ownerColors, type Site } from './data.js'
import type { OgTier } from './sign.js'

const fmtN = (n: number) => n.toLocaleString('en-US')
type Reg = Awaited<ReturnType<typeof loadRegistry>>

async function regOf(env: Env): Promise<Reg> {
  return loadRegistry(env).catch(() => ({}) as Reg)
}

/** One staged item at the scan: its stats and effective owner. */
interface Item { prefix: string; addedBy: string; stat?: PrefixStat; owner: string | null }

/** `/staged` (+ `q`): the open plan's items at the latest scan, a tile per
 * bucket holding its items, coloured by the scan's top owner. */
export async function stagedCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier): Promise<CardData> {
  const base = { tier, site: site.name, title, tiles: [] as CardTile[] }
  if (!env.DB) return { ...base, subtitle: '', total: '', empty: 'nothing staged' }
  const plan = await openPlanId(env.DB).catch(() => null)
  const rows = plan == null ? [] : (await env.DB.prepare("SELECT prefix, COALESCE(kind, 'prefix') AS kind, added_by FROM plan_items WHERE plan_id = ?").bind(plan).all<{ prefix: string; kind: 'prefix' | 'object'; added_by: string }>()).results
  // Every prefix, sized at the latest scan (as the Slack parent's totals are).
  const at = rows.length ? await stagedStats(env, rows.map(r => r.prefix)) : null
  if (!rows.length || !at) return { ...base, subtitle: plan == null ? '' : `plan #${plan}`, total: '', empty: 'nothing staged' }
  const { scan: date, stats } = at
  const reg = await regOf(env)
  let items: Item[] = rows.map(r => {
    const stat = stats[r.prefix]
    return { prefix: r.prefix, addedBy: r.added_by, stat, owner: stat?.us?.[0]?.[0] ?? null }
  })
  const name = (who: string) => reg[canonId(who, reg)]?.name ?? who.replace(/@.*$/, '')
  if (params.q) {
    const f = filterStaged(items, params.q, i => ({ prefix: i.prefix, owners: i.owner ? [i.owner] : [], stagedBy: i.addedBy }), name)
    if (f.error) return { ...base, subtitle: `plan #${plan} · scan ${date}`, total: '', empty: 'invalid filter' }
    items = f.rows
  }
  const color = ownerColors([])
  const byBucket = new Map<string, Item[]>()
  for (const it of items) {
    const bucket = it.prefix.replace(/^[a-z0-9]+:\/\//, '').split('/')[0]
    byBucket.set(bucket, [...(byBucket.get(bucket) ?? []), it])
  }
  const tiles: CardTile[] = [...byBucket].map(([bucket, its]) => {
    const kids = its.map(i => ({ name: i.prefix, b: i.stat?.b ?? 0, color: color(i.owner) }))
    const b = kids.reduce((s, k) => s + k.b, 0)
    const top = [...its].sort((x, y) => (y.stat?.b ?? 0) - (x.stat?.b ?? 0))[0]
    return { name: bucket, b, color: color(top?.owner ?? null), kids }
  })
  const b = items.reduce((s, i) => s + (i.stat?.b ?? 0), 0)
  const o = items.reduce((s, i) => s + (i.stat?.o ?? 0), 0)
  const byOwner = new Map<string | null, number>()
  for (const i of items) byOwner.set(i.owner, (byOwner.get(i.owner) ?? 0) + (i.stat?.b ?? 0))
  const legend = [...byOwner].filter(([, v]) => v > 0).sort((x, y) => y[1] - x[1]).slice(0, 6)
    .map(([u, v]) => ({ label: u ? (reg[u]?.name ?? u) : 'unowned', color: color(u), b: v }))
  return {
    ...base,
    subtitle: `plan #${plan} · scan ${date}${params.q ? ` · filter “${params.q}”` : ''}`,
    total: `${fmtN(items.length)} prefixes · ${fmtB(b)} · ${fmtN(o)} objects`,
    tiles,
    legend,
    ...(b ? {} : { empty: params.q ? 'no matches' : 'nothing left at this scan' }),
  }
}
