import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import type { CostSnapshot } from './costModel'
import { type BucketSoftDelete, confirmUndoTerms, DEFAULT_MIN_SOFT_DELETE_DAYS, MAX_MIN_SOFT_DELETE_DAYS, minimumWarning } from './softDelete'

// `/admin`'s sweep soft-delete minimum (`functions/_lib/sweepSettings.ts`): the narrowest retention a real run accepts, read at
// dispatch; what each bucket has comes from the newest cost snapshot, read-only and a day old at most, since the site's own
// account can't read bucket metadata.

const ENDPOINT = '/api/sweep/settings'
const KEY = ['sweep', 'settings'] as const

export interface SweepSettingsAnswer {
  settings: { min_soft_delete_days: number; updated_by: string; updated_at: number } | null
  min_soft_delete_days: number
}

export function useSweepSettings(enabled = true) {
  return useQuery<SweepSettingsAnswer>({
    queryKey: KEY,
    enabled,
    staleTime: 0,
    queryFn: async () => {
      const r = await fetch(ENDPOINT, { credentials: 'same-origin' })
      if (!r.ok) throw new Error(`sweep settings: HTTP ${r.status}`)
      return r.json() as Promise<SweepSettingsAnswer>
    },
  })
}

/** The newest cost snapshot's buckets' soft delete (null: this deployment writes none, or it has no storage section). */
export const bucketSoftDeleteOf = (days: readonly CostSnapshot[]): BucketSoftDelete | null => {
  const newest = days[days.length - 1]
  if (!newest?.storage) return null
  return { asOf: newest.date, off: Object.fromEntries(newest.storage.buckets.map(b => [b.bucket, b.soft_delete_off])) }
}

export function useBucketSoftDelete(enabled = true) {
  return useQuery<BucketSoftDelete | null>({
    queryKey: ['cost', 'gcp', 'soft-delete'],
    enabled,
    queryFn: async () => {
      const r = await fetch('/api/cost/gcp?days=1', { credentials: 'same-origin' })
      if (!r.ok) throw new Error(`cost: HTTP ${r.status}`)
      return bucketSoftDeleteOf(((await r.json()) as { days: CostSnapshot[] }).days)
    },
  })
}

const day = (epochS: number): string => new Date(epochS * 1000).toISOString().slice(0, 10)

/** The current value's one-line status. */
export function minimumStatus(a: SweepSettingsAnswer): string {
  const n = a.min_soft_delete_days
  const value = `${n} ${n === 1 ? 'day' : 'days'}`
  return a.settings
    ? `Minimum: ${value}. Set by ${a.settings.updated_by} on ${day(a.settings.updated_at)}.`
    : `Minimum: ${value} (the default; never set here).`
}

export interface SweepSoftDeleteViewProps {
  answer: SweepSettingsAnswer
  /** The input's value, as typed. */
  draft: string
  onDraft: (v: string) => void
  /** Ticked: the admin has read the warning (asked for only below 7). */
  understood: boolean
  onUnderstood: (b: boolean) => void
  onSave: () => void
  buckets: BucketSoftDelete | null | undefined
  busy: boolean
  error: string | null
}

