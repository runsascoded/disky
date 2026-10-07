import type { Check } from './runs'
import { fmtN } from './types'

export const CHECK_LABELS = ['deleted objects: log = D1', 'deleted bytes: log = D1', 'gone: log = D1', 'overwritten: log = D1', 'drifted dirs: log = D1', 'planned objects = decided']

export function RunChecks({ checks, live }: { checks: Check[]; live: boolean }) {
  return <section className="run-checks"><h4>Consistency checks</h4>
    <p className="dim">Final decision log ↔ D1 ↔ manifest. These verify accounting, not object recovery or a later scan.</p>
    <ul className="rd-checks">{CHECK_LABELS.map(label => {
      const c = checks.find(c => c.label === label)
      return <li key={label} className={!c ? 'pending' : c.ok ? 'ok' : 'off'}>
        <span className="check-status">{!c ? '◷ pending' : c.ok ? '✓ passed' : '≠ mismatch'}</span>
        <span>{label}{c ? <>: {fmtN(c.expected)}{!c.ok && <> vs {fmtN(c.actual)} <span className="dim">({c.actual > c.expected ? '+' : ''}{fmtN(c.actual - c.expected)})</span></>}</> : <span className="dim"> · {live ? 'awaiting finalization' : 'awaiting final log / manifest'}</span>}</span>
      </li>
    })}</ul>
    {checks.some(c => !c.ok && c.label.startsWith('planned')) && <p className="dim">A planned/decided gap can include keys in drifted dirs or roots left by a stop.</p>}
  </section>
}
