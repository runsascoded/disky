/**
 * The path filter's predicate (specs/path-store-search.md §1): a `QueryAst`
 * (`queryAst.ts`, from any syntax in `querySyntax.ts`) → the test on a node's
 * full index path (`bucket/dir/sub`), shared by the server (`scope.ts`, the
 * filter view, the search) and the client (`src/filterTree.ts`).
 *
 * Pure, DOM- and Workers-free.
 */
import type { Matcher, QueryAst, QuerySyntax } from './queryAst.js'
import { DEFAULT_SYNTAX, parseAst } from './querySyntax.js'
import { type HexRule, occurs, segmentOccurs } from './hexRuns.js'

/** A path predicate (`pos ∧ ¬neg` on the full path) carrying its AST and its
 * two halves — the positive part (monotone for substring / glob matchers:
 * once a prefix of a path holds it, every longer path does) and the negative
 * part (null without negatives). */
export type NamePred = ((path: string) => boolean) & {
  ast?: QueryAst
  /** The hex-run rule it was compiled under (`CompileOpts`), if any. */
  hexRuns?: HexRule | null
  pos?: (path: string) => boolean
  neg?: ((path: string) => boolean) | null
}

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** Compile options: `hexRuns`, the deployment's static name index generation's hex-run rule (`hexRuns.ts`): a
 *  substring then matches only where it `occurs` under the rule (an anchored one per segment, `segmentOccurs`), as
 *  the static index answers it (the server's fallback reads; the client compiles without one). */
export interface CompileOpts { hexRuns?: HexRule | null }

/** `body` with a matcher's anchors: `start` — at the path's start or right
 * after a `/` (a segment starts there), `end` — at its end or right before a
 * `/` (a segment ends there). */
const anchored = (body: string, m: { start?: true; end?: true }): RegExp =>
  new RegExp(`${m.start ? '(?:^|/)' : ''}${body}${m.end ? '(?=/|$)' : ''}`)

/** A matcher's test, given the path and its lowercase. */
export function matcherTest(m: Matcher, opts: CompileOpts = {}): (path: string, lower: string) => boolean {
  switch (m.kind) {
    case 'sub': {
      const s = m.text, rule = opts.hexRuns ?? null
      if (!m.start && !m.end) return rule ? (_, l) => occurs(s, l, rule) : (_, l) => l.includes(s)
      if (rule) {
        // Per segment, as anchored search reads it (`segmentOccurs`): only `q$` can lose an occurrence to the rule.
        const mode = m.start && m.end ? 'exact' : m.start ? 'start' : 'end'
        return (_, l) => l.split('/').some(seg => segmentOccurs(seg, s, mode, rule))
      }
      const re = anchored(esc(s), m)
      return (_, l) => re.test(l)
    }
    case 'glob': {
      const re = anchored(m.pieces.map(esc).join('[^/]*'), m)
      return (_, l) => re.test(l)
    }
    case 'regex': {
      const re = new RegExp(m.source, 'i')
      return p => re.test(p)
    }
  }
}

/** The AST's predicate. */
export function compileQuery(ast: QueryAst, opts: CompileOpts = {}): NamePred {
  const alts = ast.alts.map(a => a.map(m => matcherTest(m, opts)))
  const negs = ast.neg.map(m => matcherTest(m, opts))
  const pos = (path: string) => { const l = path.toLowerCase(); return alts.some(a => a.every(t => t(path, l))) }
  const neg = negs.length ? (path: string) => { const l = path.toLowerCase(); return negs.some(t => t(path, l)) } : null
  const pred = neg ? (path: string) => pos(path) && !neg(path) : pos
  return Object.assign(pred, { ast, pos, neg }, opts.hexRuns ? { hexRuns: opts.hexRuns } : {})
}

/** `q` in `syntax` (default `simple`) → its predicate, or null for no filter;
 * throws `QueryError` on a parse error. */
export function parseQuery(q: string | null | undefined, syntax: QuerySyntax = DEFAULT_SYNTAX): NamePred | null {
  const ast = parseAst(q, syntax)
  return ast && compileQuery(ast)
}
