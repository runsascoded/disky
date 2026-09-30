/** Per-path Open Graph routing (`cfn/og.ts`): `location.pathname` → the scan
 *  uri + og-image key the middleware rewrites into the card, and the inverse
 *  the `/og/<key>` endpoint parses. Non-scan routes keep the default card. */
import { describe, expect, it } from 'vitest'
import { keyToUri, ogRoute } from '../og'

describe('ogRoute', () => {
  it('maps a bucket root to its uri, key, and title', () => {
    expect(ogRoute('/r2/ctbk')).toEqual({
      uri: 'r2://ctbk',
      key: 'r2/ctbk',
      title: 'disk-tree — r2://ctbk',
    })
  })

  it('maps a drilled sub-path', () => {
    expect(ogRoute('/r2/ctbk/avail-v3')).toEqual({
      uri: 'r2://ctbk/avail-v3',
      key: 'r2/ctbk/avail-v3',
      title: 'disk-tree — r2://ctbk/avail-v3',
    })
  })

  it('ignores a trailing slash', () => {
    expect(ogRoute('/r2/ctbk/')).toEqual({
      uri: 'r2://ctbk',
      key: 'r2/ctbk',
      title: 'disk-tree — r2://ctbk',
    })
  })

  it('maps s3 / gcs / ssh schemes', () => {
    expect(ogRoute('/s3/bucket/x')?.uri).toBe('s3://bucket/x')
    expect(ogRoute('/gcs/bucket/x')?.uri).toBe('gcs://bucket/x')
    expect(ogRoute('/ssh/host/path')?.uri).toBe('ssh://host/path')
  })

  it('maps a local file path', () => {
    expect(ogRoute('/file/Users/ryan/c')).toEqual({
      uri: '/Users/ryan/c',
      key: 'file/Users/ryan/c',
      title: 'disk-tree — /Users/ryan/c',
    })
  })

  it('returns null for non-scan routes and scheme roots without a bucket', () => {
    for (const p of ['/', '/access', '/staged', '/recent', '/s3', '/r2', '/compare/r2/ctbk', '/file']) {
      expect(ogRoute(p)).toBeNull()
    }
  })
})

describe('keyToUri', () => {
  it('inverts scheme keys', () => {
    expect(keyToUri(['r2', 'ctbk'])).toBe('r2://ctbk')
    expect(keyToUri(['r2', 'ctbk', 'avail-v3'])).toBe('r2://ctbk/avail-v3')
    expect(keyToUri(['s3', 'b', 'x'])).toBe('s3://b/x')
  })

  it('inverts a file key', () => {
    expect(keyToUri(['file', 'Users', 'ryan', 'c'])).toBe('/Users/ryan/c')
  })

  it('returns null for too-short or unknown keys', () => {
    expect(keyToUri(['r2'])).toBeNull()
    expect(keyToUri(['bogus', 'x'])).toBeNull()
    expect(keyToUri([])).toBeNull()
  })

  it('round-trips ogRoute keys', () => {
    for (const p of ['/r2/ctbk', '/r2/ctbk/avail-v3', '/s3/b/x/y', '/file/a/b']) {
      const r = ogRoute(p)!
      expect(keyToUri(r.key.split('/'))).toBe(r.uri)
    }
  })
})
