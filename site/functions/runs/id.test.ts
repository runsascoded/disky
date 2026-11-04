import { describe, expect, it } from 'vitest'
import { runMeta, type RunCard } from './[[id]]'

const run: RunCard = { mode: 'real', scan: '2026-11-03T1622', started_ts: 1793796765, finished_ts: null, deleted_objects: 0, buckets: 'a,b,c' }

describe('runMeta: the /runs/<id> unfurl says what a run is doing, by count', () => {
  it('unknown, running, finished, dry', () => {
    expect([
      runMeta(null),
      runMeta(run),
      runMeta({ ...run, finished_ts: 1793800365, deleted_objects: 1234 }),
      runMeta({ ...run, mode: 'dry', finished_ts: 1793800365, deleted_objects: 1, buckets: 'a' }),
    ]).toEqual([
      { title: 'Deletion run', desc: 'A deletion run. Sign in to see it.' },
      { title: 'Deletion running', desc: 'Since 2026-11-04 12:52 UTC · 3 buckets · scan 2026-11-03T1622 · sign in for progress' },
      { title: 'Deletion finished', desc: '1,234 objects attempted · finished 2026-11-04 13:52 UTC · 3 buckets · scan 2026-11-03T1622' },
      { title: 'Dry run finished', desc: '1 object would be deleted · finished 2026-11-04 13:52 UTC · 1 bucket · scan 2026-11-03T1622' },
    ])
  })
})
