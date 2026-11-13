export { summarize, fmtDuration, fmtOffset, fmtClock, LANES, laneOf, timeline, ticks, uaLabel, sessionsQuery, pagerLabel, type Lane, type Line, type Tick } from '@open-athena/auth/react/session-log'
import { fmtDuration, fmtOffset, type Line } from '@open-athena/auth/react/session-log'
import type { SessionRow } from '@open-athena/auth/session-log'
import { type Registry, whoToHandle } from './identityRegistry'

/** What the timeline's first column shows: the offset into the session with the gap since the row above
 *  (`+2d 03h (+5m 10s)`), or how long ago (from `now`). */
export const AT_MODES = ['start', 'ago'] as const
export type AtMode = typeof AT_MODES[number]
export const AT_LABEL: Record<AtMode, string> = { start: 'at (Δ)', ago: 'ago' }

export function fmtAt(mode: AtMode, l: Pick<Line, 't' | 'off'>, prev: Pick<Line, 'off'> | null, now: number): string {
  if (mode === 'start') return prev ? `${fmtOffset(l.off)} (${fmtOffset(l.off - prev.off)})` : fmtOffset(l.off)
  return `${fmtDuration(Math.max(0, now - l.t))} ago`
}

/** Who a session was: a known user (rendered as a `UserChip`), else the raw email, else a guest link with no
 *  bound address (`via: 'grant'`, `email` null — which link isn't recorded), else an anonymous public viewer.
 *  `guest`: signed in through a share link (`via: 'grant'`), not their own sign-in. */
export type Viewer =
  | { kind: 'user'; id: string; email: string; guest: boolean }
  | { kind: 'email'; email: string; guest: boolean }
  | { kind: 'guest' }
  | { kind: 'anonymous' }

/** A session row's viewer. The email → user rule is `ownerIdFor`'s (`functions/_lib/me.ts`): its `user_emails` row
 *  (`emails`, lowercased keys), else a registry entry for the email's handle. Unlike there, an unknown handle is not
 *  a user (it stays the email: no chip for a made-up id). */
export function viewerOf(row: Pick<SessionRow, 'email' | 'via'>, emails: Record<string, string> | undefined, reg: Registry): Viewer {
  const guest = row.via === 'grant'
  const email = row.email
  if (!email) return guest ? { kind: 'guest' } : { kind: 'anonymous' }
  const id = emails?.[email.toLowerCase()] ?? reg[whoToHandle(email)]?.u
  return id ? { kind: 'user', id, email, guest } : { kind: 'email', email, guest }
}
