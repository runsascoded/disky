/** The map's filter from the static name index for compound queries (specs/search-extensions.md §1): AND
 *  (`a b`), OR (`a|b`, looser than AND), NOT (`a -b`, the whole query's) and exclusions alone (`-b`), every
 *  term a literal without `/` (plain, or anchored `^q` / `q$` / `^q$`), exact on every scan the answers it
 *  reads cover, from each term's static answers (`staticFilter.ts` `HitSource`).
 *
 *  Semantics. A path matches `(g₁ | g₂ | …) -b₁ … -bₘ` when its lowercase full path holds every term of some
 *  AND-group `gᵢ` and no `bⱼ` ("holds": `staticAnchors.ts` `termInPath`, under the generation's hex-run rule).
 *  The view draws the match roots — the outermost matching paths under P — each counted whole less its
 *  excluded paths: the outermost paths under it holding some `bⱼ`. Per object it is the same answer: an object
 *  is counted iff its own full path matches.
 *
 *  Why it is exact. Terms P's own path holds are true everywhere under P (a negative one: nothing under P
 *  matches); the rest lie inside one segment below P, so a path holds `t` iff it is at or under one of `t`'s
 *  match roots under P (`S_t`: its first hits — the name holds `t`, the parent doesn't). Which paths are roots
 *  depends on names only, never on the scan, so one answer serves every date (liveness per version).
 *  - **AND.** The outermost paths holding every term of `g` are `C_g = ∪_{t∈g} {r ∈ S_t : r holds every term of
 *    g}`: an outermost such path's parent misses some `t`, so the path is a first hit of `t`; and a first hit
 *    of `t` holding every term has a parent missing `t`. With every `S_t` under P listed (a light term; a heavy
 *    term's drill roots) it is a filter over them. A term whose answer under P is a rollup (`H`) is never
 *    listed; its members of `C_g` hold the listed terms (`L`), so each sits at or under some `c ∈ C_L`: c
 *    itself when c holds every H term, else the H-roots under c holding the rest — read at c (or, grouped, at
 *    an ancestor, descending through rollups whose remainder rules a child out).
 *  - **OR.** A path matches the OR iff it is at or under some `C_g`, so the match roots are the outermost paths
 *    of `∪_g C_g` (a root of one group under another group's is dropped). An AND-group whose terms P holds
 *    makes P the single match root.
 *  - **NOT.** A match root holds no `b` (it and everything under it would); under a root r the outermost
 *    paths holding a `b` are `b`'s match roots under r — subtracted from r, dropped from the tree. Where `b` is
 *    heavy under r, its rollup enumerates them when its remainder is zero: a kept child holding `b` is a root
 *    itself (read by `at`), one that doesn't is the same again below it.
 *  - **NOT alone** (or an AND-group P holds): P is the one match root, less the above.
 *
 *  Never approximate: where an answer can't be exact — every term of an AND-group heavy at P (the rollups'
 *  "N others" can't be intersected), a heavy term under a candidate, a negative's rollup with a non-zero
 *  remainder, past `budget.reads` reads or `budget.rows` hits — it declines (`BoolDecline`), and the caller
 *  refuses (`too-broad`, indexed-only) or reads as before (a path-store walk, flagged approximate). A term
 *  its source can't answer at all declines with that term (`key`), so the caller names the term's reason. */
import type { Matcher, QueryAst } from './queryAst.js'
import type { Found, HitSource } from './staticFilter.js'
import type { Rollup } from './staticDrill.js'
import type { Hit } from './staticNames.js'
import { type HexRule } from './hexRuns.js'
import { termInPath, termKey } from './staticAnchors.js'
import { ANCHOR_MIN } from './indexedOnly.js'

/** A query the static index can answer: AND-groups of literal keys (OR between them; `[]` = everything) and
 *  the query's negative keys, as `termKey` writes them (`q`, `/q`, `q/`, `/q/`). */
export interface BoolPlan { alts: string[][]; neg: string[] }

/** A matcher's static key: a non-empty substring without `/`, anchored ones at least `ANCHOR_MIN` long; else null. */
export function literalKey(m: Matcher): string | null {
  if (m.kind !== 'sub' || !m.text || m.text.includes('/')) return null
  const start = !!m.start, end = !!m.end
  if ((start || end) && [...m.text].length < (start && end ? ANCHOR_MIN.exact : start ? ANCHOR_MIN.start : ANCHOR_MIN.end)) return null
  return termKey({ text: m.text, start, end })
}

