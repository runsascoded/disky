import { describe, expect, it, vi } from 'vitest'
import { onRequestGet } from './index'
import { onRequestGet as catchAll } from './[[path]]'

vi.mock('../_lib/og/serve.js', () => ({
  serveCard: async () => new Response('not a card', { status: 404 }),
}))

describe('/og map image shortcut', () => {
  it.each(['/og', '/og/'])('also redirects when the deployed catch-all handles %s', async path => {
    const response = await catchAll({
      request: new Request(`https://r2.rbw.sh${path}?c=t&card=5`),
      env: { ASSETS: { fetch: async () => new Response('') } },
    })
    expect([response.status, response.headers.get('location'), response.headers.get('cache-control')])
      .toEqual([302, 'https://r2.rbw.sh/og/map.png?c=t&card=5', 'no-store'])
  })
  it.each([
    ['https://r2.rbw.sh/og?c=t&card=4', 'https://r2.rbw.sh/og/map.png?c=t&card=4'],
    ['https://r2.rbw.sh/og/?path=ctbk%2Fgbfs&d=261013', 'https://r2.rbw.sh/og/map.png?path=ctbk%2Fgbfs&d=261013'],
    ['https://private.example/og?path=private&sig=test', 'https://private.example/og/map.png?path=private&sig=test'],
    ['https://r2.rbw.sh/og', 'https://r2.rbw.sh/og/map.png'],
  ])('preserves the same-origin view and credentials: %s', (source, target) => {
    const response = onRequestGet({ request: new Request(source) })
    expect([response.status, response.headers.get('location'), response.headers.get('cache-control'), response.body])
      .toEqual([302, target, 'no-store', null])
  })
})
