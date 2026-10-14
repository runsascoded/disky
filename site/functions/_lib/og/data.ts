/** Card data from the deployment's own reads (specs/done/dogi.md): the same
 * thresholded, depth-capped view the page draws, its selected colours (the
 * scan's attribution with the live ledger applied, as the map does), and the
 * header/footer text. The tier decides only what is *shown* (`card.ts`): the
 * data read is the same. */
import { FilterRejected } from '../indexedOnly.js'
import type { Env } from '../auth.js'
import { pathScans, servedStore, type Lens } from '../index.js'
import { loadRegistry, canonId } from '../identity.js'
import { loadLedger } from '../ledger.js'
import { scanTimes } from '../scanTimes.js'
import { parseOwner, queryParam, QueryError } from '../scope.js'
import { snapshotsPrefix } from '../shared.js'
import { decodeSel, resolveAfter } from '../../../src/scanSlug.js'
import { ATTEN_DEFAULT, buildView, MIN_AREA_DEFAULT, NotFound } from '../view.js'
import { hexQuery } from '../staticFilter.js'
import { dateColor, HI_CONTRAST, slotColor } from '../../../src/colors.js'
import { mapColorMode } from '../../../src/colorMode.js'
import { barControls } from '../../../src/pageBar.js'
import { listingTime } from '../../../src/listingTime.js'
import { applyLedger } from '../../../src/ledgerOverlay.js'
import { ownerIndex } from '../../../src/ownerIndex.js'
import type { ColorMode, Meta, TreeNode, UserInfo } from '../../../src/types.js'
import { type CardData, type CardTile, type LegendItem, fmtB } from './card.js'
import type { OgTier } from './sign.js'

/** The unowned pool's colour (the dark theme's `--t-unattr`). */
export const UNOWNED_COLOR = '#4a4943'
const FOLD_COLOR = '#2b2e35'

/** The treemap box on the card (`card.ts`), the view's pixel budget. */
const BOX_W = 1120
const BOX_H = 410

/** A `?d=` selection's "after" scan prefix (ISO), in any spelling the page
 * accepts (`scanSlug.ts` `decodeSel`): dashless compact (`261002`,
 * `2610021200`), legacy (`261002-1200`) or ISO (`2026-10-02T1200`). A
 * look-back or `from` suffix is ignored (the card draws the after scan). */
export function scanOfSel(d: string | undefined): string | undefined {
  return decodeSel(d)?.d
}

/** A card's `?d=` names no indexed scan: `serveCard` answers 404, never a card
 * of another scan. */
export class NoScanMatch extends Error {
  constructor(readonly d: string) { super(`no scan matches d=${d}`) }
}

/** The scan a card draws: the latest scan matching the selection (the
 * resolver — a day's slug is that day's latest scan); the latest scan when
 * `d` pins no end (absent, or a look-back only). Throws `NoScanMatch` when it
 * names no scan. Null only when there are no scans at all. */
export async function resolveScan(env: Env, d: string | undefined): Promise<string | null> {
  const scans = (await pathScans(env, true)).results.map(r => r.date)
  const sel = decodeSel(d)
  const times = await scanTimes(env, scans)
  if (sel?.invalid || sel?.d) {
    const scan = resolveAfter(sel, scans, times)
    if (!scan) throw new NoScanMatch(d!)
    return scan
  }
  return resolveAfter(undefined, scans, times)
}

/** The scan's color facts: attribution ranks and access-log observation window. */
async function scanMeta(env: Env, date: string): Promise<Pick<Meta, 'users' | 'access'>> {
  try {
    const own = env.STORE_KEY ? snapshotsPrefix(env) : 'snapshots/'
    const store = servedStore(env, [own])
    const { bytes } = await store.get(`${own}${date}/meta.json`)
    return JSON.parse(new TextDecoder().decode(bytes)) as Pick<Meta, 'users' | 'access'>
  } catch {
    return {}
  }
}

/** Owner colours by the scan's rank (the map's `userColor`). */
export function ownerColors(users: UserInfo[]): (u: string | null) => string {
  const rank = new Map(users.map((u, i) => [u.u, i]))
  return u => {
    const r = u == null ? undefined : rank.get(u)
    return r == null ? UNOWNED_COLOR : HI_CONTRAST[r % HI_CONTRAST.length]
  }
}

/** A node's dominant owner: the top user slice, or null when the unowned
 * remainder outweighs it. */
export function dominant(n: TreeNode): string | null {
  const [top] = n.us ?? []
  if (!top) return null
  const owned = (n.us ?? []).reduce((s, [, b]) => s + b, 0)
  return top[1] >= n.b - owned ? top[0] : null
}

