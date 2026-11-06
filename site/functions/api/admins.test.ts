// `/api/admins`: every path to the `admin` scope, and the list's editor (add, remove, the log, the last-admin guard).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { type Env, gateFor } from '../_lib/auth'
import { type Sqlite, sqliteD1, withAdminEmails } from '../_lib/testD1'
import { onRequest } from './admins'

const SITE = 'https://gcs.example.test'
const NOW = 1_800_000_000
const [LEAD, OWNER, STAFF, GUEST] = ['lead@staff.example.test', 'owner@example.org', 'dev@staff.example.test', 'guest@example.org']

beforeEach(() => { vi.useFakeTimers({ toFake: ['Date'] }); vi.setSystemTime(NOW * 1000) })
afterEach(() => { vi.useRealTimers() })

/** A deployment whose admins are the list (`extra` overrides), `admins` on it, `GUEST` and `OWNER` allowlisted. */
async function world(admins: string[], extra: Partial<Env> = {}, table = true) {
  const { db, raw } = await sqliteD1()
  if (table) {
    withAdminEmails(raw)
    for (const [i, e] of admins.entries()) raw.prepare("INSERT INTO admin_emails (email, note, who, ts) VALUES (?, NULL, 'seed', ?)").run(e, i + 1)
  } else raw.exec('DROP TABLE IF EXISTS admin_emails')
  for (const e of [GUEST, OWNER]) raw.prepare("INSERT INTO allowed_emails (email, note, who, ts) VALUES (?, NULL, 'seed', 1)").run(e)
  const env = { DB: db, SESSION_SECRET: 'secret', BASE_SCOPE: 'gcs', STAFF_DOMAIN: 'staff.example.test', ADMIN_LIST: '1', ...extra } as Env
  const gate = gateFor(env)!
  /** A request as `email`'s session (null: signed out), or with a Bearer `token`. */
  const call = async (as: string | null | { token: string }, method = 'GET', query = '', body?: unknown): Promise<[number, unknown]> => {
    const headers: Record<string, string> = body === undefined ? {} : { 'content-type': 'application/json' }
    if (typeof as === 'string') headers.cookie = (await gate.signIn(as, new Request(SITE)))!.cookie.split(';')[0]
    else if (as) headers.authorization = `Bearer ${as.token}`
    const res = await onRequest({ request: new Request(`${SITE}/api/admins${query}`, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) }), env })
    return [res.status, await res.json()]
  }
  return { env, raw, gate, call }
}

const rows = (raw: Sqlite) => raw.prepare('SELECT email, note, who, ts, github FROM admin_emails ORDER BY email').all().map(r => ({ ...r }))
const edits = (raw: Sqlite) => raw.prepare('SELECT tbl, pk, action, who, ts, old_json, new_json FROM admin_edits ORDER BY id').all().map(r => ({ ...r }))

describe('GET /api/admins', () => {
  it('an admin sees every admin path: the rows with who added whom and when, the break-glass, the admin grants', async () => {
    const w = await world([LEAD, OWNER], { ADMIN_BOOTSTRAP: 'Root@example.org' })
    const link = await w.gate.mint({ email: null, scopes: ['gcs', 'admin'], name: 'ops link', createdBy: LEAD, expiresAt: null, maxRedeems: null })
    await w.gate.mint({ email: GUEST, scopes: ['gcs'], name: 'guest link', createdBy: LEAD, expiresAt: null, maxRedeems: null })
    const dead = await w.gate.mint({ email: null, scopes: ['*'], name: 'revoked link', createdBy: LEAD, expiresAt: null, maxRedeems: null })
    await w.gate.revoke(dead.grant.id)
    await w.gate.mint({ email: null, scopes: ['admin'], name: 'expired link', createdBy: LEAD, expiresAt: NOW - 1, maxRedeems: null })
    expect(await w.call(OWNER)).toEqual([200, {
      list: true, staffDomain: null, table: 'ok', none: false, you: OWNER,
      rows: [{ email: LEAD, note: null, who: 'seed', ts: 1, github: null }, { email: OWNER, note: null, who: 'seed', ts: 2, github: null }],
      bootstrap: ['root@example.org'],
      grants: [{ id: link.grant.id, holder: 'ops link', email: null, scopes: ['gcs', 'admin'], createdBy: LEAD, createdAt: NOW, expiresAt: null }],
    }])
  })

  it('staff off the list, a viewer, an agent token of a listed admin: 403, saying whether nobody is an admin; signed out: 401', async () => {
    const w = await world([LEAD])
    const token = await w.gate.mint({ email: LEAD, scopes: ['gcs', 'gcs:assign'], name: 'CLI / agent token', createdBy: LEAD, expiresAt: null, maxRedeems: null })
    expect([await w.call(STAFF), await w.call(GUEST), await w.call(token), await w.call(null)]).toEqual([
      [403, { error: 'forbidden', none: false }], [403, { error: 'forbidden', none: false }], [403, { error: 'forbidden', none: false }], [401, { error: 'unauthenticated' }],
    ])
  })

  it('the list on and empty (or its table missing): nobody is an admin, staff included, and the refusal says so', async () => {
    const [empty, missing] = [await world([]), await world([], {}, false)]
    expect([await empty.call(STAFF), await missing.call(STAFF), await empty.call(null)]).toEqual([
      [403, { error: 'forbidden', none: true }], [403, { error: 'forbidden', none: true }], [401, { error: 'unauthenticated' }],
    ])
  })

  it('the break-glass address is an admin over an empty list or a missing table, and is told which', async () => {
    const [empty, missing] = [await world([], { ADMIN_BOOTSTRAP: OWNER }), await world([], { ADMIN_BOOTSTRAP: OWNER }, false)]
    const body = { list: true, staffDomain: null, none: false, you: OWNER, rows: [], bootstrap: [OWNER], grants: [] }
    expect([await empty.call(OWNER), await missing.call(OWNER), await missing.call(STAFF)]).toEqual([
      [200, { ...body, table: 'ok' }], [200, { ...body, table: 'missing' }], [403, { error: 'forbidden', none: false }],
    ])
  })

  it('the list off: staff are admins, and the answer names the domain rule', async () => {
    const w = await world([OWNER], { ADMIN_LIST: undefined, ADMIN_EMAILS: '1' })
    expect(await w.call(STAFF)).toEqual([200, {
      list: false, staffDomain: 'staff.example.test', table: 'ok', none: false, you: STAFF,
      rows: [{ email: OWNER, note: null, who: 'seed', ts: 1, github: null }], bootstrap: [], grants: [],
    }])
  })
})

