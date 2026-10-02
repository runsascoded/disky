/**
 * The filter box's syntaxes and their registry (specs/path-store-search.md
 * §1, "Syntaxes"). A syntax is chosen by `qs=<id>` on the page and the API;
 * absent, the deployment's default (`QUERY_SYNTAX` on the Functions, the
 * store's `querySyntax` on the client), else `simple`.
 *
 * Pure, DOM- and Workers-free: the server and the client share it.
 */
import { type Matcher, type ParseResult, type QueryAst, QueryError, type QuerySyntax } from './queryAst.js'

/** A regex source the full-path matcher can compile, else its error. */
function regexError(source: string): ParseResult | null {
  try {
    new RegExp(source, 'i')
    return null
  } catch (e) {
    return { error: `invalid regex: ${(e as Error).message}`, code: 'invalid-regex' }
  }
}

/** The fewest literal characters in a row a positive `simple` term needs (its
 * longest piece, for a `*` term): the search index is trigram-based, so a
 * shorter term could only be answered by a scan. Exclusions are exempt. */
export const MIN_TERM = 3

/** Literal pieces (lowercase) → a matcher: one piece is a substring, more are
 * joined by in-segment wildcards. */
const piecesMatcher = (pieces: string[]): Matcher =>
  pieces.length === 1 ? { kind: 'sub', text: pieces[0] } : { kind: 'glob', pieces }

/** `simple` (the default): GitHub-search-like terms over the full path.
 *
 * - `tomat` — substring anywhere in the path;
 * - `a b` — AND: the path contains every term (in any segments);
 * - `a|b` — OR, binding looser than AND (`a b|c` = (a AND b) OR c);
 * - `-x` — NOT, of the whole query; only negatives = "everything except";
 * - `*` — any characters within one segment, also in a term with `/`;
 * - `"a b"` — a quoted term is literal (spaces, a leading `-`, `*`, `|`); an
 *   unterminated quote runs to the end;
 * - `/…/` — the whole query a full-path regex: an unadvertised fallback (the
 *   `regex` syntax is the advertised way), never served by the search index.
 *
 * Every positive term needs `MIN_TERM` (3) literal characters in a row (a
 * `*` term: its longest piece); exclusions are exempt. Blank, `|`-only and
 * `""`-only queries are "nothing to filter by"; the errors are a short term
 * and an invalid `/…/` regex. */
export const simple: QuerySyntax = makeSimple()

/** `simple` with another minimum term length (`minTerm: 1` exercises the
 * search engine's short-needle paths in tests; the registry's has 3). */
