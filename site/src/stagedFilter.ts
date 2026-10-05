// `/staged`'s text filter: the map filter's own syntax (`a|b` OR, `a b` AND,
// `-x` NOT, quotes, `*` within a segment; `parseQuery`), tested against one
// haystack per row that holds its prefix, its owners and who staged it, each
// on its own line so a term never matches across two fields. So `hedy|grace`
// keeps rows owned by Hedy or Grace, `isoflop -nemotron` narrows by path,
// and `owner:will` keeps the rows Will owns.
import { parseQuery } from './filterTree'
import { QueryError } from '../functions/_lib/queryAst'

/** What a row is searchable by. Owners and the stager as canonical ids or
 *  emails; `name` turns either into the display name, which is searched too. */
export interface StagedFields {
  prefix: string
  owners: string[]
  stagedBy: string
}

/** One row's search text: the prefix, then `owner:<id> <name>` per owner, then
 *  `staged-by:<who> <name>` — so `owner:hedy` / `staged-by:barbara` aim a term
 *  at one field. Matching is by substring, as on the map: `will` also matches
 *  `ken.alanson`; `owner:alan` or `alan-turing` doesn't. */
export function stagedHaystack(f: StagedFields, name: (who: string) => string): string {
  return [
    f.prefix,
    ...f.owners.map(o => `owner:${o} ${name(o)}`),
    `staged-by:${f.stagedBy} ${name(f.stagedBy)}`,
  ].join('\n')
}

/** The rows `q` keeps (all of them for an empty query), or the parse error. */
export function filterStaged<T>(
  rows: T[],
  q: string | null | undefined,
  fields: (r: T) => StagedFields,
  name: (who: string) => string,
): { rows: T[]; error?: string } {
  let pred
  try {
    pred = parseQuery(q)
  } catch (e) {
    if (e instanceof QueryError) return { rows, error: e.message }
    throw e
  }
  if (!pred) return { rows }
  return { rows: rows.filter(r => pred(stagedHaystack(fields(r), name))) }
}

/** The sort in the URL: `-b` (descending, the default) or `b` (ascending);
 *  any `PrefixTable` key, `staged` included. */
export function parseSort(s: string | null | undefined): { k: string; asc: boolean } {
  if (!s) return { k: 'b', asc: false }
  return s.startsWith('-') ? { k: s.slice(1), asc: false } : { k: s, asc: true }
}

export const encodeSort = ({ k, asc }: { k: string; asc: boolean }): string | undefined =>
  k === 'b' && !asc ? undefined : `${asc ? '' : '-'}${k}`
