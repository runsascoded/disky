// The stores' upkeep at a glance (specs/store-upkeep-monitoring.md §3): one chip per store, worst first, each a link to
// its block on /health (`#upkeep-<kind>`). /health draws it above Freshness; /scans, compact, in its header, from the
// same `['health']` query /health caches.
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Tooltip } from './Tooltip'
import { type HealthDoc, type StoreKind, upkeepChip, upkeepLabel, upkeepStrip } from './healthModel'
import { getJson } from './scanRunsUi'

/** `/api/health`, one cache entry for /health and /scans' strip. */
export const useHealth = () => useQuery<HealthDoc, Error>({ queryKey: ['health'], queryFn: () => getJson('/api/health'), refetchInterval: 60_000, staleTime: 30_000 })

/** A store's short name in the strip. */
export const STORE_SHORT: Record<StoreKind, string> = { interval: 'interval', static: 'static' }

/** The strip over `doc`'s stores (none: nothing). `onPage`: on /health itself (same-page anchors), else links there. */
export function UpkeepStrip({ doc, onPage = false }: { doc: HealthDoc; onPage?: boolean }) {
  const stores = upkeepStrip(doc.stores.filter(s => s.upkeep))
  if (!stores.length) return null
  return (
    <p className="upk-strip" aria-label="Store upkeep">
      <span className="dim">Upkeep</span>
      {stores.map(s => {
        const chip = <span className={`sr-status ${upkeepChip(s.upkeep)}`}>{STORE_SHORT[s.kind]}: {upkeepLabel(s.upkeep)}</span>
        const link = onPage ? <a href={`#${s.anchor}`}>{chip}</a> : <Link to={`/health#${s.anchor}`}>{chip}</Link>
        return s.upkeep.why ? <Tooltip key={s.kind} content={s.upkeep.why}>{link}</Tooltip> : <span key={s.kind}>{link}</span>
      })}
    </p>
  )
}

/** /scans' strip: the primary store's /health document, once it loads (nothing while it loads or when it fails). */
export function ScansUpkeepStrip() {
  const q = useHealth()
  return q.data ? <UpkeepStrip doc={q.data} /> : null
}
