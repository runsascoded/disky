import './dev/renderSpy'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import Root from './Root'
import { UnitsProvider } from './units'
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
