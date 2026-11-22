// `/api/digest-reapply/<job>/{plan,check,apply}` (specs/done/digest-rewrite-review.md): a dry run's rewrite plan, read
// from the data bucket, served to `/admin/slack`'s review, and a selection of it checked or applied through thrds'
// `rewriteHandler`, its applied outcomes kept in D1 so a re-run skips them.
import { contentHash, type RewriteContent, type RewritePlan } from '@open-athena/thrds'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { gateFor } from '../_lib/auth'
import { sqliteD1 } from '../_lib/testD1'
import { onRequest, route } from './digest-reapply/[job]/[op]'

vi.mock('../_lib/gcp.js', async original => ({ ...await original<typeof import('../_lib/gcp.js')>(), gcpToken: async () => 'token' }))

const ORIGIN = 'https://site.example.test'
const JOB = 'quota-digest-20261122-120000z'
const THREAD_JOB = 'quota-digest-20261122-130000z'
const PLAN_URL = `https://storage.googleapis.com/storage/v1/b/data/o/${encodeURIComponent(`digest/reapply/${JOB}.plan.json`)}?alt=media`
const TS = '1795000000.000100'
const SKIPPED = '1795000000.000200'
const NO_SENDER = { username: null, icon_url: null, icon_emoji: null }
const OLD: RewriteContent = { text: 'old', blocks: null, sender: NO_SENDER, files: [] }
const NEW: RewriteContent = { ...OLD, text: 'new' }

let env: Record<string, unknown>
let token: string
let raw: Awaited<ReturnType<typeof sqliteD1>>['raw']
let plan: RewritePlan

beforeEach(async () => {
  const d = await sqliteD1()
  raw = d.raw
  const run = raw.prepare(`INSERT INTO digest_reapply (job_id, month, channel, target, dry_run, actor, ts, out) VALUES (?, '2026-11', 'C01234567', ?, 1, 'actor', 1, 'gs://data/x.txt')`)
  run.run(JOB, 'op')
  run.run(THREAD_JOB, 'thread')
  env = { DB: d.db, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'cw', GCP_SA_KEY: 'key', DATA_BUCKET: 'data', SLACK_BOT_TOKEN: 'xoxb-test' }
  token = (await gateFor(env as never)!.mint({ email: 'ops@example.test', scopes: ['cw', 'admin'], name: 'ops', createdBy: 'test', expiresAt: null, maxRedeems: null })).token
  const h = await contentHash(OLD)
  plan = {
    thrds_rewrite_plan: 1,
    actions: [
      { kind: 'edit', channel: 'C01234567', ts: TS, thread_ts: '1795000000.000000', hash: h, old: OLD, new: NEW, reason: null },
      { kind: 'skip', channel: 'C01234567', ts: SKIPPED, thread_ts: '1795000000.000000', hash: h, old: OLD, new: null, reason: 'not selected' },
    ],
  }
})
afterEach(() => vi.unstubAllGlobals())

/** The route's status and body, and each fetch as `<method> <url>` (Slack's by method name). `live`: the text Slack
 *  reads back for `TS`. */
