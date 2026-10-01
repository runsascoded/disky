import { useQuery } from '@tanstack/react-query'
import { Navigate, useLocation } from 'react-router-dom'
import { filesRedirect, type ProxyInfo } from './objects'
import { useStore, useStoreFetch } from './store'
import { STORES } from './stores'

// `/files/*` (and a secondary store's `/meta/files/*`) was the raw scan
// browser; objects now open inside the map (specs/path-store.md §3). An old
// link lands on the same key there — in whichever configured store scans the
// proxy's bucket — or on the store root (`objects.ts` `filesRedirect`).
export function FilesRedirect() {
  const store = useStore()
  const sfetch = useStoreFetch()
  const { pathname } = useLocation()
  const base = `${store.path === '/' ? '' : store.path}/files`
  const splat = pathname.slice(base.length).replace(/^\//, '')
  const q = useQuery<ProxyInfo | null>({
    queryKey: ['store-proxy', store.key],
    queryFn: async () => {
      const r = await sfetch('/api/store')
      return r.ok ? r.json() : null
    },
    staleTime: Infinity,
    retry: false,
  })
  if (q.isPending) return null
  return <Navigate to={filesRedirect(splat, q.data ?? null, STORES, store)} replace />
}