/** Tiles: the view's children (folds grey), each with its own children. */
export function tilesOf(tree: TreeNode, color: (u: string | null) => string, mode: ColorMode = 'user', access?: Meta['access']): CardTile[] {
  const realKids = (n: TreeNode) => (n.c ?? []).filter(k => !k.n.startsWith('(')).sort((a, b) => b.b - a.b)
  const ranks = new Map(realKids(tree).map((n, i) => [n, i]))
  // Only these leaves are drawn by the card's two-level renderer, not their
  // unloaded descendants or parent means (the dashboard uses drawn leaves too).
  const dates = (tree.c ?? []).flatMap(n => n.c?.length ? n.c : [n]).filter(n => !n.n.startsWith('(') && n.d != null).map(n => n.d!)
  const lo = dates.length ? Math.min(...dates) : 0
  const hi = dates.length ? Math.max(...dates) : 0
  const fill = (n: TreeNode, slot: number, i = 0, count = 1, container = false): string => {
    if (n.n.startsWith('(')) return FOLD_COLOR
    if (mode === 'tree') return slotColor(slot, i, count)
    if (mode === 'user') return color(dominant(n))
    if (container) return '#232322'
    if (mode === 'date') return n.d != null && hi > lo ? dateColor((n.d - lo) / (hi - lo)) : '#5d5c55'
    return n.a != null && access && access.to > access.from ? dateColor((n.a - access.from) / (access.to - access.from)) : '#6e332b'
  }
  const tile = (n: TreeNode): CardTile => n.n.startsWith('(')
    ? { name: n.n, b: n.b, color: FOLD_COLOR }
    : realTile(n)
  const realTile = (n: TreeNode): CardTile => {
    const kids = realKids(n)
    const kidRanks = new Map(kids.map((k, i) => [k, i]))
    const slot = ranks.get(n)!
    return {
      name: n.n, b: n.b, color: fill(n, slot, 0, 1, !!n.c?.length),
      ...(n.c?.length ? { kids: n.c.map(k => ({ name: k.n, b: k.b, color: fill(k, slot, kidRanks.get(k) ?? 0, kids.length) })) } : {}),
    }
  }
  return (tree.c ?? []).map(tile)
}

/** The legend: the root's owners by bytes, then the unowned remainder. */
export function legendOf(tree: TreeNode, color: (u: string | null) => string, name: (u: string) => string, max = 6): LegendItem[] {
  const us = (tree.us ?? []).slice(0, max)
  const owned = (tree.us ?? []).reduce((s, [, b]) => s + b, 0)
  const items: LegendItem[] = us.map(([u, b]) => ({ label: name(u), color: color(u), b }))
  if (tree.b - owned > 0) items.push({ label: 'unowned', color: UNOWNED_COLOR, b: tree.b - owned })
  return items.sort((a, b) => b.b - a.b).slice(0, max)
}

const fmtN = (n: number) => n.toLocaleString('en-US')

export interface Site { name: string; scheme: string; defaultColor?: ColorMode }

/** The map card for a view (`routes.ts` params). Errors become a card that
 * says so, never a throw: an unfurl must always get an image. */
export async function mapCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier): Promise<CardData> {
  const base = { tier, site: site.name, title, tiles: [] as CardTile[], ...(env.PUBLIC_READ ? { publicLabels: true } : {}) }
  const date = await resolveScan(env, params.d)
  if (!date) return { ...base, subtitle: '', total: '', empty: 'no scans yet' }
  const path = params.path ?? ''
  const filterNote = params.f ? ` · filter “${params.f}”` : ''
  const meta = await scanMeta(env, date)
  const users = meta.users ?? []
  const available = barControls({ owners: !!users.length, hasAttr: !!users.length, classes: false, readRange: !!meta.access }).color
  const mode = mapColorMode(params.c, available, site.defaultColor ?? (users.length ? 'user' : 'tree'))
  const colorLabel = mode === 'date' ? listingTime(site.scheme).label : mode === 'user' ? 'owner' : mode
  const subtitle = `scan ${date}${filterNote} · color by ${colorLabel}`
  let query
  try {
    query = params.f ? queryParam(new URLSearchParams({ q: params.f, ...(params.qs ? { qs: params.qs } : {}) }), env.QUERY_SYNTAX).query : undefined
  } catch (e) {
    if (e instanceof QueryError) return { ...base, subtitle, total: '', empty: 'invalid filter' }
    throw e
  }
  const reg = await loadRegistry(env).catch(() => ({}) as Awaited<ReturnType<typeof loadRegistry>>)
  const owner = parseOwner(params.o ?? null)
  const lensUser = !owner && params.o && params.o !== 'me' ? canonId(params.o, reg) : null
  const lens: Lens | undefined = lensUser ? { key: lensUser } : undefined
  let tree: TreeNode
  try {
    // The static index's hex-run rule: the card's filter matches as the map's does.
    query = (await hexQuery(env, query)).query
    const view = await buildView(env, { date, path, w: BOX_W, h: BOX_H, minArea: MIN_AREA_DEFAULT, atten: ATTEN_DEFAULT, lens, owner, query, maxDepth: 2 })
    tree = view.tree
  } catch (e) {
    if (e instanceof NotFound) return { ...base, subtitle, total: '', empty: 'path not found' }
    if (e instanceof FilterRejected) return { ...base, subtitle, total: '', empty: 'search unavailable' }
    return { ...base, subtitle, total: '', empty: 'view unavailable' }
  }
  // The live assignments over the scan's attribution, as the map draws them.
  if (!lens && env.DB) {
    const { ownerRows } = await loadLedger(env).catch(() => ({ ownerRows: [] }))
    const idx = ownerIndex({ owners: ownerRows.map(r => ({ ...r, who: r.who ?? '', memo: null })) })
    if (idx.count) tree = applyLedger(tree, idx, site.scheme + (path ? `${path}/` : ''), u => canonId(u, reg))
  }
  const color = ownerColors(users)
  const name = (u: string) => reg[u]?.name ?? u
  const tiles = tilesOf(tree, color, mode, meta.access)
  return {
    ...base,
    subtitle,
    total: `${fmtB(tree.b)} · ${fmtN(tree.o)} objects`,
    tiles,
    legend: mode === 'user' ? legendOf(tree, color, name) : mode === 'tree'
      ? tiles.filter(t => !t.name.startsWith('(')).sort((a, b) => b.b - a.b).slice(0, 6).map(t => ({ label: t.name, b: t.b, color: t.color }))
      : [],
    ...(tree.b ? {} : { empty: query ? 'no matches' : 'empty' }),
  }
}
