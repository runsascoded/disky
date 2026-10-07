import type { ReactNode } from 'react'

export const runOutcomeLabel = (mode: 'dry' | 'real'): string => mode === 'dry' ? 'would delete' : 'deleted'

/** Predictions must stay explicit even without color, styling or tooltips. */
export function RunOutcome({ mode, children }: { mode: 'dry' | 'real'; children: ReactNode }) {
  return (
    <span className={`run-outcome ${mode}`}>
      {mode === 'dry' && <span className="prediction-label">{runOutcomeLabel(mode)}</span>}
      {children}
    </span>
  )
}
