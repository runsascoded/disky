import { TimeSeries } from '@disk-tree/react'
import { useQuery } from '@tanstack/react-query'
import { useMemo } from 'react'
import { CLASS_LABELS, type NonLiveSeries, type SeriesClass, stackedBands } from './nonLiveModel'
import { useUnits } from './units'

const COLORS: Record<SeriesClass, string> = {
  live: 'var(--s1)',
  'soft-deleted': 'var(--s6)',
  noncurrent: 'var(--s7)',
  'delete-marker': 'var(--s8)',
  mpu: 'var(--s4)',
}

const fmtDay = (x: number) => new Date(x).toISOString().slice(0, 10)
// the axis's ticks by month, ~3 of them: two years of ISO dates don't fit at phone width
const fmtMonth = (x: number) => new Date(x).toISOString().slice(0, 7)

/** The store's bytes as the provider bills them, day by day (`/api/non-live`: Cloud Monitoring's bucket totals, the scan
 *  step `non-live` with `source: metric`): live bytes, with each non-live class stacked on top. Folded under a summary;
 *  a store without the history shows nothing. */
export function NonLiveHistory() {
  const { fmtBytes } = useUnits()
  const q = useQuery<NonLiveSeries | null>({
    queryKey: ['non-live-series'],
    staleTime: 10 * 60_000,
    queryFn: async () => {
      const r = await fetch('/api/non-live')
      return r.ok ? (await r.json() as { series: NonLiveSeries | null }).series : null
    },
  })
  const stacked = useMemo(() => (q.data ? stackedBands(q.data) : null), [q.data])
  if (!q.data || !stacked?.bands.length) return null
  const { bands, gaps } = stacked
  return (
    <details className="non-live-history">
      <summary className="dim">Billed bytes over time: live + {bands.slice(1).map(b => CLASS_LABELS[b.cls as keyof typeof CLASS_LABELS]).join(' + ') || 'nothing else'} (Cloud Monitoring, as of {q.data.as_of.slice(0, 10)})</summary>
      <TimeSeries
        series={[
          ...bands.map(b => ({
            key: b.cls,
            label: b.cls === 'live' ? 'live' : CLASS_LABELS[b.cls],
            color: COLORS[b.cls],
            points: b.points,
            dots: false,
            strokeWidth: 0.75,
          })),
          // the stack's top edge in its own neutral line (the top band's color would read as that class's total)
          { key: 'billed', label: 'billed', color: 'var(--ink-3)', area: false, dots: false, strokeWidth: 1.25,
            points: bands.at(-1)!.points.map(p => ({ x: p.x, y0: 0, y: p.y })) },
        ]}
        getX={p => p.x}
        getY={p => p.y}
        getY0={p => p.y0}
        formatX={fmtMonth}
        formatTipX={fmtDay}
        xTicks={3}
        formatY={fmtBytes}
        yLabel="billed bytes"
        gaps={gaps}
        height={200}
      />
    </details>
  )
}
