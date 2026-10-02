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

/** A path predicate (`pos ∧ ¬neg` on the full path) carrying its AST and its
 * two halves — the positive part (monotone for substring / glob matchers:
 * once a prefix of a path holds it, every longer path does) and the negative
 * part (null without negatives). */
export type NamePred = ((path: string) => boolean) & {
  ast?: QueryAst
  pos?: (path: string) => boolean
  neg?: ((path: string) => boolean) | null
}

const esc = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/** A matcher's test, given the path and its lowercase. */
export function matcherTest(m: Matcher): (path: string, lower: string) => boolean {
  switch (m.kind) {
    case 'sub': {
      const s = m.text
      return (_, l) => l.includes(s)
    }
    case 'glob': {
      const re = new RegExp(m.pieces.map(esc).join('[^/]*'))
      return (_, l) => re.test(l)
    }
    case 'regex': {
      const re = new RegExp(m.source, 'i')
      return p => re.test(p)
    }
  }
}

/** The AST's predicate. */
export function compileQuery(ast: QueryAst): NamePred {
  const alts = ast.alts.map(a => a.map(matcherTest))
  const negs = ast.neg.map(matcherTest)
  const pos = (path: string) => { const l = path.toLowerCase(); return alts.some(a => a.every(t => t(path, l))) }
  const neg = negs.length ? (path: string) => { const l = path.toLowerCase(); return negs.some(t => t(path, l)) } : null
  const pred = neg ? (path: string) => pos(path) && !neg(path) : pos
  return Object.assign(pred, { ast, pos, neg })
}

/** `q` in `syntax` (default `simple`) → its predicate, or null for no filter;
 * throws `QueryError` on a parse error. */
export function parseQuery(q: string | null | undefined, syntax: QuerySyntax = DEFAULT_SYNTAX): NamePred | null {
  const ast = parseAst(q, syntax)
  return ast && compileQuery(ast)
}
