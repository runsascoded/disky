import { describe, expect, it } from 'vitest'
import { ADMIN_SCOPE, adminRoster, allowedRow, baseScope, type Ctx, type Env, identify, isAdmin, requireScope, scopesFor } from './auth'
import { sqliteD1, withAdminEmails } from './testD1'

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
  STAFF_DOMAIN: 'staff.example.test',
  VIEWER_DOMAINS: 'viewer.example.test',
  ADMIN_EMAILS: '1',
  ...extra,
})

describe('scopesFor — the in-app policy that replaces the Access policy', () => {
  it('staff get every scope', async () => {
    expect(await scopesFor(cw({}))('ryan@staff.example.test')).toEqual(['gcs', 'cw', 'admin', 'cw:assign'])
  })

  it("staff on a store other than gcs/cw also get that store's base scope", async () => {
    const env = cw({}, { BASE_SCOPE: 'laptop', STAFF_DOMAIN: 'lab.example.test' })
    expect(await scopesFor(env)('alice@lab.example.test')).toEqual(['gcs', 'cw', 'admin', 'laptop', 'laptop:assign'])
  })

  it('no STAFF_DOMAIN (unset or empty): nobody is staff by domain', async () => {
    for (const STAFF_DOMAIN of [undefined, '']) {
      const env = cw({}, { STAFF_DOMAIN })
      expect(await scopesFor(env)('ryan@staff.example.test')).toBe(null)
      expect(await scopesFor(env)('anyone@example.com')).toBe(null)
      expect(await isAdmin(env, 'ryan@staff.example.test')).toBe(false)
    }
  })

  it('a viewer domain gets the base scope + assign with no allowlist row', async () => {
    expect(await scopesFor(cw({}))('someone@viewer.example.test')).toEqual(['cw', 'cw:assign'])
  })

  it('a viewer-domain admin_emails row adds admin', async () => {
    expect(await scopesFor(cw({ admin: ['ops@viewer.example.test'] }))('ops@viewer.example.test')).toEqual(['cw', 'cw:assign', 'admin'])
  })

  it('an allowed_emails row admits any other address, lower-cased', async () => {
    expect(await scopesFor(cw({ allowed: ['guest@example.org'] }))('Guest@Example.org')).toEqual(['cw', 'cw:assign'])
  })

  it('an unlisted address from an unlisted domain is denied', async () => {
    expect(await scopesFor(cw({ allowed: ['guest@example.org'] }))('other@example.org')).toBeNull()
  })

  it('without ADMIN_EMAILS the admin table is never consulted (gcs shape)', async () => {
    const env = cw({ allowed: ['guest@example.org'] }, { ADMIN_EMAILS: undefined, VIEWER_DOMAINS: undefined, BASE_SCOPE: 'gcs' })
    expect(await scopesFor(env)('guest@example.org')).toEqual(['gcs', 'gcs:assign'])
    expect(await scopesFor(env)('someone@viewer.example.test')).toBeNull()
  })

  it('no DB (local dev) admits non-staff to the base scope + assign', async () => {
    expect(await scopesFor({ BASE_SCOPE: 'cw' })('anyone@example.org')).toEqual(['cw', 'cw:assign'])
  })
})

describe('isAdmin', () => {
  it('staff by domain, otherwise the admin_emails row where the table exists', async () => {
    expect(await isAdmin(cw({}), 'ryan@staff.example.test')).toBe(true)
    expect(await isAdmin(cw({ admin: ['ops@viewer.example.test'] }), 'Ops@viewer.example.test')).toBe(true)
    expect(await isAdmin(cw({ admin: ['ops@viewer.example.test'] }), 'other@viewer.example.test')).toBe(false)
    expect(await isAdmin(cw({ admin: ['ops@viewer.example.test'] }, { ADMIN_EMAILS: undefined }), 'ops@viewer.example.test')).toBe(false)
  })
})

