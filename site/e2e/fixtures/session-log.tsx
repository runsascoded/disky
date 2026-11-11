import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createRoot } from 'react-dom/client'
import { BrowserRouter, Routes, Route } from 'react-router-dom'
import { SelfProvider } from '../../src/SelfProvider'
import { UnitsProvider } from '../../src/units'
import { SessionLogSwitch } from '../../src/SessionLogSwitch'
import { SessionsPage } from '../../src/SessionsPage'
import { bootSessionLog } from '../../src/sessionLogBoot'
import '../../src/app.scss'

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
function Fixture() { return <main><SessionLogSwitch /><SessionsPage /></main> }
createRoot(document.getElementById('root')!).render(<QueryClientProvider client={client}><BrowserRouter><UnitsProvider><SelfProvider>
  <Routes><Route path="/admin/sessions/:sid" element={<Fixture />} /><Route path="*" element={<Fixture />} /></Routes>
</SelfProvider></UnitsProvider></BrowserRouter></QueryClientProvider>)
void bootSessionLog().then(started => { document.documentElement.dataset.sessionLogReady = String(started) })
