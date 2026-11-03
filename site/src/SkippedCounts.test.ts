import { expect, it } from 'vitest'
import type { RunAnalytics, RunBand } from './plans'
import { skippedCounts } from './SkippedCounts'

const band: RunBand = { prefix: 's3://b/p/', bytes: 0, objects: 0, gone: 0, overwritten: 2, drift_new_objects: 5, undone_objects: 0 }
const analysis: RunAnalytics['analysis'] = {
  method: 'review-snapshots', scan: 'scan', created_at: 'time',
  review_omissions: { objects: 5, bytes: 0, new_objects: 3, changed_objects: 2 },
  prefixes: [{ prefix: 'p/', objects: 5, new_objects: 3, changed_objects: 2 }],
  execution: { outside_review: 5, keys_recorded: false },
}

it('combines changed reviewed keys with the explicitly reconstructed snapshot breakdown', () => {
  expect(skippedCounts(band, analysis)).toEqual({ added: 3, changed: 4, unclassified: 0, reconstructed: true })
})

it('keeps the outside-manifest count unclassified without a matching snapshot count', () => {
  expect(skippedCounts(band)).toEqual({ added: null, changed: 2, unclassified: 5, reconstructed: false })
  expect(skippedCounts({ ...band, drift_new_objects: 6 }, analysis)).toEqual({ added: null, changed: 2, unclassified: 6, reconstructed: false })
})
