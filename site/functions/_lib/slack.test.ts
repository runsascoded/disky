import { describe, expect, it, vi } from 'vitest'
import { verifySlackSignature, slackPeople, resetSlackPeople } from './slack.js'

const SECRET = 'test-signing-secret'
async function sign(ts: string, body: string): Promise<string> {
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(SECRET), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
  const mac = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(`v0:${ts}:${body}`))
  return `v0=${[...new Uint8Array(mac)].map(b => b.toString(16).padStart(2, '0')).join('')}`
}

describe('verifySlackSignature', () => {
  const body = 'payload=%7B%22type%22%3A%22block_actions%22%7D'
  const now = 1_790_000_000
  const ts = String(now - 10)

  it('accepts a correct signature inside the replay window, rejects everything else', async () => {
    expect([
      await verifySlackSignature(SECRET, ts, await sign(ts, body), body, now),
      await verifySlackSignature(SECRET, ts, await sign(ts, body), `${body}x`, now),          // body tampered
      await verifySlackSignature('other', ts, await sign(ts, body), body, now),               // wrong secret
      await verifySlackSignature(SECRET, String(now - 301), await sign(String(now - 301), body), body, now), // stale
      await verifySlackSignature(SECRET, null, await sign(ts, body), body, now),
      await verifySlackSignature(SECRET, ts, null, body, now),
      await verifySlackSignature(SECRET, 'abc', await sign('abc', body), body, now),
    ]).toEqual([true, false, false, false, false, false, false])
  })

  it('the replay window is ±300 s inclusive, future timestamps included', async () => {
    const at = async (skew: number): Promise<boolean> => {
      const t = String(now + skew)
      return verifySlackSignature(SECRET, t, await sign(t, body), body, now)
    }
    expect([await at(-300), await at(300), await at(301), await at(-301)]).toEqual([true, true, false, false])
  })
})

describe('workspace photos', () => {
  it('does not cache a missing token or a transient lookup failure', async () => {
    resetSlackPeople()
    const fetcher = vi.fn()
      .mockRejectedValueOnce(new TypeError('offline'))
      .mockResolvedValue(new Response(JSON.stringify({ ok: true, user: { id: 'U1', profile: { real_name: 'Ann', image_192: 'https://avatars.slack-edge.com/ann.png' } } })))
    vi.stubGlobal('fetch', fetcher)
    try {
      expect(await slackPeople({}, ['ann@example.test'])).toEqual({})
      expect(fetcher.mock.calls).toEqual([])
      expect(await slackPeople({ SLACK_BOT_TOKEN: 'test-a' }, ['ann@example.test'])).toEqual({})
      expect(await slackPeople({ SLACK_BOT_TOKEN: 'test-a' }, ['ann@example.test'])).toEqual({ 'ann@example.test': { name: 'Ann', image: 'https://avatars.slack-edge.com/ann.png' } })
      expect(fetcher.mock.calls.length).toBe(2)
    } finally { vi.unstubAllGlobals(); resetSlackPeople() }
  })

  it('refreshes cached photos and isolates workspaces', async () => {
    resetSlackPeople()
    let now = 1000
    const time = vi.spyOn(Date, 'now').mockImplementation(() => now)
    const fetcher = vi.fn(async () => new Response(JSON.stringify({ ok: true, user: { id: 'U1', profile: { image_192: `https://avatars.slack-edge.com/${now}.png` } } })))
    vi.stubGlobal('fetch', fetcher)
    try {
      const a = { SLACK_BOT_TOKEN: 'test-a' }, b = { SLACK_BOT_TOKEN: 'test-b' }
      const lookup = (env: typeof a) => slackPeople(env, ['ANN@example.test', 'ann@example.test'])
      expect(await lookup(a)).toEqual({ 'ann@example.test': { name: null, image: 'https://avatars.slack-edge.com/1000.png' } })
      now = 2000
      expect(await lookup(a)).toEqual({ 'ann@example.test': { name: null, image: 'https://avatars.slack-edge.com/1000.png' } })
      expect(await lookup(b)).toEqual({ 'ann@example.test': { name: null, image: 'https://avatars.slack-edge.com/2000.png' } })
      now = 3601001
      expect(await lookup(a)).toEqual({ 'ann@example.test': { name: null, image: 'https://avatars.slack-edge.com/3601001.png' } })
      expect(fetcher.mock.calls.length).toBe(3)
    } finally { time.mockRestore(); vi.unstubAllGlobals(); resetSlackPeople() }
  })
})
