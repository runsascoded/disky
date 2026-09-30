import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { ThemeProvider, CssBaseline, Tooltip } from '@mui/material'
import { HotkeysProvider, ShortcutsModal, Omnibar, SequenceModal, LookupModal } from 'use-kbd'
import 'use-kbd/styles.css'
import { theme } from './theme'
import { Header } from './components/Header'
import { ScanList } from './components/ScanList'
import { ScanDetails } from './components/ScanDetails'
import { S3BucketList } from './components/S3BucketList'
import { RecentList } from './components/RecentList'
import { CompareView } from './components/CompareView'
import { BrowsePage } from './components/BrowsePage'
import { AccessPage } from './components/AccessPage'
import { StagedPage } from './components/StagedPage'
import { Gate } from './auth'
import './App.scss'
import type { ReactNode } from 'react'
import { useUnits } from './utils/units'
import { useTiling } from './utils/tiling'

// MUI Tooltip wrapper for use-kbd
function MuiTooltip({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <Tooltip title={title} placement="top" arrow>
      <span style={{ display: 'inline' }}>{children}</span>
    </Tooltip>
  )
}

function App() {
  // Re-render the whole tree when the SI/IEC preference flips: `formatSize`
  // reads the store directly, so every caller just needs a render.
  useUnits()
  useTiling()
  return (
    <ThemeProvider theme={theme}>
      <CssBaseline />
      <HotkeysProvider config={{ storageKey: 'disk-tree-hotkeys' }}>
        <BrowserRouter>
          <Header />
          <div className="app">
            {/* On the static deployment every `/api/*` needs a session
                (specs/done/pages-auth.md); elsewhere `Gate` is a pass-through. */}
            <Gate>
            <Routes>
              {/* Home is the scheme-agnostic union of every scan (a laptop's
                  local paths, buckets, …); `/r2`, `/gcs` scope it to one cloud. */}
              <Route path="/" element={<ScanList />} />
              <Route path="/scans" element={<Navigate to="/" replace />} />
              <Route path="/r2" element={<ScanList scheme="r2" />} />
              <Route path="/access" element={<AccessPage />} />
              <Route path="/staged" element={<StagedPage />} />
              <Route path="/file/*" element={<ScanDetails />} />
              <Route path="/s3" element={<S3BucketList />} />
              <Route path="/s3/*" element={<ScanDetails />} />
              {/* Per-scheme landing for any other cloud (scans-derived union
                  treemap; s3 keeps its live bucket lister above). */}
              <Route path="/gcs" element={<ScanList scheme="gcs" />} />
              <Route path="/gcs/*" element={<ScanDetails />} />
              <Route path="/r2/*" element={<ScanDetails />} />
              <Route path="/ssh/*" element={<ScanDetails />} />
              <Route path="/recent" element={<RecentList />} />
              <Route path="/compare/*" element={<CompareView />} />
              {/* Half A phase-1 proof: raw browse via `<FileTree>` over the live
                  API, beside the hand-rolled table (nothing retired yet). */}
              <Route path="/browse/*" element={<BrowsePage />} />
            </Routes>
            </Gate>
          </div>
          <ShortcutsModal
            editable
            groupOrder={['Navigation', 'Table: Navigation', 'Table: Selection']}
            TooltipComponent={MuiTooltip}
          />
          <Omnibar />
          <SequenceModal />
          <LookupModal />
        </BrowserRouter>
      </HotkeysProvider>
    </ThemeProvider>
  )
}

export default App
