import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import type { Rules } from './types'
import { useStore, useStoreFetch } from './store'

/** The published attribution rules (`/data/rules.json`) — one definition for
 * every page, so all read one cache entry with one policy.
 * Only the owners store publishes them; elsewhere the query never runs (a
 * plan-first deployment on R2 has no `rules.json` to 404 on, and a secondary
 * store has no ownership ledger at all). */
export function useRules(): UseQueryResult<Rules> {
  const store = useStore()
  const sfetch = useStoreFetch()
  return useQuery<Rules>({
    queryKey: ['rules', store.key],
    enabled: store.owners,
    queryFn: async () => {
      const r = await sfetch('/data/rules.json')
      if (!r.ok) throw new Error(`rules: ${r.status}`)
      return r.json()
    },
    retry: false,
    staleTime: 10 * 60_000,
  })
}