describe('POST /api/admins', () => {
  it('adds the address (lower-cased) with who added it and when, logged to `admin_edits`; it is an admin on its next request', async () => {
    const w = await world([LEAD])
    expect(await w.call(STAFF)).toEqual([403, { error: 'forbidden', none: false }])
    const row = { email: STAFF, note: 'on call', who: LEAD, ts: NOW, github: 'octo-dev' }
    expect(await w.call(LEAD, 'POST', '', { email: ' Dev@Staff.example.test ', note: ' on call ', github: ' @octo-dev ' })).toEqual([200, { ok: true, row }])
    expect([rows(w.raw), edits(w.raw)]).toEqual([
      [row, { email: LEAD, note: null, who: 'seed', ts: 1, github: null }],
      [{ tbl: 'admin_emails', pk: STAFF, action: 'insert', who: LEAD, ts: NOW, old_json: null, new_json: JSON.stringify(row) }],
    ])
    expect((await w.call(STAFF))[0]).toBe(200)
  })

  it('refuses a non-address or a non-handle (400), a listed one (409), a non-admin (403), and a database without the table or its `github` column (503)', async () => {
    const w = await world([LEAD])
    const missing = await world([], { ADMIN_BOOTSTRAP: OWNER }, false)
    const old = await world([], { ADMIN_BOOTSTRAP: OWNER }, false)
    old.raw.exec('CREATE TABLE admin_emails (email TEXT PRIMARY KEY, note TEXT, who TEXT NOT NULL, ts INTEGER NOT NULL)')
    expect([
      await w.call(LEAD, 'POST', '', { email: 'not-an-email' }), await w.call(LEAD, 'POST', '', { email: STAFF, github: 'not a handle' }), await w.call(LEAD, 'POST', '', { email: STAFF, github: '-octo' }),
      await w.call(LEAD, 'POST', '', { email: LEAD }),
      await w.call(STAFF, 'POST', '', { email: STAFF }), await missing.call(OWNER, 'POST', '', { email: STAFF }), await old.call(OWNER, 'POST', '', { email: STAFF }),
    ]).toEqual([
      [400, { error: 'not an email address' }], [400, { error: 'not a GitHub handle' }], [400, { error: 'not a GitHub handle' }],
      [409, { error: 'already an admin' }], [403, { error: 'forbidden', none: false }],
      [503, { error: '`admin_emails` does not exist in this database: apply its recipe (`site/migrations/admin-list.sql`) first' }],
      [503, { error: '`admin_emails` has no `github` column in this database: apply its recipe (`site/migrations/admin-list.sql`) first' }],
    ])
    expect([rows(w.raw), edits(w.raw)]).toEqual([[{ email: LEAD, note: null, who: 'seed', ts: 1, github: null }], []])
  })
})

