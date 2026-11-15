import './dev/renderSpy'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { exchangeKeyParam } from '@open-athena/auth/react'
import Root from './Root'
import { SelfProvider } from './SelfProvider'
import { TooltipGroup } from './Tooltip'
import { UnitsProvider } from './units'
import { withoutOg, withoutSso } from './sharePreview'
import { migrateSection } from './homeSections'
import { bootSessionLog } from './sessionLogBoot'
import { forwardPs } from './pathStoreParam'
import { describeUrlParams } from './urlParams'
import { QUERY_DEFAULTS } from './queryDefaults'
import { watchApiShape } from './apiShape'
import 'use-kbd/styles.css'
import './app.scss'

// A tab left open across a deploy that changed the API's wire shape reloads instead of misreading its bodies.
watchApiShape()
const queryClient = new QueryClient({ defaultOptions: QUERY_DEFAULTS })
// Dev / `?spy=1`: the cache on `window.__qc`, so a console session can watch
// which queries refetch (the render spy says what re-rendered; this says why).
if (import.meta.env.DEV || location.search.includes('spy=1')) (window as unknown as { __qc?: QueryClient }).__qc = queryClient

// A preview link's `og=` is for unfurlers only: drop it before the router reads the URL.
const clean = withoutOg(location.href)
if (clean != null) history.replaceState(history.state, '', clean)
// The silent sign-in's `?sso=` marker likewise: what it had to stop, it stopped on the way here.
const unmarked = withoutSso(location.href)
if (unmarked != null) history.replaceState(history.state, '', unmarked)
// A link from before `?v=` names its section `?section=over-time`: rewritten in place, before the scroll-spy reads it.
const migrated = migrateSection(location.search)
if (migrated != null) history.replaceState(history.state, '', `${location.pathname}${migrated}${location.hash}`)

// `?ps=…`: every `/api/` request reads the path store the page names (`pathStoreParam.ts`).
forwardPs(window)
// The URL params' descriptions, for the "URL params" dialog (`urlParams.ts`).
describeUrlParams()

// The session log (specs/session-log.md): off unless the deployment's window is open; never awaited.
void bootSessionLog()

async function start() {
  // Redeem and strip a share link's `key=` before BrowserRouter snapshots the
  // URL: its stale search would otherwise restore the credential on the next
  // SPA navigation (and re-redeem it on a reload).
  await exchangeKeyParam()
  createRoot(document.getElementById('root')!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <UnitsProvider>
            <SelfProvider>
              <TooltipGroup>
                <Root />
              </TooltipGroup>
            </SelfProvider>
          </UnitsProvider>
        </BrowserRouter>
      </QueryClientProvider>
    </StrictMode>,
  )
}
void start()
