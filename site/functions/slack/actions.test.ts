// `/slack/actions`: a real delete is never dispatched from Slack. A click on "Delete for real…" still on an old card gets a link
// to `/staged`; Dry-run and Reject keep the site's rules, asked of the clicker's Slack profile email.
import { afterEach, describe, expect, it, vi } from 'vitest'
import { planDigest } from '../_lib/plans'
import { planSweep } from '../_lib/planDispatch'
import { notifyPlan } from '../_lib/stagedSlack'
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
const SCAN = '2026-11-04'
const ADMIN = 'ops@example.test'
const OUTSIDER = 'guest@elsewhere.test'
const ENV = {
  SLACK_SIGNING_SECRET: SECRET, SLACK_BOT_TOKEN: 'bot', STAFF_DOMAIN: 'example.test', EXECUTOR: 'sweep',
  GCP_SA_KEY: 'key', JOB_SA: 'job@example.test', GCP_PROJECT: 'project', DATA_BUCKET: 'my-data', SWEEP_IMAGE: 'image@sha256:abc', CF_ACCOUNT_ID: 'account', D1_DB_ID: 'db',
  STORE_SCHEME: 'gs://', STORE_BUCKETS: BUCKET, BUCKET_REGIONS: JSON.stringify({ [BUCKET]: 'us-east5' }),
}
const LISTED = { ...ENV, ADMIN_LIST: '1' }

/** A plan with a finished dry run of its one item (batch 5, staged by `stager`), and an indexed scan. */
async function world(stager = OUTSIDER) {
  const { db, raw } = await sqliteD1()
  const digest = await planDigest([PREFIX])
  raw.exec("INSERT INTO plans (id, name, created_by, created_ts) VALUES (1, 'review', 'ops@example.test', 1)")
  raw.prepare('INSERT INTO stage_batches (id, plan_id, created_by, created_ts) VALUES (5, 1, ?, 1)').run(stager)
  raw.prepare('INSERT INTO plan_items (plan_id, prefix, added_by, added_ts, batch_id) VALUES (1, ?, ?, 1, 5)').run(PREFIX, stager)
  raw.prepare(`INSERT INTO deletion_runs (run_id, plan_id, ${RUN_DIR}, scan, head, exec_head, actor, mode, started_ts, finished_ts, deleted_bytes, deleted_objects, skipped_gone, skipped_overwritten, plan_digest, log_dir)
    VALUES ('dry', 1, 'manifest', ?, 0, 0, 'ops@example.test', 'dry', 1, 2, 1, 1, 0, 0, ?, 'gs://my-data/sweep/runs/gcs-sweep-dry-20261104-000000z')`).run(SCAN, digest)
  raw.prepare("INSERT INTO index_schema (date, variant, version, schema_json, gen, dir) VALUES (?, 'path', 2, '[]', 'g', ?)").run(SCAN, `listing/${SCAN}/index/g`)
  const planItems = () => raw.prepare('SELECT prefix FROM plan_items').all()
  const runs = () => raw.prepare('SELECT run_id, mode FROM deletion_runs ORDER BY run_id').all()
  return { db, raw, digest, planItems, runs }
}

/** Click a button: every URL the handler fetched, the bodies of what it told the clicker, and the jobs it submitted. */
async function click(db: unknown, action_id: string, value: string | undefined, { env = ENV as Record<string, unknown>, email = ADMIN as string | null } = {}) {
  const urls: string[] = []
  const told: unknown[] = []
  const jobs: string[] = []
  vi.stubGlobal('fetch', vi.fn(async (url: string, init?: RequestInit) => {
    urls.push(url)
    if (url.startsWith('https://slack.com/api/users.info')) return Response.json({ ok: true, user: { profile: { email } } })
    if (url === RESPOND) { told.push(JSON.parse(String(init!.body))); return new Response('ok') }
    if (url.includes('/jobs?job_id=')) jobs.push(new URL(url).searchParams.get('job_id')!.replace(/-\d{8}-\d{6}z.*$/, ''))
    return new Response('{}', { status: 200 })
  }))
  const body = new URLSearchParams({ payload: JSON.stringify({ type: 'block_actions', user: { id: 'U1' }, response_url: RESPOND, actions: [{ action_id, ...(value === undefined ? {} : { value }) }] }) }).toString()
  const ts = String(Math.floor(Date.now() / 1000))
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(SECRET), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
  const mac = await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(`v0:${ts}:${body}`))
  const signature = `v0=${[...new Uint8Array(mac)].map(b => b.toString(16).padStart(2, '0')).join('')}`
  const waits: Promise<unknown>[] = []
  const res = await onRequestPost({
    request: new Request(`${SITE}/slack/actions`, { method: 'POST', headers: { 'x-slack-request-timestamp': ts, 'x-slack-signature': signature }, body }),
    env: { ...env, DB: db } as never,
    waitUntil: p => { waits.push(p) },
  })
  await Promise.all(waits)
  return { status: res.status, urls, told, jobs }
}

const said = (text: string) => ({ response_type: 'ephemeral', replace_original: false, text })

