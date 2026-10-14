/** Assignment prefixes priced over an interval store's whole history (specs/owner-prefix-history.md): one D1 row
 * per prefix (`owner_prefix_hist`) holding its aggregate's versions over scan ordinals, as the store holds its
 * rows. An owner change reads and writes nothing; a new prefix is one history read and one row, however many
 * scans the store holds; a scan's fold takes each prefix's version live at that scan. Scans past a row's `hz`
 * (appended since it was priced) and stores without history fold per scan (`ownerTotals.ts`). */
import type { Env } from './auth.js'
import { ivHeldDates, ivHorizon, readAsksHistory, type Ask, type Row } from './index.js'
import { addAgg, newAgg, type PathAgg } from './ownerBands.js'
import { shared } from './shared.js'

/** A prefix's aggregate over scan ordinals `[vf, vt)`. */
export type Version = [number, number, PathAgg]
interface Hist { hz: number; versions: Version[] }

/** The roots marker's key: every depth-1 path is priced through its `hz`. */
const ROOTS = '0:'
const key = (depth: number, path: string) => `${depth}:${path}`

/** An aggregate with its maps' keys sorted, so equal aggregates serialize alike. */
const canon = (a: PathAgg): string => JSON.stringify([a.b, a.o, ...[a.us, a.uo, a.cb].map(m => Object.entries(m).sort(([x], [y]) => x < y ? -1 : x > y ? 1 : 0))])

/** Each `(depth, path)`'s versions from interval-store version rows (one per owner slice, each live over its own
 *  `[vf, vt)`): the history cut at every slice bound, each segment's live slices summed, equal neighbours merged.
 *  A segment with no live slice is a gap (the path isn't in those scans). */
export function versionsOf(rows: Row[]): Map<string, Version[]> {
  const groups = new Map<string, Row[]>()
  for (const r of rows) {
    const k = key(r.depth, r.path)
    const g = groups.get(k)
    if (g) g.push(r)
    else groups.set(k, [r])
  }
  const out = new Map<string, Version[]>()
  for (const [k, g] of groups) {
    g.sort((a, b) => (a.usr ?? '') < (b.usr ?? '') ? -1 : (a.usr ?? '') > (b.usr ?? '') ? 1 : a.vf! - b.vf!)
    const bounds = [...new Set(g.flatMap(r => [r.vf!, r.vt!]))].sort((a, b) => a - b)
    const vs: Version[] = []
    let last = ''
    for (let i = 0; i + 1 < bounds.length; i++) {
      const [lo, hi] = [bounds[i], bounds[i + 1]]
      const live = g.filter(r => r.vf! <= lo && lo < r.vt!)
      if (!live.length) { last = ''; continue }
      const a = newAgg()
      for (const r of live) addAgg(a, r)
      const c = canon(a)
      const prev = vs[vs.length - 1]
      if (prev && prev[1] === lo && c === last) prev[1] = hi
      else vs.push([lo, hi, a])
      last = c
    }
    out.set(k, vs)
  }
  return out
}

/** The version live at scan ordinal `d`, if the path is in that scan. */
export const liveAt = (vs: Version[], d: number): PathAgg | undefined => vs.find(([vf, vt]) => vf <= d && d < vt)?.[2]

// Per isolate: one map of rows per index tag, which every fold holds and pricing writes into, so concurrent folds
// see each other's pricing. Rows only gain prefixes or advance `hz`, so a refresh merges into it, never replaces it.
const tables = new Map<string, { at: number; rows: Map<string, Hist> }>()
const refreshing = new Map<string, Promise<Map<string, Hist>>>()
const LOAD_TTL = 300_000
const pricing = new Map<string, Promise<{ groups: number; hz: number } | null>>()
const WAIT = 60_000

/** `tag`'s rows; `refresh` re-reads D1 (once, shared by concurrent callers) into the same map. */
async function load(env: Env, tag: string, refresh = false): Promise<Map<string, Hist>> {
  const cur = tables.get(tag)
  if (cur && !refresh && Date.now() - cur.at < LOAD_TTL) return cur.rows
  return shared(refreshing, tag, async () => {
    const { results } = await env.DB!.prepare('SELECT depth, path, hz, versions FROM owner_prefix_hist WHERE idx = ?').bind(tag)
      .all<{ depth: number; path: string; hz: number; versions: string }>()
    const t = tables.get(tag) ?? { at: 0, rows: new Map<string, Hist>() }
    for (const r of results) {
      const k = key(r.depth, r.path)
      if ((t.rows.get(k)?.hz ?? -1) <= r.hz) t.rows.set(k, { hz: r.hz, versions: JSON.parse(r.versions) as Version[] })
    }
    t.at = Date.now()
    tables.set(tag, t)
    refreshing.delete(tag)
    return t.rows
  }, WAIT)
}

/** `want` (path → depth) not priced through scan ordinal `d` (and the roots, if they aren't). */
function staleOf(rows: Map<string, Hist>, want: Map<string, number>, d: number): { keys: Map<string, number>; roots: boolean } {
  const keys = new Map<string, number>()
  for (const [path, depth] of want) if (!((rows.get(key(depth, path))?.hz ?? -1) >= d)) keys.set(path, depth)
  return { keys, roots: !((rows.get(ROOTS)?.hz ?? -1) >= d) }
}

