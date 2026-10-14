/** The smallest exact set of prefixes covering a filter's match roots under a view path (`/api/filter-cover`):
 *  what a bulk action on the matches — assign, stage — takes, instead of one prefix per match.
 *
 *  A folder `A` is *full* when every object under it is under some match root: the match roots under `A`
 *  are disjoint (outermost), so that holds iff their summed objects equal `A`'s total objects, and their
 *  summed bytes `A`'s total bytes (bytes alone would let a zero-byte non-match through; objects alone
 *  suffice, bytes is the cross-check). Full folders are closed downward, so the cover is the antichain of
 *  outermost full folders plus the roots under none of them — the unique minimum: any exact prefix set must
 *  put at least one prefix inside each outermost full folder, and those are disjoint.
 *
 *  Bounds: one lookup round per depth that holds candidate folders (deepest first), each round only the
 *  candidates none of whose candidate children is already known not full (a folder holding non-matching
 *  objects poisons every ancestor). A lookup over budget (`null`) stops collapsing: the remaining
 *  candidates count as not full, so the cover is still exact — just not minimal there. A root of one object
 *  may be an object or a folder holding one: its kind is looked up too (a folder prefix ends in `/`, an
 *  object's key doesn't), within its own budget (`kinds`: the static index has no kinds, and a big flat
 *  folder's objects cost a row group per 8K); one it can't place is `kind: null`, and nothing acts on it. */

export interface CoverRoot { path: string; b: number; o: number }
export type Kind = 'file' | 'dir'
/** A path's total bytes and objects on the scan (every owner slice summed), and what it is. */
export interface PathTotal { b: number; o: number; kind: Kind }
/** Totals of `paths` (each a folder or object path below the store root); null: the read budget is spent;
 *  `'wide'`: these paths at once are too wide for one call (fewer may fit). */
export type Lookup = (paths: string[]) => Promise<Map<string, PathTotal> | null | 'wide'>

export interface CoverItem {
  path: string
  /** `file`: an object key; `dir`: a folder prefix; null: unknown (a one-object root the lookup couldn't place). */
  kind: Kind | null
  b: number
  o: number
  /** Match roots it covers (1 for a root itself). */
  roots: number
}

export interface Cover {
  items: CoverItem[]
  /** Folders whose totals were looked up. */
  looked: number
  /** Candidate folders left unchecked (a lookup over budget): the cover is exact but may not be minimal. */
  unchecked: number
}

/** `/api/filter-cover`'s version: in its cache key, and the client's `cv=` (a browser's private copy
 *  must not outlive a change either). Bumped when an answer would change. */
export const COVER_V = 6

/** Why a cover over the item cap (`/api/filter-cover` `COVER_ITEMS_MAX`) isn't offered (shown muted, on the disabled actions). */
export const overCapReason = (n: number, atLeast = false): string =>
  `Too many matches to act on at once (${atLeast ? 'at least ' : ''}${n.toLocaleString('en-US')}); narrow the search or open a folder below. Agents can bulk-assign via the API.`

/** Kind lookups: the fewest paths a too-wide chunk is halved to, and the most calls. */
export const MIN_KIND_CHUNK = 64
export const MAX_KIND_CALLS = 24

export const depthOf = (p: string): number => (p === '' ? 0 : p.split('/').length)
const parentOf = (p: string): string => { const i = p.lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i) }

/** The fullness test: matched objects and bytes equal the folder's totals. */
export const isFull = (m: { b: number; o: number }, t: { b: number; o: number }): boolean => m.o === t.o && m.b === t.b

/** The cover of `roots` (outermost match roots, disjoint) under `view`: folders collapse at depth ≥
 *  `minDepth` (and at or below `view`), never above. */
