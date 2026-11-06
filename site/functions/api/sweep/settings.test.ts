// `/api/sweep/settings`: viewers read what a dispatch uses (7 with no row, or no table), admins write it, each write logged to
// `admin_edits`; a bad value is a 400, and a deployment without the soft-delete guard refuses a write.
import { beforeAll, describe, expect, it } from 'vitest'
import { gateFor } from '../../_lib/auth'
import { minSoftDeleteDays, parseSweepSettings } from '../../_lib/sweepSettings'
import { sqliteD1 } from '../../_lib/testD1'
import { onRequestGet, onRequestPut } from './settings'

let env: Record<string, unknown>
let db: Awaited<ReturnType<typeof sqliteD1>>['db']
let raw: Awaited<ReturnType<typeof sqliteD1>>['raw']
let admin: string
let viewer: string

beforeAll(async () => {
  ;({ db, raw } = await sqliteD1())
  env = { DB: db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw', EXECUTOR: 'sweep' }
  const gate = gateFor(env as never)!
  const tok = async (email: string, scopes: string[]) =>
    (await gate.mint({ email, scopes, name: email, createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  admin = await tok('ops@example.test', ['cw', 'admin'])
  viewer = await tok('ann@example.test', ['cw'])
})

const req = (method: string, token: string, body?: unknown, over: Record<string, unknown> = {}) =>
  ({ request: new Request('https://site.example.test/api/sweep/settings', {
    method, headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body),
  }), env: { ...env, ...over } }) as never
const answer = async (r: Response) => [r.status, await r.json() as Record<string, unknown>] as const
const norm = ([status, body]: readonly [number, Record<string, unknown>]) =>
  [status, { ...body, ...(body.settings ? { settings: { ...body.settings as object, updated_at: '<ts>' } } : {}) }]

describe('/api/sweep/settings', () => {
  it('validates strictly', () => {
    expect([
      parseSweepSettings({ min_soft_delete_days: 0 }),
      parseSweepSettings({ min_soft_delete_days: 90 }),
      parseSweepSettings({ min_soft_delete_days: 91 }),
      parseSweepSettings({ min_soft_delete_days: -1 }),
      parseSweepSettings({ min_soft_delete_days: 1.5 }),
      parseSweepSettings({ min_soft_delete_days: '7' }),
      parseSweepSettings({}),
      parseSweepSettings([]),
      parseSweepSettings(null),
    ]).toEqual([
      { min_soft_delete_days: 0 },
      { min_soft_delete_days: 90 },
      ...Array(7).fill('min_soft_delete_days: an integer 0..90'),
    ])
  })

  it('no row reads as 7; a viewer reads, an admin writes, each write logged, and a dispatch reads what was written', async () => {
    expect(await answer(await onRequestGet(req('GET', viewer)))).toEqual([200, { settings: null, min_soft_delete_days: 7 }])
    expect(await minSoftDeleteDays(db as never, 'sweep')).toBe(7)
    expect((await onRequestPut(req('PUT', viewer, { min_soft_delete_days: 0 }))).status).toBe(403)

    const set = { min_soft_delete_days: 0, updated_by: 'ops@example.test', updated_at: '<ts>' }
    expect(norm(await answer(await onRequestPut(req('PUT', admin, { min_soft_delete_days: 0 }))))).toEqual([200, { settings: set, min_soft_delete_days: 0 }])
    expect(norm(await answer(await onRequestGet(req('GET', viewer))))).toEqual([200, { settings: set, min_soft_delete_days: 0 }])
    expect(await minSoftDeleteDays(db as never, 'sweep')).toBe(0)

    await onRequestPut(req('PUT', admin, { min_soft_delete_days: 14 }))
    expect(await minSoftDeleteDays(db as never, 'sweep')).toBe(14)
    const log = raw.prepare("SELECT pk, action, who, old_json, new_json FROM admin_edits WHERE tbl = 'sweep_settings' ORDER BY id").all() as { pk: string; action: string; who: string; old_json: string | null; new_json: string }[]
    const days = (j: string | null) => j === null ? null : (JSON.parse(j) as { min_soft_delete_days: number }).min_soft_delete_days
    expect(log.map(e => [e.pk, e.action, e.who, days(e.old_json), days(e.new_json)])).toEqual([
      ['1', 'insert', 'ops@example.test', null, 0],
      ['1', 'update', 'ops@example.test', 0, 14],
    ])
  })

  it('a bad value is a 400 and changes nothing', async () => {
    const before = await minSoftDeleteDays(db as never, 'sweep')
    expect(await answer(await onRequestPut(req('PUT', admin, { min_soft_delete_days: 91 })))).toEqual([400, { error: 'min_soft_delete_days: an integer 0..90' }])
    expect(await minSoftDeleteDays(db as never, 'sweep')).toBe(before)
  })

  it('the table itself refuses a value out of range, and a second row', async () => {
    expect(() => raw.prepare("INSERT OR REPLACE INTO sweep_settings (id, min_soft_delete_days, updated_by, updated_at) VALUES (1, 91, 'x', 1)").run()).toThrow(/CHECK constraint failed/)
    expect(() => raw.prepare("INSERT INTO sweep_settings (id, min_soft_delete_days, updated_by, updated_at) VALUES (2, 7, 'x', 1)").run()).toThrow(/CHECK constraint failed/)
  })

  it('on `plan-sweep`, no row reads as 0, and a write takes', async () => {
    const plan = { EXECUTOR: 'plan-sweep' }
    expect(await answer(await onRequestGet(req('GET', viewer, undefined, plan)))).toEqual([200, { settings: null, min_soft_delete_days: 0 }])
    expect(await minSoftDeleteDays(db as never, 'plan-sweep')).toBe(0)
    const set = { min_soft_delete_days: 3, updated_by: 'ops@example.test', updated_at: '<ts>' }
    expect(norm(await answer(await onRequestPut(req('PUT', admin, { min_soft_delete_days: 3 }, plan))))).toEqual([200, { settings: set, min_soft_delete_days: 3 }])
    expect(await minSoftDeleteDays(db as never, 'plan-sweep')).toBe(3)
  })

  it('a D1 without the table reads as 7, and a write says what to apply', async () => {
    const { db: bare, raw: bareRaw } = await sqliteD1()
    bareRaw.exec('DROP TABLE sweep_settings')
    expect(await minSoftDeleteDays(bare as never, 'sweep')).toBe(7)
    const bareEnv = { ...env, DB: bare }
    const { token } = await gateFor(bareEnv as never)!.mint({ email: 'ops@example.test', scopes: ['cw', 'admin'], name: 'ops', createdBy: 'test', expiresAt: null, maxRedeems: null })
    expect(await answer(await onRequestGet(req('GET', token, undefined, { DB: bare })))).toEqual([200, { settings: null, min_soft_delete_days: 7 }])
    expect(await answer(await onRequestPut(req('PUT', token, { min_soft_delete_days: 0 }, { DB: bare })))).toEqual([503, { error: 'not set up on this deployment: apply the sweep-soft-delete migration' }])
  })
})
