import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { DEFAULT_STORE, storeFetch, storeUrl, type Store } from './stores'

// Which store a page subtree renders (specs/multi-store.md phase 2). Root
// mounts one <StoreProvider> per configured store under its `path`; anything
// outside a provider — every page of a single-store build — is the primary,
// so components that read `useStore()` behave exactly as they did reading
// `DEFAULT_STORE`. The two fetch helpers are the one seam every data request
// goes through: for the primary they are the global `fetch` and the identity,
// for a secondary store they carry `store=<key>`.

const StoreCtx = createContext<Store>(DEFAULT_STORE)

export function StoreProvider({ store, children }: { store: Store; children: ReactNode }) {
  return <StoreCtx.Provider value={store}>{children}</StoreCtx.Provider>
}

/** The store this subtree renders (the primary outside any provider). */
export const useStore = (): Store => useContext(StoreCtx)

/** `fetch` for this subtree's store (`storeFetch`). */
export function useStoreFetch(): typeof fetch {
  const store = useStore()
  return useMemo(() => storeFetch(store), [store])
}

/** `url` as this subtree's store requests it (`storeUrl`). */
export function useStoreUrl(): (url: string) => string {
  const store = useStore()
  return useMemo(() => (url: string) => storeUrl(url, store), [store])
}
