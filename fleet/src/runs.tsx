/** What the page says about a scan run, wherever it says it (the strip, the charts' readouts, the run tables): its mark,
 *  its outcome in words, its trigger, its link, and the tooltip that sums it up. The outcome rule is the Slack post's
 *  (`functions/_lib/runOutcome.ts`). */
import { fmtSI } from '@disk-tree/pages-kit/og/text'
import { fmtHours, fmtUsd } from '../functions/_lib/card'
import type { FleetDeployment, FleetRun } from '../functions/_lib/fleetTypes'
import { type Outcome, runOutcome } from '../functions/_lib/runOutcome'

export const DAY = 86400

/** Each outcome's mark: the Slack post's emoji, as text. */
export const MARK: Record<Outcome, string> = { running: '⏳', cancelled: '🚫', failed: '❌', issues: '⚠️', ok: '✅' }

/** What started a run, as the deployment's scan-health thread shows it. */
export const TRIGGER: Record<'schedule' | 'manual', { emoji: string; label: string }> = {
  schedule: { emoji: '⏰', label: 'scheduled' },
  manual: { emoji: '👤', label: 'started by hand' },
}

/** A run's outcome in a few words. */
export function runWords(r: FleetRun): string {
  switch (runOutcome(r)) {
    case 'running': return 'running'
    case 'cancelled': return 'cancelled'
    case 'failed': return r.failed_at ? `failed at ${r.failed_at}` : 'failed'
    case 'issues': return `open: ${r.open_issues.join(', ')}`
    case 'ok': return 'ok'
  }
}

export const runHref = (d: FleetDeployment, r: FleetRun): string => `${d.url}/scans/${encodeURIComponent(r.run_id)}`

/** `11-07 14:00` (UTC). */
export const when = (t: number | null): string => (t === null ? '—' : new Date(t * 1000).toISOString().slice(5, 16).replace('T', ' '))
/** Seconds as a short age: `40m`, `9h`, `3d`. */
export const age = (s: number | null): string => (s === null ? '—' : s < 3600 ? `${Math.round(s / 60)}m` : s < 172800 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`)
export const bytes = (b: number | null | undefined): string => (b == null ? '—' : fmtSI(b))
export const usd = (c: number | null | undefined): string => (c == null ? '—' : fmtUsd(c))
export const hours = (s: number | null | undefined): string => (s == null ? '—' : fmtHours(s))

/** A signed change, coloured by polarity (green grew, red shrank), or nothing when either end is missing. */
export function Delta({ now, then, fmt = fmtSI, suffix }: { now: number | null | undefined; then: number | null | undefined; fmt?: (n: number) => string; suffix?: string }) {
  if (now == null || then == null) return null
  const d = now - then
  return <span className={d > 0 ? 'grew' : d < 0 ? 'shrank' : 'muted'}>{d < 0 ? '−' : '+'}{fmt(Math.abs(d))}{suffix ? ` ${suffix}` : ''}</span>
}

/** The tooltip's summary of one run. */
export function RunInfo({ d, r, extra }: { d: FleetDeployment; r: FleetRun; extra?: React.ReactNode }) {
  const o = runOutcome(r)
  return (
    <div className="run-info">
      <div className="tip-head">
        <span className="dot" style={{ background: d.color }} /> <b>{d.name}</b> {r.scan}
      </div>
      <div className={`o-${o}`}>{MARK[o]} {runWords(r)}</div>
      <div className="muted">
        {r.trigger ? `${TRIGGER[r.trigger].emoji} ${TRIGGER[r.trigger].label} · ` : ''}started {when(r.started)} UTC
      </div>
      <div>
        {o === 'running' ? 'running' : hours(r.secs)} · {r.cost_usd === null ? 'cost n/a' : fmtUsd(r.cost_usd)}
      </div>
      {extra}
    </div>
  )
}
