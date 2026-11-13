import { describe, expect, it } from 'vitest'
import { API_SHAPE, API_SHAPE_HEADER } from '../../src/apiShape'
import { BUILD_HEADER, buildId, withBuild } from './build'

describe('withBuild', () => {
  const stamped = (r: Response) => [r.headers.get(BUILD_HEADER), r.headers.get(API_SHAPE_HEADER)]
  it('stamps the build and the API shape, in place on a mutable response', () => {
    const res = new Response('{}')
    expect([withBuild(res) === res, ...stamped(res)]).toEqual([true, buildId(), String(API_SHAPE)])
  })
  it('on a copy when the headers are immutable', () => {
    const res = Response.redirect('https://example.org/', 302)
    const out = withBuild(res)
    expect([out === res, ...stamped(out), out.status]).toEqual([false, buildId(), String(API_SHAPE), 302])
  })
})
