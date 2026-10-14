/**
 * `FILTER_INDEXED_ONLY=1` (a deployment flag): the map's filter accepts only what the static name index
 * answers exactly — literal substrings of a file or folder name, plain or anchored (`^q` a name starting with
 * it, `q$` ending with it, `^q$` the whole name; specs/search-extensions.md §2), combined by AND (`a b`), OR
 * (`a|b`) and NOT (`-b`; §1, `staticBool.ts`), with no `/`, `*` or regex, unscoped — and every other form is a
 * 400 with a reason code, never a slow or approximate scan of the path store
 * (specs/architecture/static-name-search.md, "Indexed-only filter"). A combination the index can't answer
 * exactly within its budget is the same 400 (`too-broad`). A literal on a scan the index doesn't cover is
 * `scan-not-indexed` — on every deployment with the static filter, indexed-only or not (`view.ts`).
 *
 * Pure, DOM- and Workers-free: the server (`subtree`, `diff`, `series`, `/names`) and the client (the filter
 * box's inline message and help) share it.
 */
import type { QueryAst, SyntaxHelp } from './queryAst.js'
import { ESCAPE_NOTE, KIND_ERROR, makeSimple, resolveSyntax } from './querySyntax.js'

/** Why an indexed-only deployment refuses a filter. */
export type FilterRejectCode =
  | 'unsupported-regex'
  | 'unsupported-glob'
  | 'unsupported-kind'
  | 'unsupported-slash'
  | 'unsupported-scope'
  | 'scan-not-indexed'
  | 'term-too-common'
  | 'anchor-too-short'
  | 'anchor-not-indexed'
  | 'scan-dirs-only'
  | 'too-broad'

export interface FilterReject { code: FilterRejectCode; message: string }

export const ONLY_PLAIN = 'Only text search (substrings of file or folder names, combined with spaces, | and -) is supported here'

export const REJECT_MESSAGES: Record<FilterRejectCode, string> = {
  'unsupported-regex': `${ONLY_PLAIN}; regular expressions aren’t.`,
  'unsupported-glob': `${ONLY_PLAIN}; wildcards (*) aren’t.`,
  'unsupported-kind': `${KIND_ERROR}.`,
  'unsupported-slash': `${ONLY_PLAIN}; a term can’t contain “/” (it matches within one name).`,
  'unsupported-scope': 'Search isn’t available with an owner, user or storage-class scope here; clear the scope to search.',
  'scan-not-indexed': 'Search isn’t available for this scan yet.',
  'term-too-common': 'This term matches too many files to search here; try a longer one.',
  'anchor-too-short': 'An anchored term needs at least 3 characters before “$” (add the dot: “.gz$”) and 2 after “^”.',
  'anchor-not-indexed': 'Starts-with (^) and ends-with ($) search isn’t indexed on this deployment yet; search for a plain substring instead.',
  'too-broad': 'These terms match too much here to combine exactly; open a folder, or make a term more specific.',
  'scan-dirs-only': 'One of these scans lists folders only (files aren’t searchable on it), so a search can’t be compared across the two; compare two scans that both list files.',
}

export const reject = (code: FilterRejectCode): FilterReject => ({ code, message: REJECT_MESSAGES[code] })

/** `anchor-too-short`, worded for the anchor actually used (`^q`, `q$`, `^q$`). */
export const ANCHOR_SHORT = {
  start: 'A “^” term needs at least 2 characters after the “^” (e.g. “^ck”).',
  end: 'A “$” term needs at least 3 characters before the “$” (add the dot: “.gz$”).',
  exact: 'A “^…$” term needs at least 1 character between “^” and “$”.',
} as const
export const anchorTooShort = (kind: keyof typeof ANCHOR_SHORT): FilterReject => ({ code: 'anchor-too-short', message: ANCHOR_SHORT[kind] })

/** The deployment's flag. */
export const indexedOnly = (env: { FILTER_INDEXED_ONLY?: string } | undefined): boolean => env?.FILTER_INDEXED_ONLY === '1'

