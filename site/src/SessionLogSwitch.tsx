import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { lastDay, type SwitchState } from '../functions/_lib/sessionLogShape'

// `/admin`'s session-log switch (specs/session-log.md): on through a UTC date (default 7 days, at most 31), or off.
// It ends by itself after that date; every change is recorded in `admin_edits`.

const ENDPOINT = '/api/session-log/switch'
const KEY = ['session-log', 'switch'] as const

const day = (epochS: number): string => new Date(epochS * 1000).toISOString().slice(0, 10)

/** The switch's one-line status. */
export function switchStatus(s: SwitchState): string {
  const by = s.who && s.ts !== null ? ` Set by ${s.who} on ${day(s.ts)}.` : ''
  if (s.reason === 'not set up') return 'Not set up on this deployment (no session_log tables): apply the session-log migration.'
  if (s.enabled && s.until !== null) return `On through ${lastDay(s.until)} (UTC).${by}`
  if (s.reason === 'expired' && s.until !== null) return `Off: ended after ${lastDay(s.until)} (UTC).${by}`
  return `Off.${by}`
}

export interface SwitchViewProps {
  state: SwitchState
  /** Today's UTC date: the earliest a new end may be. */
  today: string
  /** The date field's value (`YYYY-MM-DD`). */
  date: string
  onDate: (d: string) => void
  /** Turn on through `date` (also: change the date while on), or off. */
  onSet: (enabled: boolean) => void
  busy: boolean
  error: string | null
}

/** The switch, as markup (pure: `SessionLogSwitch` wires it to the API). */
export function SessionLogSwitchView({ state, today, date, onDate, onSet, busy, error }: SwitchViewProps) {
  const setUp = state.reason !== 'not set up'
  const changed = state.enabled && state.until !== null && date !== lastDay(state.until)
  return (
    <section className="session-log-switch">
      <h2>Session log</h2>
      <p>
        Records what viewers do (page views, clicks on controls, the path filter, requests and errors) so a reported bug
        can be replayed: <Link to="/admin/sessions">/admin/sessions</Link>. Viewers see no notice. It turns itself off
        after the chosen day.
      </p>
      <p className="status">{switchStatus(state)}</p>
      {setUp && (
        <form onSubmit={e => { e.preventDefault(); onSet(true) }}>
          <label className="switch">
            <input type="checkbox" role="switch" checked={state.enabled} disabled={busy} onChange={e => onSet(e.target.checked)} />
            {' '}Logging
          </label>
          <label>
            {' '}through{' '}
            <input type="date" value={date} min={today} max={lastDay(state.maxUntil)} disabled={busy} onChange={e => onDate(e.target.value)} />
            {' '}(UTC)
          </label>
          {changed && <button type="submit" disabled={busy}>Set date</button>}
        </form>
      )}
      {error && <p className="err">{error}</p>}
    </section>
  )
}

async function call(init?: RequestInit): Promise<SwitchState> {
  const r = await fetch(ENDPOINT, { credentials: 'include', cache: 'no-store', ...init })
  const body = await r.json().catch(() => null) as (SwitchState & { error?: string }) | null
  if (!r.ok) throw new Error(body?.error ?? `session-log switch: ${r.status}`)
  return body as SwitchState
}

export function SessionLogSwitch() {
  const qc = useQueryClient()
  const q = useQuery<SwitchState, Error>({ queryKey: KEY, queryFn: () => call() })
  const [draft, setDraft] = useState<string | null>(null)
  const set = useMutation<SwitchState, Error, { enabled: boolean; until: string }>({
    mutationFn: ({ enabled, until }) => call({
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(enabled ? { enabled, until } : { enabled }),
    }),
    onSuccess: s => { qc.setQueryData(KEY, s); setDraft(null) },
  })
  if (q.error) return <section className="session-log-switch"><h2>Session log</h2><p className="err">{q.error.message}</p></section>
  if (!q.data) return null
  const s = q.data
  const date = draft ?? lastDay(s.enabled && s.until !== null ? s.until : s.defaultUntil)
  return (
    <SessionLogSwitchView
      state={s}
      today={new Date().toISOString().slice(0, 10)}
      date={date}
      onDate={setDraft}
      onSet={enabled => set.mutate({ enabled, until: date })}
      busy={set.isPending}
      error={set.error?.message ?? null}
    />
  )
}