// `ADMIN_LIST`: admin is the deployment's explicit list (`admin_emails` rows, plus the break-glass
// `ADMIN_BOOTSTRAP`), and the staff domain alone is not.
describe('ADMIN_LIST — admin is the explicit list, not the staff domain', () => {
  const listed = (rows: { allowed?: string[]; admin?: string[] }, extra: Partial<Env> = {}): Env =>
    cw(rows, { BASE_SCOPE: 'gcs', STAFF_DOMAIN: 'staff.example.test', VIEWER_DOMAINS: undefined, ADMIN_EMAILS: undefined, ADMIN_LIST: '1', ...extra })

  it('unlisted staff keep every scope but `admin`', async () => {
    expect(await scopesFor(listed({ admin: ['lead@staff.example.test'] }))('dev@staff.example.test')).toEqual(['gcs', 'cw', 'gcs:assign'])
  })

  it('listed staff get every scope, lower-cased', async () => {
    expect(await scopesFor(listed({ admin: ['lead@staff.example.test'] }))('Lead@Staff.Example.test')).toEqual(['gcs', 'cw', 'admin', 'gcs:assign'])
  })

  it('a listed viewer gets `admin` on top of the viewer scopes; an unlisted one does not', async () => {
    const env = listed({ allowed: ['owner@example.org', 'guest@example.org'], admin: ['owner@example.org'] })
    expect([await scopesFor(env)('owner@example.org'), await scopesFor(env)('guest@example.org')]).toEqual([['gcs', 'gcs:assign', 'admin'], ['gcs', 'gcs:assign']])
  })

  it('a listed address is admitted by the list itself (no allowlist row, no viewer domain)', async () => {
    const env = listed({ admin: ['owner@example.org'] })
    expect([await scopesFor(env)('owner@example.org'), await scopesFor(env)('other@example.org')]).toEqual([['gcs', 'gcs:assign', 'admin'], null])
  })

  it('`ADMIN_BOOTSTRAP` (break-glass) names admins whatever the list holds: admitted, admin, case-insensitive', async () => {
    const env = listed({}, { ADMIN_BOOTSTRAP: 'Owner@Example.org, lead@staff.example.test' })
    expect([await scopesFor(env)('owner@example.org'), await scopesFor(env)('LEAD@staff.example.test'), await scopesFor(env)('dev@staff.example.test'), await scopesFor(env)('other@example.org')]).toEqual([
      ['gcs', 'gcs:assign', 'admin'],
      ['gcs', 'cw', 'admin', 'gcs:assign'],
      ['gcs', 'cw', 'gcs:assign'],
      null,
    ])
  })

  it('`ADMIN_BOOTSTRAP` without `ADMIN_LIST` names nobody (other deployments are unchanged)', async () => {
    const env = listed({}, { ADMIN_LIST: undefined, ADMIN_BOOTSTRAP: 'owner@example.org' })
    expect([await scopesFor(env)('owner@example.org'), await isAdmin(env, 'owner@example.org'), await scopesFor(env)('dev@staff.example.test')]).toEqual([null, false, ['gcs', 'cw', 'admin', 'gcs:assign']])
  })

  it('an empty list and no break-glass: nobody is an admin, and staff are not a fallback', async () => {
    const env = listed({})
    expect([await isAdmin(env, 'dev@staff.example.test'), await scopesFor(env)('dev@staff.example.test')]).toEqual([false, ['gcs', 'cw', 'gcs:assign']])
  })

  it('isAdmin: a row or the break-glass, never the domain', async () => {
    const env = listed({ admin: ['owner@example.org'] }, { ADMIN_BOOTSTRAP: 'lead@staff.example.test' })
    expect(await Promise.all(['Owner@example.org', 'lead@staff.example.test', 'dev@staff.example.test', 'other@example.org'].map(e => isAdmin(env, e)))).toEqual([true, true, false, false])
  })

  describe('D1', () => {
    const d1Env = (db: Env['DB'], extra: Partial<Env> = {}): Env => ({ DB: db, BASE_SCOPE: 'gcs', STAFF_DOMAIN: 'staff.example.test', ADMIN_LIST: '1', ...extra })

    it('a database without `admin_emails`: sign-in still works, nobody is an admin by the list, the break-glass still is', async () => {
      const { db, raw } = await sqliteD1()
      raw.exec('DROP TABLE IF EXISTS admin_emails')
      const env = d1Env(db, { ADMIN_BOOTSTRAP: 'lead@staff.example.test' })
      expect([await scopesFor(env)('dev@staff.example.test'), await isAdmin(env, 'dev@staff.example.test'), await scopesFor(env)('lead@staff.example.test')]).toEqual([
        ['gcs', 'cw', 'gcs:assign'], false, ['gcs', 'cw', 'admin', 'gcs:assign'],
      ])
      expect(await adminRoster(env)).toEqual({ list: true, staffDomain: null, table: 'missing', handles: false, rows: [], bootstrap: ['lead@staff.example.test'], none: false })
    })

    it('adminRoster: the rows with who added them and when, and `none` when the list is on with nobody on it', async () => {
      const { db, raw } = await sqliteD1()
      withAdminEmails(raw)
      expect(await adminRoster(d1Env(db))).toEqual({ list: true, staffDomain: null, table: 'ok', handles: true, rows: [], bootstrap: [], none: true })
      raw.exec("INSERT INTO admin_emails (email, note, who, ts, github) VALUES ('owner@example.org', 'org owner', 'lead@staff.example.test', 7, 'octo-owner'), ('lead@staff.example.test', NULL, 'seed', 3, NULL)")
      const rows = [{ email: 'lead@staff.example.test', note: null, who: 'seed', ts: 3, github: null }, { email: 'owner@example.org', note: 'org owner', who: 'lead@staff.example.test', ts: 7, github: 'octo-owner' }]
      expect(await adminRoster(d1Env(db))).toEqual({ list: true, staffDomain: null, table: 'ok', handles: true, bootstrap: [], none: false, rows })
      // The list off: the staff domain is the admin rule, the table (where `ADMIN_EMAILS`) adds to it.
      expect(await adminRoster(d1Env(db, { ADMIN_LIST: undefined, ADMIN_EMAILS: '1' }))).toEqual({ list: false, staffDomain: 'staff.example.test', table: 'ok', handles: true, bootstrap: [], none: false, rows })
      expect(await adminRoster(d1Env(db, { ADMIN_LIST: undefined }))).toEqual({ list: false, staffDomain: 'staff.example.test', table: 'unused', handles: false, rows: [], bootstrap: [], none: false })
    })

    it('adminRoster: a table from before the `github` column lists the same admins, with no handles', async () => {
      const { db, raw } = await sqliteD1()
      raw.exec('DROP TABLE IF EXISTS admin_emails')
      raw.exec('CREATE TABLE admin_emails (email TEXT PRIMARY KEY, note TEXT, who TEXT NOT NULL, ts INTEGER NOT NULL)')
      raw.exec("INSERT INTO admin_emails (email, note, who, ts) VALUES ('owner@example.org', NULL, 'seed', 7)")
      expect([await adminRoster(d1Env(db)), await isAdmin(d1Env(db), 'owner@example.org')]).toEqual([
        { list: true, staffDomain: null, table: 'ok', handles: false, bootstrap: [], none: false, rows: [{ email: 'owner@example.org', note: null, who: 'seed', ts: 7, github: null }] },
        true,
      ])
    })
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

describe('D1', () => {
  describe('allowedRow — a D1 without `allowed_emails.read_only`', () => {
    it('still admits an allowlisted email, as a full viewer (sign-in never breaks on a missing column)', async () => {
      const { db, raw } = await sqliteD1()
      raw.exec('ALTER TABLE allowed_emails DROP COLUMN read_only')
      raw.exec("INSERT INTO allowed_emails (email, note, who, ts) VALUES ('guest@example.org', NULL, 'admin', 1)")
      expect([await allowedRow(db, 'guest@example.org'), await allowedRow(db, 'nobody@example.org')]).toEqual([{ read_only: 0 }, null])
      const env = { DB: db, BASE_SCOPE: 'cw', STAFF_DOMAIN: 'staff.example.test' } as Env
      expect(await scopesFor(env)('Guest@example.org')).toEqual(['cw', 'cw:assign'])
    })

    it('a `read_only` row is the read-only tier alone — no assign', async () => {
      const { db, raw } = await sqliteD1()
      raw.exec("INSERT INTO allowed_emails (email, note, who, ts, read_only) VALUES ('ro@example.org', NULL, 'admin', 1, 1)")
      const env = { DB: db, BASE_SCOPE: 'cw', STAFF_DOMAIN: 'staff.example.test' } as Env
      expect(await scopesFor(env)('ro@example.org')).toEqual(['cw:read'])
    })
  })
})
