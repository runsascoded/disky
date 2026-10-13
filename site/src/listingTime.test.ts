import { describe, expect, it } from 'vitest'
import { listingTime } from './listingTime'

describe('listing timestamp semantics', () => {
  it.each([
    ['gs://', { label: 'created', noun: 'creation', description: 'creation time' }],
    ['gcs://', { label: 'created', noun: 'creation', description: 'creation time' }],
    ['s3://', { label: 'modified', noun: 'modification', description: 'last-modified time' }],
    ['r2://', { label: 'modified', noun: 'modification', description: 'last-modified time' }],
    ['file://', { label: 'modified', noun: 'modification', description: 'last-modified time' }],
  ] as const)('labels the existing timestamp of %s without reinterpreting its value', (scheme, expected) => {
    expect(listingTime(scheme)).toEqual(expected)
  })
})
