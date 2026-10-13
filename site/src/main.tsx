import './dev/renderSpy'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { exchangeKeyParam } from '@open-athena/auth/react'
import Root from './Root'
import { UnitsProvider } from './units'
import { withoutOg } from './sharePreview'
import { bootSessionLog } from './sessionLogBoot'
import 'use-kbd/styles.css'
import './app.scss'

// A tab left open on the latest scan should pick up new data on its own, so
// queries stay fresh in the background rather than only on mount.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      refetchOnWindowFocus: true,
      retry: 2,
    },
  },
})
// Dev / `?spy=1`: the cache on `window.__qc`, so a console session can watch
// which queries refetch (the render spy says what re-rendered; this says why).
if (import.meta.env.DEV || location.search.includes('spy=1')) (window as unknown as { __qc?: QueryClient }).__qc = queryClient

// A preview link's `og=` is for unfurlers only: drop it before the router reads the URL.
const clean = withoutOg(location.href)
if (clean != null) history.replaceState(history.state, '', clean)

async function start() {
  // Redeem and remove the key before BrowserRouter snapshots the URL. Otherwise
  // its stale search restores the credential on the next SPA navigation.
  await exchangeKeyParam()
  void bootSessionLog()
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <UnitsProvider>
            <Root />
          </UnitsProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </StrictMode>,
  )
}
void start()
