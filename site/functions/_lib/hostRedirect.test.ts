// A renamed deployment's old hosts redirect GETs to the new ones, keeping path and query; webhooks pass through.
import { describe, expect, it } from 'vitest'
import { hostRedirect, hostRedirects } from './hostRedirect'

const ENV = { HOST_REDIRECTS: 'cw-s3.oa.dev=cw.oa.dev, dev.cw-s3.oa.dev=dev.cw.oa.dev' }
const go = (url: string, method = 'GET') => {
  const r = hostRedirect(new Request(url, { method }), ENV)
  return r && [r.status, r.headers.get('location')]
}

describe('hostRedirect', () => {
  it('old host → new host, path + query + hash-free href kept', () => {
    expect([
      go('https://cw-s3.oa.dev/marin-us-east-02a/marin/grug?d=26101620&c=w'),
      go('https://dev.cw-s3.oa.dev/staged', 'HEAD'),
      go('https://CW-S3.oa.dev/'),
    ]).toEqual([
      [301, 'https://cw.oa.dev/marin-us-east-02a/marin/grug?d=26101620&c=w'],
      [301, 'https://dev.cw.oa.dev/staged'],
      [301, 'https://cw.oa.dev/'],
    ])
  })
  it('serves the new hosts, other hosts and non-GETs (a webhook on the old host)', () => {
    expect([go('https://cw.oa.dev/'), go('https://oa-cw-s3-usage.pages.dev/'), go('https://cw-s3.oa.dev/slack/actions', 'POST')]).toEqual([null, null, null])
    expect(hostRedirect(new Request('https://cw-s3.oa.dev/'), {})).toBe(null)
  })
  it('parses the map, skipping blanks and malformed entries', () => {
    expect([...hostRedirects(' a=b, ,c, d = e ,f=f')]).toEqual([['a', 'b'], ['d', 'e']])
  })
})
