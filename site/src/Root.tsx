import { Navigate, Route, Routes, useLocation } from 'react-router-dom'
import { lazy, Suspense, useEffect } from 'react'
import { HotkeysProvider } from 'use-kbd'
import { HelpCard, HelpProvider } from './Help'
import { AdminDbPage } from './AdminDbPage'
import { AdminPage } from './AdminPage'
import App from './App'
import { AuthGate, SignInPage } from './AuthGate'
import { PrivacyPage } from './PrivacyPage'
import { FilesRedirect } from './FilesRedirect'
import { AssignmentsPage } from './AssignmentsPage'
import { StagedPage } from './StagedPage'
import { RunPage } from './RunPage'
import { ScanRunPage, ScansPage } from './ScansPage'
import { HealthPage } from './HealthPage'
import { NamePage } from './NamePage'
import { OgPage } from './OgPage'
import { UserOgPage, UserPage, UsersOgPage, UsersPage } from './UserPage'
import { StoreProvider } from './store'
import { DEFAULT_STORE, STORES } from './stores'
import { useLoadIdentities } from './identities'
import { PerfOverlay } from './dev/PerfOverlay'

// The session log's viewer (admin-only): its own chunk, so the main bundle carries none of it.
const SessionsPage = lazy(() => import('./SessionsPage').then(m => ({ default: m.SessionsPage })))
const sessions = <AuthGate><Suspense fallback={<p className="loading">Loading…</p>}><SessionsPage /></Suspense></AuthGate>

// `?perf=1` at load: the time-to-render panel (specs/render-bench.md). The
// marks themselves are always on (`perf.ts`); only the panel is opt-in.
const PERF = typeof location !== 'undefined' && new URLSearchParams(location.search).get('perf') === '1'

// `<store>/og` → redacted fixed-size treemap for that store's og:image
// screenshot (public, ungated — it's what unfurl crawlers render); `/files/*`
// (the retired scan browser) → the same key in the map; every other path → the
// treemap app for the primary store. A secondary store (`VITE_STORES_EXTRA`,
// specs/multi-store.md phase 2) gets the same app under its own path (`/meta/*`,
// and `/meta/files/*` redirecting like `/files/*`),
// inside a <StoreProvider> so every data request carries its `store=`; the
// primary's routes are exactly the single-store build's. The data-backed
// routes sit behind <AuthGate>, which shows a login wall when there's no
// session. One hotkey/omnibar registry for the whole site (SiteKbd renders
// the chrome on each page; pages register their own actions on top of the
// shared ones).
/** Leave for the data bucket's `/meta` tree on its own host (`/meta/x?d=…` → `<to>/x?d=…`). */
function MetaTreeRedirect({ to }: { to: string }) {
  const { pathname, search, hash } = useLocation()
  useEffect(() => { location.replace(to.replace(/\/$/, '') + pathname.replace(/^\/meta/, '') + search + hash) }, [to, pathname, search, hash])
  return <p className="loading">Opening the data bucket's meta tree…</p>
}

export default function Root() {
  useLoadIdentities()
  return (
    <HotkeysProvider config={{ storageKey: 'gcs-usage' }}>
    <HelpProvider>
    <Routes>
      {STORES.map(s => (
        <Route key={s.key} path={`${s.path.replace(/\/$/, '')}/og`} element={<OgPage store={s} />} />
      ))}
      {/* The wall as a page (ungated): where the inline "sign in" links go. */}
      <Route path="/signin" element={<SignInPage />} />
      <Route path="/privacy" element={<PrivacyPage />} />
      <Route path="/admin" element={<AuthGate><AdminPage /></AuthGate>} />
      <Route path="/admin/db" element={<AuthGate><AdminDbPage /></AuthGate>} />
      <Route path="/admin/db/:table" element={<AuthGate><AdminDbPage /></AuthGate>} />
      <Route path="/admin/sessions" element={sessions} />
      <Route path="/admin/sessions/:sid" element={sessions} />
      <Route path="/files/*" element={<AuthGate><FilesRedirect /></AuthGate>} />
      {/* `/meta` served by another deployment (`Store.metaTree`): go there, keeping the subpath + query. */}
      {DEFAULT_STORE.metaTree && !STORES.some(s => s.path.replace(/\/$/, '') === '/meta') && (
        <Route path="/meta/*" element={<MetaTreeRedirect to={DEFAULT_STORE.metaTree} />} />
      )}
      {/* The owner pages exist only on an attribution store; elsewhere they go home. */}
      {DEFAULT_STORE.owners ? (<>
      <Route path="/assignments" element={<AuthGate><AssignmentsPage /></AuthGate>} />
      <Route path="/users/og" element={<UsersOgPage />} />
      <Route path="/user/:id/og" element={<UserOgPage />} />
      <Route path="/users" element={<AuthGate><UsersPage /></AuthGate>} />
      <Route path="/user/:id" element={<AuthGate><UserPage /></AuthGate>} />
      </>) : (
      // caseSensitive: React Router matches case-insensitively, and a laptop
      // store's drill paths start `/Users/…`.
      <Route path="/users/*" caseSensitive element={<Navigate to="/" replace />} />
      )}
      {/* The opt-in deletion console: what the trash gesture staged, and its runs. */}
      <Route path="/staged" element={<AuthGate><StagedPage /></AuthGate>} />
      <Route path="/runs/*" element={<AuthGate><RunPage /></AuthGate>} />
      {/* What each scan job did (specs/scan-runs-ui.md). */}
      <Route path="/scans" element={<AuthGate><ScansPage /></AuthGate>} />
      <Route path="/scans/*" element={<AuthGate><ScanRunPage /></AuthGate>} />
      {/* The append-only stores' state (specs/health-page.md). */}
      <Route path="/health" element={<AuthGate><HealthPage /></AuthGate>} />
      <Route path="/names" element={<AuthGate><NamePage /></AuthGate>} />
      {/* Retired pages: the mark & sweep console became /staged; the review
          lenses became the home page's owner axis; the name-search preview
          became /names. Old links land somewhere sane. */}
      <Route path="/sweep" element={<Navigate to="/staged" replace />} />
      <Route path="/hot" element={<Navigate to="/names" replace />} />
      <Route path="/marks" element={<Navigate to="/" replace />} />
      <Route path="/mark" element={<Navigate to="/" replace />} />
      {/* Secondary stores: the map (drill paths below it, objects opened in
          it), under the store's path. No ownership, staging or executor pages —
          those are the primary's (their Functions 404 for any other store). */}
      {STORES.slice(1).map(s => {
        const base = s.path.replace(/\/$/, '')
        return [
          <Route key={`${s.key}:files`} path={`${base}/files/*`} element={<AuthGate><StoreProvider store={s}><FilesRedirect /></StoreProvider></AuthGate>} />,
          <Route key={s.key} path={`${base}/*`} element={<AuthGate><StoreProvider store={s}><App /></StoreProvider></AuthGate>} />,
        ]
      })}
      <Route path="*" element={<AuthGate><App /></AuthGate>} />
    </Routes>
    <HelpCard />
    {PERF && <PerfOverlay />}
    </HelpProvider>
    </HotkeysProvider>
  )
}