export async function coverSet(
  roots: CoverRoot[],
  view: string,
  lookup: Lookup,
  { minDepth = 1, full = isFull, kindChunk = 2048, kinds: kindLookup = lookup }: {
    minDepth?: number
    full?: (m: { b: number; o: number }, t: { b: number; o: number }) => boolean
    kindChunk?: number
    /** The kinds' own lookup (default `lookup`): its budget is the kinds', not the folders'. */
    kinds?: Lookup
  } = {},
): Promise<Cover> {
  const floor = Math.max(minDepth, depthOf(view))
  // Each candidate folder's matched sums: every proper ancestor of a root, at or below the view, at ≥ floor.
  const sums = new Map<string, { b: number; o: number; n: number }>()
  for (const r of roots) {
    for (let a = parentOf(r.path); depthOf(a) >= floor; a = parentOf(a)) {
      if (view !== '' && a !== view && !a.startsWith(view + '/')) break
      const s = sums.get(a)
      if (s) { s.b += r.b; s.o += r.o; s.n++ } else sums.set(a, { b: r.b, o: r.o, n: 1 })
    }
  }
  const byDepth = new Map<number, string[]>()
  for (const a of sums.keys()) { const d = depthOf(a); byDepth.set(d, [...(byDepth.get(d) ?? []), a]) }
  const fullSet = new Set<string>(), notFull = new Set<string>()
  const poison = (a: string) => { for (let p = a; sums.has(p) && !notFull.has(p); p = parentOf(p)) notFull.add(p) }
  let looked = 0, unchecked = 0, stopped = false
  for (const d of [...byDepth.keys()].sort((x, y) => y - x)) {
    const ask = byDepth.get(d)!.filter(a => !notFull.has(a)).sort()
    if (!ask.length) continue
    const got0 = stopped ? null : await lookup(ask)
    const got = got0 === 'wide' ? null : got0
    if (!got) { stopped = true; unchecked += ask.length; for (const a of ask) poison(a); continue }
    looked += ask.length
    for (const a of ask) {
      const t = got.get(a)
      if (t && t.kind === 'dir' && full(sums.get(a)!, t)) fullSet.add(a)
      else poison(a)
    }
  }
  const outermostFull = (p: string): string | null => {
    let best: string | null = null
    for (let a = parentOf(p); sums.has(a); a = parentOf(a)) if (fullSet.has(a)) best = a
    return best
  }
  // One-object roots not inside a full folder: object or folder? Asked in path order, `kindChunk` at a time,
  // so a budget cut leaves the ones already placed placed.
  const ambiguous = roots.filter(r => r.o <= 1 && !outermostFull(r.path)).map(r => r.path)
    .sort((x, y) => depthOf(x) - depthOf(y) || (x < y ? -1 : x > y ? 1 : 0))
  const kinds = new Map<string, PathTotal>()
  // A chunk too wide for one call is halved (down to `MIN_KIND_CHUNK`, past which it is skipped); a spent
  // budget stops; at most `MAX_KIND_CALLS` calls.
  for (let i = 0, size = kindChunk, calls = 0; i < ambiguous.length && calls < MAX_KIND_CALLS; calls++) {
    const got = await kindLookup(ambiguous.slice(i, i + size))
    if (got === null) break
    if (got === 'wide') {
      if (size > MIN_KIND_CHUNK) { size = Math.max(MIN_KIND_CHUNK, size >> 1); continue }
      i += size
      continue
    }
    for (const [p, t] of got) kinds.set(p, t)
    i += size
  }
  const items = new Map<string, CoverItem>()
  for (const r of roots) {
    const f = outermostFull(r.path)
    if (f) {
      const it = items.get(f)
      if (it) { it.b += r.b; it.o += r.o; it.roots++ } else items.set(f, { path: f, kind: 'dir', b: r.b, o: r.o, roots: 1 })
      continue
    }
    const kind: Kind | null = r.o > 1 ? 'dir' : kinds.get(r.path)?.kind ?? null
    items.set(r.path, { path: r.path, kind, b: r.b, o: r.o, roots: 1 })
  }
  return { items: [...items.values()].sort((x, y) => (x.path < y.path ? -1 : x.path > y.path ? 1 : 0)), looked, unchecked }
}

/** A lower bound on `coverSet(roots, view, …)`'s item count, under any lookup budget: what decides an over-cap
 *  cover without computing it (`/api/filter-cover`).
 *
 *  The root count alone isn't one: full folders collapse many roots into one item (a folder of 70K matching
 *  run dirs is one item). The bound is the cover found top-down, one depth at a time from the floor. Every
 *  root is either *settled* (its item is known: a root at or above the current depth, or a folder proven
 *  full whose ancestors were all proven not full) or under one *open* folder at the current depth (not yet
 *  proven not full). An open folder holds at least one item of the cover whatever its fullness, and open
 *  folders are disjoint, so `settled + open` never exceeds the minimum cover — which `coverSet` never goes
 *  below (a lookup it can't make only stops collapsing). Each round looks up the open folders: a full one
 *  settles as one item, the rest open their children at the next depth (roots there settle). The count only
 *  grows; it stops as soon as it passes `cap` (`over`), at a lookup over budget or too wide (the count so far
 *  is still a bound), or when nothing is open (`exact`: the minimum cover's size).
 *
 *  Round 0 reads nothing: the roots at or above the floor, plus the distinct floor-depth folders holding the
 *  rest. A filter whose matches sit one level below few folders passes the cap after one small lookup. */
export async function coverFloor(
  roots: CoverRoot[],
  view: string,
  lookup: Lookup,
  { minDepth = 1, full = isFull, cap }: { minDepth?: number; full?: (m: { b: number; o: number }, t: { b: number; o: number }) => boolean; cap: number },
): Promise<{ n: number; over: boolean; exact: boolean; looked: number }> {
  const floor = Math.max(minDepth, depthOf(view))
  const at = (p: string, d: number) => p.split('/').slice(0, d).join('/')
  let settled = 0, looked = 0
  let open = new Map<string, CoverRoot[]>()
  const place = (r: CoverRoot, d: number) => {
    if (depthOf(r.path) <= d) { settled++; return }
    const a = at(r.path, d), rs = open.get(a)
    if (rs) rs.push(r); else open.set(a, [r])
  }
  for (const r of roots) place(r, floor)
  for (let d = floor; ; d++) {
    const n = settled + open.size
    if (n > cap || !open.size) return { n, over: n > cap, exact: !open.size, looked }
    const ask = [...open.keys()].sort()
    const got = await lookup(ask)
    if (!got || got === 'wide') return { n, over: false, exact: false, looked }
    looked += ask.length
    const next = open
    open = new Map()
    for (const a of ask) {
      const rs = next.get(a)!
      const t = got.get(a)
      const m = rs.reduce((s, r) => ({ b: s.b + r.b, o: s.o + r.o }), { b: 0, o: 0 })
      if (t && t.kind === 'dir' && full(m, t)) settled++
      else for (const r of rs) place(r, d + 1)
    }
  }
}

/** What an action names for an item: a folder's prefix (`…/`), an object's key; null for an unknown kind. */
export const itemPrefix = (scheme: string, it: { path: string; kind: Kind | null }): string | null =>
  it.kind === 'dir' ? `${scheme}${it.path}/` : it.kind === 'file' ? `${scheme}${it.path}` : null
