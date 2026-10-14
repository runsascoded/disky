import { divergingColor } from '@rdub/treemap/divergingColor'
import type { Env } from '../auth.js'
import { pathScans, type Lens } from '../index.js'
import { canonId, loadRegistry } from '../identity.js'
import { scanTimes } from '../scanTimes.js'
import { parseClasses, parseOwner, queryParam } from '../scope.js'
import { hexQuery } from '../staticFilter.js'
import { ATTEN_DEFAULT, buildDiff, MIN_AREA_DEFAULT, type Diff } from '../view.js'
import { buildTree, type DiffNode } from '../../../src/diffModel.js'
import { decodeSel, resolveAfter, resolveBefore, scanCmp, sortScans } from '../../../src/scanSlug.js'
import { fmtB, type CardData, type CardTile } from './card.js'
import { NoScanMatch, type Site } from './data.js'
import type { OgTier } from './sign.js'

export async function resolveDiffScans(env: Env, d: string | undefined): Promise<{ from: string | null; to: string } | null> {
  const ids = (await pathScans(env, true)).results.map(r => r.date)
  const times = await scanTimes(env, ids)
  const scans = sortScans(ids, times)
  const sel = decodeSel(d)
  const to = resolveAfter(sel, scans, times)
  if (!to) {
    if (sel?.invalid || sel?.d) throw new NoScanMatch(d!)
    return null
  }
  const chosen = resolveBefore(sel, to, scans, times)
  if (chosen === null) throw new NoScanMatch(d!)
  const from = chosen ?? scans.find(s => scanCmp(s, to, times) < 0) ?? null
  return { from, to }
}

export const fmtChange = (n: number): string => `${n < 0 ? '−' : '+'}${fmtB(Math.abs(n))}`

/** Same DOM-free model as the dashboard: parent churn is not double-counted. */
export function diffCardData(
  site: Site,
  title: string,
  params: Record<string, string>,
  tier: OgTier,
  from: string,
  to: string,
  diff: Diff,
): CardData {
  const mode = params.dm === 'max' ? 'max' : 'delta'
  const { cells } = buildTree({ ...diff, prev: from, curr: to }, mode, !params.path)
  const tile = (n: DiffNode): CardTile => ({
    name: n.label, b: n.weight, value: n.first ? `${fmtB(n.size_new)} first scanned` : fmtChange(n.delta),
    color: n.fs ? '#4269d0' : divergingColor(-n.delta / Math.max(n.size_old, n.size_new, 1)),
    ...(n.children?.length ? { kids: n.children.filter(k => k.weight > 0).map(k => ({ ...tile(k), kids: undefined })) } : {}),
  })
  const caution = diff.partial || diff.approximate || diff.truncated || diff.lookups_capped || diff.lookups_late || diff.interiors?.late ? ' · bounded/partial detail' : ''
  return {
    tier, site: site.name, title,
    subtitle: `${from} → ${to} · area ${mode === 'max' ? 'max size' : '|Δ|'}${caution}`,
    total: `${fmtB(diff.total_a)} → ${fmtB(diff.total_b)} · net ${fmtChange(diff.total_b - diff.total_a)}`,
    tiles: cells.map(tile),
    empty: 'no changes',
  }
}

export async function diffCard(env: Env, site: Site, title: string, params: Record<string, string>, tier: OgTier, summary = false): Promise<CardData> {
  const scans = await resolveDiffScans(env, params.d)
  const empty = { tier, site: site.name, title, subtitle: '', total: '', tiles: [], empty: scans ? 'no earlier scan' : 'no scans yet' }
  if (!scans?.from) return empty
  if (params.o === 'me') return { ...empty, empty: 'choose a named owner before sharing' }
  const query = (await hexQuery(env, queryParam(new URLSearchParams({ q: params.f ?? '', ...(params.qs ? { qs: params.qs } : {}) }), env.QUERY_SYNTAX).query)).query
  const owner = parseOwner(params.o ?? null)
  const registry = await loadRegistry(env)
  const lens: Lens | undefined = !owner && params.o ? { key: canonId(params.o, registry) } : undefined
  const diff = await buildDiff(env, {
    from: scans.from, to: scans.to, path: params.path ?? '', w: 1120, h: 410,
    minArea: MIN_AREA_DEFAULT, atten: ATTEN_DEFAULT, top: 150, depth: 2, summary,
    owner, lens, query, classes: parseClasses(params.cl ?? null),
  })
  return diffCardData(site, title, params, tier, scans.from, scans.to, diff)
}
