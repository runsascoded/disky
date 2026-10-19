// Chart annotations on "Size over time" (spec `chart-annotations.md`): which notes the chart shows, where, and
// their lane labels — pure, so the placement and range filtering are tested apart from the chart.
import type { Marker } from '@disk-tree/react'
import type { ChartAnnotation } from '../functions/_lib/annotationShape'

export type { AnnotationsBody, ChartAnnotation } from '../functions/_lib/annotationShape'

/** A note as a chart marker. */
export interface NoteMarker extends Marker {
  note: ChartAnnotation
}

/** The lane label's longest text: a note's first line, cut with an ellipsis. */
export const LABEL_MAX = 28

export function shortLabel(text: string, max = LABEL_MAX): string {
  const line = text.split('\n')[0].trim()
  return line.length > max ? `${line.slice(0, max - 1).trimEnd()}…` : line
}

/**
 * The notes the chart draws: each at `xOf(at)` (the scan's own x when `at` is a scan the chart plots; a date
 * with no scan sits at its UTC midnight), kept only inside `[xMin, xMax]` — the plotted span, so the `1w` / `1m`
 * ranges hide older notes — and ordered by x (ties by id). The y-axis mode (`abs` / `Δ` / `%`) never moves
 * a note: it is pinned to a date, not a value.
 */
export function notesInView(notes: readonly ChartAnnotation[], xOf: (at: string) => number, xMin: number, xMax: number): NoteMarker[] {
  return notes
    .map(note => ({ key: `note-${note.id}`, x: xOf(note.at), label: shortLabel(note.text), note }))
    .filter(m => Number.isFinite(m.x) && m.x >= xMin && m.x <= xMax)
    .sort((a, b) => a.x - b.x || a.note.id - b.note.id)
}

/** The notes for the chart at `prefix` (`bucket/a/b`, `''` the store's root): the store-wide ones (no `path`) and
 *  those whose `path` is `prefix` or above it (a note on `marin-us-east-02a` shows there and under it, not at the
 *  root), less any of a `hidden` kind. */
export function notesHere(notes: readonly ChartAnnotation[], prefix: string, hidden: ReadonlySet<string>): ChartAnnotation[] {
  const p = prefix.replace(/^\/+|\/+$/g, '')
  return notes.filter(n => (n.path === null || p === n.path || p.startsWith(`${n.path}/`)) && !(n.kind !== null && hidden.has(n.kind)))
}

/** The kinds among the notes the chart at `prefix` could show, sorted: one toggle each. */
export const noteKinds = (notes: readonly ChartAnnotation[], prefix: string): string[] =>
  [...new Set(notesHere(notes, prefix, new Set()).flatMap(n => (n.kind === null ? [] : [n.kind])))].sort()

/** The date a new note's form starts at: the diff window's end when one is set, else the latest plotted scan. */
export const defaultNoteAt = (window: [string, string] | undefined, latest: string | undefined): string =>
  window?.[1] ?? latest ?? ''

/** The author as shown (an email's local part: a public deployment never shows an address). */
export const authorName = (author: string): string => author.split('@')[0]
