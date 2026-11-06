import { expect, it } from 'vitest'
import { askedRunsPage, autoRunsPage, runsPage, runsWindow, selectRuns, type RunTableOptions } from './runTable'
import type { RunView } from './runs'

const rows: RunView[] = [
  { key: 'a', job: { job_id: 'a', mode: 'dry', by: 'alice', state: 'SUCCEEDED', created: '2026-10-01T00:00:00Z' }, ops: [] },
  { key: 'b', job: { job_id: 'b', mode: 'real', by: 'bob', state: 'RUNNING', created: '2026-10-02T00:00:00Z' }, ops: [] },
  { key: 'c', job: { job_id: 'c', mode: 'real', by: 'alice', state: 'CANCELLED', created: '2026-10-03T00:00:00Z' }, ops: [] },
]
const options: RunTableOptions = { user: '', mode: '', state: '', query: '', sort: 'started', ascending: false }
it('filters and stably sorts the joined run rows', () => {
  const select = (updates: Partial<RunTableOptions>) => selectRuns(rows, { ...options, ...updates }, new Map(), 0).map(v => v.key)
  expect(select({})).toEqual(['c', 'b', 'a'])
  expect(select({ user: 'alice', mode: 'real' })).toEqual(['c'])
  expect(select({ state: 'RUNNING' })).toEqual(['b'])
  expect(select({ query: 'ALICE', ascending: true })).toEqual(['a', 'c'])
  expect(select({ sort: 'by', ascending: true })).toEqual(['a', 'c', 'b'])
  expect(selectRuns(rows, { ...options, sort: 'deleted' }, new Map([['a', { deleted: 10 }], ['b', { deleted: 40 }], ['c', { deleted: 20 }]]), 0).map(v => v.key)).toEqual(['b', 'c', 'a'])
})

it('the table opens on the open run\'s page, else the first', () => {
  expect([autoRunsPage(-1, 20), autoRunsPage(0, 20), autoRunsPage(19, 20), autoRunsPage(20, 20), autoRunsPage(45, 20), autoRunsPage(45, Infinity)]).toEqual([0, 0, 0, 1, 2, 0])
})

it('a page asked for wins over the open run\'s, clamped to the list', () => {
  // 45 runs, 20 a page: pages 0..2; the open run is row 44 (page 2).
  expect([runsPage(null, 44, 20, 45), runsPage(0, 44, 20, 45), runsPage(1, 44, 20, 45), runsPage(9, 44, 20, 45), runsPage(null, -1, 20, 45), runsPage(3, -1, 20, 0), runsPage(2, 44, Infinity, 45)]).toEqual([2, 0, 1, 2, 0, 0, 0])
})

it('the URL names a page only when it is not the one the table opens on', () => {
  expect([askedRunsPage(2, 44, 20), askedRunsPage(0, 44, 20), askedRunsPage(0, -1, 20), askedRunsPage(1, -1, 20)]).toEqual([null, 0, null, 1])
})

it('a page\'s rows, half-open: "all" is one page of everything', () => {
  expect([runsWindow(0, 20, 45), runsWindow(2, 20, 45), runsWindow(0, Infinity, 45), runsWindow(0, 20, 0)]).toEqual([[0, 20], [40, 45], [0, 45], [0, 0]])
})
