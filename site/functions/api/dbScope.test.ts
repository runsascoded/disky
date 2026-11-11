import { describe, expect, it } from 'vitest'
import { type Env, gateFor } from '../_lib/auth'
import { sqliteD1, withAdminEmails } from '../_lib/testD1'
import { TABLES } from '../_lib/tables'
import { onRequest } from './db/[[path]]'

// `/api/db`'s viewer tables are the deployment's base scope's, whatever it is named: a listed admin on a `cw`-scoped
// site holds `cw`, `cw:assign` and `admin`, not `gcs`.

const LISTED = 'lee@example.org'
const ORIGIN = 'https://cw.example.test'
// The registry tables this D1 has: `/api/db` offers no others.
const PRESENT = new Set((await sqliteD1()).raw.prepare("SELECT name FROM sqlite_master WHERE type = 'table'").all().map(r => (r as { name: string }).name))
const BASE_TABLES = TABLES.filter(t => PRESENT.has(t.name) && t.readScope !== 'admin').map(t => t.name)
const ALL_TABLES = TABLES.filter(t => PRESENT.has(t.name)).map(t => t.name)

// Both ways a non-staff address is a listed admin: `ADMIN_EMAILS` needs the address allowed too; `ADMIN_LIST` admits it by the list.
type AdminMode = 'ADMIN_EMAILS' | 'ADMIN_LIST'

async function site(BASE_SCOPE: string, mode: AdminMode = 'ADMIN_LIST') {
  const { db, raw } = await sqliteD1()
  withAdminEmails(raw)
  const env = { BASE_SCOPE, [mode]: '1', SESSION_SECRET: 's'.repeat(32), STAFF_DOMAIN: 'staff.example.test', DB: db } as Env
  await db.prepare('INSERT INTO admin_emails (email, note, who, ts) VALUES (?, NULL, ?, 0)').bind(LISTED, 'test').run()
  if (mode === 'ADMIN_EMAILS') await db.prepare('INSERT INTO allowed_emails (email, who, ts) VALUES (?, ?, 0)').bind(LISTED, 'test').run()
  const gate = gateFor(env)!
  const signed = await gate.signIn(LISTED, new Request(`${ORIGIN}/signin`))
  const headersOf = async (scopes: string[]) => ({ authorization: `Bearer ${(await gate.mint({ email: null, scopes, name: 'link', createdBy: 'test', expiresAt: null, maxRedeems: null })).token}` })
  const get = async (path: string, headers: Record<string, string>): Promise<[number, unknown]> => {
    const res = await onRequest({ request: new Request(`${ORIGIN}/api/db${path}`, { headers }), env })
    return [res.status, await res.json()]
  }
  return { get, admin: { cookie: signed!.cookie.split(';')[0] }, headersOf }
}

const names = ([status, body]: [number, unknown]) => [status, (body as { tables: { name: string }[] }).tables.map(t => t.name)]
const canWrite = ([status, body]: [number, unknown]) => [status, (body as { spec: { canWrite: boolean } }).spec.canWrite]

describe('D1', () => {
  describe.each(['cw', 'gcs', 'laptop'])('a `%s`-scoped deployment', scope => {
    it.each(['ADMIN_EMAILS', 'ADMIN_LIST'] as const)('a non-staff listed admin (`%s`) lists and reads every table', async mode => {
      const { get, admin } = await site(scope, mode)
      expect(names(await get('', admin))).toEqual([200, ALL_TABLES])
      expect(canWrite(await get('/allowed_emails', admin))).toEqual([200, true])
    })
    it('a link with the base scope reads the viewer tables and no others; a read-only link none', async () => {
      const { get, headersOf } = await site(scope)
      const viewer = await headersOf([scope])
      const readOnly = await headersOf([`${scope}:read`])
      expect(names(await get('', viewer))).toEqual([200, BASE_TABLES])
      expect(canWrite(await get('/allowed_emails', viewer))).toEqual([200, false])
      expect(await get('/admin_edits', viewer)).toEqual([403, { error: 'forbidden' }])
      expect(names(await get('', readOnly))).toEqual([200, []])
      expect(await get('/allowed_emails', readOnly)).toEqual([403, { error: 'forbidden' }])
    })
  })

  it('another deployment\'s name for the base scope reads nothing: a `gcs` link on a `cw` site', async () => {
    const { get, headersOf } = await site('cw')
    const stray = await headersOf(['gcs'])
    expect(names(await get('', stray))).toEqual([200, []])
    expect(await get('/allowed_emails', stray)).toEqual([403, { error: 'forbidden' }])
  })
})
