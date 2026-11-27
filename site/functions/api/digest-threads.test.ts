// `/api/digest-threads` (specs/digest-thread-variant.md): viewers read a thread's row (the job), admins set it from a
// reviewed switch preview, whose plan's deletes the row keeps; each write logged to `admin_edits`.
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { gateFor } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { onRequestGet, onRequestPut, parsePut, parseSwitchPlan, planRefusal, type SwitchPlan } from './digest-threads'

vi.mock('../_lib/gcp.js', () => ({ gcpToken: async () => 'gcp-token' }))

const CH = 'C0C9J6CE8BD'
const JOB = 'quota-digest-20261127-120000z'
const PLAN: SwitchPlan = {
  digest_switch: 1, month: '2026-11', channel: CH, from: 'sender', to: 'weekly', at: '2026-11-27T12:01:00+00:00',
  edit: [],
  post: [{ key: 'wk-2026-10-26', scan: '2026-11-01T0716', headline: 'week' }],
  delete: [{ key: '2026-11-01', ts: '1793519629.118359' }, { key: '2026-11-02', ts: '1793606287.128509' }],
  reorder: [], thread_read: true, links_checked: false, notes: [],
}
const RUN = { month: '2026-11', channel: CH, target: 'thread', dry_run: 1, variant: 'weekly', ts: 100 }

let env: Record<string, unknown>
let db: { prepare(sql: string): { all(): Promise<{ results: Record<string, unknown>[] }> } }
let raw: Awaited<ReturnType<typeof sqliteD1>>['raw']
let admin: string
let viewer: string