/** Price `keys` (and the roots) over every scan the store holds in one read; write one row per prefix (an
 *  empty `versions`: in none of them) and update `rows`. Null when the store's handle isn't `tag`'s. */
async function price(env: Env, tag: string, rows: Map<string, Hist>, keys: Map<string, number>, roots: boolean): Promise<{ groups: number; hz: number } | null> {
  const id = `${tag}|${roots ? 'R' : ''}|${[...keys].map(([p, d]) => key(d, p)).sort().join('\n')}`
  return shared(pricing, id, async () => {
    const asks: Ask[] = [...keys].map(([path, depth]) => ({ depth, path }))
    if (roots) asks.push({ depth: 1, under: '' })
    const fields: (keyof Row)[] = ['path', 'depth', 'usr', 'size', 'n_files', 'cls2', 'cls3', 'cls4']
    const got = await readAsksHistory(env, await ivHeldDates(env), 'path', asks, r => keys.get(r.path) === r.depth || (roots && r.depth === 1),
      { fields, maxGroups: Math.max(400, 2 * asks.length) })
    if (!got || got.index !== tag) return null
    const vs = versionsOf(got.rows)
    const put = new Map<string, Hist>()
    for (const [path, depth] of keys) put.set(key(depth, path), { hz: got.hz, versions: vs.get(key(depth, path)) ?? [] })
    if (roots) {
      for (const [k, v] of vs) if (k.startsWith('1:')) put.set(k, { hz: got.hz, versions: v })
      put.set(ROOTS, { hz: got.hz, versions: [] })
    }
    const now = Math.floor(Date.now() / 1000)
    const stmts = [...put].map(([k, h]) => {
      const i = k.indexOf(':')
      return env.DB!.prepare('INSERT OR REPLACE INTO owner_prefix_hist (idx, depth, path, hz, versions, updated_ts) VALUES (?, ?, ?, ?, ?, ?)')
        .bind(tag, Number(k.slice(0, i)), k.slice(i + 1), h.hz, JSON.stringify(h.versions), now)
    })
    for (let i = 0; i < stmts.length; i += 100) await env.DB!.batch(stmts.slice(i, i + 100))
    for (const [k, h] of put) rows.set(k, h)
    return { groups: got.groups, hz: got.hz }
  }, WAIT)
}

/** Prefixes a fold prices over the history in its own request; more (a first fill) fold per scan meanwhile. */
export const HIST_PRICE_MAX = 64

/** `date`'s aggregates (scan ordinal `d`) for `want` and the roots from the history rows at `tag`, pricing what
 *  isn't priced through `d` when that is at most `HIST_PRICE_MAX` prefixes. Null: too many to price here, or
 *  the store doesn't serve `tag` — the caller folds per scan. */
export async function histAggs(env: Env, tag: string, d: number, want: Map<string, number>): Promise<{ aggs: Map<string, PathAgg>; buckets: string[]; prefixes: number; groups: number } | null> {
  let fresh = await load(env, tag)
  let { keys, roots } = staleOf(fresh, want, d)
  if (keys.size || roots) {
    // Another isolate may have priced them since this one loaded.
    fresh = await load(env, tag, true)
    ;({ keys, roots } = staleOf(fresh, want, d))
  }
  const prefixes = keys.size
  if (prefixes > HIST_PRICE_MAX) return null
  let groups = 0
  if (prefixes || roots) {
    const got = await price(env, tag, fresh, keys, roots)
    if (!got || got.hz < d) return null
    groups = got.groups
  }
  const aggs = new Map<string, PathAgg>()
  const buckets: string[] = []
  for (const [k, h] of fresh) {
    if (!k.startsWith('1:')) continue
    const a = liveAt(h.versions, d)
    if (a) { buckets.push(k.slice(2)); aggs.set(k.slice(2), a) }
  }
  for (const [path, depth] of want) {
    if (depth === 1) continue
    const h = fresh.get(key(depth, path))
    // Priced through `d` above, by this fold or a concurrent one, into this same map.
    if (!h || h.hz < d) throw new Error(`owner history: ${key(depth, path)} not priced through ${d}`)
    const a = liveAt(h.versions, d)
    if (a) aggs.set(path, a)
  }
  return { aggs, buckets: buckets.sort(), prefixes, groups }
}

/** The first fill (and catching up after a new generation): price up to `n` of `want` not priced through the
 *  store's newest scan, in one read. `remaining` counts what is still unpriced after it. */
export async function fillHist(env: Env, tag: string, want: Map<string, number>, n: number): Promise<{ priced: number; remaining: number; groups: number } | null> {
  const hz = await ivHorizon(env)
  if (hz == null) return null
  const rows = await load(env, tag, true)
  const { keys, roots } = staleOf(rows, want, hz)
  const batch = new Map([...keys].slice(0, n))
  if (!batch.size && !roots) return { priced: 0, remaining: 0, groups: 0 }
  const got = await price(env, tag, rows, batch, roots)
  if (!got) return null
  return { priced: batch.size, remaining: keys.size - batch.size, groups: got.groups }
}