describe("an old card's real-delete button", () => {
  const ANSWER = {
    status: 200,
    urls: [RESPOND],
    told: [said(`Slack no longer starts deletes, and this click deleted nothing. To delete, use <${SITE}/staged|the staged page>.`)],
    jobs: [],
  }
  it.each<[string, (digest: string) => string | undefined]>([
    ['the value a card with the minimum carries', d => `1:${d}:7`],
    ['the value from before the minimum', d => `1:${d}`],
    ['a value that never parsed', () => 'x:abc:7'],
    ['no value', () => undefined],
  ])('%s: dispatches nothing, asks nothing of Slack or D1, and links to /staged', async (_, value) => {
    const w = await world()
    expect(await click(w.db, 'staged_real', value(w.digest))).toEqual(ANSWER)
    expect(w.runs()).toEqual([{ run_id: 'dry', mode: 'dry' }])
    expect(vi.mocked(notifyPlan)).not.toHaveBeenCalled()
  })
  it('the same for a listed admin, under every executor', async () => {
    const w = await world()
    withAdminEmails(w.raw)
    w.raw.prepare("INSERT INTO admin_emails (email, note, who, ts) VALUES (?, NULL, 'seed', 1)").run(ADMIN)
    const prepare = vi.spyOn(planSweep, 'prepare')
    expect([
      await click(w.db, 'staged_real', `1:${w.digest}:7`, { env: LISTED }),
      await click(w.db, 'staged_real', `1:${w.digest}`, { env: { ...ENV, EXECUTOR: 'plan-sweep' } }),
    ]).toEqual([ANSWER, ANSWER])
    expect(prepare).not.toHaveBeenCalled()
  })
})

describe("the new card's link", () => {
  it.each(['staged_open', 'staged_delete_link'])('%s, a URL button Slack still calls here for: acked, nothing else', async action => {
    const w = await world()
    expect(await click(w.db, action, undefined)).toEqual({ status: 200, urls: [], told: [], jobs: [] })
  })
})

describe('Dry-run', () => {
  const REFUSED = (text: string) => ({ status: 200, told: [said(text)], jobs: [] })
  const without = ({ urls: _, ...r }: Awaited<ReturnType<typeof click>>) => r
  it('an admin dispatches a dry run of the newest scan, announced as from Slack', async () => {
    const w = await world()
    expect(without(await click(w.db, 'staged_dry', '1'))).toEqual({ status: 200, told: [], jobs: ['gcs-sweep-dry'] })
    expect(vi.mocked(notifyPlan).mock.calls.map(([, , plan, site, ev]) => {
      const { run, phase, via } = ev as { run: { mode: string; scan: string; actor: string }; phase: string; via: string }
      return [plan, site, run.mode, run.scan, run.actor, phase, via]
    })).toEqual([[1, SITE, 'dry', SCAN, ADMIN, 'dispatched', 'Slack']])
  })
  it('anyone else is refused, and nothing is submitted', async () => {
    const w = await world()
    expect(without(await click(w.db, 'staged_dry', '1', { email: OUTSIDER }))).toEqual(REFUSED('Only admins can dispatch runs.'))
  })
  it('`ADMIN_LIST`: a staff-domain clicker off the list is refused, a listed one dispatches, as does the break-glass address', async () => {
    const [off, listed, glass] = [await world(), await world(), await world()]
    withAdminEmails(off.raw)
    withAdminEmails(listed.raw)
    listed.raw.prepare("INSERT INTO admin_emails (email, note, who, ts) VALUES (?, NULL, 'seed', 1)").run(ADMIN)
    glass.raw.exec('DROP TABLE IF EXISTS admin_emails')
    expect([
      without(await click(off.db, 'staged_dry', '1', { env: LISTED })),
      without(await click(listed.db, 'staged_dry', '1', { env: LISTED })),
      without(await click(glass.db, 'staged_dry', '1', { env: { ...LISTED, ADMIN_BOOTSTRAP: 'Ops@example.test' } })),
    ]).toEqual([
      REFUSED('Only admins can dispatch runs.'),
      { status: 200, told: [], jobs: ['gcs-sweep-dry'] },
      { status: 200, told: [], jobs: ['gcs-sweep-dry'] },
    ])
  })
  it('refuses without dispatch configured, on a bad value, a missing or closed plan, and with no scan', async () => {
    const [w, closed, unscanned] = [await world(), await world(), await world()]
    closed.raw.exec("UPDATE plans SET state = 'closed' WHERE id = 1")
    unscanned.raw.exec('DELETE FROM index_schema')
    expect([
      without(await click(w.db, 'staged_dry', '1', { env: { ...ENV, GCP_SA_KEY: undefined } })),
      without(await click(w.db, 'staged_dry', 'x')),
      without(await click(w.db, 'staged_dry', '99')),
      without(await click(closed.db, 'staged_dry', '1')),
      without(await click(unscanned.db, 'staged_dry', '1')),
    ]).toEqual([
      REFUSED('Dispatch is not configured for this deployment; use www.'),
      REFUSED('Bad button value.'),
      REFUSED('That plan no longer exists.'),
      REFUSED('That plan is closed.'),
      REFUSED('No indexed scan to run against.'),
    ])
  })
})

describe('Reject', () => {
  const outcome = async (w: Awaited<ReturnType<typeof world>>, email: string) => {
    const { told, jobs } = await click(w.db, 'staged_reject', '1:5', { email })
    return { told, jobs, items: w.planItems(), notified: vi.mocked(notifyPlan).mock.calls.length }
  }
  const STAYS = { told: [said('Only an admin or the person who staged a batch can reject it.')], jobs: [], items: [{ prefix: PREFIX }], notified: 0 }
  const GONE = { told: [], jobs: [], items: [], notified: 1 }
  it("an admin rejects anyone's batch", async () => {
    expect(await outcome(await world(OUTSIDER), ADMIN)).toEqual(GONE)
  })
  it('the stager, no admin, rejects their own', async () => {
    expect(await outcome(await world(OUTSIDER), OUTSIDER)).toEqual(GONE)
  })
  it('anyone else is refused', async () => {
    expect(await outcome(await world('someone@elsewhere.test'), OUTSIDER)).toEqual(STAYS)
  })
})
