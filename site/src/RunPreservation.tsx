import type { RunAnalytics } from './plans'
import { fmtN } from './types'

export function ReviewPreservation({ analytics }: { analytics: RunAnalytics | undefined }) {
  const recorded = analytics?.review?.skipped_after_as_of
  const reconstructed = analytics?.analysis
  const counts = recorded?.new_objects != null && recorded.changed_objects != null ? recorded : reconstructed?.review_omissions
  if (!counts) return null
  const backfilled = counts === reconstructed?.review_omissions
  return <section>
    <h5>Excluded at manifest scan{backfilled ? ' · reconstructed' : ''}</h5>
    <dl className="rd-totals">
      <div><dt>new since review</dt><dd>{fmtN(counts.new_objects!)}</dd></div>
      <div><dt>changed since review</dt><dd>{fmtN(counts.changed_objects!)}</dd></div>
    </dl>
    {backfilled && <p className="dim">Reconstructed from the original review and manifest scans. Execution-time preserved key names were not recorded.</p>}
  </section>
}
