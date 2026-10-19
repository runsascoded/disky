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
  /** The path the note belongs to (`bucket/a/b`, no scheme): it shows on the chart there and below. null: the whole
   *  store's (every chart). */
  path: string | null
  /** A link the note records (e.g. the Slack message it summarizes). */
  url: string | null
  /** A short tag the chart's options toggle (e.g. `cleanup ask`). */
  kind: string | null
}

/** `GET /api/annotations`: the store's notes, oldest `at` first; `applied: false` = the deployment's D1 lacks
 *  the table (migration `chart-annotations` not yet applied), so the list is empty and writes 503. */
export interface AnnotationsBody {
  annotations: ChartAnnotation[]
  applied: boolean
}

/** The longest note text accepted, in characters. */
export const MAX_TEXT = 2000
export const MAX_URL = 2000
export const MAX_KIND = 32
export const MAX_PATH = 1024

/** A write's fields: `at` and `text`, plus the optional `path`, `url` and `kind` (null clears one). */
export type NoteFields = { at?: string; text?: string; path?: string | null; url?: string | null; kind?: string | null }

/** `a//b/` → `a/b`: a path's segments, joined; null for the store's root (`''`, `/`). */
export function normPath(p: string): string | null {
  const segs = p.split('/').filter(Boolean)
  return segs.length ? segs.join('/') : null
}

/** A write's fields, validated: trimmed text, 1..`MAX_TEXT` chars; `at` a full scan id; `path` a bucket-relative
 *  path without `.`/`..` segments (empty = the whole store); `url` an http(s) URL; `kind` 1..`MAX_KIND` chars. A
 *  POST needs `at` and `text`; a PATCH (`partial`) changes only what it names, and must name something. */
export function parseNote(body: unknown, partial = false): NoteFields | { error: string } {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return { error: 'want a JSON object' }
  const { at, text, path, url, kind } = body as Record<string, unknown>
  const out: NoteFields = {}
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
  if (path !== undefined) {
    if (path !== null && typeof path !== 'string') return { error: 'path: want a string or null' }
    const p = path === null ? null : normPath(path)
    if (p !== null && p.split('/').some(s => s === '.' || s === '..')) return { error: 'path: no . or .. segments' }
    if (p !== null && p.length > MAX_PATH) return { error: `path: over ${MAX_PATH} characters` }
    out.path = p
  }
  if (url !== undefined) {
    if (url !== null && typeof url !== 'string') return { error: 'url: want a string or null' }
    const u = url === null ? '' : url.trim()
    if (u) {
      let parsed: URL
      try {
        parsed = new URL(u)
      } catch {
        return { error: 'url: not a URL' }
      }
      if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return { error: 'url: want http(s)' }
      if (u.length > MAX_URL) return { error: `url: over ${MAX_URL} characters` }
    }
    out.url = u || null
  }
  if (kind !== undefined) {
    if (kind !== null && typeof kind !== 'string') return { error: 'kind: want a string or null' }
    const k = kind === null ? '' : kind.trim()
    if (k.length > MAX_KIND) return { error: `kind: over ${MAX_KIND} characters` }
    out.kind = k || null
  }
  if (Object.keys(out).length === 0) return { error: 'nothing to change (at, text, path, url, kind)' }
  return out
}
