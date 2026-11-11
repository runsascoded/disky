import { describe, expect, it } from 'vitest'
import { shareUrl, withoutOg, withoutSso } from './sharePreview'

describe('withoutOg: the address bar sheds the preview token, nothing else', () => {
  it('drops og= wherever it sits; keeps path, params in order, hash; null when absent', () => {
    expect([
      withoutOg('https://site.example.org/marin-a/ckpt?d=261002&og=EZjUobkMrUuF&f=tomat#age'),
      withoutOg('https://site.example.org/?og=EZjUobkMrUuF'),
      withoutOg('https://site.example.org/staged?og=x&key=abc'),
      withoutOg('https://site.example.org/staged?q=hedy%7Cgrace'),
    ]).toEqual(['/marin-a/ckpt?d=261002&f=tomat#age', '/', '/staged?key=abc', null])
  })
})

describe('shareUrl: the page link, with exactly the token asked for', () => {
  it('promotes a section fragment, or a legacy `?section=`, into the server-visible `?v=` (as the page itself would); other fragments stay', () => {
    expect([
      shareUrl('https://site.example.org/bucket?d=26092812-26091700&v=d', { og: 'preview123' }),
      shareUrl('https://site.example.org/bucket?d=26092812-26091700#diff', { og: 'preview123' }),
      shareUrl('https://site.example.org/bucket?d=26092812-26091700#changes'),
      shareUrl('https://site.example.org/bucket?v=d#tree-map'),
      shareUrl('https://site.example.org/bucket?v=d#age'),
      shareUrl('https://site.example.org/bucket?d=26092812-26091700&section=over-time'),
      shareUrl('https://site.example.org/staged#batch-12'),
    ]).toEqual([
      'https://site.example.org/bucket?d=26092812-26091700&v=d&og=preview123',
      'https://site.example.org/bucket?d=26092812-26091700&v=d&og=preview123',
      'https://site.example.org/bucket?d=26092812-26091700&v=d',
      'https://site.example.org/bucket?v=m',
      'https://site.example.org/bucket?v=d#age',
      'https://site.example.org/bucket?d=26092812-26091700&v=o',
      'https://site.example.org/staged#batch-12',
    ])
  })
  it('replaces any carried og= / key=', () => {
    const page = 'https://site.example.org/marin-a?d=261002&og=OLD&key=OLDKEY&f=tomat#age'
    expect([shareUrl(page), shareUrl(page, { og: 'EZjUobkMrUuF' }), shareUrl(page, { key: 'k123' })]).toEqual([
      'https://site.example.org/marin-a?d=261002&f=tomat#age',
      'https://site.example.org/marin-a?d=261002&f=tomat&og=EZjUobkMrUuF#age',
      'https://site.example.org/marin-a?d=261002&f=tomat&key=k123#age',
    ])
  })
})

describe('withoutSso (the silent sign-in\'s marker, specs/oa-login-rp.md phase 2)', () => {
  it('drops `sso=` and keeps the rest, params in order', () => {
    expect([
      withoutSso('https://x.test/scans?a=1&sso=login_required&b=2#h'),
      withoutSso('https://x.test/?sso=error'),
      withoutSso('https://x.test/scans?a=1'),
    ]).toEqual(['/scans?a=1&b=2#h', '/', null])
  })
})
