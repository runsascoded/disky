import { type ReactNode, useMemo } from 'react'
import { FederatedScanPicker } from './FederatedScanPicker'
import type { PickerSource } from './federatedPickerModel'
import { cutoffOfScan, pickCutoff, sourcesBefore } from './unionSources'

/** The page's scan picker on a union view: `FederatedScanPicker` over the members' observations. A pick writes the
 *  familiar `?d=` scan id when one says it exactly (`onChange`), else the cutoff instant (`onAt`: `?at=`/`?atf=`),
 *  as `pickCutoff` decides. The picker shows `at` when the page holds one, else `value`'s instant; `before` (an
 *  instant) limits the cutoffs to those strictly earlier (a diff's start). A `value` no member observes (the
 *  union's list not yet refreshed) shows `fallback`. */
export function UnionScanPicker({ sources, value, at, scans, onChange, onAt, label, before, compact, fallback }: {
  sources: readonly PickerSource[]
  value: string | null
  /** The page's cutoff instant, when the URL names one (`?at=`). */
  at?: string | null
  /** The page's scan list: what a `?d=` can name. */
  scans: readonly string[]
  onChange: (id: string) => void
  onAt: (at: string) => void
  label: string
  before?: string | null
  compact?: boolean
  fallback?: ReactNode
}) {
  const shown = useMemo(() => (before ? sourcesBefore(sources, before) : [...sources]), [sources, before])
  const pickable = useMemo(() => new Set(scans), [scans])
  const cutoff = at ?? (value ? cutoffOfScan(sources, value) : null)
  if (!cutoff) return <>{fallback}</>
  return (
    <FederatedScanPicker sources={shown} value={cutoff} label={label} compact={compact}
      onChange={t => {
        const p = pickCutoff(shown, t, pickable)
        if ('at' in p) { if (p.at !== at) onAt(p.at) } else if (p.d !== value || at) onChange(p.d)
      }} />
  )
}
