import type { RunAnalytics, RunBand } from './plans'
import { fmtN } from './types'
import { Tooltip } from './Tooltip'

export function skippedCounts(band: RunBand, analysis?: RunAnalytics['analysis']) {
  const relative = band.prefix.replace(/^[^:]+:\/\/[^/]+\//, '')
  const snapshot = analysis?.prefixes?.find(p => p.prefix === relative)
  const reconstructed = !!snapshot && snapshot.objects === band.drift_new_objects
  return {
    added: reconstructed ? snapshot!.new_objects : null,
    changed: band.overwritten + (reconstructed ? snapshot!.changed_objects : 0),
    unclassified: reconstructed ? 0 : band.drift_new_objects,
    reconstructed,
  }
}

export function SkippedCounts({ band, analysis }: { band: RunBand; analysis?: RunAnalytics['analysis'] }) {
  const counts = skippedCounts(band, analysis)
  if (!band.drift_new_objects && !band.overwritten) return <span className="dim">0</span>
  const explanation = counts.reconstructed
    ? 'Added/changed split reconstructed at the manifest scan. The execution recorded the combined skipped count, without key names.'
    : 'Changed: reviewed keys replaced before deletion. Unclassified: outside the reviewed manifest; an added/changed split was not recorded.'
  return <Tooltip content={explanation}><span className="skipped-counts">
    {counts.added !== null && <span className="skipped-added">{fmtN(counts.added)} added</span>}
    {(counts.added !== null || counts.changed > 0) && <span className={counts.changed ? 'skipped-changed' : 'dim'}>{fmtN(counts.changed)} changed</span>}
    {counts.unclassified > 0 && <span className="dim">{fmtN(counts.unclassified)} unclassified</span>}
  </span></Tooltip>
}
