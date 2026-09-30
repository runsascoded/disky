import { describe, expect, it } from 'vitest'
import { verifySlackSignature } from './slack.js'

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
