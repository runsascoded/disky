/** `?next=` through the sign-in routes, to the `Location` a browser follows: an emailed link's landing, and a declined
 *  OIDC attempt's return (the state signed by the start, read back by the callback). A dead link's landing is `/signin`,
 *  whose own `next` (`site/src/signInNext.ts`) reads this value. */
import { describe, expect, it } from 'vitest'
import type { Env } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { onRequest as email } from './email/[[path]]'
import { onRequest as oaStart } from './oa'
import { onRequest as oaCallback } from './oa/callback'

const SITE = 'https://site.example.test'
const envOf = async (): Promise<Env> => ({
  DB: (await sqliteD1()).db,
  SESSION_SECRET: 's'.repeat(32),
  RESEND_API_KEY: 'unused',
  MAIL_FROM: 'noreply@example.test',
  OA_LOGIN_ISSUER: 'https://login.example.test',
  OA_LOGIN_CLIENT_ID: 'disky-test',
  OA_LOGIN_CLIENT_SECRET: 'shh',
}) as unknown as Env
const ctx = (env: Env, path: string, cookie?: string) => ({ request: new Request(`${SITE}${path}`, cookie ? { headers: { cookie } } : {}), env }) as never

const verify = async (next: string): Promise<string | null> =>
  (await email(ctx(await envOf(), `/auth/email/verify?token=none&next=${encodeURIComponent(next)}`))).headers.get('location')

async function declined(next: string): Promise<string | null> {
  const env = await envOf()
  const start = await oaStart(ctx(env, `/auth/oa?prompt=none&next=${encodeURIComponent(next)}`))
  const state = new URL(start.headers.get('location')!).searchParams.get('state')!
  const nonce = start.headers.getSetCookie().find(c => c.startsWith('oa_login_nonce='))!.split(';')[0]
  return (await oaCallback(ctx(env, `/auth/oa/callback?error=login_required&state=${encodeURIComponent(state)}`, `oa_sso_skip=attempt; ${nonce}`))).headers.get('location')
}

const HOSTILE = [
  ['a backslash', '/\\example.test'],
  ['a tab', '/\t/example.test'],
  ['dot segments that resolve to `//`', '/x/..//example.test'],
  ['two slashes', '//example.test'],
  ['an absolute URL', 'https://example.test/x'],
]

describe('`next` off this origin goes home', () => {
  it.each(HOSTILE)('an emailed link, %s', async (_, next) => {
    expect(await verify(next)).toBe('/signin?error=link&next=%2F')
  })
  it.each(HOSTILE)('a declined OIDC attempt, %s', async (_, next) => {
    expect(await declined(next)).toBe('/?sso=login_required')
  })
})

describe('a path on this origin survives with its query', () => {
  it('an emailed link', async () => {
    expect(await verify('/scans?d=2026-11-01&p=a%2Fb')).toBe('/signin?error=link&next=%2Fscans%3Fd%3D2026-11-01%26p%3Da%252Fb')
  })
  it('a declined OIDC attempt', async () => {
    expect(await declined('/scans?d=2026-11-01&p=a%2Fb')).toBe('/scans?d=2026-11-01&p=a%2Fb&sso=login_required')
  })
})
