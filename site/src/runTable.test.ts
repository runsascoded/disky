import { expect, it } from 'vitest'
import { selectRuns, type RunTableOptions } from './runTable'
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
