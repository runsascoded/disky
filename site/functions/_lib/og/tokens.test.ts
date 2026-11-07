import { describe, expect, it } from 'vitest'
import { hashToken } from '@open-athena/auth'
import { sqliteD1 } from '../testD1'
import { DAY_S, EPOCH, expDay } from './sign'
import { fullTier, grantLive, listTokens, mint, NEVER_DAY, revoke, shareKeyGrant, shareKeyLive, tokenDays, TOKEN_DAYS_DEFAULT } from './tokens'
import { pageView } from './routes'

// 2026-10-02T12:00Z: day 274.
const NOW = EPOCH + 274 * 86400 + 12 * 3600
const page = (p: string) => new URL(`https://site.example.org${p}`)
const VIEW = { path: 'marin-a/ckpt', d: '261002' }

describe('D1', () => {
  describe('og_tokens: the row is the whole truth (foreign keys on)', () => {
    it('a mint is good for exactly its view until its expiry day ends; revoking one mint leaves the others', async () => {
      const { db } = await sqliteD1()
      const m = await mint(db, u => pageView(u, 'marin GCS'), page('/marin-a/ckpt?d=261002&n=50&og=stale'), 'ann@example.org', NOW, 30, 'TokAAAAAAA')
      const m2 = await mint(db, u => pageView(u, 'marin GCS'), page('/marin-a/ckpt?d=261002'), 'bo@example.org', NOW + 60, 30, 'TokBBBBBBB')
      expect([m, m2]).toEqual([
        { token: 'TokAAAAAAA', url: 'https://site.example.org/marin-a/ckpt?d=261002&n=50&og=TokAAAAAAA', expDay: 304 },
        { token: 'TokBBBBBBB', url: 'https://site.example.org/marin-a/ckpt?d=261002&og=TokBBBBBBB', expDay: 304 },
      ])
      const end304 = EPOCH + 305 * 86400
      expect([
        await fullTier(db, 'map', VIEW, 'TokAAAAAAA', NOW),
        await fullTier(db, 'map', { ...VIEW, path: 'marin-a/ckpt/run-1' }, 'TokAAAAAAA', NOW),
        await fullTier(db, 'map', { ...VIEW, path: 'marin-a' }, 'TokAAAAAAA', NOW),
        await fullTier(db, 'map', { ...VIEW, f: 'x' }, 'TokAAAAAAA', NOW),
        await fullTier(db, 'staged', VIEW, 'TokAAAAAAA', NOW),
        await fullTier(db, 'map', VIEW, 'TokAAAAAAA', end304 - 1),
        await fullTier(db, 'map', VIEW, 'TokAAAAAAA', end304),
        await fullTier(db, 'map', VIEW, 'TokCCCCCCC', NOW),
        await fullTier(db, 'map', VIEW, 'TokAAAAAA-', NOW),
        await fullTier(db, 'map', VIEW, null, NOW),
      ]).toEqual([{ day: 304 }, null, null, null, null, { day: 304 }, null, null, null, null])
      expect([await revoke(db, 'TokAAAAAAA', 'admin@example.org', NOW + 120), await revoke(db, 'TokAAAAAAA', 'admin@example.org', NOW + 180)]).toEqual([1, 0])
      expect([await fullTier(db, 'map', VIEW, 'TokAAAAAAA', NOW), await fullTier(db, 'map', VIEW, 'TokBBBBBBB', NOW)]).toEqual([null, { day: 304 }])
      expect((await listTokens(db)).map(r => [r.token, r.minted_by, r.page, r.exp_day, r.revoked_by])).toEqual([
        ['TokBBBBBBB', 'bo@example.org', '/marin-a/ckpt?d=261002', 304, null],
        ['TokAAAAAAA', 'ann@example.org', '/marin-a/ckpt?d=261002&n=50', 304, 'admin@example.org'],
      ])
    })
    it('a non-admin revokes only their own mints', async () => {
      const { db } = await sqliteD1()
      await mint(db, u => pageView(u, 'marin GCS'), page('/marin-a'), 'ann@example.org', NOW, 30, 'TokAAAAAAA')
      await mint(db, u => pageView(u, 'marin GCS'), page('/marin-a'), 'bo@example.org', NOW, 30, 'TokBBBBBBB')
      expect([
        await revoke(db, 'TokBBBBBBB', 'ann@example.org', NOW, 'ann@example.org'),
        await revoke(db, 'TokAAAAAAA', 'ann@example.org', NOW, 'ann@example.org'),
        await fullTier(db, 'map', { path: 'marin-a' }, 'TokAAAAAAA', NOW),
        await fullTier(db, 'map', { path: 'marin-a' }, 'TokBBBBBBB', NOW),
      ]).toEqual([0, 1, null, { day: 304 }])
    })
    it('each mint is a fresh random token', async () => {
      const { db } = await sqliteD1()
      const a = await mint(db, u => pageView(u, 'marin GCS'), page('/marin-a'), 'ann@example.org', NOW)
      const b = await mint(db, u => pageView(u, 'marin GCS'), page('/marin-a'), 'ann@example.org', NOW)
      expect([/^[a-z0-9]{12}$/.test(a!.token), /^[a-z0-9]{12}$/.test(b!.token), a!.token === b!.token]).toEqual([true, true, false])
    })
    it('a token works whatever case the link arrives in (Slack lowercases URLs), a 10-char mixed-case one too, and revokes so', async () => {
      const { db } = await sqliteD1()
      const view = (u: URL) => pageView(u, 'marin GCS')
      const legacy = (await mint(db, view, page('/marin-a'), 'ann@example.org', NOW, 30, 'VhdAbC1234'))!
      const fresh = (await mint(db, view, page('/marin-b'), 'ann@example.org', NOW))!
      const tier = (path: string, t: string) => fullTier(db, 'map', { path }, t, NOW).then(r => r != null)
      expect(await Promise.all([
        tier('marin-a', 'VhdAbC1234'), tier('marin-a', 'vhdabc1234'), tier('marin-a', 'VHDABC1234'),
        tier('marin-b', fresh.token), tier('marin-b', fresh.token.toUpperCase()),
      ])).toEqual([true, true, true, true, true])
      expect([legacy.token, (await revoke(db, 'vhdabc1234', 'admin@example.org', NOW)), await tier('marin-a', 'VhdAbC1234')])
        .toEqual(['VhdAbC1234', 1, false])
    })
    it('pages without a card mint nothing', async () => {
      const { db } = await sqliteD1()
      expect(await mint(db, u => pageView(u, 'marin GCS'), page('/api/subtree'), 'ann@example.org', NOW)).toBe(null)
    })
  })

  describe('shareKeyLive: a `key=` share link earns the full card iff its bearer would get in', () => {
    it('the gate\'s presented-token rule (a used-up redeem cap still serves a bearer); a viewer scope; nothing redeemed or touched', async () => {
      const { db, raw } = await sqliteD1()
      const K = (s: string) => `${s}-share-link-key-000000`
      const grant = async (name: string, cols: Record<string, string | number> = {}) => {
        const row: Record<string, string | number> = { id: name, token_hash: await hashToken(K(name)), scopes: 'gcs:read', created_at: 1, created_by: 'admin@example.org', ...cols }
        const ks = Object.keys(row)
        raw.prepare(`INSERT INTO grants (${ks.join(',')}) VALUES (${ks.map(() => '?').join(',')})`).run(...Object.values(row))
      }
      await grant('live')
      await grant('viewer', { scopes: 'gcs' })
      await grant('revoked', { revoked_at: NOW - 1 })
      await grant('disabled', { disabled_at: NOW - 1 })
      await grant('expired', { expires_at: NOW - 1 })
      await grant('used-up', { max_redeems: 1, redeems: 1 })
      await grant('other-scope', { scopes: 'cw:read' })
      const live = (k: string | null) => shareKeyLive(db, k, ['gcs', 'gcs:read'], NOW)
      expect([
        await live(K('live')), await live(K('viewer')), await live(K('revoked')), await live(K('disabled')), await live(K('expired')),
        await live(K('used-up')), await live(K('other-scope')), await live(K('unknown')), await live(null), await live('short'),
      ]).toEqual([true, true, false, false, false, true, false, false, false, false])
      expect(raw.prepare('SELECT SUM(redeems) AS r, COUNT(last_used_at) AS used FROM grants').all()).toEqual([{ r: 1, used: 0 }])
    })
    it('a secondary store\'s own scope (`need`): the link must carry it too, as its reads demand', async () => {
      const { db, raw } = await sqliteD1()
      const K = (s: string) => `${s}-share-link-key-000000`
      for (const [id, scopes] of [['viewer', 'gcs'], ['staff', 'gcs,admin'], ['star', '*'], ['admin-only', 'admin']]) {
        raw.prepare('INSERT INTO grants (id, token_hash, scopes, created_at, created_by) VALUES (?, ?, ?, 1, ?)').run(id, await hashToken(K(id)), scopes, 'admin@example.org')
      }
      const grant = (k: string, need?: string) => shareKeyGrant(db, K(k), ['gcs', 'gcs:read'], NOW, need)
      expect([
        await grant('viewer'), await grant('viewer', 'admin'), await grant('staff', 'admin'), await grant('star', 'admin'), await grant('admin-only', 'admin'),
        await grantLive(db, 'viewer', ['gcs', 'gcs:read'], NOW, 'admin'), await grantLive(db, 'staff', ['gcs', 'gcs:read'], NOW, 'admin'),
      ]).toEqual(['viewer', null, 'staff', 'star', null, false, true])
    })
  })

  describe('share-link expiry (any length, or never)', () => {
    const now = Date.UTC(2026, 9, 13) / 1000

    async function minted(days: number, token: string) {
      const { db } = await sqliteD1()
      const m = (await mint(db, u => pageView(u, 'Demo'), new URL('https://demo.example/bucket'), 'demo', now, days, token))!
      const view = pageView(new URL(m.url), 'Demo')!
      return { m, valid: (t: number) => fullTier(db, view.kind, view.params, m.token, t) }
    }

    it('a request body\'s days: 0 means never, any positive whole number is kept, anything else is the default', () => {
      expect([0, 1, 7.9, 90, 365, 3650, -1, NaN, '30', null, undefined].map(tokenDays))
        .toEqual([0, 1, 7, 90, 365, 3650, TOKEN_DAYS_DEFAULT, TOKEN_DAYS_DEFAULT, TOKEN_DAYS_DEFAULT, TOKEN_DAYS_DEFAULT, TOKEN_DAYS_DEFAULT])
    })

    it('a token minted for longer than 90 days lasts that long', async () => {
      const { m, valid } = await minted(365, 'LongTokAAA')
      expect([m.expDay, await valid(now + 364 * DAY_S), await valid(now + 366 * DAY_S)])
        .toEqual([expDay(now, 365), { day: expDay(now, 365) }, null])
    })

    it('a token minted with days 0 never expires', async () => {
      const { m, valid } = await minted(0, 'NeverTokAA')
      expect([m.expDay, await valid(now + 100 * 365 * DAY_S)]).toEqual([NEVER_DAY, { day: NEVER_DAY }])
    })
  })
})
