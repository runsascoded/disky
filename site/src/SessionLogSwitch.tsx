import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { lastDay, type SwitchState } from '../functions/_lib/sessionLogShape'

// `/admin`'s session-log switch (specs/session-log.md): on through a UTC date (default 7 days, at most 31), on with no
// end date (until an admin turns it off), or off. A dated run ends by itself after its date; every change is recorded
// in `admin_edits`.

const ENDPOINT = '/api/session-log/switch'
const KEY = ['session-log', 'switch'] as const

const day = (epochS: number): string => new Date(epochS * 1000).toISOString().slice(0, 10)

/** The switch's one-line status. */
export function switchStatus(s: SwitchState): string {
  const by = s.who && s.ts !== null ? ` Set by ${s.who} on ${day(s.ts)}.` : ''
  if (s.reason === 'not set up') return 'Not set up on this deployment (no session_log tables): apply the session-log migration.'
  if (s.enabled && s.indefinite) return `On (no end date).${by}`
  if (s.enabled && s.until !== null) return `On through ${lastDay(s.until)} (UTC).${by}`
  if (s.reason === 'expired' && s.until !== null) return `Off: ended after ${lastDay(s.until)} (UTC).${by}`
  return `Off.${by}`
}

/** The end an admin has picked: through `date`, or no end date. */
export type End = 'dated' | 'indefinite'

export interface SwitchViewProps {
  state: SwitchState
  /** Today's UTC date: the earliest a new end may be. */
  today: string
  /** The date field's value (`YYYY-MM-DD`). */
  date: string
  onDate: (d: string) => void
  /** The end choice (radio): through `date`, or none. */
  end: End
  onEnd: (e: End) => void
  /** Turn on with the chosen end (also: change the end while on), or off. */
  onSet: (enabled: boolean) => void
  busy: boolean
  error: string | null
}

/** The switch, as markup (pure: `SessionLogSwitch` wires it to the API). */
export function SessionLogSwitchView({ state, today, date, onDate, end, onEnd, onSet, busy, error }: SwitchViewProps) {
  const setUp = state.reason !== 'not set up'
  const changed = state.enabled && (end === 'indefinite'
    ? !state.indefinite
    : state.indefinite || (state.until !== null && date !== lastDay(state.until)))
  return (
    <section className="session-log-switch">
      <h2>Session log</h2>
      <p>
        Records what viewers do (page views, clicks on controls, the path filter, requests and errors), to replay a
        reported bug (<Link to="/admin/sessions">/admin/sessions</Link>) and for the activity feed
        (<Link to="/admin/activity">/admin/activity</Link>). While it's on, <Link to="/privacy">/privacy</Link> says
        what is recorded. It turns itself off after the chosen day, or runs until switched off with no end date.
      </p>
      <p className="status">{switchStatus(state)}</p>
      {setUp && (
        <form onSubmit={e => { e.preventDefault(); onSet(true) }}>
          <label className="switch">
            <input type="checkbox" role="switch" checked={state.enabled} disabled={busy} onChange={e => onSet(e.target.checked)} />
            {' '}Logging
          </label>
          <span className="ends" role="radiogroup" aria-label="Ends">
            <label>
              <input type="radio" name="session-log-end" checked={end === 'dated'} disabled={busy} onChange={() => onEnd('dated')} />
              {' '}through{' '}
              <input type="date" value={date} min={today} max={lastDay(state.maxUntil)} disabled={busy} onChange={e => { onDate(e.target.value); onEnd('dated') }} />
              {' '}(UTC)
            </label>
            <label>
              <input type="radio" name="session-log-end" checked={end === 'indefinite'} disabled={busy} onChange={() => onEnd('indefinite')} />
              {' '}no end date
            </label>
          </span>
          {changed && <button type="submit" disabled={busy}>Set end</button>}
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
  const [endDraft, setEndDraft] = useState<End | null>(null)
  const set = useMutation<SwitchState, Error, { enabled: boolean; until: string; end: End }>({
    mutationFn: ({ enabled, until, end }) => call({
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(!enabled ? { enabled } : end === 'indefinite' ? { enabled, indefinite: true } : { enabled, until }),
    }),
    onSuccess: s => { qc.setQueryData(KEY, s); setDraft(null); setEndDraft(null) },
  })
  if (q.error) return <section className="session-log-switch"><h2>Session log</h2><p className="err">{q.error.message}</p></section>
  if (!q.data) return null
  const s = q.data
  const date = draft ?? lastDay(s.enabled && !s.indefinite && s.until !== null ? s.until : s.defaultUntil)
  const end = endDraft ?? (s.indefinite ? 'indefinite' : 'dated')
  return (
    <SessionLogSwitchView
      state={s}
      today={new Date().toISOString().slice(0, 10)}
      date={date}
      onDate={setDraft}
      end={end}
      onEnd={setEndDraft}
      onSet={enabled => set.mutate({ enabled, until: date, end })}
      busy={set.isPending}
      error={set.error?.message ?? null}
    />
  )
}
