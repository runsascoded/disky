import { describe, expect, it } from 'vitest'
import { B62, canonical, EPOCH, expDay, imagePath, ogKey, packDays, randomToken, resolveImage, unpackDays } from './sign'

// 2026-10-02T12:00Z: day 274 since EPOCH (2026-01-01).
const NOW = EPOCH + 274 * 86400 + 12 * 3600
const at = (p: string) => new URL(`https://site.example.org${p}`)
const VIEW = { path: 'marin-a/ckpt', d: '261002' }

describe('canonical, days', () => {
  it('sorted keys, empties dropped, encoded', () => {
    expect([
      canonical({ path: 'b/x y', d: '261002', f: '' }),
      canonical({ f: undefined, d: '261002', path: 'b/x y' }),
      canonical({ q: 'hedy|grace', s: null }),
      canonical({}),
    ]).toEqual(['d=261002&path=b%2Fx+y', 'd=261002&path=b%2Fx+y', 'q=hedy%7Cgrace', ''])
  })
  it('expiry days pack to 2 base62 chars', () => {
    expect([expDay(NOW, 7), packDays(0), packDays(61), packDays(62), packDays(281), packDays(3843), unpackDays('Eh'), unpackDays('E'), unpackDays('E-')])
      .toEqual([281, 'AA', 'A9', 'BA', 'Eh', '99', 281, null, null])
    expect(() => packDays(3844)).toThrow('expiry day out of range: 3844')
  })
})

describe('randomToken: base62, uniform', () => {
  it('maps bytes < 248 by % 62 and skips the rest (no modulo bias)', () => {
    const bytes = [0, 61, 62, 247, 248, 255, 25, 26, 52, 123, 200, 9]
    let i = 0
    const rand = (b: Uint8Array) => { for (let j = 0; j < b.length; j++) b[j] = bytes[i++ % bytes.length]; return b }
    // 248 and 255 are rejected: 10 chars from the other 10 bytes.
    expect(randomToken(10, rand)).toBe('A9A9Za09OJ')
  })
  it('10 base62 chars from the real RNG, distinct each time', () => {
    const ts = Array.from({ length: 50 }, () => randomToken())
    expect([ts.every(t => t.length === 10 && [...t].every(c => B62.includes(c))), new Set(ts).size]).toEqual([true, 50])
  })
})

// Vectors cross-checked in Python: HMAC-SHA256 under HMAC(b's3cret', b'og-card:v1'),
// first 8 bytes big-endian, 10 base62 digits (mod 62¹⁰), most significant first.
describe('card URLs: anonymous unsigned, full with a 12-char base62 sig', () => {
  it('test vectors', async () => {
    const k = await ogKey('s3cret')
    expect([
      await imagePath(null, 'map', VIEW, 'anon'),
      await imagePath(k, 'map', VIEW, 'full', 281),
      await imagePath(null, 'staged', {}, 'anon'),
      await imagePath(k, 'staged', {}, 'full', 281),
    ]).toEqual([
      '/og/map.png?d=261002&path=marin-a%2Fckpt',
      '/og/map.png?d=261002&path=marin-a%2Fckpt&sig=Ehz6ybQr7K6n',
      '/og/staged.png',
      '/og/staged.png?sig=EhWAtagBytVE',
    ])
  })
  it('full only for the exact signed view and an unexpired sig; anything else is the anonymous card', async () => {
    const k = await ogKey('s3cret')
    const p = await imagePath(k, 'map', VIEW, 'full', 281)
    const r = (q: string, now = NOW) => resolveImage(k, at(q), now)
    const anon = (params: Record<string, string>, why?: string) => ({ kind: 'map', params, tier: 'anon', ...(why ? { why } : {}) })
    expect([
      await r(p),
      await r(p.replace('path=marin-a%2Fckpt', 'path=marin-a')),
      await r(p.replace('d=261002', 'd=261001')),
      await r(p.replace('sig=Eh', 'sig=Ei')),
      await r(p.replace(/sig=.*/, 'sig=short')),
      await r(p, EPOCH + 282 * 86400),
      await r(p, EPOCH + 282 * 86400 - 1),
      await resolveImage(await ogKey('other'), at(p), NOW),
      await r('/og/map.png?path=marin-a%2Fckpt&d=261002'),
      await resolveImage(k, at('/api/subtree'), NOW),
    ]).toEqual([
      { kind: 'map', params: VIEW, tier: 'full', day: 281 },
      anon({ path: 'marin-a', d: '261002' }, 'bad signature'),
      anon({ path: 'marin-a/ckpt', d: '261001' }, 'bad signature'),
      anon(VIEW, 'bad signature'),
      anon(VIEW, 'bad sig'),
      anon(VIEW, 'expired'),
      { kind: 'map', params: VIEW, tier: 'full', day: 281 },
      anon(VIEW, 'bad signature'),
      anon({ path: 'marin-a/ckpt', d: '261002' }),
      null,
    ])
  })
})
