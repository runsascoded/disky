/** Chart annotations' wire shape and validation (spec `chart-annotations.md`), shared by `/api/annotations`
 *  and the site (`src/chartNotes.ts`). DOM- and Workers-free. */
import { isScanId } from '../../src/scanSlug.js'

/** One hand-written note on the "Size over time" chart, as `/api/annotations` returns it. */
export interface ChartAnnotation {
  id: number
  /** A scan id or a date: `YYYY-MM-DD` or `YYYY-MM-DDTHHMM` (UTC). */
  at: string
  text: string
  /** Who wrote (or last edited) it: the admin's email. */
  author: string
  /** Unix seconds. */
  created: number
  updated: number
}

/** `GET /api/annotations`: the store's notes, oldest `at` first; `applied: false` = the deployment's D1 lacks
 *  the table (migration `chart-annotations` not yet applied), so the list is empty and writes 503. */
export interface AnnotationsBody {
  annotations: ChartAnnotation[]
  applied: boolean
}

/** The longest note text accepted, in characters. */
export const MAX_TEXT = 2000

/** A write's `{at, text}`, validated: trimmed text, 1..`MAX_TEXT` chars; `at` a full scan id. On `partial` (a
 *  PATCH) either may be absent, not both. */
export function parseNote(body: unknown, partial = false): { at?: string; text?: string } | { error: string } {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return { error: 'want a JSON object' }
  const { at, text } = body as { at?: unknown; text?: unknown }
  const out: { at?: string; text?: string } = {}
  if (at !== undefined || !partial) {
    if (!isScanId(at)) return { error: 'at: want YYYY-MM-DD or YYYY-MM-DDTHHMM' }
    out.at = at
  }
  if (text !== undefined || !partial) {
    if (typeof text !== 'string') return { error: 'text: want a string' }
    const t = text.trim()
    if (!t) return { error: 'text: empty' }
    if (t.length > MAX_TEXT) return { error: `text: over ${MAX_TEXT} characters` }
    out.text = t
  }
  if (out.at === undefined && out.text === undefined) return { error: 'nothing to change (at, text)' }
  return out
}
