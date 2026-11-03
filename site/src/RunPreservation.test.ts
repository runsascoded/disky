import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { expect, it } from 'vitest'
import { ReviewPreservation } from './RunPreservation'
import type { RunAnalytics } from './plans'

const text = (analytics: RunAnalytics | undefined) => renderToStaticMarkup(createElement(ReviewPreservation, { analytics })).replace(/<[^>]*>/g, '')

it('labels reconstructed snapshot counts separately from executor observations', () => {
  expect(text({ review: null, result: null, analysis: {
    method: 'review-snapshots', scan: 'scan', created_at: 'time',
    review_omissions: { objects: 906, bytes: 1000, new_objects: 800, changed_objects: 106 },
    execution: { outside_review: 906, keys_recorded: false },
  } })).toBe('Excluded at manifest scan · reconstructednew since review800changed since review106Reconstructed from the original review and manifest scans. Execution-time preserved key names were not recorded.')
})

it('uses recorded review analytics when available', () => {
  expect(text({ result: null, review: { objects: 1, bytes: 7, as_of: {},
    skipped_after_as_of: { objects: 2, bytes: 14, new_objects: 1, changed_objects: 1 },
  } })).toBe('Excluded at manifest scannew since review1changed since review1')
})

it('omits the split when no recorded or reconstructed evidence exists', () => {
  expect(text(undefined)).toBe('')
  expect(text({ review: null, result: null })).toBe('')
})
