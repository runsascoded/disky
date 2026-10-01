import { Navigate, Route, Routes } from 'react-router-dom'
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
import { OgPage } from './OgPage'
import { UserOgPage, UserPage, UsersOgPage, UsersPage } from './UserPage'
import { StoreProvider } from './store'
import { DEFAULT_STORE, STORES } from './stores'
import { useLoadIdentities } from './identities'
import { PerfOverlay } from './dev/PerfOverlay'

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
      <Route path="/files/*" element={<AuthGate><FilesRedirect /></AuthGate>} />
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
      {/* Retired pages: the mark & sweep console became /staged; the review
          lenses became the home page's owner axis. Old links land somewhere sane. */}
      <Route path="/sweep" element={<Navigate to="/staged" replace />} />
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
