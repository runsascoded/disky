/**
 * `FILTER_INDEXED_ONLY=1` (a deployment flag): the map's filter accepts only what the static name index
 * answers exactly — literal substrings of a file or folder name, plain or anchored (`^q` a name starting with
 * it, `q$` ending with it, `^q$` the whole name; specs/search-extensions.md §2), combined by AND (`a b`), OR
 * (`a|b`) and NOT (`-b`; §1, `staticBool.ts`), with no `/`, unscoped. Its syntax is `plain` (`querySyntax.ts`):
 * `*`, `?` and regex characters are literal text, `qs=` doesn't apply. What the index can't answer yet is a
 * 400 with a reason code (specs/search-box-no-refusals.md), never a slow or approximate scan of the path store
 * (specs/architecture/static-name-search.md, "Indexed-only filter"). A combination the index can't answer
 * exactly within its budget is the same 400 (`too-broad`). A literal on a scan the index doesn't cover is
 * `scan-not-indexed` — on every deployment with the static filter, indexed-only or not (`view.ts`).
 *
 * Pure, DOM- and Workers-free: the server (`subtree`, `diff`, `series`, `/names`) and the client (the filter
 * box's inline message and help) share it.
 */
import type { QueryAst, SyntaxHelp } from './queryAst.js'
import { ESCAPE_NOTE, KIND_ERROR, plain } from './querySyntax.js'

/** Why an indexed-only deployment refuses a filter. */
export type FilterRejectCode =
  | 'unsupported-kind'
  | 'unsupported-slash'
  | 'slash-tail-short'
  | 'unsupported-scope'
  | 'owner-too-common'
  | 'scan-not-indexed'
  | 'term-too-common'
  | 'anchor-too-short'
  | 'anchor-not-indexed'
  | 'scan-dirs-only'
  | 'too-broad'

export interface FilterReject { code: FilterRejectCode; message: string }

export const ONLY_PLAIN = 'Only text search (substrings of file or folder names, combined with spaces, | and -) is supported here'