/** The query as AND-groups and negatives of static keys, else null (a glob, a regex, a `/`, an anchor too
 *  short). Each group's terms deduplicated, a group holding every term of another dropped (it matches a subset
 *  of it), duplicate groups and negatives dropped. */
export function boolPlan(ast: QueryAst | undefined): BoolPlan | null {
  if (!ast) return null
  const groups = ast.alts.map(g => g.map(literalKey))
  const neg = ast.neg.map(literalKey)
  if (groups.some(g => g.some(t => t == null)) || neg.some(t => t == null)) return null
  const sets = groups.map(g => [...new Set(g as string[])])
  if (!sets.some(g => g.length) && !neg.length) return null
  const sub = (a: string[], b: string[]) => a.every(t => b.includes(t))
  const alts = sets.filter((g, i) => !sets.some((h, j) => j !== i && sub(h, g) && (h.length < g.length || j < i)))
  return { alts, neg: [...new Set(neg as string[])] }
}

/** A plan beyond one positive literal (that one is `staticFilter.ts`'s own path). */
export const compound = (p: BoolPlan | null): p is BoolPlan => !!p && !(p.alts.length === 1 && p.alts[0].length === 1 && !p.neg.length)

/** The answer under P, any date. `whole`: P is the single match root (an AND-group P's path holds, or
 *  exclusions alone). `none`: a negative term P's path holds — nothing under P matches. */
export interface BoolAnswer {
  whole: boolean
  none?: true
  /** The match roots' hits (each root's every version of every owner slice); none with `whole`. */
  roots: Hit[]
  /** The outermost excluded paths' hits, strictly under a root (under P with `whole`). */
  excl: Hit[]
  /** The scans every answer read covers (absent: every scan of the store). */
  scans?: string[]
  io: { reads: number; hits: number; ms: number }
}

/** Why a plan wasn't answered: `too-broad` (the combination can't be made exact within the budget: `reason`
 *  says which rule), or `term` — the source answers none for `key` (its own refusal names why). */
export type BoolDecline = { declined: 'too-broad'; reason: string } | { declined: 'term'; key: string; reason: string }

export const isDecline = (a: BoolAnswer | BoolDecline): a is BoolDecline => 'declined' in a

export interface BoolBudget { reads: number; rows: number }
/** Reads beyond each term's own at P, and hits held, per answer. */
export const BOOL_BUDGET: BoolBudget = { reads: 24, rows: 400_000 }

const parentOf = (p: string): string => { const i = p.lastIndexOf('/'); return i === -1 ? '' : p.slice(0, i) }
/** `p`'s child of `X` on the way down (`p` strictly under `X`). */
const childOf = (p: string, X: string): string => { const rest = X === '' ? p : p.slice(X.length + 1); const i = rest.indexOf('/'); return i === -1 ? p : (X === '' ? '' : X + '/') + rest.slice(0, i) }
/** Whether `p` has a strict ancestor in `set`, strictly under `P`. */
function insideAny(p: string, set: Set<string> | Map<string, unknown>, P: string): boolean {
  for (let q = parentOf(p); q.length > P.length; q = parentOf(q)) if (set.has(q)) return true
  return false
}

/** Whether a rollup's child holds no root on any date: not kept, and the remainder never non-zero. */
function emptyChild(r: Rollup, name: string): boolean {
  if (r.cells.some(c => c.kind === 1 && c.child === name)) return false
  return !r.cells.some(c => c.kind === 2 && (c.b !== 0n || c.o !== 0n))
}
const hasRest = (r: Rollup): boolean => r.cells.some(c => c.kind === 2 && (c.b !== 0n || c.o !== 0n))

class Decline extends Error {
  constructor(readonly why: BoolDecline) { super(why.reason) }
}
const broad = (reason: string) => new Decline({ declined: 'too-broad', reason })

/** One evaluation's reads: each `(term, path)` answer once, counted against the budget. */
class Ctx {
  reads = 0
  rows = 0
  scans: Set<string> | null = null
  private memo = new Map<string, Promise<Found | null>>()
  constructor(readonly src: HitSource, readonly budget: BoolBudget, readonly free: Set<string>, readonly rule: HexRule | null) {}

  holds(t: string, p: string): boolean { return termInPath(t, p, this.rule) }

  private note(f: Found | null): Found | null {
    if (!f) return f
    if (f.hits) { this.rows += f.hits.length; if (this.rows > this.budget.rows) throw broad(`over ${this.budget.rows} hits`) }
    if (f.scans) {
      const s = new Set(f.scans)
      this.scans = this.scans ? new Set([...this.scans].filter(x => s.has(x))) : s
    }
    return f
  }

