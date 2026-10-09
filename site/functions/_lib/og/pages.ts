/** Card data for the non-map pages (specs/done/dogi.md, phase 2): `/staged`,
 * `/users`, `/user/:id` (the map card under that user's lens) and
 * `/assignments`. Same rule as the map: the data read is the page's; the tier
 * only decides what is labelled. Failures become a card that says so. */
import type { Env } from '../auth.js'
import { canonId, loadRegistry } from '../identity.js'
import { loadLedger } from '../ledger.js'
import { ownerTotals } from '../ownerTotals.js'
import { openPlanId } from '../plans.js'
import { prefixesAt, type PrefixStat } from '../prefixes.js'
import { filterStaged } from '../../../src/stagedFilter.js'
import { ownerIndex } from '../../../src/ownerIndex.js'
import { type CardData, type CardTile, type Grid, fmtB } from './card.js'
import { mapCard, ownerColors, resolveScan, UNOWNED_COLOR, type Site } from './data.js'
import type { OgTier } from './sign.js'

const fmtN = (n: number) => n.toLocaleString('en-US')
type Reg = Awaited<ReturnType<typeof loadRegistry>>

async function regOf(env: Env): Promise<Reg> {
  return loadRegistry(env).catch(() => ({}) as Reg)
}

async function scanUsersOf(env: Env, date: string) {
  // Colour slots follow the scan's ranking (the map's), from owner totals.
  const t = await ownerTotals(env, date)
  return Object.entries(t.users).map(([u, v]) => ({ u, b: v.b })).sort((a, b) => b.b - a.b)
}

/** `/user/<id>`: the map at the root, under that user's lens. */
export function userCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier): Promise<CardData> {
  return mapCard(env, site, title, { o: params.id, ...(params.d ? { d: params.d } : {}) }, tier)
}

/** `/users`: everyone's owned bytes as a treemap, one tile per person. */
export async function usersCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier): Promise<CardData> {
  const base = { tier, site: site.name, title, tiles: [] as CardTile[] }
  const date = await resolveScan(env, params.d)
  if (!date) return { ...base, subtitle: '', total: '', empty: 'no scans yet' }
  const [t, reg] = await Promise.all([ownerTotals(env, date), regOf(env)])
  const users = Object.entries(t.users).map(([u, v]) => ({ u, b: v.b })).sort((a, b) => b.b - a.b)
  const color = ownerColors(users)
  const owned = users.reduce((s, u) => s + u.b, 0)
  const tiles: CardTile[] = users.map(u => ({ name: reg[u.u]?.name ?? u.u, b: u.b, color: color(u.u) }))
  if (t.bytes > owned) tiles.push({ name: 'unowned', b: t.bytes - owned, color: UNOWNED_COLOR })
  return {
    ...base,
    subtitle: `scan ${date} · ${users.length} people`,
    total: `${fmtB(owned)} owned of ${fmtB(t.bytes)}`,
    tiles,
  }
}

/** `/assignments`: the assigner × assignee matrix as a heatmap. */
export async function assignmentsCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier): Promise<CardData> {
  const base = { tier, site: site.name, title, tiles: [] as CardTile[] }
  const date = await resolveScan(env, params.d)
  if (!date || !env.DB) return { ...base, subtitle: '', total: '', empty: 'no scans yet' }
  const [t, reg, ue] = await Promise.all([
    ownerTotals(env, date),
    regOf(env),
    env.DB.prepare('SELECT email, user FROM user_emails').all<{ email: string; user: string }>().then(r => r.results).catch(() => []),
  ])
  const emap = new Map(ue.map(r => [r.email.toLowerCase(), r.user]))
  const cells = new Map<string, number>()
  for (const c of t.assignments) {
    if (c.repainted_by || !c.owner) continue
    const by = c.who ? (emap.get(c.who.toLowerCase()) ?? c.who) : 'unknown'
    const k = `${by}\u0000${c.owner}`
    cells.set(k, (cells.get(k) ?? 0) + c.bytes)
  }
  const rowB = new Map<string, number>()
  const colB = new Map<string, number>()
  for (const [k, b] of cells) {
    const [by, to] = k.split('\u0000')
    rowB.set(by, (rowB.get(by) ?? 0) + b)
    colB.set(to, (colB.get(to) ?? 0) + b)
  }
  const rows = [...rowB].sort((a, b) => b[1] - a[1]).map(e => e[0]).slice(0, 9)
  const cols = [...colB].sort((a, b) => b[1] - a[1]).map(e => e[0]).slice(0, 11)
  const name = (u: string) => reg[canonId(u, reg)]?.name ?? u.replace(/@.*$/, '')
  const grid: Grid = {
    rows: rows.map(name),
    cols: cols.map(name),
    cells: [...cells].flatMap(([k, b]) => {
      const [by, to] = k.split('\u0000')
      const i = rows.indexOf(by)
      const j = cols.indexOf(to)
      return i >= 0 && j >= 0 ? [[i, j, b] as [number, number, number]] : []
    }),
  }
  const total = [...cells.values()].reduce((s, b) => s + b, 0)
  return {
    ...base,
    subtitle: `scan ${date} · ${rowB.size} assigners × ${colB.size} assignees`,
    total: `${fmtB(total)} assigned`,
    grid,
    ...(cells.size ? {} : { empty: 'no assignments yet' }),
  }
}

/** One staged item at the scan: its stats and effective owner. */
interface Item { prefix: string; addedBy: string; stat?: PrefixStat; owner: string | null }

/** `/staged` (+ `q`): the open plan's items at the latest scan, a tile per
 * bucket holding its items, coloured by owner (the ledger's assignee, else
 * the scan's top owner). */
export async function stagedCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier): Promise<CardData> {
  const base = { tier, site: site.name, title, tiles: [] as CardTile[] }
  if (!env.DB) return { ...base, subtitle: '', total: '', empty: 'nothing staged' }
  const plan = await openPlanId(env.DB).catch(() => null)
  const rows = plan == null ? [] : (await env.DB.prepare('SELECT prefix, added_by FROM plan_items WHERE plan_id = ?').bind(plan).all<{ prefix: string; added_by: string }>()).results
  const date = await resolveScan(env, undefined)
  if (!rows.length || !date) return { ...base, subtitle: plan == null ? '' : `plan #${plan}`, total: '', empty: 'nothing staged' }
  const [{ stats }, reg, ledger, users] = await Promise.all([
    prefixesAt(env, date, rows.map(r => r.prefix).slice(0, 1000)),
    regOf(env),
    loadLedger(env).catch(() => ({ ownerRows: [] })),
    scanUsersOf(env, date).catch(() => []),
  ])
  const idx = ownerIndex({ owners: ledger.ownerRows.map(r => ({ ...r, who: r.who ?? '', memo: null })) })
  let items: Item[] = rows.map(r => {
    const stat = stats[r.prefix]
    const assignment = idx.assignmentOf(r.prefix)
    return { prefix: r.prefix, addedBy: r.added_by, stat, owner: assignment ? canonId(assignment.who, reg) : stat?.us?.[0]?.[0] ?? null }
  })
  const name = (who: string) => reg[canonId(who, reg)]?.name ?? who.replace(/@.*$/, '')
  if (params.q) {
    const f = filterStaged(items, params.q, i => ({ prefix: i.prefix, owners: i.owner ? [i.owner] : [], stagedBy: i.addedBy }), name)
    if (f.error) return { ...base, subtitle: `plan #${plan} · scan ${date}`, total: '', empty: 'invalid filter' }
    items = f.rows
  }
  const color = ownerColors(users)
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
