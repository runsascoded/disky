import {
  FloatingFocusManager, FloatingPortal, autoUpdate, flip, offset, shift, size,
  useClick, useDismiss, useFloating, useInteractions, useRole,
} from '@floating-ui/react'
import { useId, useMemo, useState } from 'react'
import { adjacentCutoffs, cutoffCoverage, cutoffDays } from './federatedPickerModel'
import { resolveFederation, utcInstant, type SourceCatalog } from './federatedScans'
import './federatedScans.scss'

export interface FederatedScanPickerProps {
  catalogs: readonly SourceCatalog[]
  value: string
  onChange: (at: string) => void
  label: string
  /** Latest takes one wall-clock cutoff. Injected by previews/tests only. */
  now?: () => string
  timeZone?: string
}

/** Reused for main/before/after selection. Resolution is outside presentation;
 * neither date grouping nor source chips round the selected UTC cutoff. */
export function FederatedScanPicker({ catalogs, value, onChange, label, now = () => new Date().toISOString(), timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone }: FederatedScanPickerProps) {
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
  const groups = useMemo(() => cutoffDays(catalogs, timeZone), [catalogs, timeZone])
  const view = useMemo(() => resolveFederation(catalogs, value), [catalogs, value])
  const { previous, next } = adjacentCutoffs(catalogs, value)
  const coverage = cutoffCoverage(catalogs, value)
  const display = (at: string) => new Date(at).toLocaleString(undefined, { timeZone, month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit' })
  const choose = (at: string) => { onChange(utcInstant(at)); setOpen(false) }
  const apply = () => {
    let at: string
    try { at = utcInstant(draft.trim()) }
    catch { setError('Enter a valid UTC instant, such as 2026-10-13T12:00:00Z.'); return }
    choose(at)
  }
  return <div className="federated-picker">
    <span className="fp-label">{label}</span>
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
            <h3>{g.day}</h3><div className="fp-chips">{g.events.map(e => <button type="button" key={e.at} aria-pressed={e.at === view.at} onClick={() => choose(e.at)}>
              <time dateTime={e.at}>{e.time}</time><span>{e.labels.join(', ')}</span>
            </button>)}</div>
          </section>)}
        </div>
        <details className="fp-sources">
          <summary>{coverage.ready}/{coverage.total} sources observed at this cutoff</summary>
          <ul>{view.sources.map(s => <li key={s.source}><strong>{catalogs.find(c => c.source === s.source)?.label ?? s.source}</strong>
            {s.scan ? <span>{display(s.scan.observed_through)}{s.scan.observed_from !== s.scan.observed_through && <small>capture began {display(s.scan.observed_from)}</small>}</span> : <span>No scan before cutoff</span>}
          </li>)}</ul>
        </details>
        <button type="button" className="fp-close" onClick={() => setOpen(false)}>Close</button>
      </div>
    </FloatingFocusManager></FloatingPortal>}
  </div>
}
