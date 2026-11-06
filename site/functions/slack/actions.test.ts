// `/slack/actions`, the real-delete button: its value carries the minimum its confirm stated, and a click on a card older than
// the current minimum deletes nothing. The confirm's text is fixed when the card is drawn, and the minimum is read at dispatch.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { planDigest } from '../_lib/plans'
import { planSweep } from '../_lib/planDispatch'
import { refuse } from '../_lib/dispatch'
import { notifyPlan, renderParent } from '../_lib/stagedSlack'
import { RUN_DIR, sqliteD1, withAdminEmails } from '../_lib/testD1'
import { onRequestPost } from './actions'

vi.mock('../_lib/gcp.js', async original => ({ ...await original<typeof import('../_lib/gcp.js')>(), gcpToken: async () => 'token' }))
vi.mock('../_lib/stagedSlack.js', async original => ({ ...await original<typeof import('../_lib/stagedSlack.js')>(), notifyPlan: vi.fn(async () => undefined) }))

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.mocked(notifyPlan).mockClear()
})

const BUCKET = 'gcs-a'
const PREFIX = `gs://${BUCKET}/a/`
const SITE = 'https://site.example.test'
const RESPOND = 'https://hooks.slack.test/respond'
const SECRET = 'signing'
const ENV = {
  SLACK_SIGNING_SECRET: SECRET, SLACK_BOT_TOKEN: 'bot', STAFF_DOMAIN: 'example.test', EXECUTOR: 'sweep',
  GCP_SA_KEY: 'key', JOB_SA: 'job@example.test', GCP_PROJECT: 'project', DATA_BUCKET: 'my-data', SWEEP_IMAGE: 'image@sha256:abc', CF_ACCOUNT_ID: 'account', D1_DB_ID: 'db',
  STORE_SCHEME: 'gs://', STORE_BUCKETS: BUCKET, BUCKET_REGIONS: JSON.stringify({ [BUCKET]: 'us-east5' }),
}

/** A plan with a finished dry run of its one item, the minimum at `minimum`. */
async function world(minimum: number) {
  const { db, raw } = await sqliteD1()
  const digest = await planDigest([PREFIX])
  raw.exec("INSERT INTO plans (id, name, created_by, created_ts) VALUES (1, 'review', 'ops@example.test', 1)")
  raw.prepare("INSERT INTO plan_items (plan_id, prefix, added_by, added_ts) VALUES (1, ?, 'ops@example.test', 1)").run(PREFIX)
  const dry = { run_id: 'dry', mode: 'dry', scan: '2026-11-04', actor: 'ops@example.test', started_ts: 1, finished_ts: 2, deleted_bytes: 1, deleted_objects: 1, skipped_gone: 0, skipped_overwritten: 0, plan_digest: digest } as const
  raw.prepare(`INSERT INTO deletion_runs (run_id, plan_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, deleted_bytes, deleted_objects, skipped_gone, skipped_overwritten, plan_digest, log_dir)
    VALUES ('dry', 1, 'manifest', '2026-11-04', 0, 0, 'ops@example.test', 'dry', 1, 2, 1, 1, 0, 0, ?, 'gs://my-data/sweep/runs/gcs-sweep-dry-20261104-000000z')`).run(digest)
  const setMinimum = (n: number) => raw.prepare("INSERT INTO sweep_settings (id, min_soft_delete_days, updated_by, updated_at) VALUES (1, ?, 'ops', 1) ON CONFLICT (id) DO UPDATE SET min_soft_delete_days = excluded.min_soft_delete_days").run(n)
  setMinimum(minimum)
  /** The button a card drawn at this minimum shows. */
  const button = (shown: number | undefined) => (renderParent({ planId: 1, siteUrl: SITE, items: 1, batches: 1, stagers: [], digest, actions: true, closed: false, runs: [dry], minSoftDeleteDays: shown }).blocks as { elements?: { action_id: string; value?: string }[] }[])
    .flatMap(b => b.elements ?? []).find(e => e.action_id === 'staged_real')!.value!
  return { db, raw, digest, setMinimum, button }
}

/** Click `value` on the real-delete button: what the clicker is told, and each submitted job's `-s`. */
async function click(db: unknown, value: string, env: Record<string, unknown> = ENV) {
  const told: string[] = []
  const minimums: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    if (url.startsWith('https://slack.com/api/users.info')) return Response.json({ ok: true, user: { profile: { email: 'ops@example.test' } } })
    if (url === RESPOND) { told.push((JSON.parse(String(init!.body)) as { text: string }).text); return new Response('ok') }
    if (url.includes('/jobs?job_id=')) {
      const spec = JSON.parse(String(init!.body)) as { taskGroups: { taskSpec: { runnables: { container: { commands: string[] } }[] } }[] }
      minimums.push(/ -s (\d+) /.exec(spec.taskGroups[0].taskSpec.runnables[0].container.commands[1])![1])
    }
    return new Response('{}', { status: 200 })
  }))
  const body = new URLSearchParams({ payload: JSON.stringify({ type: 'block_actions', user: { id: 'U1' }, response_url: RESPOND, actions: [{ action_id: 'staged_real', value }] }) }).toString()
  const ts = String(Math.floor(Date.now() / 1000))
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(SECRET), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
  const mac = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(`v0:${ts}:${body}`))
  const signature = `v0=${[...new Uint8Array(mac)].map(b => b.toString(16).padStart(2, '0')).join('')}`
  const waits: Promise<unknown>[] = []
  await onRequestPost({
    request: new Request(`${SITE}/slack/actions`, { method: 'POST', headers: { 'x-slack-request-timestamp': ts, 'x-slack-signature': signature }, body }),
    env: { ...env, DB: db } as never,
    waitUntil: p => { waits.push(p) },
  })
  await Promise.all(waits)
  return { told, minimums }
}