async function call(method: 'GET' | 'POST', path: string, body?: unknown, live = 'old'): Promise<[number, unknown, string[]]> {
  const fetched = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input instanceof Request ? input.url : input)
    if (url === PLAN_URL) return new Response(JSON.stringify(plan))
    if (url.startsWith('https://slack.com/api/conversations.replies')) return Response.json({ ok: true, messages: [{ ts: TS, text: live, bot_id: 'B1' }] })
    if (url.startsWith('https://slack.com/api/chat.update')) return Response.json({ ok: true, ts: TS })
    void init
    return new Response('not found', { status: 404 })
  })
  vi.stubGlobal('fetch', fetched)
  const r = await onRequest({
    request: new Request(`${ORIGIN}${path}`, {
      method,
      headers: { authorization: `Bearer ${token}`, ...(body === undefined ? {} : { 'content-type': 'application/json', origin: ORIGIN }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    }),
    env: env as never,
  })
  const calls = fetched.mock.calls.map(([input, init]) => {
    const url = String(input instanceof Request ? input.url : input)
    const slack = /^https:\/\/slack\.com\/api\/([\w.]+)/.exec(url)
    return `${(input instanceof Request ? input.method : init?.method) ?? 'GET'} ${slack ? slack[1] : url}`
  })
  return [r.status, await r.json(), calls]
}

const receipts = () => raw.prepare('SELECT plan, ts, kind, status FROM thrds_rewrite_receipts ORDER BY at').all()

describe('/api/digest-reapply/<job>/<op>', () => {
  it('reads a job and an op from the path', () => {
    expect([
      route(`${ORIGIN}/api/digest-reapply/${JOB}/plan`),
      route(`${ORIGIN}/api/digest-reapply/${JOB}/apply`),
      route(`${ORIGIN}/api/digest-reapply/${JOB}/delete`),
      route(`${ORIGIN}/api/digest-reapply/not-a-job/plan`),
      route(`${ORIGIN}/api/digest-reapply/${JOB}/plan/x`),
    ]).toEqual([{ job: JOB, op: 'plan' }, { job: JOB, op: 'apply' }, null, null, null])
  })

  it('serves the run’s plan, with its counts', async () => {
    expect(await call('GET', `/api/digest-reapply/${JOB}/plan`)).toEqual([
      200, { path: '2026-11 · op · C01234567', plan: { ...plan, counts: { edit: 1, repost: 0, noop: 0, skip: 1, blocked: 0 } } }, [`GET ${PLAN_URL}`],
    ])
  })

  it('says why there is no plan: no run, a whole-thread run, none written yet', async () => {
    const missing = 'quota-digest-20261122-140000z'
    raw.prepare(`INSERT INTO digest_reapply (job_id, month, channel, target, dry_run, actor, ts, out) VALUES (?, '2026-11', 'C01234567', 'op', 1, 'actor', 1, 'x')`).run(missing)
    expect([
      await call('GET', '/api/digest-reapply/quota-digest-20261122-150000z/plan'),
      await call('GET', `/api/digest-reapply/${THREAD_JOB}/plan`),
      await call('GET', `/api/digest-reapply/${missing}/plan`),
    ]).toEqual([
      [404, { error: 'no run quota-digest-20261122-150000z' }, []],
      [404, { error: `${THREAD_JOB} has no plan: only a dry run of the OP or of days writes one` }, []],
      [404, { error: `${missing} wrote no plan (still running, failed, or from before plans)` }, [
        `GET https://storage.googleapis.com/storage/v1/b/data/o/${encodeURIComponent(`digest/reapply/${missing}.plan.json`)}?alt=media`,
      ]],
    ])
  })

  it('checks a selection without writing, then applies it once: a re-run skips it by its receipt', async () => {
    expect(await call('POST', `/api/digest-reapply/${JOB}/check`, { ids: [TS] })).toEqual([
      200, { outcomes: [{ ts: TS, kind: 'edit', status: 'pending' }] }, [`GET ${PLAN_URL}`, 'GET conversations.replies'],
    ])
    expect(receipts()).toEqual([])
    expect(await call('POST', `/api/digest-reapply/${JOB}/apply`, { ids: [TS] })).toEqual([
      200, { outcomes: [{ ts: TS, kind: 'edit', status: 'done' }] }, [`GET ${PLAN_URL}`, 'GET conversations.replies', 'POST chat.update'],
    ])
    expect(await call('POST', `/api/digest-reapply/${JOB}/apply`, { ids: [TS] })).toEqual([
      200, { outcomes: [{ ts: TS, kind: 'edit', status: 'receipt' }] }, [`GET ${PLAN_URL}`],
    ])
    expect(receipts()).toEqual([{ plan: JOB, ts: TS, kind: 'edit', status: 'done' }])
  })

  it('refuses a message that changed since the plan, and ids that aren’t its edits', async () => {
    expect([
      await call('POST', `/api/digest-reapply/${JOB}/apply`, { ids: [TS] }, 'edited by hand'),
      await call('POST', `/api/digest-reapply/${JOB}/apply`, { ids: [SKIPPED] }),
    ]).toEqual([
      [200, { outcomes: [{ ts: TS, kind: 'edit', status: 'stale', error: 'changed since planning' }] }, [`GET ${PLAN_URL}`, 'GET conversations.replies']],
      [400, { error: `not edits or reposts in this plan: ${SKIPPED}` }, [`GET ${PLAN_URL}`]],
    ])
    expect(receipts()).toEqual([{ plan: JOB, ts: TS, kind: 'edit', status: 'stale' }])
  })

  it('needs the bot token to check or apply, not to read the plan', async () => {
    delete env.SLACK_BOT_TOKEN
    expect([
      (await call('GET', `/api/digest-reapply/${JOB}/plan`))[0],
      await call('POST', `/api/digest-reapply/${JOB}/check`, { ids: [TS] }),
    ]).toEqual([200, [503, { error: 'not configured (SLACK_BOT_TOKEN)' }, []]])
  })
})
