import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { expect, it, vi } from 'vitest'
import { RunPage } from './RunPage'

vi.mock('./plans', () => ({
  CAPS: { runFiles: false },
  useRunDetail: () => ({ data: { run: { run_id: 'run', mode: 'real', started_ts: 100, finished_ts: 200, deleted_objects: 5 } } }),
  useExecJobs: () => ({ data: { jobs: [] } }),
  useRunFiles: () => [],
}))
vi.mock('./StagedRuns', () => ({ RunDetail: ({ run }: { run: { deleted_objects: number } }) => `${run.deleted_objects} deleted` }))
vi.mock('./SiteNav', () => ({ SiteNav: () => null }))
vi.mock('./SiteKbd', () => ({ SiteKbd: () => null }))
vi.mock('./title', () => ({ useDocTitle: () => {} }))
vi.mock('./units', () => ({ useUnits: () => ({ units: 'si', suffixB: true }) }))

it('shows database-backed run statistics when run-file browsing is unavailable', () => {
  const markup = renderToStaticMarkup(createElement(MemoryRouter, { initialEntries: ['/runs/run'] },
    createElement(Routes, null, createElement(Route, { path: '/runs/*', element: createElement(RunPage) }))))
  expect(markup.replace(/<[^>]*>/g, '')).toBe('Staged deletions / runsrunReal deletion · done5 deleted')
})
