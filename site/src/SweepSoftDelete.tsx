import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { DEFAULT_STORE } from './stores'
import { type BucketSoftDelete, confirmUndoTerms, DEFAULT_MIN_SOFT_DELETE_DAYS, defaultMinSoftDeleteDays, MAX_MIN_SOFT_DELETE_DAYS, minimumWarning, SWEEP_SETTINGS_KEY as KEY } from './softDelete'

// `/admin`'s sweep soft-delete minimum (`functions/_lib/sweepSettings.ts`): the narrowest retention a real run accepts, read at
// dispatch; what each bucket has is the job's newest reading (`/api/sweep/soft-delete`, the scan step `soft-delete`), as old
// as the last scan that ran the step, since the site's own account can't read bucket metadata. A bucket with no reading is
// unknown, and is shown so.

const ENDPOINT = '/api/sweep/settings'

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

/** The buckets' soft delete as the job last read it, for any viewer (null: no reading, or an executor that doesn't restore from
 * soft delete). */
export function useBucketSoftDelete(enabled = true) {
  return useQuery<BucketSoftDelete | null>({
    queryKey: ['sweep', 'soft-delete'],
    enabled,
    queryFn: async () => {
      const r = await fetch('/api/sweep/soft-delete', { credentials: 'same-origin' })
      if (!r.ok) throw new Error(`soft delete: HTTP ${r.status}`)
      return ((await r.json()) as { soft_delete: BucketSoftDelete | null }).soft_delete
    },
  })
}

/** `useBucketSoftDelete` as the run pages take it: undefined while it isn't known whether there is a reading (not `enabled`: an
 * executor whose undo isn't a soft-delete restore; or still loading), so nothing is qualified yet; null: none (or the read
 * failed), so a recorded undo is one this site can't vouch for. */
export function useSoftDeleteNow(enabled: boolean): BucketSoftDelete | null | undefined {
  const q = useBucketSoftDelete(enabled)
  return !enabled ? undefined : q.isError ? null : q.data
}

/** What a bucket's row says: `on`, `off`, or `unknown` (no reading names it). */
export const bucketState = (bucket: string, known: BucketSoftDelete | null | undefined): 'on' | 'off' | 'unknown' =>
  known && Object.hasOwn(known.off, bucket) ? known.off[bucket] ? 'off' : 'on' : 'unknown'

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
  /** The buckets a sweep acts on (the store's): each gets a row, `unknown` where no reading names it. */
  swept: readonly string[]
  /** The executor's minimum with no row. */
  defaultMin?: number
  busy: boolean
  error: string | null
}

/** The editor, as markup (pure: `SweepSoftDelete` wires it to the API). */
export function SweepSoftDeleteView({ answer, draft, onDraft, understood, onUnderstood, onSave, buckets, swept, defaultMin = DEFAULT_MIN_SOFT_DELETE_DAYS, busy, error }: SweepSoftDeleteViewProps) {
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
        (with what its buckets actually had) on its page; changing it here doesn't touch a run in progress. On an S3-style store,
        soft delete means versioning with a noncurrent-version rule.
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
        <span className="dim">default {defaultMin}; 0..{MAX_MIN_SOFT_DELETE_DAYS}</span>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
      {swept.length > 0 && (
        <>
          <p className="dim">{buckets
            ? `What each bucket a sweep acts on has, as the job read it at ${buckets.asOf} (its soft-delete step, after a scan); a run measures its buckets itself when it starts:`
            : 'No reading of the buckets\' soft delete (the job\'s soft-delete step writes one after a scan), so this site can\'t say what a past run\'s undo has to work with; a run measures its buckets itself when it starts:'}</p>
          <table className="sweep-soft-delete-buckets">
            <thead><tr><th>bucket</th><th>soft delete</th></tr></thead>
            <tbody>
              {[...swept].sort((a, b) => a.localeCompare(b)).map(bucket => {
                const state = bucketState(bucket, buckets)
                return <tr key={bucket}><td><code>{bucket}</code></td><td className={state === 'off' ? 'warn' : state === 'unknown' ? 'dim' : undefined}>{state}</td></tr>
              })}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}

/** The real-delete confirm's undo sentence, for a run of `buckets` (`/staged`). Anything but a promise reads as the danger it is.
 * `min` is the caller's `useSweepSettings`: the confirm sends the minimum this states, so the two can't differ. */
export function UndoTerms({ buckets, min }: { buckets: readonly string[]; min: ReturnType<typeof useSweepSettings> }) {
  const known = useBucketSoftDelete()
  const text = min.data ? confirmUndoTerms(min.data.min_soft_delete_days, buckets, known.data ?? null)
    : min.isError ? 'Could not read this deployment\'s soft-delete minimum: treat these deletes as permanent.'
    : 'Checking what undo this run would have…'
  return <p className={/^(Recoverable|Checking)/.test(text) ? 'dispatch-path' : 'dispatch-path staged-err'} role="note">{text}</p>
}

/** `/admin`'s section. */
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
      swept={DEFAULT_STORE.buckets}
      defaultMin={defaultMinSoftDeleteDays(DEFAULT_STORE.executor)}
      busy={save.isPending}
      error={save.error?.message ?? null}
    />
  )
}
