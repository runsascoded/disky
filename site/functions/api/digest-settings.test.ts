// `/api/digest-settings` (specs/digest-runway-marks.md §4): viewers read the live row (the job), admins write it, each
// write logged to `admin_edits`; bad values are 400s.
import { beforeAll, describe, expect, it } from 'vitest'
import { gateFor } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { onRequestGet, onRequestPut, parseSettings } from './digest-settings'

let env: Record<string, unknown>
let db: { prepare(sql: string): { all(): Promise<{ results: Record<string, unknown>[] }> } }
let admin: string
let viewer: string

beforeAll(async () => {
  const d1 = await sqliteD1()
  db = d1.db as never
  env = { DB: d1.db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw' }
  const gate = gateFor(env as never)!
  const tok = async (email: string, scopes: string[]) =>
    (await gate.mint({ email, scopes, name: email, createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  admin = await tok('ops@example.test', ['cw', 'admin'])
  viewer = await tok('ann@example.test', ['cw'])
})

const req = (method: string, qs: string, token: string, body?: unknown) =>
  ({ request: new Request(`https://site.example.test/api/digest-settings${qs}`, {
    method, headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body),
  }), env }) as never
type Body = { settings?: Record<string, unknown> | null; error?: string }
const get = async (qs: string, token: string) => { const r = await onRequestGet(req('GET', qs, token)); return [r.status, (await r.json()) as Body] as const }
const put = async (body: unknown, token: string) => { const r = await onRequestPut(req('PUT', '', token, body)); return [r.status, (await r.json()) as Body] as const }
const norm = (s: Record<string, unknown> | null) => s && { ...s, updated_at: '<ts>' }

describe('/api/digest-settings', () => {
  it('validates strictly', () => {
    expect([
      parseSettings({ template: 'cw', params: 'qh,pp', quota_marks: 'runway-hue' }),
      parseSettings({ template: 'hf', params: '', quota_marks: 'runway' }),
      parseSettings({ template: 'cw', params: '', quota_marks: 'squares' }),
      parseSettings({ template: 'cw', params: 'qx', quota_marks: 'runway' }),
      parseSettings([]),
    ]).toEqual([
      { template: 'cw', params: 'qh,pp', quota_marks: 'runway-hue' },
      'template: cw | gcs',
      'quota_marks: warning | circles | runway | runway-hue',
      'params: digest params: unreadable field "qx" in "qx"',
      'body: a JSON object',
    ])
  })

  it('no row reads as null; an admin writes, a viewer reads; each write is logged', async () => {
    expect(await get('?template=cw', viewer)).toEqual([200, { settings: null }])
    expect(await get('?template=zz', viewer)).toEqual([400, { error: 'template: cw | gcs' }])
    expect((await put({ template: 'cw', params: 'qr,pp', quota_marks: 'runway' }, viewer))[0]).toBe(403)
    const [s1, b1] = await put({ template: 'cw', params: 'qr,pp', quota_marks: 'runway' }, admin)
    expect([s1, norm(b1.settings ?? null)]).toEqual([200, { template: 'cw', params: 'qr,pp', quota_marks: 'runway', updated_by: 'ops@example.test', updated_at: '<ts>' }])
    await put({ template: 'cw', params: 'qh,pp', quota_marks: 'runway-hue' }, admin)
    const [s2, b2] = await get('?template=cw', viewer)
    expect([s2, norm(b2.settings ?? null)]).toEqual([200, { template: 'cw', params: 'qh,pp', quota_marks: 'runway-hue', updated_by: 'ops@example.test', updated_at: '<ts>' }])
    expect(await put({ template: 'cw', params: '', quota_marks: 'squares' }, admin)).toEqual([400, { error: 'quota_marks: warning | circles | runway | runway-hue' }])
    const log = (await db.prepare("SELECT pk, action, who, old_json, new_json FROM admin_edits WHERE tbl = 'digest_settings' ORDER BY id").all()).results
      .map(r => ({ ...r, old_json: r.old_json && JSON.parse(r.old_json as string).quota_marks, new_json: JSON.parse(r.new_json as string).quota_marks }))
    expect(log).toEqual([
      { pk: 'cw', action: 'insert', who: 'ops@example.test', old_json: null, new_json: 'runway' },
      { pk: 'cw', action: 'update', who: 'ops@example.test', old_json: 'runway', new_json: 'runway-hue' },
    ])
  })
})
