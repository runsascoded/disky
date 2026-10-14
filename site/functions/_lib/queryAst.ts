/**
 * The path filter's query, independent of how it was written
 * (specs/path-store-search.md §1, "Syntaxes"). A `QuerySyntax` turns the
 * filter box's text into a `QueryAst`; everything downstream — the path
 * predicate (`pathQuery.ts` `compileQuery`), the search index's plan
 * (`searchQuery.ts`), the NOT subtraction (`view.ts`) — reads only the AST.
 *
 * Pure, DOM- and Workers-free.
 */

/** One test on a node's full index path (`bucket/dir/sub`). Case-insensitive.
 *
 * - `sub` — `text` (lowercase) is a substring of the path;
 * - `glob` — literal `pieces` (lowercase, ≥ 2) joined by "any characters
 *   within one segment" (`[^/]*`);
 * - either one anchored (specs/search-extensions.md §2): `start` — the match
 *   begins a segment (`^q`: some name starts with `q`), `end` — it ends one
 *   (`q$`: some name ends with `q`), both — a whole segment (`^q$`);
 * - `regex` — a JS regex (`source`, flag `i`) over the full path. Never served
 *   by the search index: a query holding one reads as before (§5). */
export type Matcher =
  | { kind: 'sub'; text: string; start?: true; end?: true }
  | { kind: 'glob'; pieces: string[]; start?: true; end?: true }
  | { kind: 'regex'; source: string }

/** `(OR of AND-groups of positive matchers) AND NOT (any negative matcher)`.
 * Negatives are the whole query's, whichever alternative a syntax let them be
 * written in: per-alternative NOT would make the predicate non-monotone along
 * a path, which the match-root / exclusion model can't express (§1). An empty
 * AND-group is true (a query of only negatives = "everything except"). A
 * parsed query has at least one group and at least one matcher. */
export interface QueryAst {
  alts: Matcher[][]
  neg: Matcher[]
}

/** A syntax's help, rendered by the filter box's help card (`QueryHelp.tsx`):
 * a new syntax brings its own. */
export interface SyntaxHelp {
  /** Short name in the card's syntax picker. */
  label: string
  /** One line: what a query is, in this syntax. */
  summary: string
  /** The filter box's placeholder (keep it short). */
  placeholder: string
  /** The forms, one example each. */
  forms: { form: string; meaning: string; example: string }[]
  notes: string[]
}

/** Why a query doesn't parse:
 * - `short-term` — a positive term with under `MIN_TERM` literal characters
 *   in a row (the search index is trigram-based);
 * - `invalid-regex` — a regex that doesn't compile;
 * - `unknown-syntax` — a `qs=` no syntax has. */
export type QueryErrorCode = 'short-term' | 'invalid-regex' | 'unknown-syntax'

/** What a syntax's `parse` returns: an AST, null for "nothing to filter by"
 * (blank), or a typed error whose message the client shows under the box (a
 * 400 from the API). */
export type ParseResult =
  | { ast: QueryAst | null; error?: undefined; code?: undefined }
  | { ast?: undefined; error: string; code: QueryErrorCode }

export interface QuerySyntax {
  /** The `qs=` value. */
  id: string
  parse(q: string): ParseResult
  describe(): SyntaxHelp
}

/** A parse error, thrown by the convenience entry points (`parseAst`,
 * `parseQuery`); API handlers turn it into a 400. */
export class QueryError extends Error {
  constructor(message: string, readonly code: QueryErrorCode) {
    super(message)
    this.name = 'QueryError'
  }
}
