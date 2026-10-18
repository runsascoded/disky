import { useQuery } from '@tanstack/react-query'
import { CLASS_LABELS, classTotals, type NonLiveRow } from './nonLiveModel'
import { storeUrl, type Store } from './stores'
import { Tooltip } from './Tooltip'
import { useUnits } from './units'

/** The scan's non-live bytes (`non-live.json`), when it has any: one line under the root view's meters, per class
 *  its bytes and when the last of it expires. A store or scan without the file shows nothing. */
export function NonLive({ store, scan }: { store: Store; scan: string | null | undefined }) {
  const { fmtBytes } = useUnits()
  const q = useQuery<NonLiveRow[] | null>({
    queryKey: ['non-live', store.key, scan],
    enabled: !!scan,
    staleTime: Infinity,
    queryFn: async () => {
      const r = await fetch(storeUrl(`${store.base}/${scan}/non-live.json`, store))
      return r.ok ? (await r.json() as { rows: NonLiveRow[] }).rows : null
    },
  })
  const totals = classTotals(q.data ?? [])
  if (!totals.length) return null
  const day = (iso: string) => iso.slice(0, 10)
  return (
    <p className="non-live dim" aria-label="Bytes not in this view">
      Not in this view:{' '}
      {totals.map((t, i) => (
        <span key={t.cls}>
          {i > 0 && ' · '}
          <Tooltip content={<>
            {t.objects.toLocaleString()} {CLASS_LABELS[t.cls]} {t.cls === 'delete-marker' ? '(0 bytes each)' : `object${t.objects === 1 ? '' : 's'}`} in {t.buckets.join(', ')}
            {t.expires ? <>; the provider keeps (and bills) them until {day(t.expires)}</> : <>; no expiry known: they stay until removed</>}
          </>}>
            <span className="non-live-class">
              {t.cls === 'delete-marker' ? `${t.objects.toLocaleString()} ${CLASS_LABELS[t.cls]}` : <><b>{fmtBytes(t.bytes)}</b> {CLASS_LABELS[t.cls]}</>}
              {t.expires && <span className="nowrap"> (until {day(t.expires)})</span>}
            </span>
          </Tooltip>
        </span>
      ))}
    </p>
  )
}
