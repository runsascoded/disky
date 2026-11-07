/** Revocation reaches already-issued card images (the schema's
 * `og_tokens`, cw 0010 / gcs 0034): the page's full image URL names its credential, and every
 * fetch of it re-checks that credential. Before this, a full image URL stayed
 * full until its signed expiry whatever happened to the token. */
import { hashToken } from '@open-athena/auth'
import { describe, expect, it } from 'vitest'
import { stagedCardUrl } from '../stagedSlack'
import { sqliteD1 } from '../testD1'
import { imageParams, splitImageParams } from './cred'
import { EPOCH, expDay, IMAGE_TTL_DAYS, imagePath, ogKey, resolveImage } from './sign'
import { credLive, pageTier } from './tier'
import { mint, revoke } from './tokens'
import { pageView } from './routes'

const NOW = EPOCH + 274 * 86400 + 12 * 3600
const ORIGIN = 'https://site.example.org'

/** What a fetch of `image` serves now: the tier after the signature and the credential check. */
async function served(env: Parameters<typeof credLive>[0], key: CryptoKey, image: string, now = NOW): Promise<string> {
  const r = (await resolveImage(key, new URL(image), now))!
  if (r.tier !== 'full') return `anon (${r.why ?? 'unsigned'})`
  const { view, cred } = splitImageParams(r.params)
  return (await credLive(env, r.kind, view, cred, now)) ? 'full' : 'anon (revoked)'
}

/** The image a page fetch stamps (as `stampPage` does). */
async function stamped(env: Parameters<typeof credLive>[0], key: CryptoKey, page: string): Promise<string> {
  const url = new URL(page, ORIGIN)
  const params = { path: url.pathname.slice(1) }
  const { tier, day, cred } = await pageTier(env, 'map', params, url, NOW)
  return ORIGIN + await imagePath(key, 'map', imageParams(params, null, cred), tier, day)
}

describe('D1', () => {
  describe('revocation reaches issued card images', () => {
    it('og= token: mint → full; revoke → the same image URL serves anonymous', async () => {
      const { db } = await sqliteD1()
      const env = { DB: db, BASE_SCOPE: 'gcs' }
      const key = await ogKey('s3cret')
      const m = (await mint(db, u => pageView(u, 'marin GCS'), new URL(`${ORIGIN}/marin-a`), 'ann@example.org', NOW, 30, 'TokAAAAAAA'))!
      const image = await stamped(env, key, `/marin-a?og=${m.token}`)
      expect(image.replace(/sig=.*/, 'sig=…')).toBe(`${ORIGIN}/og/map.png?path=marin-a&t=TokAAAAAAA&sig=…`)
      const before = await served(env, key, image)
      await revoke(db, m.token, 'admin@example.org', NOW + 60)
      expect([before, await served(env, key, image), await stamped(env, key, `/marin-a?og=${m.token}`)])
        .toEqual(['full', 'anon (revoked)', `${ORIGIN}/og/map.png?path=marin-a`])
    })
    it('the credential is bound to the view: a token for one path can\'t back another path\'s image', async () => {
      const { db } = await sqliteD1()
      const env = { DB: db, BASE_SCOPE: 'gcs' }
      const key = await ogKey('s3cret')
      await mint(db, u => pageView(u, 'marin GCS'), new URL(`${ORIGIN}/marin-a`), 'ann@example.org', NOW, 30, 'TokAAAAAAA')
      // A validly signed full URL for marin-b naming marin-a's token (as if the signer were tricked).
      const forged = ORIGIN + await imagePath(key, 'map', { path: 'marin-b', t: 'TokAAAAAAA' }, 'full', expDay(NOW, IMAGE_TTL_DAYS))
      expect(await served(env, key, forged)).toBe('anon (revoked)')
    })
    it('key= share link: full while the grant lives; revoking the grant reverts its card', async () => {
      const { db, raw } = await sqliteD1()
      const env = { DB: db, BASE_SCOPE: 'gcs' }
      const key = await ogKey('s3cret')
      const K = 'live-share-link-key-0123456789'
      raw.prepare('INSERT INTO grants (id, token_hash, scopes, created_at, created_by) VALUES (?, ?, ?, ?, ?)').run('grant123', await hashToken(K), 'gcs:read', 1, 'admin@example.org')
      const image = await stamped(env, key, `/marin-a?key=${K}`)
      expect(image.replace(/sig=.*/, 'sig=…')).toBe(`${ORIGIN}/og/map.png?g=grant123&path=marin-a&sig=…`)
      const before = await served(env, key, image)
      raw.prepare('UPDATE grants SET revoked_at = ? WHERE id = ?').run(NOW, 'grant123')
      expect([before, await served(env, key, image)]).toEqual(['full', 'anon (revoked)'])
    })
    it('the staged Slack card is backed by one reusable slack:staged row; revoking it reverts the card', async () => {
      const { db, raw } = await sqliteD1()
      const env = { DB: db, BASE_SCOPE: 'gcs', OG_CARDS: '1', SESSION_SECRET: 's3cret' }
      const key = await ogKey('s3cret')
      const a = (await stagedCardUrl(env, db, ORIGIN, 'abcdef0123456789', NOW))!
      const b = (await stagedCardUrl(env, db, ORIGIN, '0123456789abcdef', NOW + 3600))!
      const rows = raw.prepare("SELECT token, kind, view, page, minted_by FROM og_tokens").all()
      const tok = (rows[0] as { token: string }).token
      expect([
        a.replace(/sig=.*/, 'sig=…'), b.replace(/sig=.*/, 'sig=…'),
        rows.map(r => ({ ...r, token: '…' })),
        await served(env, key, a),
      ]).toEqual([
        `${ORIGIN}/og/staged.png?t=${tok}&v=abcdef01&sig=…`,
        `${ORIGIN}/og/staged.png?t=${tok}&v=01234567&sig=…`,
        [{ token: '…', kind: 'staged', view: '', page: '/staged', minted_by: 'slack:staged' }],
        'full',
      ])
      await revoke(db, tok, 'admin@example.org', NOW + 7200)
      const c = (await stagedCardUrl(env, db, ORIGIN, 'abcdef0123456789', NOW + 7200))!
      expect([await served(env, key, a), c.includes(`t=${tok}`), await served(env, key, c)]).toEqual(['anon (revoked)', false, 'full'])
    })
  })
})
