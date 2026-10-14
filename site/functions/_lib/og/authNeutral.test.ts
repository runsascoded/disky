/** `og=` is a link-preview token only: it must never change who a request is.
 * Every request shape goes through the real gate twice (fresh databases),
 * with and without a valid-looking `og=`, and must come out identical. */
import { emailSub, hashToken, sessionCookie, signSession } from '@open-athena/auth'
import { describe, expect, it } from 'vitest'
import { type Env, requireViewer } from '../auth'
import { LINEAGES, sqliteD1 } from '../testD1'
import { EPOCH } from './sign'

const SECRET = 'test-session-secret-0123456789abcdef'
const KEY = 'live-share-link-key-0123456789'
const OG = 'TokAAAAAAA'

describe.each(LINEAGES)('D1, %s lineage', lineage => {
  async function envWith(): Promise<Env> {
    const { db, raw } = await sqliteD1(lineage)
    raw.prepare("INSERT INTO allowed_emails (email, who, ts) VALUES ('viewer@example.org', 'admin@example.org', 1)").run()
    raw.prepare('INSERT INTO grants (id, token_hash, scopes, created_at, created_by) VALUES (?, ?, ?, ?, ?)').run('g1', await hashToken(KEY), 'gcs:read', 1, 'admin@example.org')
    // A live preview token for the requested view, so `og=` is a real one.
    raw.prepare('INSERT INTO og_tokens (token, kind, view, page, minted_by, minted_ts, exp_day) VALUES (?, ?, ?, ?, ?, ?, ?)').run(OG, 'map', 'path=marin-a', '/marin-a', 'viewer@example.org', 1, Math.floor((Date.now() / 1000 - EPOCH) / 86400) + 30)
    return { DB: db, SESSION_SECRET: SECRET, BASE_SCOPE: 'gcs', STAFF_DOMAIN: 'openathena.ai' } as Env
  }

  /** The auth outcome: the identity (sans the volatile subject) or the status. */
  async function outcome(url: string, headers: Record<string, string> = {}): Promise<unknown> {
    const r = await requireViewer({ request: new Request(url, { headers }), env: await envWith() })
    if (r instanceof Response) return { status: r.status, body: await r.json() }
    const { subject: _s, ...id } = r
    return id
  }

  describe('og= has no effect on site auth', () => {
    it('same outcome with and without og=, for every way of (not) being signed in', async () => {
      const og = OG
      const cookie = sessionCookie(await signSession(emailSub('viewer@example.org'), SECRET, Date.now(), 3600), { name: 'oa_auth', ttlS: 3600, secure: true }).split(';')[0]
      const shapes: [string, string, Record<string, string>?][] = [
        ['anonymous', 'https://gcs.example.org/api/subtree?path=marin-a'],
        ['share key', `https://gcs.example.org/api/subtree?path=marin-a&key=${KEY}`],
        ['bad share key', 'https://gcs.example.org/api/subtree?path=marin-a&key=not-a-real-key-000000'],
        ['bearer', 'https://gcs.example.org/api/subtree?path=marin-a', { authorization: `Bearer ${KEY}` }],
        ['session', 'https://gcs.example.org/marin-a', { cookie }],
      ]
      const got: unknown[] = []
      const want: unknown[] = []
      for (const [name, url, headers] of shapes) {
        want.push([name, await outcome(url, headers)])
        got.push([name, await outcome(`${url}${url.includes('?') ? '&' : '?'}og=${og}`, headers)])
      }
      expect(got).toEqual(want)
      // And the shapes really differ, so the comparison means something.
      expect(want.map(w => (w as [string, { via?: string; status?: number }])[1]).map(o => o.via ?? o.status)).toEqual([401, 'grant', 401, 'grant', 'session'])
    })
  })
})