describe('PATCH /api/admins', () => {
  it('sets a listed address\'s GitHub handle and note, logged with the row before and after; a no-op logs nothing', async () => {
    const w = await world([LEAD, OWNER])
    const before = { email: OWNER, note: null, who: 'seed', ts: 2, github: null }
    const after = { ...before, github: 'octo-owner' }
    const noted = { ...after, note: 'org owner' }
    expect([
      await w.call(LEAD, 'PATCH', '', { email: 'Owner@example.org', github: '@octo-owner' }),
      await w.call(LEAD, 'PATCH', '', { email: OWNER, github: 'octo-owner' }),
      await w.call(LEAD, 'PATCH', '', { email: OWNER, note: ' org owner ' }),
    ]).toEqual([[200, { ok: true, row: after }], [200, { ok: true, row: after }], [200, { ok: true, row: noted }]])
    expect([rows(w.raw), edits(w.raw)]).toEqual([
      [{ email: LEAD, note: null, who: 'seed', ts: 1, github: null }, noted],
      [
        { tbl: 'admin_emails', pk: OWNER, action: 'update', who: LEAD, ts: NOW, old_json: JSON.stringify(before), new_json: JSON.stringify(after) },
        { tbl: 'admin_emails', pk: OWNER, action: 'update', who: LEAD, ts: NOW, old_json: JSON.stringify(after), new_json: JSON.stringify(noted) },
      ],
    ])
  })

  it('clears a handle with a blank; refuses a non-handle (400), an unlisted address (404), a non-admin (403)', async () => {
    const w = await world([LEAD])
    w.raw.exec("UPDATE admin_emails SET github = 'octo-lead'")
    expect([
      await w.call(LEAD, 'PATCH', '', { email: LEAD, github: 'a--b' }), await w.call(LEAD, 'PATCH', '', { email: OWNER, github: 'octo' }),
      await w.call(STAFF, 'PATCH', '', { email: LEAD, github: 'octo' }), await w.call(LEAD, 'PATCH', '', { email: LEAD, github: '' }),
    ]).toEqual([
      [400, { error: 'not a GitHub handle' }], [404, { error: 'not on the list' }], [403, { error: 'forbidden', none: false }],
      [200, { ok: true, row: { email: LEAD, note: null, who: 'seed', ts: 1, github: null } }],
    ])
    expect(rows(w.raw)).toEqual([{ email: LEAD, note: null, who: 'seed', ts: 1, github: null }])
  })
})

describe('DELETE /api/admins', () => {
  it('removes a listed address, logged with the row it was; they stop being an admin on their next request', async () => {
    const w = await world([LEAD, OWNER])
    expect(await w.call(LEAD, 'DELETE', `?email=${encodeURIComponent('Owner@example.org')}`)).toEqual([200, { ok: true }])
    expect([rows(w.raw), edits(w.raw), await w.call(OWNER)]).toEqual([
      [{ email: LEAD, note: null, who: 'seed', ts: 1, github: null }],
      [{ tbl: 'admin_emails', pk: OWNER, action: 'delete', who: LEAD, ts: NOW, old_json: JSON.stringify({ email: OWNER, note: null, who: 'seed', ts: 2, github: null }), new_json: null }],
      [403, { error: 'forbidden', none: false }],
    ])
  })

  it('the last admin can\'t be removed, by themselves or by an admin grant: nothing changes, nothing is logged', async () => {
    const w = await world([LEAD])
    const link = await w.gate.mint({ email: null, scopes: ['gcs', 'admin'], name: 'ops link', createdBy: LEAD, expiresAt: null, maxRedeems: null })
    expect([await w.call(LEAD, 'DELETE', `?email=${LEAD}`), await w.call(link, 'DELETE', `?email=${LEAD}`), await w.call(LEAD, 'DELETE', `?email=${OWNER}`)]).toEqual([
      [409, { error: 'you are the last admin: add another before removing yourself', last: true }],
      [409, { error: 'that is the last admin: add another before removing them', last: true }],
      [404, { error: 'not on the list' }],
    ])
    expect([rows(w.raw), edits(w.raw)]).toEqual([[{ email: LEAD, note: null, who: 'seed', ts: 1, github: null }], []])
  })

  it('with a second admin listed, an admin may remove themselves', async () => {
    const w = await world([LEAD, OWNER])
    expect([await w.call(LEAD, 'DELETE', `?email=${LEAD}`), rows(w.raw), await w.call(LEAD)]).toEqual([
      [200, { ok: true }], [{ email: OWNER, note: null, who: 'seed', ts: 2, github: null }], [403, { error: 'forbidden', none: false }],
    ])
  })

  it('the last row may go where an admin remains without it: a break-glass address, or the list off (staff)', async () => {
    const glass = await world([LEAD], { ADMIN_BOOTSTRAP: OWNER })
    const off = await world([OWNER], { ADMIN_LIST: undefined, ADMIN_EMAILS: '1' })
    expect([await glass.call(OWNER, 'DELETE', `?email=${LEAD}`), rows(glass.raw), await off.call(STAFF, 'DELETE', `?email=${OWNER}`), rows(off.raw)]).toEqual([
      [200, { ok: true }], [], [200, { ok: true }], [],
    ])
  })
})

it('a deployment with no admin list (neither `ADMIN_LIST` nor `ADMIN_EMAILS`) has nothing to edit; other methods are refused', async () => {
  const w = await world([], { ADMIN_LIST: undefined })
  const on = await world([LEAD])
  expect([await w.call(STAFF, 'POST', '', { email: GUEST }), await w.call(STAFF), await on.call(LEAD, 'PUT', '', {})]).toEqual([
    [404, { error: 'this deployment keeps no admin list (`ADMIN_LIST` / `ADMIN_EMAILS` unset)' }],
    [200, { list: false, staffDomain: 'staff.example.test', table: 'unused', none: false, you: STAFF, rows: [], bootstrap: [], grants: [] }],
    [405, { error: 'method not allowed' }],
  ])
})