/** Why `q` (in syntax `qs`, else `deflt`) isn't an indexed query; null when it is one (or is blank). Parsed
 *  without the minimum term length (the syntax's own parse says "too short"), so a term made of `*` alone is a
 *  wildcard, and `is:file` / `is:dir` are `unsupported-kind`. */
export function rejectQuery(q: string | null | undefined, qs?: string | null, deflt?: string | null): FilterReject | null {
  const syntax = resolveSyntax(qs, deflt)
  if (!(q ?? '').trim()) return null
  if (syntax.id === 'regex') return reject('unsupported-regex')
  const r = makeSimple({ minTerm: 1 }).parse(q ?? '')
  if (r.error !== undefined) return reject(r.code === 'invalid-regex' ? 'unsupported-regex' : r.code === 'unsupported-kind' ? 'unsupported-kind' : 'unsupported-glob')
  return r.ast ? rejectAst(r.ast) : null
}

/** The fewest characters an anchored literal needs (code points): `q$` reads the suffix shards (suffixes of ≥ 3
 *  characters), `^q` the name index's range `/q…` (one shard needs its first three characters, `/` included);
 *  `^q$` is one exact key. */
export const ANCHOR_MIN = { end: 3, start: 2, exact: 1 } as const

/** Why a parsed query isn't indexed literals (`staticBool.ts` `boolPlan`'s shape); null when it is. Each term,
 *  positive or negative, is checked alike. */
export function rejectAst(ast: QueryAst): FilterReject | null {
  const all = [...ast.alts.flat(), ...ast.neg]
  if (all.some(m => m.kind === 'regex')) return reject('unsupported-regex')
  if (all.some(m => m.kind === 'glob')) return reject('unsupported-glob')
  if (all.some(m => m.kind === 'sub' && m.text.includes('/'))) return reject('unsupported-slash')
  for (const m of all) {
    if (m.kind !== 'sub' || !(m.start || m.end)) continue
    const need = m.start && m.end ? ANCHOR_MIN.exact : m.start ? ANCHOR_MIN.start : ANCHOR_MIN.end
    if ([...m.text].length < need) return anchorTooShort(m.start && m.end ? 'exact' : m.start ? 'start' : 'end')
  }
  return null
}

/** A filter under a scope (owner pool, user lens, storage classes) isn't indexed. */
export const rejectScope = (scoped: boolean): FilterReject | null => (scoped ? reject('unsupported-scope') : null)

/** The 400 an API answers a refusal with. */
export const rejectBody = (r: FilterReject): string => JSON.stringify({ error: r.message, code: r.code })

/** A refusal raised mid-read (`scan-not-indexed`, from the view). */
export class FilterRejected extends Error {
  constructor(readonly reject: FilterReject) { super(reject.message); this.name = 'FilterRejected' }
}

/** The filter box's help on an indexed-only deployment: the supported forms. */
export const INDEXED_HELP: SyntaxHelp = {
  label: 'plain text',
  summary: 'a substring of a file or folder name, case-insensitive',
  placeholder: 'search names, e.g. ckpt',
  forms: [
    { form: 'text', meaning: 'every file or folder whose name contains it (the outermost ones)', example: 'ckpt' },
    { form: '^text', meaning: 'names starting with it', example: '^train' },
    { form: 'text$', meaning: 'names ending with it (3+ characters)', example: '.safetensors$' },
    { form: '^text$', meaning: 'names that are exactly it', example: '^config.json$' },
    { form: 'a b', meaning: 'both (AND), in any names along the path', example: 'llama sft' },
    { form: 'a|b', meaning: 'either (OR; looser than AND)', example: 'qwen|llama' },
    { form: '-x', meaning: 'exclude what contains x (the whole query’s)', example: 'ckpt -tmp' },
    { form: '"…"', meaning: 'literal, spaces, -, |, ^ and $ included', example: '"final ckpt"' },
  ],
  notes: [
    'No wildcards (*), “/” or regular expressions. Terms too common to combine exactly here are refused; open a folder, or make one more specific.',
    'Unscoped: clear an owner, user or storage-class scope to search.',
    ESCAPE_NOTE,
  ],
}