const REFUSED = (now: string, shown: string) => `Dispatch refused: not deleting: the soft-delete minimum is now ${now}, lower than the ${shown} these terms promised, so the undo they describe may not exist. Re-read the terms and confirm again.`

describe('the real-delete button', () => {
  it('a click on an old card after the minimum drops deletes nothing, and redraws the card', async () => {
    const { db, digest, setMinimum, button } = await world(7)
    const card = button(7)
    expect(card).toBe(`1:${digest}:7`)
    setMinimum(0)
    expect(await click(db, card)).toEqual({ told: [REFUSED('0 days', '7 days')], minimums: [] })
    // The refusal says to re-read, so the terms it means have to be there: the card is what's stale.
    expect(vi.mocked(notifyPlan).mock.calls.map(([, , plan, site, ev]) => [plan, site, ev])).toEqual([[1, SITE, undefined]])
  })
  it('a 2-part value, from before the field, counts as 7', async () => {
    const { db, digest, setMinimum } = await world(7)
    const old = `1:${digest}`
    setMinimum(3)
    expect(await click(db, old)).toEqual({ told: [REFUSED('3 days', '7 days')], minimums: [] })
    setMinimum(7)
    expect(await click(db, old)).toEqual({ told: [], minimums: ['7'] })
  })
  it('the redrawn card, naming 0, deletes under 0', async () => {
    const { db, button } = await world(0)
    expect(await click(db, button(0))).toEqual({ told: [], minimums: ['0'] })
  })
  it('raising the minimum since the card was drawn is no refusal: it runs under the higher one', async () => {
    const { db, button, setMinimum } = await world(7)
    const card = button(7)
    setMinimum(14)
    expect(await click(db, card)).toEqual({ told: [], minimums: ['14'] })
  })
  it.each(['1:abc:', '1:abc:x', '1:abc:-1', '1:abc:7.5', '1:abc:7:9', 'x:abc:7'])('%s is a bad value', async value => {
    const { db } = await world(7)
    expect(await click(db, value)).toEqual({ told: ['Bad button value.'], minimums: [] })
  })
  it('`plan-sweep`, with no soft-delete guard: its 2-part card reaches its executor, whatever `sweep_settings` says', async () => {
    const { db, digest, button } = await world(0)
    const card = button(undefined)
    expect(card).toBe(`1:${digest}`)
    const prepare = vi.spyOn(planSweep, 'prepare').mockResolvedValue(refuse(418, 'reached plan-sweep'))
    expect(await click(db, card, { ...ENV, EXECUTOR: 'plan-sweep' })).toEqual({ told: ['Dispatch refused: reached plan-sweep'], minimums: [] })
    expect(prepare.mock.calls.map(([, , req]) => req.shownMinSoftDeleteDays)).toEqual([undefined])
    expect(vi.mocked(notifyPlan)).not.toHaveBeenCalled()
  })
})

// Who may click: the clicker's Slack profile email, asked of `isAdmin` (the site's one admin rule), nothing of Slack's.
describe('who may dispatch from Slack', () => {
  const LISTED = { ...ENV, ADMIN_LIST: '1' }
  it('the list off: the staff domain dispatches', async () => {
    const { db, button } = await world(7)
    expect(await click(db, button(7))).toEqual({ told: [], minimums: ['7'] })
  })
  it('`ADMIN_LIST`: a staff-domain clicker off the list is refused (an empty list and a missing table too), and nothing is submitted', async () => {
    const [empty, other, missing] = [await world(7), await world(7), await world(7)]
    for (const w of [empty, other]) withAdminEmails(w.raw)
    other.raw.exec("INSERT INTO admin_emails (email, note, who, ts) VALUES ('lead@example.test', NULL, 'seed', 1)")
    missing.raw.exec('DROP TABLE IF EXISTS admin_emails')
    const refused = { told: ['Only admins can dispatch runs.'], minimums: [] }
    expect([await click(empty.db, empty.button(7), LISTED), await click(other.db, other.button(7), LISTED), await click(missing.db, missing.button(7), LISTED)]).toEqual([refused, refused, refused])
  })
  it('`ADMIN_LIST`: a listed clicker dispatches, and so does the break-glass address', async () => {
    const [listed, glass] = [await world(7), await world(7)]
    withAdminEmails(listed.raw)
    listed.raw.exec("INSERT INTO admin_emails (email, note, who, ts) VALUES ('ops@example.test', NULL, 'seed', 1)")
    glass.raw.exec('DROP TABLE IF EXISTS admin_emails')
    expect([await click(listed.db, listed.button(7), LISTED), await click(glass.db, glass.button(7), { ...LISTED, ADMIN_BOOTSTRAP: 'Ops@example.test' })]).toEqual([
      { told: [], minimums: ['7'] }, { told: [], minimums: ['7'] },
    ])
  })
})
