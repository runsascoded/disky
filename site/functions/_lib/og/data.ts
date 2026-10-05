/** Card data from the deployment's own reads (specs/done/dogi.md): the same
 * thresholded, depth-capped view the page draws, its owner colours (the
 * scan's attribution with the live ledger applied, as the map does), and the
 * header/footer text. The tier decides only what is *shown* (`card.ts`): the
 * data read is the same. */
import { S3Store } from '@rdub/file-tree/stores/s3'
import type { Env } from '../auth.js'
import { pathScans, storeCreds, storeTarget, type Lens } from '../index.js'
import { loadRegistry, canonId } from '../identity.js'
import { loadLedger } from '../ledger.js'
import { parseOwner, queryParam, QueryError } from '../scope.js'
import { snapshotsPrefix } from '../shared.js'
import { ATTEN_DEFAULT, buildView, MIN_AREA_DEFAULT, NotFound } from '../view.js'
import { HI_CONTRAST } from '../../../src/colors.js'
import { applyLedger } from '../../../src/ledgerOverlay.js'
import { ownerIndex } from '../../../src/ownerIndex.js'
import type { TreeNode, UserInfo } from '../../../src/types.js'
import { type CardData, type CardTile, type LegendItem, fmtB } from './card.js'
import type { OgTier } from './sign.js'

/** The unclaimed pool's colour (the dark theme's `--t-unattr`). */
export const UNOWNED_COLOR = '#4a4943'
const FOLD_COLOR = '#2b2e35'

/** The treemap box on the card (`card.ts`), the view's pixel budget. */
const BOX_W = 1120
const BOX_H = 410

/** A `?d=` selection's "after" scan: the leading compact (`261002`,
 * `261002-1200`) or ISO (`2026-10-02`, `2026-10-02T1200`) id. A look-back or
 * `from` suffix is ignored (the card draws the after scan). */
export function scanOfSel(d: string | undefined): string | undefined {
  if (!d) return undefined
  const iso = /^(\d{4}-\d{2}-\d{2}(?:T\d{4})?)/.exec(d)
  if (iso) return iso[1]
  const c = /^(\d{2})(\d{2})(\d{2})(?:-(\d{4}))?(?:-|$)/.exec(d)
  return c ? `20${c[1]}-${c[2]}-${c[3]}${c[4] && !/^\d{6}/.test(d.slice(7)) ? `T${c[4]}` : ''}` : undefined
}

/** The scan a card draws: the selected one if indexed, else the latest. */
export async function resolveScan(env: Env, d: string | undefined): Promise<string | null> {
  const scans = (await pathScans(env, true)).results.map(r => r.date)
  const want = scanOfSel(d)
  return want && scans.includes(want) ? want : scans[scans.length - 1] ?? null
}

/** The scan's `meta.json` user list (rank = colour slot), or none. */
async function scanUsers(env: Env, date: string): Promise<UserInfo[]> {
  try {
    const own = env.STORE_KEY ? snapshotsPrefix(env) : 'snapshots/'
    const store = S3Store({ ...storeTarget(env), prefixes: [own], ...storeCreds(env) })
    const { bytes } = await store.get(`${own}${date}/meta.json`)
    return (JSON.parse(new TextDecoder().decode(bytes)) as { users?: UserInfo[] }).users ?? []
  } catch {
    return []
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
export function tilesOf(tree: TreeNode, color: (u: string | null) => string): CardTile[] {
  const tile = (n: TreeNode): CardTile => n.n.startsWith('(')
    ? { name: n.n, b: n.b, color: FOLD_COLOR }
    : { name: n.n, b: n.b, color: color(dominant(n)), ...(n.c?.length ? { kids: n.c.map(k => ({ name: k.n, b: k.b, color: k.n.startsWith('(') ? FOLD_COLOR : color(dominant(k)) })) } : {}) }
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

export interface Site { name: string; scheme: string }

/** The map card for a view (`routes.ts` params). Errors become a card that
 * says so, never a throw: an unfurl must always get an image. */
export async function mapCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier): Promise<CardData> {
  const base = { tier, site: site.name, title, tiles: [] as CardTile[] }
  const date = await resolveScan(env, params.d)
  if (!date) return { ...base, subtitle: '', total: '', empty: 'no scans yet' }
  const path = params.path ?? ''
  const filterNote = params.f ? ` · filter “${params.f}”` : ''
  const subtitle = `scan ${date}${filterNote}`
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
    const view = await buildView(env, { date, path, w: BOX_W, h: BOX_H, minArea: MIN_AREA_DEFAULT, atten: ATTEN_DEFAULT, lens, owner, query, maxDepth: 2 })
    tree = view.tree
  } catch (e) {
    if (e instanceof NotFound) return { ...base, subtitle, total: '', empty: 'path not found' }
    return { ...base, subtitle, total: '', empty: 'view unavailable' }
  }
  // The live assignments over the scan's attribution, as the map draws them.
  if (!lens && env.DB) {
    const { ownerRows } = await loadLedger(env).catch(() => ({ ownerRows: [] }))
    const idx = ownerIndex({ owners: ownerRows.map(r => ({ ...r, who: r.who ?? '', memo: null })) })
    if (idx.count) tree = applyLedger(tree, idx, site.scheme + (path ? `${path}/` : ''), u => canonId(u, reg))
  }
  const color = ownerColors(await scanUsers(env, date))
  const name = (u: string) => reg[u]?.name ?? u
  return {
    ...base,
    subtitle,
    total: `${fmtB(tree.b)} · ${fmtN(tree.o)} objects`,
    tiles: tilesOf(tree, color),
    legend: legendOf(tree, color, name),
    ...(tree.b ? {} : { empty: query ? 'no matches' : 'empty' }),
  }
}