export const REJECT_MESSAGES: Record<FilterRejectCode, string> = {
  'unsupported-kind': `${KIND_ERROR}.`,
  'unsupported-slash': `${ONLY_PLAIN}; a term can’t contain “/” (it matches within one name).`,
  'slash-tail-short': 'After a term’s last “/”, type at least 2 characters of the name (e.g. “ckpt/mo”), or end it with “$” (“ckpt/m$”).',
  'unsupported-scope': 'Search isn’t available with a person or storage-class scope here; clear the scope to search.',
  'owner-too-common': 'This term is too common in this folder to split by owner here; open a folder, or clear the owner scope.',
  'scan-not-indexed': 'Search isn’t available for this scan yet.',
  'term-too-common': 'This term matches too many files to search here; try a longer one.',
  'anchor-too-short': 'An anchored term needs at least 3 characters before “$” (add the dot: “.gz$”) and 2 after “^”.',
  'anchor-not-indexed': 'Starts-with (^), ends-with ($) and “/” search isn’t indexed on this deployment yet; search for a plain substring instead.',
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

/** Why `q` isn't an indexed query; null when it is one (or is blank). Parsed as `plain` (every term a literal:
 *  `*`, `/…/` and regex characters are text), so the only parse error is `is:file` / `is:dir`
 *  (`unsupported-kind`). */
export function rejectQuery(q: string | null | undefined): FilterReject | null {
  if (!(q ?? '').trim()) return null
  const r = plain.parse(q ?? '')
  if (r.error !== undefined) return reject('unsupported-kind')
  return r.ast ? rejectAst(r.ast) : null
}

/** The fewest characters an anchored literal needs (code points): `q$` reads the suffix shards (suffixes of ≥ 3
 *  characters), `^q` the name index's range `/q…` (one shard needs its first three characters, `/` included);
 *  `^q$` is one exact key. */
export const ANCHOR_MIN = { end: 3, start: 2, exact: 1 } as const

/** A path term's tail: its text after the last `/` (the start of a match root's name; with `$`, the whole name). */
export const pathTail = (text: string): string => text.slice(text.lastIndexOf('/') + 1)
/** The fewest tail characters (code points) a path term needs: its tail's name-index range `/t…` lies in one shard
 *  from two (`ANCHOR_MIN.start`; `staticAnchors.ts` reads it); `$` reads the exact key `/t`, one is enough. */
export const PATH_TAIL_MIN = { prefix: 2, exact: 1 } as const
/** Whether a path term's tail is long enough to read (`PATH_TAIL_MIN`). */
export const pathTailOk = (text: string, end: boolean): boolean => [...pathTail(text)].length >= (end ? PATH_TAIL_MIN.exact : PATH_TAIL_MIN.prefix)

/** `slash-tail-short`, worded for the term. An empty tail (`x/`: what is inside folders ending in x) names the folder
 *  term instead, `x$`: a folder's bytes and objects are its contents'. Its rewrite isn't served for it: the static
 *  index's hits carry no kind, so a match of `x$` that is an object (no contents) can't be told from a folder holding
 *  one (specs/search-box-no-refusals.md §3.3). */
export function slashTailShort(text: string, start: boolean): FilterReject {
  const folder = pathTail(text) === '' ? text.replace(/\/+$/, '') : ''
  return folder
    ? { code: 'slash-tail-short', message: `A term ending in “/” (what’s inside a folder) isn’t indexed here; search for the folder itself: “${start ? '^' : ''}${folder}$”.` }
    : reject('slash-tail-short')
}

/** Why a parsed query isn't indexed literals (`staticBool.ts` `boolPlan`'s shape); null when it is. Each term,
 *  positive or negative, is checked alike. */
export function rejectAst(ast: QueryAst): FilterReject | null {
  const all = [...ast.alts.flat(), ...ast.neg]
  // `plain` parses neither; another syntax's AST (a test's) is refused as a slash-less literal can't be.
  if (all.some(m => m.kind !== 'sub')) throw new Error(`indexed-only: a ${all.find(m => m.kind !== 'sub')!.kind} matcher (only \`plain\` literals)`)
  for (const m of all) {
    // A path term (its text holds `/`): read from its tail's name-index range, which needs the tail (`staticAnchors.ts`).
    if (m.kind === 'sub' && m.text.includes('/')) { if (!pathTailOk(m.text, !!m.end)) return slashTailShort(m.text, !!m.start); continue }
    if (m.kind !== 'sub' || !(m.start || m.end)) continue
    const need = m.start && m.end ? ANCHOR_MIN.exact : m.start ? ANCHOR_MIN.start : ANCHOR_MIN.end
    if ([...m.text].length < need) return anchorTooShort(m.start && m.end ? 'exact' : m.start ? 'start' : 'end')
  }
  return null
}

/** A filter under a user lens or storage classes isn't indexed (the static index's hits carry an owner slice, so
 *  an owner pool is; its rollups don't: `owner-too-common`). */
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
  summary: 'a substring of a file or folder name, case-insensitive; every character is itself (* and ? too)',
  placeholder: 'search names, e.g. ckpt',
  forms: [
    { form: 'text', meaning: 'every file or folder whose name contains it (the outermost ones)', example: 'ckpt' },
    { form: '^text', meaning: 'names starting with it', example: '^train' },
    { form: 'text$', meaning: 'names ending with it (3+ characters)', example: '.safetensors$' },
    { form: '^text$', meaning: 'names that are exactly it', example: '^config.json$' },
    { form: 'a b', meaning: 'both (AND), in any names along the path', example: 'llama sft' },
    { form: 'a|b', meaning: 'either (OR; looser than AND)', example: 'qwen|llama' },
    { form: '-x', meaning: 'exclude what contains x (the whole query’s)', example: 'ckpt -tmp' },
    { form: 'a/b', meaning: 'along the path: a name ending in a, then one starting with b', example: 'run-a/ckpt' },
    { form: '"…"', meaning: 'literal, spaces, -, |, ^ and $ included', example: '"final ckpt"' },
  ],
  notes: [
    'Every character is literal text: *, ?, ( and . are searched as themselves. Terms too common to combine exactly here are refused; open a folder, or make one more specific.',
    'Owner pools (owned, unowned, all but some people) apply; clear a person or storage-class scope to search.',
    ESCAPE_NOTE,
  ],
}
