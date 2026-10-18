/** Card data from the deployment's own reads (specs/done/dogi.md): the same
 * thresholded, depth-capped view the page draws, its selected colours (the
 * scan's attribution with the live ledger applied, as the map does), and the
 * header/footer text. The tier decides only what is *shown* (`card.ts`): the
 * data read is the same. */
import { FilterRejected, indexedOnly } from '../indexedOnly.js'
import type { Env } from '../auth.js'
import { pathScans, servedStore, type Lens } from '../index.js'
import { loadRegistry, canonId } from '../identity.js'
import { loadLedger } from '../ledger.js'
import { scanTimes } from '../scanTimes.js'
import { parseOwner, queryParam, QueryError } from '../scope.js'
import { snapshotsPrefix } from '../shared.js'
import { decodeSel, fmtScanUtc, resolveAfter } from '../../../src/scanSlug.js'
import { ATTEN_DEFAULT, buildView, MIN_AREA_DEFAULT, NotFound, type View } from '../view.js'
import { hexQuery } from '../staticFilter.js'
import { dateColor, HI_CONTRAST, slotColor } from '../../../src/colors.js'
import { mapColorMode } from '../../../src/colorMode.js'
import { barControls } from '../../../src/pageBar.js'
import { listingTime } from '../../../src/listingTime.js'
import { readState, readThrough } from '../../../src/readLens.js'
import { applyLedger } from '../../../src/ledgerOverlay.js'
import { ownerIndex } from '../../../src/ownerIndex.js'
import type { ColorMode, Meta, TreeNode, UserInfo } from '../../../src/types.js'
import { type CardData, type CardTile, type LegendItem, fmtB } from './card.js'
import type { OgTier } from './sign.js'

/** The unowned pool's colour (the dark theme's `--t-unattr`). */
export const UNOWNED_COLOR = '#4a4943'
const FOLD_COLOR = '#2b2e35'
/** A container that carries no colour of its own: date / read modes' dirs, tree mode's lone top-level dirs. */
const FRAME_COLOR = '#232322'

/** The treemap box on the card (`card.ts`), the view's pixel budget. */
const BOX_W = 1120
const BOX_H = 410

/** The card's view depth (specs/done/dogi.md "Depth by budget"): the floor is always read; the deeper
 * views then race the rest of the wall-clock budget, and the deepest one landed within it is drawn.
 * `minArea`/`atten` already prune what a deeper level can add, so the cap only bounds the read. */
export const OG_FLOOR_DEPTH = 2
export const OG_MAX_DEPTH = 5
/** ms from the start of the floor read (`OG_DEPTH_BUDGET_MS`; `0`: the floor alone). Slack's unfurler
 *  waits a few seconds for the whole image. */
export const OG_DEPTH_BUDGET_MS = 1500

export interface DepthPlan { floor: number; deeper: number[]; budgetMs: number }

/** The env's plan: the floor, then depth 3 and the cap (`OG_MAX_DEPTH`), under `OG_DEPTH_BUDGET_MS`. */
export function depthPlan(env: Pick<Env, 'OG_DEPTH_BUDGET_MS' | 'OG_MAX_DEPTH'>): DepthPlan {
  const num = (v: string | undefined, dflt: number) => v != null && v.trim() !== '' && Number.isFinite(Number(v)) && Number(v) >= 0 ? Number(v) : dflt
  const max = Math.max(OG_FLOOR_DEPTH, Math.floor(num(env.OG_MAX_DEPTH, OG_MAX_DEPTH)))
  return { floor: OG_FLOOR_DEPTH, deeper: [...new Set([OG_FLOOR_DEPTH + 1, max])].filter(d => d > OG_FLOOR_DEPTH && d <= max), budgetMs: num(env.OG_DEPTH_BUDGET_MS, OG_DEPTH_BUDGET_MS) }
}