/** The editor, as markup (pure: `SweepSoftDelete` wires it to the API). */
export function SweepSoftDeleteView({ answer, draft, onDraft, understood, onUnderstood, onSave, buckets, busy, error }: SweepSoftDeleteViewProps) {
  const n = Number(draft)
  const valid = draft.trim() !== '' && Number.isInteger(n) && n >= 0 && n <= MAX_MIN_SOFT_DELETE_DAYS
  const warning = valid ? minimumWarning(n) : null
  const changed = valid && n !== answer.min_soft_delete_days
  return (
    <section className="sweep-soft-delete">
      <h2>Sweep soft-delete minimum</h2>
      <p>
        A real sweep refuses any bucket retaining soft deletes for less than this many days. Undo restores from soft delete, so
        this is the shortest undo a run can have. A run reads it when dispatched, keeps that value to its end, and records it
        (with what its buckets actually had) on its page; changing it here doesn't touch a run in progress.
      </p>
      <p className="status">{minimumStatus(answer)}</p>
      {warning && <p className="warn" role="alert">{warning}</p>}
      <form onSubmit={e => { e.preventDefault(); if (changed && (!warning || understood)) onSave() }}>
        <label>
          Days{' '}
          <input type="number" min={0} max={MAX_MIN_SOFT_DELETE_DAYS} step={1} value={draft} disabled={busy} onChange={e => onDraft(e.target.value)} />
        </label>
        {warning && changed && (
          <label>
            <input type="checkbox" checked={understood} disabled={busy} onChange={e => onUnderstood(e.target.checked)} />
            {' '}I understand
          </label>
        )}
        <button type="submit" disabled={busy || !changed || (!!warning && !understood)}>Save</button>
        <span className="dim">default {DEFAULT_MIN_SOFT_DELETE_DAYS}; 0..{MAX_MIN_SOFT_DELETE_DAYS}</span>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
      {buckets && (
        <>
          <p className="dim">What each bucket has, as of {buckets.asOf} (<Link to="/admin/cost/gcp">cost snapshot</Link>; a run measures its buckets itself when it starts):</p>
          <table className="sweep-soft-delete-buckets">
            <thead><tr><th>bucket</th><th>soft delete</th></tr></thead>
            <tbody>
              {Object.entries(buckets.off).sort(([a], [b]) => a.localeCompare(b)).map(([bucket, off]) => (
                <tr key={bucket}><td><code>{bucket}</code></td><td className={off ? 'warn' : undefined}>{off ? 'off' : 'on'}</td></tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}

/** The real-delete confirm's undo sentence, for a run of `buckets` (`/staged`). Anything but a promise reads as the danger it is. */
export function UndoTerms({ buckets }: { buckets: readonly string[] }) {
  const min = useSweepSettings()
  const known = useBucketSoftDelete()
  const text = min.data ? confirmUndoTerms(min.data.min_soft_delete_days, buckets, known.data ?? null)
    : min.isError ? 'Could not read this deployment\'s soft-delete minimum: treat these deletes as permanent.'
    : 'Checking what undo this run would have…'
  return <p className={/^(Recoverable|Checking)/.test(text) ? 'dispatch-path' : 'dispatch-path staged-err'} role="note">{text}</p>
}

/** `/admin`'s section: only on the `sweep` executor, the one with a soft-delete guard. */
export function SweepSoftDelete() {
  const qc = useQueryClient()
  const q = useSweepSettings()
  const buckets = useBucketSoftDelete()
  const [edit, setEdit] = useState<string | null>(null)
  const [understood, setUnderstood] = useState(false)
  const save = useMutation({
    mutationFn: async (min_soft_delete_days: number) => {
      const r = await fetch(ENDPOINT, {
        method: 'PUT', credentials: 'same-origin', headers: { 'content-type': 'application/json' }, body: JSON.stringify({ min_soft_delete_days }),
      })
      const body = await r.json() as SweepSettingsAnswer & { error?: string }
      if (!r.ok) throw new Error(body.error ?? `HTTP ${r.status}`)
      return body
    },
    onSuccess: body => {
      qc.setQueryData(KEY, body)
      setEdit(null)
      setUnderstood(false)
    },
  })
  if (!q.data) return q.error ? <section className="sweep-soft-delete"><h2>Sweep soft-delete minimum</h2><p className="error" role="alert">{q.error.message}</p></section> : null
  const draft = edit ?? String(q.data.min_soft_delete_days)
  return (
    <SweepSoftDeleteView
      answer={q.data}
      draft={draft}
      onDraft={v => { setEdit(v); setUnderstood(false) }}
      understood={understood}
      onUnderstood={setUnderstood}
      onSave={() => save.mutate(Number(draft))}
      buckets={buckets.data}
      busy={save.isPending}
      error={save.error?.message ?? null}
    />
  )
}