beforeAll(async () => {
  const d1 = await sqliteD1()
  db = d1.db as never
  raw = d1.raw
  env = { DB: d1.db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw', DATA_BUCKET: 'data', GCP_SA_KEY: '{}' }
  const gate = gateFor(env as never)!
  const tok = async (email: string, scopes: string[]) =>
    (await gate.mint({ email, scopes, name: email, createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  admin = await tok('ops@example.test', ['cw', 'admin'])
  viewer = await tok('ann@example.test', ['cw'])
  raw.prepare('INSERT INTO digest_reapply (job_id, month, channel, target, dry_run, params, quota_marks, variant, actor, ts, out) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)')
    .run(JOB, '2026-11', CH, 'thread', 1, 'weekly', 'ops@example.test', 100, `gs://data/digest/reapply/${JOB}.txt`)
})

afterEach(() => vi.unstubAllGlobals())

const req = (method: string, qs: string, token: string, body?: unknown) =>
  ({ request: new Request(`https://site.example.test/api/digest-threads${qs}`, {
    method, headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body),
  }), env }) as never
type Body = { thread?: Record<string, unknown> | null; error?: string }
const get = async (qs: string, token: string) => { const r = await onRequestGet(req('GET', qs, token)); return [r.status, (await r.json()) as Body] as const }
const put = async (body: unknown, token: string) => { const r = await onRequestPut(req('PUT', '', token, body)); return [r.status, (await r.json()) as Body] as const }
/** GCS answering the plan read: `plan` (404 when null), and the URLs asked for. */
const gcs = (plan: unknown) => {
  const urls: string[] = []
  vi.stubGlobal('fetch', async (url: string) => {
    urls.push(url)
    return plan === null ? new Response('not found', { status: 404 }) : new Response(JSON.stringify(plan), { status: 200 })
  })
  return urls
}
const norm = (t: Record<string, unknown> | null | undefined) => t && { ...t, updated_at: '<ts>' }

describe('/api/digest-threads', () => {
  it('reads a switch plan and a PUT strictly', () => {
    const { edit: _e, reorder: _r, thread_read: _t, links_checked: _l, notes: _n, ...first } = PLAN
    expect([parseSwitchPlan(PLAN), parseSwitchPlan({ ...PLAN, digest_switch: 2 }), parseSwitchPlan({ ...PLAN, delete: [{ key: 'x' }] }), parseSwitchPlan([]),
      parseSwitchPlan({ ...PLAN, reorder: [{ user: 'U1' }] }), parseSwitchPlan(first)])
      .toEqual([PLAN, 'not a switch plan (version 1)', 'switch plan: delete', 'not a switch plan', 'switch plan: reorder',
        // a plan from before edits and reorders: none of either
        { ...first, edit: [], reorder: [], thread_read: false, links_checked: false, notes: [] }])
    expect([
      parsePut({ channel: CH, month: '2026-11', variant: 'weekly', plan_job: JOB }),
      parsePut({ channel: 'general', month: '2026-11', variant: 'weekly', plan_job: JOB }),
      parsePut({ channel: CH, month: '2026-13', variant: 'weekly', plan_job: JOB }),
      parsePut({ channel: CH, month: '2026-11', variant: 'daily', plan_job: JOB }),
      parsePut({ channel: CH, month: '2026-11', variant: 'weekly', plan_job: "x'; rm" }),
    ]).toEqual([
      { channel: CH, month: '2026-11', variant: 'weekly', plan_job: JOB, allow_reorder: false },
      'channel: a Slack channel id', 'month: YYYY-MM', 'variant: sender | body | weekly',
      'plan_job: the reviewed dry run (<template>-digest-<stamp>z)',
    ])
  })

  it('takes a plan only from its own thread and style, and never an older one than the row', () => {
    const r = { channel: CH, month: '2026-11', variant: 'weekly' }
    const prev = { channel: CH, month: '2026-11', variant: 'sender', deletes: [], edits: [], reorder: [], plan_job: JOB, updated_by: 'b@example.test', updated_at: 200 }
    const moves = { ...PLAN, reorder: [{ ts: '1795000000.000100', user: 'U9' }] }
    expect([
      planRefusal(r, RUN, PLAN, null),
      planRefusal(r, null, null, null),
      planRefusal(r, { ...RUN, target: 'op' }, PLAN, null),
      planRefusal(r, { ...RUN, variant: 'body' }, PLAN, null),
      planRefusal(r, { ...RUN, channel: 'D0CE8QE83EH' }, PLAN, null),
      planRefusal(r, RUN, null, null),
      planRefusal(r, RUN, 'switch plan: delete', null),
      planRefusal(r, RUN, { ...PLAN, to: 'body' }, null),
      planRefusal(r, RUN, PLAN, prev),
      planRefusal(r, RUN, moves, null),
      planRefusal({ ...r, allow_reorder: true }, RUN, moves, null),
    ]).toEqual([
      null,
      'no such dry run',
      "that run isn't a whole-thread preview under weekly",
      "that run isn't a whole-thread preview under weekly",
      `that run previewed 2026-11 in D0CE8QE83EH`,
      'that run wrote no switch plan (still running, failed, or the thread is already that style)',
      'switch plan: delete',
      `its plan switches 2026-11 in ${CH} to body`,
      "the thread's style was set after that preview (by b@example.test); preview again",
      "the switch would move 1 people's replies above its new ones (1795000000.000100 by U9): allow reorder to go ahead",
      null,
    ])
  })

  it('no row reads as null; an admin sets it from the reviewed plan, a viewer reads it; each write is logged', async () => {
    expect(await get(`?channel=${CH}&month=2026-11`, viewer)).toEqual([200, { thread: null }])
    expect(await get('?channel=general&month=2026-11', viewer)).toEqual([400, { error: 'channel: a Slack channel id' }])
    expect((await put({ channel: CH, month: '2026-11', variant: 'weekly', plan_job: JOB }, viewer))[0]).toBe(403)
    // the run wrote no plan yet
    gcs(null)
    expect(await put({ channel: CH, month: '2026-11', variant: 'weekly', plan_job: JOB }, admin))
      .toEqual([409, { error: 'that run wrote no switch plan (still running, failed, or the thread is already that style)' }])
    const urls = gcs(PLAN)
    const [s, b] = await put({ channel: CH, month: '2026-11', variant: 'weekly', plan_job: JOB, deletes: ['forged'] }, admin)
    const row = { channel: CH, month: '2026-11', variant: 'weekly', deletes: ['1793519629.118359', '1793606287.128509'], edits: [], reorder: [], plan_job: JOB,
      updated_by: 'ops@example.test', updated_at: '<ts>' }
    expect([s, norm(b.thread)]).toEqual([200, row])
    expect(urls).toEqual([`https://storage.googleapis.com/storage/v1/b/data/o/${encodeURIComponent(`digest/reapply/${JOB}.switch.json`)}?alt=media`])
    const [s2, b2] = await get(`?channel=${CH}&month=2026-11`, viewer)
    expect([s2, norm(b2.thread)]).toEqual([200, row])
    // the same preview again: the row is newer than it now
    expect((await put({ channel: CH, month: '2026-11', variant: 'weekly', plan_job: JOB }, admin))[0]).toBe(409)
    const log = (await db.prepare("SELECT pk, action, who, new_json FROM admin_edits WHERE tbl = 'digest_threads' ORDER BY id").all()).results
      .map(r => ({ ...r, new_json: JSON.parse(r.new_json as string).variant }))
    expect(log).toEqual([{ pk: `${CH}/2026-11`, action: 'insert', who: 'ops@example.test', new_json: 'weekly' }])
  })

  it('stores the reorders an admin allowed, and the edits the plan makes', async () => {
    raw.prepare('INSERT INTO digest_reapply (job_id, month, channel, target, dry_run, params, quota_marks, variant, actor, ts, out) VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)')
      .run('quota-digest-20261227-120000z', '2026-12', CH, 'thread', 1, 'body', 'ops@example.test', 9_999_999_999, 'gs://data/x.txt')
    const plan = { ...PLAN, month: '2026-12', to: 'body', edit: [{ key: '2026-12-01', ts: '1796000000.000100', to: '2026-12-01', scan: 's', was: 'a', headline: 'b' }],
      reorder: [{ ts: '1796000000.000900', user: 'U9' }] }
    gcs(plan)
    const body = { channel: CH, month: '2026-12', variant: 'body', plan_job: 'quota-digest-20261227-120000z' }
    expect(await put(body, admin)).toEqual([409, { error: "the switch would move 1 people's replies above its new ones (1796000000.000900 by U9): allow reorder to go ahead" }])
    const [s, b] = await put({ ...body, allow_reorder: true }, admin)
    expect([s, b.thread && { edits: b.thread.edits, reorder: b.thread.reorder }]).toEqual([200, { edits: ['1796000000.000100'], reorder: ['1796000000.000900'] }])
  })

  it('refuses a PUT where dispatch is off', async () => {
    const r = await onRequestPut({ ...(req('PUT', '', admin, { channel: CH, month: '2026-11', variant: 'weekly', plan_job: JOB }) as object), env: { ...env, SWEEP_DISPATCH: '0' } } as never)
    expect(r.status).toBe(403)
  })
})
