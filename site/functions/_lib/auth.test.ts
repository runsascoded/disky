import { describe, expect, it } from 'vitest'
import { ADMIN_SCOPE, allowedRow, baseScope, type Ctx, type Env, identify, isAdmin, requireScope, scopesFor } from './auth'

// A non-localhost request, so identify() doesn't take the dev short-circuit
// (which would return a full-scope admin regardless of PUBLIC_READ).
const ctxOf = (env: Partial<Env>): Ctx => ({
  request: new Request('https://r2.rbw.sh/api/subtree'),
  env: env as Env,
})

describe('requireScope — PUBLIC_READ (public/no-gate deploys)', () => {
  it('grants the base viewer scope anonymously — reads open', async () => {
    const env: Partial<Env> = { PUBLIC_READ: '1', BASE_SCOPE: 'r2' }
    const id = await requireScope(ctxOf(env), baseScope(env as Env))
    expect(id).toEqual({ email: null, name: null, scopes: ['r2'], admin: false, via: 'public', subject: null })
  })

  it('still gates a non-base scope — mutations/admin stay closed', async () => {
    const res = await requireScope(ctxOf({ PUBLIC_READ: '1', BASE_SCOPE: 'r2' }), ADMIN_SCOPE)
    expect(res).toBeInstanceOf(Response)
    expect((res as Response).status).toBe(401)
  })

  it('without PUBLIC_READ, the base scope is closed by default', async () => {
    const env: Partial<Env> = { BASE_SCOPE: 'r2' }
    const res = await requireScope(ctxOf(env), baseScope(env as Env))
    expect(res).toBeInstanceOf(Response)
    expect((res as Response).status).toBe(401)
  })
})

// A D1 stand-in for the two policy lookups: `allowed_emails` and `admin_emails`
// rows, keyed by table. Only the `prepare().bind().first()` shape the policy
// uses is modelled; any other SQL is a test bug.
const db = (rows: { allowed?: string[]; admin?: string[] }) => ({
  prepare: (sql: string) => {
    const table = /FROM (\w+)/.exec(sql)?.[1]
    const have = table === 'allowed_emails' ? rows.allowed ?? [] : table === 'admin_emails' ? rows.admin ?? [] : null
    if (have === null) throw new Error(`unexpected SQL in policy: ${sql}`)
    return { bind: (email: string) => ({ first: async () => (have.includes(email) ? { email } : null) }) }
  },
}) as unknown as Env['DB']

const cw = (rows: { allowed?: string[]; admin?: string[] }, extra: Partial<Env> = {}): Env => ({
  DB: db(rows),
  BASE_SCOPE: 'cw',
  STAFF_DOMAIN: 'openathena.ai',
  VIEWER_DOMAINS: 'coreweave.com',
  ADMIN_EMAILS: '1',
  ...extra,
})

describe('scopesFor — the in-app policy that replaces the Access policy', () => {
  it('staff get every scope', async () => {
    expect(await scopesFor(cw({}))('ryan@openathena.ai')).toEqual(['gcs', 'cw', 'admin', 'requests'])
  })

  it("staff on a store other than gcs/cw also get that store's base scope", async () => {
    const env = cw({}, { BASE_SCOPE: 'laptop', STAFF_DOMAIN: 'runsascoded.com' })
    expect(await scopesFor(env)('ryan@runsascoded.com')).toEqual(['gcs', 'cw', 'admin', 'requests', 'laptop'])
  })

  it('no STAFF_DOMAIN (unset or empty): nobody is staff by domain', async () => {
    for (const STAFF_DOMAIN of [undefined, '']) {
      const env = cw({}, { STAFF_DOMAIN })
      expect(await scopesFor(env)('ryan@openathena.ai')).toBe(null)
      expect(await scopesFor(env)('anyone@example.com')).toBe(null)
      expect(await isAdmin(env, 'ryan@openathena.ai')).toBe(false)
    }
  })

  it('a viewer domain gets the base scope with no allowlist row', async () => {
    expect(await scopesFor(cw({}))('someone@coreweave.com')).toEqual(['cw'])
  })

  it('a viewer-domain admin_emails row adds admin', async () => {
    expect(await scopesFor(cw({ admin: ['ops@coreweave.com'] }))('ops@coreweave.com')).toEqual(['cw', 'admin'])
  })

  it('an allowed_emails row admits any other address, lower-cased', async () => {
    expect(await scopesFor(cw({ allowed: ['guest@example.org'] }))('Guest@Example.org')).toEqual(['cw'])
  })

  it('an unlisted address from an unlisted domain is denied', async () => {
    expect(await scopesFor(cw({ allowed: ['guest@example.org'] }))('other@example.org')).toBeNull()
  })

  it('without ADMIN_EMAILS the admin table is never consulted (gcs shape)', async () => {
    const env = cw({ allowed: ['guest@example.org'] }, { ADMIN_EMAILS: undefined, VIEWER_DOMAINS: undefined, BASE_SCOPE: 'gcs' })
    expect(await scopesFor(env)('guest@example.org')).toEqual(['gcs'])
    expect(await scopesFor(env)('someone@coreweave.com')).toBeNull()
  })

  it('no DB (local dev) admits non-staff to the base scope', async () => {
    expect(await scopesFor({ BASE_SCOPE: 'cw' })('anyone@example.org')).toEqual(['cw'])
  })
})

describe('isAdmin', () => {
  it('staff by domain, otherwise the admin_emails row where the table exists', async () => {
    expect(await isAdmin(cw({}), 'ryan@openathena.ai')).toBe(true)
    expect(await isAdmin(cw({ admin: ['ops@coreweave.com'] }), 'Ops@coreweave.com')).toBe(true)
    expect(await isAdmin(cw({ admin: ['ops@coreweave.com'] }), 'other@coreweave.com')).toBe(false)
    expect(await isAdmin(cw({ admin: ['ops@coreweave.com'] }, { ADMIN_EMAILS: undefined }), 'ops@coreweave.com')).toBe(false)
  })
})

describe('identify — without a gate there is no identity', () => {
  it('no session store and no session → anonymous (a stray Access header is just a header)', async () => {
    expect(await identify({
      request: new Request('https://cw-s3.oa.dev/api/whoami', { headers: { 'Cf-Access-Jwt-Assertion': 'not-a-jwt' } }),
      env: cw({}),
    })).toBeNull()
  })
})

describe('allowedRow — a D1 without the `read_only` migration', () => {
  it('still admits an allowlisted email, as a full viewer (sign-in never breaks on a dropped migration)', async () => {
    const { sqliteD1 } = await import('./testD1')
    const { db, raw } = await sqliteD1('cw', { before: '0009_allowlist_read_only.sql' })
    raw.exec("INSERT INTO allowed_emails (email, note, who, ts) VALUES ('guest@example.org', NULL, 'admin', 1)")
    expect([await allowedRow(db, 'guest@example.org'), await allowedRow(db, 'nobody@example.org')]).toEqual([{ read_only: 0 }, null])
    const env = { DB: db, BASE_SCOPE: 'cw', STAFF_DOMAIN: 'openathena.ai' } as Env
    expect(await scopesFor(env)('Guest@example.org')).toEqual(['cw'])
  })
})