  private spend(): void {
    if (++this.reads > this.budget.reads) throw broad(`over ${this.budget.reads} reads`)
  }

  /** `t`'s answer strictly under `X`; none from the source declines naming `t`. */
  async hits(t: string, X: string): Promise<Found> {
    const k = `${t}\0${X}`
    let p = this.memo.get(k)
    if (!p) {
      if (!this.free.has(k)) this.spend()
      p = this.src.hits(t, X).then(f => this.note(f))
      this.memo.set(k, p)
    }
    const f = await p
    if (!f) throw new Decline({ declined: 'term', key: t, reason: `no answer for ${t} under ${X || '/'}` })
    return f
  }

  /** `t`'s root at exactly `path` (its hits). */
  async at(t: string, path: string): Promise<Hit[]> {
    if (!this.src.at) throw broad(`${t}: no root reads`)
    this.spend()
    const f = this.note(await this.src.at(t, path))
    if (!f?.hits) throw broad(`${t}: no root answer at ${path}`)
    return f.hits
  }
}

/** Hits grouped by path, first-seen order. */
function byPath(hits: Iterable<Hit>): Map<string, Hit[]> {
  const m = new Map<string, Hit[]>()
  for (const h of hits) { const a = m.get(h.path); if (a) a.push(h); else m.set(h.path, [h]) }
  return m
}

/** `t`'s match roots under each of `cands` (paths strictly under `X`, or `X` itself; none holding `t`), read at
 *  `X` and, through rollups, below it: per candidate its roots' hits, or (`X` itself, heavy) `X`'s rollup. */
async function rootsOf(ctx: Ctx, t: string, X: string, cands: string[]): Promise<Map<string, Hit[] | Rollup>> {
  const out = new Map<string, Hit[] | Rollup>()
  if (!cands.length) return out
  const f = await ctx.hits(t, X)
  if (f.hits) {
    if (cands.includes(X)) { out.set(X, f.hits); return out }
    // Candidates are disjoint subtrees: bucket the hits by the candidate above them.
    const set = new Set(cands)
    for (const c of cands) out.set(c, [])
    for (const h of f.hits) {
      for (let q = parentOf(h.path); q.length > X.length; q = parentOf(q)) {
        if (set.has(q)) { (out.get(q) as Hit[]).push(h); break }
      }
    }
    return out
  }
  const r = f.rollup!
  if (cands.length === 1 && cands[0] === X) { out.set(X, r); return out }
  const groups = new Map<string, string[]>()
  for (const c of cands) {
    if (c === X) throw broad(`${t} heavy under a candidate and its descendants`)
    const k = childOf(c, X)
    groups.set(k, [...(groups.get(k) ?? []), c])
  }
  const parts = await Promise.all([...groups].map(async ([k, cs]) => {
    const name = X === '' ? k : k.slice(X.length + 1)
    if (emptyChild(r, name)) return new Map(cs.map(c => [c, [] as Hit[]]))
    return rootsOf(ctx, t, k, cs)
  }))
  for (const m of parts) for (const [c, v] of m) out.set(c, v)
  return out
}

/** Every match root of `b` strictly under `X` (`X` holds no `b`), enumerated through rollups: a kept child
 *  holding `b` read by `at`, one that doesn't recursively. A rollup with a non-zero remainder (which children
 *  hold `b` is unknown) declines. */
async function expand(ctx: Ctx, b: string, X: string, given?: Hit[] | Rollup): Promise<Hit[]> {
  const got = given ?? (await rootsOf(ctx, b, X, [X])).get(X)!
  if (Array.isArray(got)) return got
  const r = got
  if (hasRest(r)) throw broad(`-${b}: heavy under ${X || '/'} (its rollup's remainder hides which folders hold it)`)
  const kids = [...new Set(r.cells.filter(c => c.kind === 1).map(c => c.child))]
  if (ctx.reads + kids.length > ctx.budget.reads) throw broad(`over ${ctx.budget.reads} reads`)
  const parts = await Promise.all(kids.map(name => {
    const k = X === '' ? name : `${X}/${name}`
    return ctx.holds(b, k) ? ctx.at(b, k) : expand(ctx, b, k)
  }))
  return parts.flat()
}

