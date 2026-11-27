// `/api/digest-settings` (specs/digest-runway-marks.md §4): viewers read the live row (the job), admins write it, each
// write logged to `admin_edits`; bad values are 400s.
import { beforeAll, describe, expect, it } from 'vitest'
import { gateFor } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { onRequestGet, onRequestPut, parseSettings } from './digest-settings'

let env: Record<string, unknown>
let db: { prepare(sql: string): { all(): Promise<{ results: Record<string, unknown>[] }> } }
let raw: Awaited<ReturnType<typeof sqliteD1>>['raw']
let admin: string
let viewer: string

beforeAll(async () => {
  const d1 = await sqliteD1()
  db = d1.db as never
  raw = d1.raw
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
      parseSettings({ template: 'quota', params: 'qh,pp', quota_marks: 'runway-hue' }),
      parseSettings({ template: 'hf', params: '', quota_marks: 'runway' }),
      parseSettings({ template: 'quota', params: '', quota_marks: 'squares' }),
      parseSettings({ template: 'quota', params: 'qx', quota_marks: 'runway' }),
      parseSettings([]),
    ]).toEqual([
      { template: 'quota', params: 'qh,pp', quota_marks: 'runway-hue', variant: null },
      'template: quota | cost',
      'quota_marks: warning | circles | runway | runway-hue | arrow-chip',
      'params: digest params: unreadable field "qx" in "qx"',
      'body: a JSON object',
    ])
  })

  it('takes a variant of the template, or null: the default for a new month', () => {
    expect([
      parseSettings({ template: 'quota', params: '', quota_marks: 'runway', variant: 'weekly' }),
      parseSettings({ template: 'cost', params: '', quota_marks: 'runway', variant: 'body' }),
      parseSettings({ template: 'quota', params: '', quota_marks: 'runway', variant: null }),
    ]).toEqual([
      { template: 'quota', params: '', quota_marks: 'runway', variant: 'weekly' },
      'variant: sender | weekly | null',
      { template: 'quota', params: '', quota_marks: 'runway', variant: null },
    ])
  })

  it('no row reads as null; an admin writes, a viewer reads; each write is logged', async () => {
    expect(await get('?template=quota', viewer)).toEqual([200, { settings: null }])
    expect(await get('?template=zz', viewer)).toEqual([400, { error: 'template: quota | cost' }])
    expect((await put({ template: 'quota', params: 'qr,pp', quota_marks: 'runway' }, viewer))[0]).toBe(403)
    const [s1, b1] = await put({ template: 'quota', params: 'qr,pp', quota_marks: 'runway' }, admin)
    expect([s1, norm(b1.settings ?? null)]).toEqual([200, { template: 'quota', params: 'qr,pp', quota_marks: 'runway', variant: null, updated_by: 'ops@example.test', updated_at: '<ts>' }])
    await put({ template: 'quota', params: 'qh,pp', quota_marks: 'runway-hue' }, admin)
    const [s2, b2] = await get('?template=quota', viewer)
    expect([s2, norm(b2.settings ?? null)]).toEqual([200, { template: 'quota', params: 'qh,pp', quota_marks: 'runway-hue', variant: null, updated_by: 'ops@example.test', updated_at: '<ts>' }])
    expect(await put({ template: 'quota', params: '', quota_marks: 'squares' }, admin)).toEqual([400, { error: 'quota_marks: warning | circles | runway | runway-hue | arrow-chip' }])
    const log = (await db.prepare("SELECT pk, action, who, old_json, new_json FROM admin_edits WHERE tbl = 'digest_settings' ORDER BY id").all()).results
      .map(r => ({ ...r, old_json: r.old_json && JSON.parse(r.old_json as string).quota_marks, new_json: JSON.parse(r.new_json as string).quota_marks }))
    expect(log).toEqual([
      { pk: 'quota', action: 'insert', who: 'ops@example.test', old_json: null, new_json: 'runway' },
      { pk: 'quota', action: 'update', who: 'ops@example.test', old_json: 'runway', new_json: 'runway-hue' },
    ])
  })

  it("reads and writes a template's old name as its name, and its old-named row until the recipe re-keys it", async () => {
    // a D1 the `digest-template-rename` recipe hasn't reached: the row is still under `gcs`
    raw.prepare('INSERT INTO digest_settings (template, params, quota_marks, updated_by, updated_at) VALUES (?, ?, ?, ?, ?)').run('gcs', 'qc', 'circles', 'old@example.test', 1)
    const was = { template: 'cost', params: 'qc', quota_marks: 'circles', variant: null, updated_by: 'old@example.test', updated_at: 1 }
    expect([await get('?template=cost', viewer), await get('?template=gcs', viewer)]).toEqual([[200, { settings: was }], [200, { settings: was }]])
    expect(parseSettings({ template: 'cw', params: '', quota_marks: 'runway' })).toEqual({ template: 'quota', params: '', quota_marks: 'runway', variant: null })
    const [s, b] = await put({ template: 'gcs', params: 'qr,pp', quota_marks: 'runway' }, admin)
    expect([s, norm(b.settings ?? null)]).toEqual([200, { template: 'cost', params: 'qr,pp', quota_marks: 'runway', variant: null, updated_by: 'ops@example.test', updated_at: '<ts>' }])
    expect(raw.prepare("SELECT template, params FROM digest_settings WHERE template IN ('gcs', 'cost')").all().map(r => ({ ...r }))).toEqual([{ template: 'cost', params: 'qr,pp' }])
    expect(await get('?template=gcs', viewer).then(([st, x]) => [st, norm(x.settings ?? null)])).toEqual([200, { template: 'cost', params: 'qr,pp', quota_marks: 'runway', variant: null, updated_by: 'ops@example.test', updated_at: '<ts>' }])
  })
})
