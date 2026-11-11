import { describe, expect, it } from 'vitest'
import type { RunShape } from '../_lib/og/run'
import { runMeta } from './[[id]]'

const run = { scan: '2026-11-03T1622' }
const running: RunShape = {
  mode: 'real', state: 'running', started_ts: 1793796765, done: 4889000, total: 36062888, unanswered: 0, n_buckets: 3,
  as_of: 1793800365, series: [], peak: null, latest: null, avg: null,
}

describe('runMeta: the /runs/<id> unfurl says what a run is doing, by count, as the card does', () => {
  it('running, planning, finished, dry, one object, no plan, unanswered', () => {
    expect([
      runMeta(run, running),
      runMeta(run, { ...running, state: 'planning', done: null, total: null, as_of: null }),
      runMeta(run, { ...running, state: 'finished', done: 1234, total: 1234 }),
      runMeta(run, { ...running, mode: 'dry', state: 'finished', done: 1, total: 1, n_buckets: 1 }),
      runMeta(run, { ...running, total: null, n_buckets: 0 }),
      runMeta(run, { ...running, state: 'stalled', unanswered: 12 }),
    ]).toEqual([
      { title: 'Deletion running', desc: '4,889,000 of 36,062,888 objects attempted · as of 2026-11-04 13:52 UTC · 3 buckets · scan 2026-11-03T1622' },
      { title: 'Deletion planning', desc: 'No progress yet · since 2026-11-04 12:52 UTC · 3 buckets · scan 2026-11-03T1622' },
      { title: 'Deletion finished', desc: '1,234 of 1,234 objects attempted · as of 2026-11-04 13:52 UTC · 3 buckets · scan 2026-11-03T1622' },
      { title: 'Dry run finished', desc: '1 of 1 object would be deleted · as of 2026-11-04 13:52 UTC · 1 bucket · scan 2026-11-03T1622' },
      { title: 'Deletion running', desc: '4,889,000 objects attempted · as of 2026-11-04 13:52 UTC · scan 2026-11-03T1622' },
      { title: 'Deletion stalled', desc: '4,889,000 of 36,062,888 objects attempted · as of 2026-11-04 13:52 UTC · 3 buckets · 12 unanswered · scan 2026-11-03T1622' },
    ])
  })
})