export function makeSimple({ minTerm = MIN_TERM }: { minTerm?: number } = {}): QuerySyntax {
  return {
    id: 'simple',
    parse(q: string): ParseResult {
      const raw = q.trim()
      if (!raw) return { ast: null }
      if (raw.length > 2 && raw.startsWith('/') && raw.endsWith('/')) {
        const source = raw.slice(1, -1)
        return regexError(source) ?? { ast: { alts: [[{ kind: 'regex', source }]], neg: [] } }
      }
      const s = raw.toLowerCase()
      const alts: { terms: Matcher[]; any: boolean }[] = [{ terms: [], any: false }]
      const neg: Matcher[] = []
      const sep = (c: string) => c === '|' || /\s/.test(c)
      let i = 0
      while (i < s.length) {
        const c = s[i]
        if (/\s/.test(c)) { i++; continue }
        if (c === '|') { alts.push({ terms: [], any: false }); i++; continue }
        let isNeg = false
        if (c === '-' && i + 1 < s.length && !sep(s[i + 1])) { isNeg = true; i++ }
        const pieces = ['']
        let quoted = false
        while (i < s.length && (quoted || !sep(s[i]))) {
          const ch = s[i++]
          if (ch === '"') quoted = !quoted
          else if (ch === '*' && !quoted) pieces.push('')
          else pieces[pieces.length - 1] += ch
        }
        const alt = alts[alts.length - 1]
        alt.any = true
        if (pieces.length === 1 && !pieces[0]) continue // `""`
        if (isNeg) neg.push(piecesMatcher(pieces))
        else if (Math.max(...pieces.map(p => p.length)) < minTerm) {
          return { error: `type at least ${minTerm} characters (“${pieces.join('*')}”)`, code: 'short-term' }
        } else alt.terms.push(piecesMatcher(pieces))
      }
      // A blank alternative (`a|`, `||`) adds nothing; one that held only
      // negatives is "everything" (the negatives are the query's).
      const kept = alts.filter(a => a.any).map(a => a.terms)
      if (!kept.some(a => a.length) && !neg.length) return { ast: null }
      return { ast: { alts: kept.length ? kept : [[]], neg } }
    },
    describe: () => ({
      label: 'simple',
      summary: 'terms match anywhere in the full path (bucket/dir/…), case-insensitive',
      placeholder: 'filter paths, e.g. ckpt -tmp',
      forms: [
        { form: 'text', meaning: 'substring anywhere in the path', example: 'ckpt' },
        { form: 'a b', meaning: 'both (AND), in any segments', example: 'ckpt final' },
        { form: 'a|b', meaning: 'either (OR; looser than AND)', example: 'ttl=7d|tmp' },
        { form: '-x', meaning: 'exclude, like GitHub search: drops what contains x and subtracts its bytes', example: 'ckpt -tmp' },
        { form: '*', meaning: 'any characters within one name', example: '*.safetensors' },
        { form: '"…"', meaning: 'literal: spaces, a leading -, |, *', example: '"a b"' },
        { form: 'a/b', meaning: 'a term with / spans segments', example: 'run-a/ckpt' },
      ],
      notes: [
        `Each term needs at least ${minTerm} characters in a row (a * term: its longest part); exclusions are exempt.`,
        'Exclusions apply to the whole query (every | alternative); only exclusions = everything except them.',
      ],
    }),
  }
}

/** `regex`: the whole query is one JS regex (flag `i`) over the full path —
 * no terms, no exclusion, no minimum length; never served by the search
 * index (the thresholded read answers, flagged `approximate`). */
export const regex: QuerySyntax = {
  id: 'regex',
  parse(q: string): ParseResult {
    const source = q.trim()
    if (!source) return { ast: null }
    return regexError(source) ?? { ast: { alts: [[{ kind: 'regex', source }]], neg: [] } }
  },
  describe: () => ({
    label: 'regex',
    summary: 'one JavaScript regex over the full path (bucket/dir/…), case-insensitive',
    placeholder: 'filter paths by regex',
    forms: [
      { form: 're', meaning: 'matches anywhere in the path', example: 'ckpt.*final' },
      { form: '^…', meaning: 'anchored at the bucket', example: '^my-bucket/tmp/' },
      { form: '…$', meaning: 'anchored at the name’s end', example: '\\.safetensors$' },
      { form: '[^/]*', meaning: 'stay within one name', example: 'ckpt[^/]*final' },
    ],
    notes: ['No exclusion; the search index never serves a regex, so big views read slower.'],
  }),
}

/** Every syntax, the default first. */
export const SYNTAXES: readonly QuerySyntax[] = [simple, regex]
export const DEFAULT_SYNTAX = simple

/** The syntax with id `id`; undefined for an unknown id. */
export const syntaxById = (id: string): QuerySyntax | undefined => SYNTAXES.find(s => s.id === id)

/** `qs=` (explicit) over the deployment's default, else `simple`; an unknown
 * explicit id is an error, an unknown default falls back. */
export function resolveSyntax(qs: string | null | undefined, deflt?: string | null): QuerySyntax {
  if (qs) {
    const s = syntaxById(qs)
    if (!s) throw new QueryError(`unknown query syntax '${qs}' (want ${SYNTAXES.map(x => x.id).join('|')})`, 'unknown-syntax')
    return s
  }
  return (deflt && syntaxById(deflt)) || DEFAULT_SYNTAX
}

/** Parse `q` with `syntax` (default `simple`): the AST, null for no filter;
 * throws `QueryError` on a parse error. */
export function parseAst(q: string | null | undefined, syntax: QuerySyntax = DEFAULT_SYNTAX): QueryAst | null {
  const r = syntax.parse(q ?? '')
  if (r.error !== undefined) throw new QueryError(r.error, r.code)
  return r.ast
}
