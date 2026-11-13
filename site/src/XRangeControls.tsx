import { MdFitScreen, MdZoomOut } from 'react-icons/md'
import { Explain } from './Help'
import { parseXr, presetOf, X_PRESETS, type XDomain } from './xDomain'

/** Shared by size-over-time and scan durations. The displayed range stays separate from a chart's selection. */
export function XRangeControls({ domain, set, fit }: {
  domain: XDomain
  set: (domain: XDomain) => void
  fit?: { onClick: () => void; label: string; description: string }
}) {
  const on = presetOf(domain)
  return (
    <span className="gran x-range" role="group" aria-label="X-axis range">
      {X_PRESETS.map(([r, days]) => (
        <Explain key={r} text={days ? `Only the last ${days === 7 ? 'week' : '30 days'} of scans` : 'Every scan'}>
          <button type="button" role="radio" aria-checked={on === r} className={on === r ? 'on' : ''} onClick={() => set(parseXr(r))}>{r}</button>
        </Explain>
      ))}
      {fit && <Explain text={fit.description}>
        <button type="button" className="icon" aria-label={fit.label} onClick={fit.onClick}><MdFitScreen /></button>
      </Explain>}
      {domain.kind === 'span' && <Explain text="Show every scan again (or double-click the strip under the chart)">
        <button type="button" className="icon" aria-label="reset zoom" onClick={() => set({ kind: 'all' })}><MdZoomOut /></button>
      </Explain>}
    </span>
  )
}