/** Iterative deepening under a wall-clock budget: `read(floor)` (awaited whatever it takes); then, if
 * `more` says its view can deepen and budget is left, every `deeper` read at once, raced against the
 * rest of the budget; the deepest that landed wins, else the floor. A deeper read that fails or runs
 * past the budget is dropped (never awaited, never thrown). */
export async function deepestView<T>(plan: DepthPlan, read: (depth: number) => Promise<T>, more: (v: T) => boolean, now: () => number = Date.now): Promise<{ value: T; depth: number }> {
  const t0 = now()
  const floor = await read(plan.floor)
  const left = plan.budgetMs - (now() - t0)
  if (!plan.deeper.length || left <= 0 || !more(floor)) return { value: floor, depth: plan.floor }
  const landed = new Map<number, T>()
  const runs = plan.deeper.map(d => read(d).then(v => { landed.set(d, v) }, () => {}))
  let timer: ReturnType<typeof setTimeout> | null = null
  await Promise.race([Promise.all(runs), new Promise<void>(res => { timer = setTimeout(res, left) })])
  clearTimeout(timer)
  const depth = Math.max(...landed.keys(), plan.floor)
  return { value: landed.get(depth) ?? floor, depth }
}

/** A view that a deeper read could add cells to: some child of the view's children has children of
 * its own to draw (a level whose cells all fall under the threshold has no deeper cells either). */
export const canDeepen = (tree: TreeNode, floor = OG_FLOOR_DEPTH): boolean => {
  const at = (n: TreeNode, d: number): boolean => d === floor ? !n.n.startsWith('(') && n.k !== 'file' : (n.c ?? []).some(k => at(k, d + 1))
  return at(tree, 0)
}

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

/** Tiles: the view's children (folds grey), each with its children nested to the view's depth. In
 * `tree` mode a cell takes its depth-1 ancestor's slot and its depth-2 ancestor's rank among siblings
 * (the map's `slotOf`), the slots one level further down for each lone top-level dir (a single
 * bucket's view would otherwise be one hue; the lone dirs are a neutral frame); in `date`/`read` modes a
 * container is neutral and its leaves carry the scale. */
export function tilesOf(tree: TreeNode, color: (u: string | null) => string, mode: ColorMode = 'user', access?: Meta['access']): CardTile[] {
  const realKids = (n: TreeNode) => (n.c ?? []).filter(k => !k.n.startsWith('(')).sort((a, b) => b.b - a.b)
  const ranks = new Map(realKids(tree).map((n, i) => [n, i]))
  // The date scale spans the drawn leaves only, not their unloaded descendants or containers' means
  // (the dashboard uses drawn leaves too).
  let lo = Infinity
  let hi = -Infinity
  const leaves = (n: TreeNode): void => {
    for (const k of n.c ?? []) {
      if (k.n.startsWith('(')) continue
      if (k.c?.length) leaves(k)
      else if (k.d != null) { lo = Math.min(lo, k.d); hi = Math.max(hi, k.d) }
    }
  }
  leaves(tree)
  if (lo > hi) { lo = 0; hi = 0 }
  const fill = (n: TreeNode, slot: number, i: number, count: number, container: boolean): string => {
    if (mode === 'tree') return slotColor(slot, i, count)
    if (mode === 'user') return color(dominant(n))
    if (container) return FRAME_COLOR
    if (mode === 'date') return n.d != null && hi > lo ? dateColor((n.d - lo) / (hi - lo)) : '#5d5c55'
    // The app's read lens (`readLens.ts`); its window runs to `access.through`'s day when the scan has it.
    if (!access) return '#6e332b'
    const thru = readThrough(access)
    // A card has no hatch and no `?rt=` (always the default tint): a `written` cell takes its write's age.
    const rs = readState(n.a, true, { min: access.from, max: Math.max(access.to, thru?.day ?? access.to) }, { d: n.d, dir: n.k !== 'file' })
    return rs.kind === 'read' || rs.kind === 'written' ? dateColor(rs.t) : '#6e332b'
  }
  /** `depth` 1 is the view's child; `slot`/`i`/`count` its tree-mode colour key. */
  // Tree mode's slot level: below each lone top-level dir that has children of its own.
  let lone = 0
  if (mode === 'tree') for (let n = tree; realKids(n).length === 1 && realKids(realKids(n)[0]).length; n = realKids(n)[0]) lone++
  const tile = (n: TreeNode, depth: number, slot: number, i: number, count: number): CardTile => {
    if (n.n.startsWith('(')) return { name: n.n, b: n.b, color: FOLD_COLOR }
    if (!n.c?.length) return { name: n.n, b: n.b, color: fill(n, slot, i, count, false) }
    const kids = realKids(n)
    const kidRanks = new Map(kids.map((k, j) => [k, j]))
    if (depth <= lone) return { name: n.n, b: n.b, color: FRAME_COLOR, kids: n.c.map(k => tile(k, depth + 1, kidRanks.get(k) ?? 0, 0, 1)) }
    return {
      name: n.n, b: n.b, color: fill(n, slot, i, count, true),
      kids: n.c.map(k => depth === lone + 1 ? tile(k, depth + 1, slot, kidRanks.get(k) ?? 0, kids.length) : tile(k, depth + 1, slot, i, count)),
    }
  }
  return (tree.c ?? []).map(n => tile(n, 1, ranks.get(n) ?? 0, 0, 1))
}

