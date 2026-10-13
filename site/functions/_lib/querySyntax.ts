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

/** A term's anchors: `^` before it (a segment starts there), `$` after it (one ends there). */
export interface Anchors { start?: boolean; end?: boolean }

/** Literal pieces (lowercase) → a matcher: one piece is a substring, more are
 * joined by in-segment wildcards; anchored as `a` says. */
const piecesMatcher = (pieces: string[], a: Anchors = {}): Matcher => {
  const anchors = { ...(a.start ? { start: true as const } : {}), ...(a.end ? { end: true as const } : {}) }
  return pieces.length === 1 ? { kind: 'sub', text: pieces[0], ...anchors } : { kind: 'glob', pieces, ...anchors }
}

/** The help's one line on literal `^`/`$` (the filter box's and the indexed-only deployment's). */
export const ESCAPE_NOTE = '^/$ anchor a name; write \\^ or \\$ (or quote it) to search them literally.'

/** The characters a `\` escapes in a `simple` term: `\^` and `\$` are literal (never anchors), `\\` one `\`. */
const ESCAPABLE = '^$\\'

/** A literal as written in a term: a `^` first or a `$` last escaped (else they'd read as anchors), and a `\`
 *  escaped where it would otherwise escape what follows it (an escapable character, or the term's end, where an
 *  anchor `$` may follow). */
export const escapeLiteral = (text: string): string => [...text].map((c, i, cs) =>
  (c === '^' && i === 0) || (c === '$' && i === cs.length - 1) ? `\\${c}`
    : c === '\\' && (i === cs.length - 1 || ESCAPABLE.includes(cs[i + 1])) ? '\\\\' : c).join('')

/** How a matcher reads back in the box (`^…$` around its literal, `*` between pieces; `escapeLiteral`). */
export const matcherText = (m: Matcher): string => m.kind === 'regex'
  ? `/${m.source}/`
  : `${m.start ? '^' : ''}${escapeLiteral(m.kind === 'sub' ? m.text : m.pieces.join('*'))}${m.end ? '$' : ''}`

/** `simple` (the default): GitHub-search-like terms over the full path.
 *
 * - `tomat` — substring anywhere in the path;
 * - `a b` — AND: the path contains every term (in any segments);
 * - `a|b` — OR, binding looser than AND (`a b|c` = (a AND b) OR c);
 * - `-x` — NOT, of the whole query; only negatives = "everything except";
 * - `*` — any characters within one segment, also in a term with `/`;
 * - `^q` / `q$` / `^q$` — anchored: some name (path segment) starts with,
 *   ends with, or is `q` (an unquoted `^` first, `$` last);
 * - `"a b"` — a quoted term is literal (spaces, a leading `-`, `*`, `|`, `^`,
 *   `$`, `\`); an unterminated quote runs to the end;
 * - `\^` / `\$` — the character itself, anywhere in a term (never an anchor);
 *   `\\` is one `\`; any other `\` is itself;
 * - `/…/` — the whole query a full-path regex: an unadvertised fallback (the
 *   `regex` syntax is the advertised way), never served by the search index.
 *
 * Every positive term needs `MIN_TERM` (3) literal characters in a row (a
 * `*` term: its longest piece); exclusions are exempt, and so is a query that
 * is one literal alone (`ab`, `.`): the static name index's drilldown answers
 * any single literal exactly, and elsewhere the search reads it as a bounded
 * names scan (flagged when it is cut). Blank, `|`-only and
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
      /** The first positive term under `minTerm`, an error unless it is the whole query. */
      let short: string | null = null
      const sep = (c: string) => c === '|' || /\s/.test(c)
      let i = 0
      while (i < s.length) {
        const c = s[i]
        if (/\s/.test(c)) { i++; continue }
        if (c === '|') { alts.push({ terms: [], any: false }); i++; continue }
        let isNeg = false
        if (c === '-' && i + 1 < s.length && !sep(s[i + 1])) { isNeg = true; i++ }
        let pieces = ['']
        let quoted = false
        // An unquoted `^` opening the term, and an unquoted `$` closing it, are anchors.
        const start = s[i] === '^'
        if (start) i++
        let end = false
        while (i < s.length && (quoted || !sep(s[i]))) {
          const ch = s[i++]
          end = ch === '$' && !quoted
          // Unquoted `\^`, `\$`, `\\`: the character itself (never an anchor); any other `\` is literal.
          if (ch === '\\' && !quoted && i < s.length && ESCAPABLE.includes(s[i])) pieces[pieces.length - 1] += s[i++]
          else if (ch === '"') quoted = !quoted
          else if (ch === '*' && !quoted) pieces.push('')
          else pieces[pieces.length - 1] += ch
        }
        if (end) pieces[pieces.length - 1] = pieces[pieces.length - 1].slice(0, -1)
        // Anchors around nothing (`^`, `$`, `^$`) are the characters themselves.
        const bare = pieces.every(p => !p)
        if (bare && (start || end)) pieces = [`${start ? '^' : ''}${end ? '$' : ''}`]
        const anchors = bare ? {} : { start, end }
        const alt = alts[alts.length - 1]
        alt.any = true
        if (pieces.length === 1 && !pieces[0]) continue // `""`
        if (isNeg) neg.push(piecesMatcher(pieces, anchors))
        else {
          if (short == null && Math.max(...pieces.map(p => p.length)) < minTerm) short = matcherText(piecesMatcher(pieces, anchors))
          alt.terms.push(piecesMatcher(pieces, anchors))
        }
      }
      // A blank alternative (`a|`, `||`) adds nothing; one that held only
      // negatives is "everything" (the negatives are the query's).
      const kept = alts.filter(a => a.any).map(a => a.terms)
      const lone = kept.length === 1 && kept[0].length === 1 && kept[0][0].kind === 'sub' && !neg.length
      if (short != null && !lone) return { error: `type at least ${minTerm} characters (“${short}”)`, code: 'short-term' }
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
        { form: '^x', meaning: 'a file or folder name starting with x', example: '^ckpt' },
        { form: 'x$', meaning: 'a file or folder name ending with x', example: '.safetensors$' },
        { form: '^x$', meaning: 'a file or folder named exactly x', example: '^config.json$' },
        { form: '"…"', meaning: 'literal: spaces, a leading -, |, *, ^, $', example: '"a b"' },
        { form: 'a/b', meaning: 'a term with / spans segments', example: 'run-a/ckpt' },
      ],
      notes: [
        `Each term needs at least ${minTerm} characters in a row (a * term: its longest part); exclusions, and a query of one word alone, are exempt.`,
        'Exclusions apply to the whole query (every | alternative); only exclusions = everything except them.',
        ESCAPE_NOTE,
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
      { form: '^…', meaning: 'anchored at the bucket (the full path’s start, not a name’s)', example: '^my-bucket/tmp/' },
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