/** One AND-group's match roots under P (every term absent from P's path): their hits by path. */
async function conj(ctx: Ctx, terms: string[], P: string, depth = 0): Promise<Map<string, Hit[]>> {
  if (depth > 3) throw broad('nested too deep')
  const found = await Promise.all(terms.map(t => ctx.hits(t, P)))
  const L = terms.filter((_, i) => found[i].hits)
  const H = terms.filter((_, i) => found[i].rollup)
  // Every term heavy here: nothing to start from but rollups (their children's totals, not roots).
  if (!L.length) throw broad(`${terms.join(' ')}: every term heavy under ${P || '/'}`)
  const C = new Map<string, Hit[]>()
  for (const t of L) {
    for (const [p, hs] of byPath(found[terms.indexOf(t)].hits!)) {
      if (!C.has(p) && L.every(u => ctx.holds(u, p))) C.set(p, hs)
    }
  }
  if (!H.length) return C
  const out = new Map<string, Hit[]>()
  const missing = new Map<string, string[]>()
  for (const [c, hs] of C) {
    const m = H.filter(h => !ctx.holds(h, c))
    if (!m.length) out.set(c, hs)
    else missing.set(c, m)
  }
  // One heavy term missing: its roots under each such candidate, read grouped from P down.
  for (const h of H) {
    const cs = [...missing].filter(([, m]) => m.length === 1 && m[0] === h).map(([c]) => c)
    const got = await rootsOf(ctx, h, P, cs)
    for (const c of cs) {
      const v = got.get(c)!
      if (!Array.isArray(v)) throw broad(`${h}: heavy under ${c}`)
      for (const [p, hs] of byPath(v)) out.set(p, hs)
    }
  }
  // Several: the conjunction of those under the candidate.
  for (const [c, m] of missing) {
    if (m.length < 2) continue
    for (const [p, hs] of await conj(ctx, m, c, depth + 1)) out.set(p, hs)
  }
  return out
}

/** The plan's answer under P, any date, or why it can't be exact (`BoolDecline`). `rule`: the generation's
 *  hex-run rule (what "a path holds a term" means, as the sources' first hits do). The `(term, P)` answers are
 *  free (the view's own); every other read counts against `budget`. */
export async function evalBool(src: HitSource, plan: BoolPlan, P: string, rule: HexRule | null = null, budget: BoolBudget = BOOL_BUDGET): Promise<BoolAnswer | BoolDecline> {
  const t0 = Date.now()
  const free = new Set([...plan.alts.flat(), ...plan.neg].map(t => `${t}\0${P}`))
  const ctx = new Ctx(src, budget, free, rule)
  const done = (a: Omit<BoolAnswer, 'io' | 'scans'>): BoolAnswer => ({ ...a, ...(ctx.scans ? { scans: [...ctx.scans].sort() } : {}), io: { reads: ctx.reads, hits: ctx.rows, ms: Date.now() - t0 } })
  // A negative term P holds: everything under P holds it.
  if (plan.neg.some(b => ctx.holds(b, P))) return done({ whole: false, none: true, roots: [], excl: [] })
  const groups = plan.alts.map(g => g.filter(t => !ctx.holds(t, P)))
  const whole = groups.some(g => !g.length)
  try {
    let roots = new Map<string, Hit[]>()
    if (!whole) {
      const parts = await Promise.all(groups.map(g => conj(ctx, g, P)))
      for (const m of parts) for (const [p, hs] of m) if (!roots.has(p)) roots.set(p, hs)
      // OR: the outermost of the groups' roots (a root of one group under another's is inside a match).
      if (parts.length > 1) roots = new Map([...roots].filter(([p]) => !insideAny(p, roots, P)))
      // A root holding a negative term is no root (everything under it holds it too).
      if (plan.neg.length) roots = new Map([...roots].filter(([p]) => !plan.neg.some(b => ctx.holds(b, p))))
    }
    const excl = new Map<string, Hit[]>()
    for (const b of plan.neg) {
      const cands = whole ? [P] : [...roots.keys()]
      const got = await rootsOf(ctx, b, P, cands)
      for (const c of cands) for (const [p, hs] of byPath(await expand(ctx, b, c, got.get(c)!))) if (!excl.has(p)) excl.set(p, hs)
    }
    // The outermost excluded paths (one negative's roots may sit under another's).
    const outer = [...excl].filter(([p]) => !insideAny(p, excl, P))
    return done({ whole, roots: [...roots.values()].flat(), excl: outer.flatMap(([, hs]) => hs) })
  } catch (e) {
    if (e instanceof Decline) return e.why
    throw e
  }
}
