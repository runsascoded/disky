import {
  FloatingFocusManager, FloatingPortal, autoUpdate, flip, offset, shift, size,
  useClick, useDismiss, useFloating, useInteractions, useRole,
} from '@floating-ui/react'
import { useId, useMemo, useState } from 'react'
import { adjacentCutoffs, cutoffCoverage, cutoffDays, dayCutoffs, fmtCutoff, markOf, sourcesAt, type PickerSource } from './federatedPickerModel'
import { utcInstant } from './federatedScans'
import { SourceMark } from './SourceMark'
import './federatedScans.scss'

export interface FederatedScanPickerProps {
  /** The sources whose observations are the cutoffs (a `SourceCatalog` is one; so is a union member). */
  sources: readonly PickerSource[]
  value: string
  onChange: (at: string) => void
  label: string
  /** Latest takes one wall-clock cutoff. Injected by previews/tests only. */
  now?: () => string
  timeZone?: string
  /** One line, no visible label (the controls still name it): for a toolbar. */
  compact?: boolean
}

/** Reused for main/before/after selection. Resolution is outside presentation;
 * neither date grouping nor source chips round the selected UTC cutoff. */
export function FederatedScanPicker({ sources, value, onChange, label, now = () => new Date().toISOString(), timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone, compact }: FederatedScanPickerProps) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState(value)
  const [error, setError] = useState<string | null>(null)
  const id = useId()
  const { refs, floatingStyles, context } = useFloating({
    open, onOpenChange: o => { setOpen(o); if (o) { setDraft(value); setError(null) } },
    placement: 'bottom-start', middleware: [offset(6), flip(), shift({ padding: 8 }), size({ padding: 8,
      apply: ({ availableHeight, elements }) => { elements.floating.style.maxHeight = `${Math.max(1, Math.min(640, availableHeight))}px` },
    })], whileElementsMounted: autoUpdate,
  })
  const { getReferenceProps, getFloatingProps } = useInteractions([
    useClick(context), useDismiss(context), useRole(context, { role: 'dialog' }),
  ])
  const groups = useMemo(() => cutoffDays(sources, timeZone), [sources, timeZone])
  const at = useMemo(() => utcInstant(value), [value])
  const selected = useMemo(() => sourcesAt(sources, at), [sources, at])
  const { previous, next } = adjacentCutoffs(sources, value)
  const coverage = cutoffCoverage(sources, value)
  const days = useMemo(() => dayCutoffs(sources), [sources])
  const display = (t: string, day = days.has(t)) => fmtCutoff(t, timeZone, day)
  const choose = (at: string) => { onChange(utcInstant(at)); setOpen(false) }
  const apply = () => {
    let at: string
    try { at = utcInstant(draft.trim()) }
    catch { setError('Enter a valid UTC instant, such as 2026-10-13T12:00:00Z.'); return }
    choose(at)
  }
  return <div className={`federated-picker${compact ? ' compact' : ''}`}>
    {!compact && <span className="fp-label">{label}</span>}
    <div className="fp-controls">
      <button type="button" disabled={!previous} aria-label={`${label}: previous update`} onClick={() => previous && choose(previous)}>‹</button>
      <button type="button" ref={refs.setReference} {...getReferenceProps()} aria-label={`${label}: choose cutoff`} className="fp-trigger">
        {display(value)} <span aria-hidden>▾</span>
      </button>
      <button type="button" disabled={!next} aria-label={`${label}: next update`} onClick={() => next && choose(next)}>›</button>
    </div>
    {open && <FloatingPortal><FloatingFocusManager context={context} modal={false}>
      <div className="fp-pop" ref={refs.setFloating} style={floatingStyles} {...getFloatingProps()} aria-label={`${label}: cutoff selection`}>
        <div className="fp-pop-heading"><strong>{label}</strong><button type="button" onClick={() => choose(now())}>Latest observed</button></div>
        <form onSubmit={e => { e.preventDefault(); apply() }} className="fp-exact">
          <label htmlFor={`${id}-at`}>Exact cutoff (UTC)</label>
          <div><input id={`${id}-at`} value={draft} onChange={e => setDraft(e.target.value)} aria-invalid={error ? true : undefined} aria-describedby={error ? `${id}-error` : undefined} spellCheck={false} /><button type="submit">Apply</button></div>
          {error && <p id={`${id}-error`} role="alert">{error}</p>}
        </form>
        <div className="fp-zone">Updates in {timeZone}</div>
        <div className="fp-days">
          {groups.length === 0 && <p>No completed source scans yet. Earlier coverage is unknown, not zero.</p>}
          {groups.map(g => <section key={g.day} aria-label={g.day}>
            <h3>{g.day}</h3><div className="fp-chips">{g.events.map(e => <button type="button" key={e.at} aria-pressed={e.at === at} onClick={() => choose(e.at)}>
              <time dateTime={e.at}>{e.time}</time><span className="fp-chip-sources">{e.sources.map(m => <SourceMark key={m.source} mark={m} />)}</span>
            </button>)}</div>
          </section>)}
        </div>
        <details className="fp-sources">
          <summary>{coverage.ready}/{coverage.total} sources observed at this cutoff</summary>
          <ul>{selected.map(s => <li key={s.source.source}><strong><SourceMark mark={markOf(s.source)} /></strong>
            {s.scan ? <span>{display(s.scan.observed_through, !!s.scan.day)}{s.scan.observed_from !== undefined && s.scan.observed_from !== s.scan.observed_through && <small>capture began {display(s.scan.observed_from)}</small>}</span> : <span>No scan before cutoff</span>}
          </li>)}</ul>
        </details>
        <button type="button" className="fp-close" onClick={() => setOpen(false)}>Close</button>
      </div>
    </FloatingFocusManager></FloatingPortal>}
  </div>
}