/** Tree mode's keyed tiles: the top level, or the children of a lone top-level dir framed by `tilesOf`. */
export function slotTiles(tiles: CardTile[]): CardTile[] {
  const real = tiles.filter(t => !t.name.startsWith('('))
  return real.length === 1 && real[0].color === FRAME_COLOR && real[0].kids?.length ? slotTiles(real[0].kids) : tiles
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

/** The map card for a view (`routes.ts` params), its depth by `depthPlan`'s budget (`deepen: false`: the
 * floor view alone, for callers that need only the totals). Errors become a card that says so, never a
 * throw: an unfurl must always get an image. */
export async function mapCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier, opts: { deepen?: boolean } = {}): Promise<CardData> {
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
  const subtitle = `scan ${fmtScanUtc(date)}${filterNote} · color by ${colorLabel}`
  let query
  try {
    query = params.f ? queryParam(new URLSearchParams({ q: params.f, ...(params.qs ? { qs: params.qs } : {}) }), env.QUERY_SYNTAX, indexedOnly(env)).query : undefined
  } catch (e) {
    if (e instanceof QueryError) return { ...base, subtitle, total: '', empty: 'invalid filter' }
    throw e
  }
  const reg = await loadRegistry(env).catch(() => ({}) as Awaited<ReturnType<typeof loadRegistry>>)
  const owner = parseOwner(params.o ?? null)
  const lensUser = !owner && params.o && params.o !== 'me' ? canonId(params.o, reg) : null
  const lens: Lens | undefined = lensUser ? { key: lensUser } : undefined
  let tree: TreeNode
  let depth: number
  const plan = depthPlan(env)
  try {
    // The static index's hex-run rule: the card's filter matches as the map's does.
    query = (await hexQuery(env, query)).query
    const q = query
    const read = (maxDepth: number): Promise<View> => buildView(env, { date, path, w: BOX_W, h: BOX_H, minArea: MIN_AREA_DEFAULT, atten: ATTEN_DEFAULT, lens, owner, query: q, maxDepth })
    const got = await deepestView(opts.deepen === false ? { ...plan, deeper: [] } : plan, read, v => canDeepen(v.tree))
    tree = got.value.tree
    depth = got.depth
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
    depth,
    legend: mode === 'user' ? legendOf(tree, color, name) : mode === 'tree'
      ? slotTiles(tiles).filter(t => !t.name.startsWith('(')).sort((a, b) => b.b - a.b).slice(0, 6).map(t => ({ label: t.name, b: t.b, color: t.color }))
      : [],
    ...(tree.b ? {} : { empty: query ? 'no matches' : 'empty' }),
  }
}
