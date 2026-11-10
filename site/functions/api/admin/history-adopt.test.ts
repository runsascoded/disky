import { describe, expect, it } from 'vitest'
import { onRequestGet, onRequestPost, parseHistoryAdoption } from './history-adopt'

describe('/api/admin/history-adopt', () => {
  it('requires an explicit exact previous ETag or explicit absence', () => {
    expect([parseHistoryAdoption({ version: 'v1', expected_etag: 'etag1' }), parseHistoryAdoption({ version: 'v1', expected_etag: null })]).toEqual([
      { version: 'v1', expectedEtag: 'etag1' }, { version: 'v1', expectedEtag: null },
    ])
    for (const expected_etag of [undefined, '', '*', 123]) {
      expect(() => parseHistoryAdoption({ version: 'v1', expected_etag })).toThrowError('expected_etag must be the exact current ETag, or null for an absent pointer')
    }
  })

  it('gates reads and writes before reading the history pointer', async () => {
    const request = new Request('https://example.test/api/admin/history-adopt')
    expect((await Promise.all([onRequestGet({ request, env: {} }), onRequestPost({ request, env: {} })])).map(r => r.status)).toEqual([401, 401])
  })

  it('explains when this environment has no configured pointer', async () => {
    const response = await onRequestGet({ request: new Request('http://localhost/api/admin/history-adopt'), env: {} })
    expect([response.status, response.headers.get('cache-control'), await response.json()]).toEqual([
      200, 'no-store', { enabled: false, reason: 'This environment has no history version pointer configured', pointer: null },
    ])
  })
})
