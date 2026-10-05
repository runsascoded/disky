import { describe, expect, it } from 'vitest'
import { hostPair, otherHostUrl } from './hosts'

const pair = { prod: 'gcs.oa.dev', dev: 'dev.gcs.oa.dev' }

describe('hostPair', () => {
  it('needs both hosts', () => {
    expect(hostPair('gcs.oa.dev', 'dev.gcs.oa.dev')).toEqual(pair)
    expect(hostPair('gcs.oa.dev', '')).toBe(null)
    expect(hostPair(undefined, 'dev.gcs.oa.dev')).toBe(null)
  })
})

describe('otherHostUrl', () => {
  it('prod → dev, keeping path, query and hash', () => {
    expect(otherHostUrl('https://gcs.oa.dev/staged?q=hedy&s=-b#x', pair)).toBe('https://dev.gcs.oa.dev/staged?q=hedy&s=-b#x')
  })
  it('dev → prod', () => {
    expect(otherHostUrl('https://dev.gcs.oa.dev/?path=marin-us-east5%2Fcheckpoints', pair)).toBe('https://gcs.oa.dev/?path=marin-us-east5%2Fcheckpoints')
  })
  it('a local dev server goes to the dev host over https, port dropped', () => {
    expect(otherHostUrl('http://localhost:3253/users', pair)).toBe('https://dev.gcs.oa.dev/users')
  })
})
