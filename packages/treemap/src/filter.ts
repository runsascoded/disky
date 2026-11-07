/**
 * Path matching for the filter plane (spec: viz-widgets.md §5, v0).
 *
 * v0 semantics are deliberately narrow and must be labeled as such in the UI:
 * this filters *what is displayed at the current level*. It does not
 * re-aggregate — a directory's size still counts children that the filter
 * hides. (True re-aggregation is v1, and needs either a server round-trip or
 * the segment index.) Saying "highlight + re-layout" is honest; saying
 * "filtered totals" would not be.
 */

import type { CellStyle } from './cellTypes'

export interface QueryOpts {
  /** Match case-sensitively. Default: false. */
  caseSensitive?: boolean
}

/**
 * Compile a query string into a path predicate.
 *
 * `/…/` (optionally with trailing flags) is a regex; anything else is a plain
 * substring. **An in-progress or invalid regex never throws** — it degrades to
 * a literal substring match, because this runs on every keystroke and a
 * half-typed `(` must not blank the view. An empty query matches everything.
 */
export function parseQuery(query: string, opts: QueryOpts = {}): (path: string) => boolean {
  const q = query.trim()
  if (!q) return () => true
  const flags = opts.caseSensitive ? '' : 'i'
  const re = /^\/(.*)\/([gimsuy]*)$/.exec(q)
  if (re) {
    try {
      const compiled = new RegExp(re[1], [...new Set(re[2] + flags)].join(''))
      return (path: string) => compiled.test(path)
    } catch {
      // fall through to substring
    }
  }
  if (opts.caseSensitive) return (path: string) => path.includes(q)
  const lower = q.toLowerCase()
  return (path: string) => path.toLowerCase().includes(lower)
}

/** Keep the nodes whose extracted path matches `query`. */
export function filterNodes<T>(
  nodes: T[],
  getPath: (n: T) => string,
  query: string,
  opts?: QueryOpts,
): T[] {
  const match = parseQuery(query, opts)
  return nodes.filter(n => match(getPath(n)))
}

export interface DimOpts {
  /** Opacity applied to non-matching cells. Default 0.15. */
  opacity?: number
}

/**
 * Dim a cell that doesn't match — pass through the treemap's `lens` slot so it
 * *stacks* on the palette (and on the age lens) instead of replacing colors.
 * Returns `null` for matching cells, i.e. "leave this one alone".
 */
export function dimUnmatched(style: CellStyle, matched: boolean, opts?: DimOpts): CellStyle | null {
  if (matched) return null
  return { ...style, opacity: opts?.opacity ?? 0.15 }
}
