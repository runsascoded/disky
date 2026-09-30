/** The ownership ledger applied to a user lens, server-side
 * (specs/view-serving.md §2, "claims applied to the user lens"): of a path's
 * bytes, how many are U's once claims repaint attribution.
 *
 * Model (`claims.ts`): every live claim is a band — its subtree minus deeper
 * claims' subtrees — whose claimant is the newest claim on its
 * ancestor-or-self chain (recency beats specificity, so a newer ancestor
 * repaints a deeper claim). A band with a claimant belongs to that person
 * whole; a band under no claim (or under a release) belongs to whoever the
 * scan attributed each slice to. U's bytes under N are therefore
 *
 *   value(N) = residual(N) + Σ_{claims C strictly under N} band_U(C)
 *
 *   residual(N) = [cover(N) is U]      · (all(N) − Σ_{top C under N} all(C))
 *               + [cover(N) is nobody] · (mine(N) − Σ_{top C under N} mine(C))
 *   band_U(C)   = [claimant(C) is U]      · (all(C) − Σ_{child claims D} all(D))
 *               + [claimant(C) is nobody] · (mine(C) − Σ_{child claims D} mine(D))
 *
 * where all(·) is the subtree's total bytes, mine(·) the scan's U-attributed
 * slice, `top` the outermost claims under N and `child` the direct children
 * in the claim trie. Bands partition the subtree, so this is exact. all(C) /
 * mine(C) come from the totals manifest (`ClaimRow.bytes` / `.us`); all(N)
 * and mine(N) from the view's rows. The by-path tier is only needed where
 * U's share can exceed U's attributed slice: at N itself when U's claim
 * covers it (`needsTotal`), and under the outermost U-claimed subtrees the
 * scan attributes to others (`regions`) — a by-path read of those ranges
 * gives all(·) for every path inside. */
import { canonId } from './identity.js'
import type { Registry } from '../../src/identityRegistry.js'
import type { ClaimRow } from './claims.js'
import { idxKey } from './claims.js'

export interface Region { path: string; depth: number; all: number; objects: number }

export interface OwnerLens {
  /** Is `path` inside a U claim whose subtree the scan attributes partly to
   * others? Its total bytes are then needed — `value` wants `all` (a claim
   * path itself can fall back to the manifest's total). */
  needsTotal(path: string): boolean
  /** The outermost U-claimed subtrees strictly under `path` whose bytes the
   * scan doesn't already attribute wholly to U — the ranges a view reads
   * from the by-path tier to draw inside them. Index-key paths with depth
   * and the manifest's subtree total. */
  regions(path: string): Region[]
  /** Is `path` itself a live claim's prefix (its total is in the manifest)? */
  isClaim(path: string): boolean
  /** U's bytes under `path` (index key form) from its total bytes (`all`;
   * null = not read: fine where `needsTotal` is false, and at a claim's own
   * path, where the manifest's total stands in) and U's scan-attributed
   * bytes there (`mine`; null = not read, in which case the residual counts
   * as 0 and the value is the bands below — a lower bound used for a
   * region's unread ancestors). */
  value(path: string, all: number | null, mine: number | null): number
}

interface Claim {
  path: string
  ts: number
  action_id: number
  /** Canonical claimant, or null for a release. */
  who: string | null
  all: number
  mine: number
  objects: number
}

/** null when the ledger holds no claims at all — the lens is then the
 * scan's attribution, untouched. */
