/** The page's scope axes, applied server-side to a view's rows
 * (specs/view-serving.md §2): the owner axis (`o=`), the storage-class axis
 * (`cl=`), and the name filter (`q=`). A user lens (`lens=user:`) lives in
 * `owners.ts` — it needs the ledger.
 *
 * Both are pure functions over index rows / per-path aggregates so `buildView`
 * stays one pipeline: read a superset by total bytes, narrow per row, fold. */

/** `o=`: `owned` = rows some person owns (`usr` set), `unowned` = the
 * NULL-`usr` slices (ownership has one axis: a person or nobody). `{not}` =
 * owned, but by nobody in the excluded set — the "owned by someone other than
 * these users" pool (`o=!<id>,<id>`), used to surface the paths a sweep would
 * touch that its sweeper does NOT own. Index rows are owner slices, so all
 * three are exact per path. */
export type OwnerScope = 'owned' | 'unowned' | { not: string[] }

// `claimed` / `unclaimed` were the pools' names until 2026-09-07; old links
// and cached clients still send them. `!a,b` = owned-except-{a,b}.
export const parseOwner = (raw: string | null): OwnerScope | undefined =>
  raw === 'owned' || raw === 'claimed' ? 'owned'
  : raw === 'unowned' || raw === 'unclaimed' ? 'unowned'
  : raw?.startsWith('!') ? { not: raw.slice(1).split(',').filter(Boolean) }
  : undefined

export const ownerOk = (usr: string | null, o: OwnerScope | undefined): boolean => {
  if (!o) return true
  if (o === 'owned') return usr != null
  if (o === 'unowned') return usr == null
  return usr != null && !o.not.includes(usr) // owned, excluding o.not
}

/** `cl=` ⊆ `snca` (Standard / Nearline / Coldline / Archive): the storage-
 * class axis. Unlike the owner pools it doesn't pick rows — every index row
 * is a (path, owner) slice carrying its own class split (`size` with
 * `cls2..cls4`, Standard = the rest) — it *scales* each row down to the
 * allowed classes' bytes. Objects aren't tracked per class, so `n_files`
 * (and the written-time weight) scale with the byte share. All/none/absent
 * = no scope. */
export type ClassScope = ReadonlySet<'1' | '2' | '3' | '4'>
const CLASS_LETTERS: Record<string, '1' | '2' | '3' | '4'> = { s: '1', n: '2', c: '3', a: '4' }
export function parseClasses(raw: string | null): ClassScope | undefined {
  if (!raw) return undefined
  const out = new Set<'1' | '2' | '3' | '4'>()
  for (const ch of raw) if (CLASS_LETTERS[ch]) out.add(CLASS_LETTERS[ch])
  return out.size === 0 || out.size === 4 ? undefined : out
}
/** The canonical letters for a scope (cache keys). */
export const classKey = (cl: ClassScope | undefined): string =>
  cl ? ['1', '2', '3', '4'].filter(c => cl.has(c as '1')).map(c => ({ 1: 's', 2: 'n', 3: 'c', 4: 'a' })[c as '1' | '2' | '3' | '4']).join('') : ''
/** A row cut down to its allowed classes' bytes (a copy; `r` untouched). */
export function classRow<R extends { size: number; n_files: number; mtime_w: number; cls2: number; cls3: number; cls4: number }>(r: R, cl: ClassScope | undefined): R {
  if (!cl) return r
  const c1 = Math.max(0, r.size - r.cls2 - r.cls3 - r.cls4)
  const size = (cl.has('1') ? c1 : 0) + (cl.has('2') ? r.cls2 : 0) + (cl.has('3') ? r.cls3 : 0) + (cl.has('4') ? r.cls4 : 0)
  const f = r.size > 0 ? size / r.size : 0
  return { ...r, size, n_files: Math.round(r.n_files * f), mtime_w: r.mtime_w * f, cls2: cl.has('2') ? r.cls2 : 0, cls3: cl.has('3') ? r.cls3 : 0, cls4: cl.has('4') ? r.cls4 : 0 }
}

/** `q=`: `/…/` = regex (case-insensitive); anything else = substring (ci),
 * with `|` splitting alternatives. Predicates receive the index path
 * (`bucket/dir/sub`), so `grug/swarm` matches across segments. Mirrors the
 * client's `parseQuery` (`site/src/filterTree.ts`) exactly. */
export type NamePred = (path: string) => boolean

export function parseQuery(q: string | null): NamePred | null {
  const s = (q ?? '').trim()
  if (!s) return null
  if (s.length > 2 && s.startsWith('/') && s.endsWith('/')) {
    try {
      const re = new RegExp(s.slice(1, -1), 'i')
      return path => re.test(path)
    } catch {
      return null
    }
  }
  const needles = s.toLowerCase().split('|').map(t => t.trim()).filter(Boolean)
  if (!needles.length) return null
  return path => {
    const p = path.toLowerCase()
    return needles.some(t => p.includes(t))
  }
}

/** Name-filter a set of per-path aggregates (every path under the view root
 * that was read, plus the root itself as `''`-keyed `root`): the outermost
 * matching paths are the *match roots*; a path's filtered value is the sum of
 * the match roots at-or-under it (its own whole aggregate when it matches).
 * Returns the filtered aggregates (paths with zero filtered value dropped)
 * and the match-root set, so the response can flag them for bulk actions.
 *
 * Bytes are matched bytes only, never double-counted: a match root's
 * ancestors get its aggregate once, its descendants are not visited. Depth
 * is bounded by what was read — matches below the read's threshold are not
 * found (a store-wide deep search is a separate index; view-serving.md §2). */
export function nameFilter<A>(
  root: { path: string; agg: A },
  aggs: Map<string, { depth: number; agg: A }>,
  pred: NamePred,
  sum: (into: A, from: A) => void,
  empty: () => A,
): { root: A; aggs: Map<string, { depth: number; agg: A }>; matches: Set<string> } {
  const parentOf = (p: string): string => {
    const cut = p.lastIndexOf('/')
    return cut === -1 ? '' : p.slice(0, cut)
  }
  const matches = new Set<string>()
  if (pred(root.path)) {
    // The root itself matches: everything under it is matched bytes.
    matches.add(root.path)
    return { root: root.agg, aggs, matches }
  }
  // Outermost matches: a path whose every strict ancestor (down to the root)
  // fails the predicate. Ancestors of a read path are read too (descendant-
  // inclusive bytes are monotone), so walking up through `aggs` suffices.
  const isMatchRoot = (p: string): boolean => {
    if (!pred(p)) return false
    for (let a = parentOf(p); a !== root.path && a.length >= root.path.length; a = parentOf(a)) {
      if (pred(a)) return false
      if (a === '') break
    }
    return true
  }
  for (const p of aggs.keys()) if (isMatchRoot(p)) matches.add(p)
  // Filtered value per path = Σ match roots at-or-under it: seed each match
  // root with its own aggregate, then add it to every ancestor up to the root.
  const out = new Map<string, { depth: number; agg: A }>()
  const rootAgg = empty()
  for (const m of matches) {
    const src = aggs.get(m)!
    let p = m
    while (true) {
      let e = out.get(p)
      if (!e) out.set(p, (e = { depth: aggs.get(p)!.depth, agg: empty() }))
      sum(e.agg, src.agg)
      const par = parentOf(p)
      if (par === root.path || (par === '' && root.path === '')) break
      if (!aggs.has(par)) break
      p = par
    }
    sum(rootAgg, src.agg)
  }
  return { root: rootAgg, aggs: out, matches }
}
