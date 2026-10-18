// Chart annotations on "Size over time" (spec `chart-annotations.md`): the store's notes (`/api/annotations`),
// each marker's tooltip, and the admin's add / edit / delete form. Non-admins read only.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { useStore, useStoreFetch } from './store'
import { isScanId } from './scanSlug'
import { authorName, type AnnotationsBody, type ChartAnnotation } from './chartNotes'
import type { ScanLabel } from './scan'

const MAX_TEXT = 2000

/** The current store's notes; `applied: false` = its D1 lacks the table (writes are off). */
export function useChartNotes(): { notes: ChartAnnotation[]; applied: boolean } {
  const store = useStore()
  const sfetch = useStoreFetch()
  const q = useQuery<AnnotationsBody>({
    queryKey: ['annotations', store.key],
    staleTime: 5 * 60_000,
    // A note is decoration: a failed read leaves the chart as it was.
    retry: 1,
    queryFn: async () => {
      const r = await sfetch('/api/annotations', { credentials: 'include' })
      if (!r.ok) throw new Error(`annotations: ${r.status}`)
      return await r.json() as AnnotationsBody
    },
  })
  return { notes: q.data?.annotations ?? [], applied: q.data?.applied ?? false }
}

type Write =
  | { method: 'POST'; body: { at: string; text: string } }
  | { method: 'PATCH'; body: { id: number; at: string; text: string } }
  | { method: 'DELETE'; id: number }

/** Add / edit / delete a note in the current store; the list refetches on success. */
export function useNoteWrite() {
  const store = useStore()
  const sfetch = useStoreFetch()
  const qc = useQueryClient()
  return useMutation({
    mutationFn: async (w: Write) => {
      const r = w.method === 'DELETE'
        ? await sfetch(`/api/annotations?id=${w.id}`, { method: 'DELETE', credentials: 'include' })
        : await sfetch('/api/annotations', { method: w.method, credentials: 'include', headers: { 'content-type': 'application/json' }, body: JSON.stringify(w.body) })
      if (!r.ok) throw new Error(((await r.json().catch(() => null)) as { error?: string } | null)?.error ?? `HTTP ${r.status}`)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['annotations', store.key] }),
  })
}

/** A marker's tooltip: the whole note, who and when; edit / delete for an admin. */
export function NoteTip({ note, fmt, onEdit, onDelete }: { note: ChartAnnotation; fmt: ScanLabel; onEdit?: () => void; onDelete?: () => void }) {
  // Delete takes a second click (the button turns into its confirmation).
  const [armed, setArmed] = useState(false)
  return (
    <div className="note-tip">
      <div className="note-text">{note.text}</div>
      <div className="note-meta">
        {fmt(note.at)} · {authorName(note.author)}{note.updated !== note.created && ' (edited)'}
      </div>
      {(onEdit || onDelete) && (
        <div className="note-actions">
          {onEdit && <button type="button" onClick={onEdit}>edit</button>}
          {onDelete && <button type="button" className="danger" onClick={() => (armed ? onDelete() : setArmed(true))}>{armed ? 'really delete?' : 'delete'}</button>}
        </div>
      )}
    </div>
  )
}

/** The admin's compact form: a new note at `at` (prefilled), or `note` to edit. */
export function NoteForm({ at, note, onClose }: { at: string; note?: ChartAnnotation; onClose: () => void }) {
  const [when, setWhen] = useState(note?.at ?? at)
  const [text, setText] = useState(note?.text ?? '')
  const write = useNoteWrite()
  const okAt = isScanId(when)
  const okText = text.trim().length > 0 && text.trim().length <= MAX_TEXT
  const save = () => write.mutate(
    note ? { method: 'PATCH', body: { id: note.id, at: when, text } } : { method: 'POST', body: { at: when, text } },
    { onSuccess: onClose },
  )
  return (
    <form className="note-form" onSubmit={e => { e.preventDefault(); if (okAt && okText) save() }}>
      <label>
        date
        <input
          className={okAt ? '' : 'bad'}
          value={when}
          onChange={e => setWhen(e.target.value.trim())}
          placeholder="YYYY-MM-DD"
          size={16}
          aria-label="note date (YYYY-MM-DD or a scan id)"
        />
      </label>
      <textarea
        value={text}
        onChange={e => setText(e.target.value)}
        placeholder="What happened here (e.g. a scope change, not a deletion)"
        rows={2}
        maxLength={MAX_TEXT}
        aria-label="note text"
        autoFocus
        onKeyDown={e => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey) && okAt && okText) save() }}
      />
      <div className="note-form-actions">
        <button type="submit" disabled={!okAt || !okText || write.isPending}>{note ? 'save' : 'add note'}</button>
        <button type="button" onClick={onClose}>cancel</button>
        {write.isError && <span className="err">{(write.error as Error).message}</span>}
      </div>
    </form>
  )
}