export function ownerLens(claims: ClaimRow[], user: string, reg: Registry = {}): OwnerLens | null {
  if (!claims.length) return null
  const u = canonId(user, reg)
  const share = (us: Record<string, number>): number => {
    let b = 0
    for (const [k, v] of Object.entries(us)) if (canonId(k, reg) === u) b += v
    return b
  }
  const all: Claim[] = claims
    .map(c => ({ path: idxKey(c.prefix).path, ts: c.ts, action_id: c.action_id, who: c.owner == null ? null : canonId(c.owner, reg), all: c.bytes, mine: share(c.us), objects: c.objects }))
    .sort((a, b) => (a.path < b.path ? -1 : a.path > b.path ? 1 : 0))
  const byPath = new Map(all.map(c => [c.path, c]))
  const parentOf = (p: string): string => {
    const cut = p.lastIndexOf('/')
    return cut === -1 ? '' : p.slice(0, cut)
  }
  const newer = (a: Claim, b: Claim) => a.ts > b.ts || (a.ts === b.ts && a.action_id > b.action_id)
  // Nearest strict ancestor claim (the trie parent) and the claimant of each
  // claim's own band: the newest claim on its ancestor-or-self chain.
  const anc = new Map<string, Claim | null>()
  const kids = new Map<string, Claim[]>()
  const cover = (path: string): Claim | null => {
    let win: Claim | null = null
    for (let q = path; ; q = parentOf(q)) {
      const c = byPath.get(q)
      if (c && (!win || newer(c, win))) win = c
      if (q === '') break
    }
    return win
  }
  const claimant = (path: string): string | null => cover(path)?.who ?? null
  const eff = new Map<string, string | null>()
  for (const c of all) {
    let a: Claim | null = null
    for (let q = parentOf(c.path); ; q = parentOf(q)) {
      const hit = byPath.get(q)
      if (hit) { a = hit; break }
      if (q === '') break
    }
    anc.set(c.path, a)
    if (a) {
      const arr = kids.get(a.path)
      if (arr) arr.push(c)
      else kids.set(a.path, [c])
    }
    eff.set(c.path, claimant(c.path))
  }
  const lowerBound = (key: string): number => {
    let lo = 0
    let hi = all.length
    while (lo < hi) {
      const mid = (lo + hi) >> 1
      if (all[mid].path < key) lo = mid + 1
      else hi = mid
    }
    return lo
  }
  // Claims strictly under `path`: one contiguous run of the sorted list.
  const under = (path: string): Claim[] => {
    const pfx = path === '' ? '' : path + '/'
    const out: Claim[] = []
    for (let i = lowerBound(pfx); i < all.length; i++) {
      const c = all[i]
      if (pfx && !c.path.startsWith(pfx)) break
      if (c.path === path) continue
      out.push(c)
    }
    return out
  }
  const isTop = (c: Claim, path: string): boolean => {
    const a = anc.get(c.path) ?? null
    return !a || a.path.length <= path.length
  }
  // A cover the scan already attributes wholly to U: every path inside has
  // all = mine, so U's slice is the total and no by-path read is needed.
  const partial = (c: Claim) => c.mine < c.all
  return {
    isClaim: path => byPath.has(path),
    needsTotal(path) {
      const c = cover(path)
      return !!c && c.who === u && partial(c)
    },
    regions(path) {
      const out: Region[] = []
      for (const c of under(path)) { // sorted: an outer region precedes what it holds
        if (eff.get(c.path) !== u || !partial(c)) continue
        const last = out[out.length - 1]
        if (last && c.path.startsWith(last.path + '/')) continue
        out.push({ path: c.path, depth: c.path.split('/').length, all: c.all, objects: c.objects })
      }
      return out
    },
    value(path, allB, mine) {
      const cv = cover(path)
      const cov = cv?.who ?? null
      const inside = under(path)
      let base = 0
      if (cov === u) {
        if (allB == null) allB = !partial(cv!) ? mine : byPath.get(path)?.all ?? null
        if (allB == null) throw new Error(`owner lens: total bytes needed at ${path || '<root>'}`)
        base = allB
        for (const c of inside) if (isTop(c, path)) base -= c.all
      } else if (cov == null && mine != null) {
        base = mine
        for (const c of inside) if (isTop(c, path)) base -= c.mine
      }
      let bands = 0
      for (const c of inside) {
        const e = eff.get(c.path)
        if (e !== u && e != null) continue
        const pick = (x: Claim) => (e === u ? x.all : x.mine)
        let band = pick(c)
        for (const d of kids.get(c.path) ?? []) band -= pick(d)
        bands += Math.max(0, band)
      }
      return Math.max(0, base + bands)
    },
  }
}
