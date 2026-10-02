/**
 * The search index's query planner (specs/path-store-search.md §1, §4.1): a
 * query AST (`queryAst.ts`) → the candidate condition on a match
 * root's **last segment**, as an OR of branches, each a trigram formula (what
 * the postings can narrow) plus the exact name test (what a candidate must
 * pass). Pure; the reads are `search.ts`'s.
 *
 * The planner never changes what matches: the view still tests full paths
 * with `compileQuery`'s predicate. It only answers "which last segments can a
 * match root (or an excluded path) have?" — and returns null where that set
 * isn't a function of one segment (a term ending in `/`, a `regex` matcher);
 * the view then reads as before.
 */
import type { Matcher, QueryAst } from './queryAst.js'

/** A monotone boolean formula over trigram codes: `true` (no constraint), a
 * trigram (the name contains it), or an AND / OR of formulas. */
export type Formula = true | number | { and: Formula[] } | { or: Formula[] }

export interface Branch {
  /** What the postings narrow: every name passing `test` satisfies it. */
  formula: Formula
  /** The exact condition on a candidate name (the segment as stored). */
  test: (name: string) => boolean
}

/** An OR of branches: a name is a candidate when some branch's test passes. */
export interface SearchPlan { branches: Branch[] }

const isAscii = (c: number) => c < 128

/** The trigram code of 3 ASCII characters: `c0<<16 | c1<<8 | c2`. */
export const triCode = (s: string, i = 0): number => (s.charCodeAt(i) << 16) | (s.charCodeAt(i + 1) << 8) | s.charCodeAt(i + 2)

/** Distinct all-ASCII trigrams of the lowercased string, ascending. Non-ASCII
 * trigrams are never looked up (case mappings of non-ASCII text differ
 * between DuckDB and JS; an ASCII run is the same run under any of them). */
export function trigrams(s: string): number[] {
  const l = s.toLowerCase()
  const out = new Set<number>()
  for (let i = 0; i + 3 <= l.length; i++) {
    if (isAscii(l.charCodeAt(i)) && isAscii(l.charCodeAt(i + 1)) && isAscii(l.charCodeAt(i + 2))) out.add(triCode(l, i))
  }
  return [...out].sort((a, b) => a - b)
}

export function and(fs: Formula[]): Formula {
  const out: Formula[] = []
  for (const f of fs) {
    if (f === true) continue
    if (typeof f === 'object' && 'and' in f) out.push(...f.and)
    else out.push(f)
  }
  return out.length === 0 ? true : out.length === 1 ? out[0] : { and: out }
}

export function or(fs: Formula[]): Formula {
  const out: Formula[] = []
  for (const f of fs) {
    if (f === true) return true
    if (typeof f === 'object' && 'or' in f) out.push(...f.or)
    else out.push(f)
  }
  // Absorption: an alternative requiring a superset of another's trigrams
  // adds nothing (`ckpt | ckpts` = `ckpt`); equal sets collapse to one.
  const flat = (f: Formula): number[] | null => typeof f === 'number' ? [f] : typeof f === 'object' && 'and' in f && f.and.every(x => typeof x === 'number') ? f.and as number[] : null
  const sets = out.map(f => { const t = flat(f); return t && new Set(t) })
  const kept = out.filter((_, i) => {
    const si = sets[i]
    if (!si) return true
    return !sets.some((sj, j) => j !== i && sj && [...sj].every(t => si.has(t)) && (sj.size < si.size || j < i))
  })
  // An empty OR can't arise from a query (every alternative yields a formula).
  return kept.length === 1 ? kept[0] : { or: kept }
}

/** A string's formula: every name containing it holds all its trigrams. */
const stringFormula = (s: string): Formula => and(trigrams(s))

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** A substring / glob matcher's literal pieces (a substring is one piece);
 * null for a regex. */
const piecesOf = (m: Matcher): string[] | null => (m.kind === 'sub' ? [m.text] : m.kind === 'glob' ? m.pieces : null)

/** One matcher's branch: what a match root's last segment must satisfy for
 * the term to have become true there (spec §1), or null when the term
 * constrains no segment (it ends in `/`) or isn't indexable (a regex). */
export function termBranch(m: Matcher): Branch | null {
  const pieces = piecesOf(m)
  if (!pieces) return null
  const t = { pieces }
  // The part after the term's last `/` (wildcards never match `/`, so that
  // `/` is the separator before the root's last segment, which then starts
  // with the rest); a slash-free term lies inside the segment.
  let k = t.pieces.length - 1
  while (k >= 0 && !t.pieces[k].includes('/')) k--
  const anchored = k >= 0
  const tail = anchored ? [t.pieces[k].slice(t.pieces[k].lastIndexOf('/') + 1), ...t.pieces.slice(k + 1)] : t.pieces
  if (anchored && tail.length === 1 && !tail[0]) return null
  const formula = and(tail.map(stringFormula))
  if (tail.length === 1) {
    const u = tail[0]
    return { formula, test: anchored ? n => n.toLowerCase().startsWith(u) : n => n.toLowerCase().includes(u) }
  }
  const re = new RegExp((anchored ? '^' : '') + tail.map(esc).join('[^/]*'))
  return { formula, test: n => re.test(n.toLowerCase()) }
}

/** The candidate names of an OR of matchers — one branch each; null when
 * some matcher constrains no segment. */
export function planTerms(terms: Matcher[]): SearchPlan | null {
  const branches: Branch[] = []
  for (const t of terms) {
    const b = termBranch(t)
    if (!b) return null
    branches.push(b)
  }
  return branches.length ? { branches } : null
}

/** Where the positive part of `ast` can become true: a match root's last
 * segment satisfies some term of the alternative that became true there —
 * so the union of every positive term's candidates (AND = union, then the
 * exact filter). Null for a regex matcher or an alternative with no positive
 * term (the positive part is everywhere true). */
export function planPositive(ast: QueryAst): SearchPlan | null {
  if (ast.alts.some(a => !a.length)) return null
  return planTerms(ast.alts.flat())
}

/** Where the negative part can become true: the excluded paths' last segments. */
export const planNegative = (ast: QueryAst): SearchPlan | null => planTerms(ast.neg)
