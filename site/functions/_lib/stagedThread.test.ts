// The staged plan's Slack thread end to end (`notifyPlan`, `refreshThread`,
// `/slack/actions`' reject) over an in-memory D1 (the cw lineage) and a faked
// Slack Web API: no mentions in automatic posts, a stager's stages coalescing
// into one reply, rotation when the parent carries a ✅, and the parent sized
// over the whole queue.
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { D1Database } from '@cloudflare/workers-types'
import { MAX_PREFIXES, type PrefixStat } from './prefixes.js'
import { LINEAGES, migrations, sqliteD1 } from './testD1.js'

const SCAN = '2026-10-08'
const TiB = 2 ** 40
// Every prefix holds 1 TiB / 10 objects at the scan, owned by `ann-bee`,
// except those under an `empty/` folder (absent: nothing left).
const lookups: number[] = []
vi.mock('./index.js', async orig => ({
  ...(await orig<typeof import('./index.js')>()),
  storeReady: () => true,
  pathScans: async () => ({ results: [{ date: SCAN }] }),
}))
vi.mock('./prefixes.js', async orig => ({
  ...(await orig<typeof import('./prefixes.js')>()),
  prefixesAt: async (_env: unknown, date: string, prefixes: string[]) => {
    if (date !== SCAN) throw new Error(`unexpected scan ${date}`)
    if (prefixes.length > MAX_PREFIXES) throw new Error(`lookup too wide: ${prefixes.length} prefixes`)
    lookups.push(prefixes.length)
    const stats: Record<string, PrefixStat> = {}
    for (const p of prefixes) if (!p.includes('/empty/')) stats[p] = { b: TiB, o: 10, us: [['ann-bee', TiB]] }
    return { stats, groups: 1 }
  },
}))
vi.mock('./identity.js', async orig => ({
  ...(await orig<typeof import('./identity.js')>()),
  loadRegistry: async () => ({ 'ann-bee': { name: 'Ann Bee' } }),
}))

const { notifyPlan, refreshThread } = await import('./stagedSlack.js')
const { onRequestPost: slackActions } = await import('../slack/actions.js')

const SITE = 'https://site.example.org'
const CH = 'CTEST'
const PARENT = '100.000001'
const ANN = 'ann.bee@example.org'
const env = { SLACK_BOT_TOKEN: 'xoxb-test', SLACK_ADMIN_CHANNEL: CH, SLACK_SIGNING_SECRET: 'shh' }

interface Call { method: string; body: Record<string, unknown> }

/** A fake Slack: posts get increasing ts (`200.000001`, …), the parent's
 *  reactions come from `reactions`, every call is recorded. */
function fakeSlack({ reactions = {} as Record<string, string[]>, delayMs = 0 } = {}): Call[] {
  const calls: Call[] = []
  let n = 0
  const json = (o: unknown) => new Response(JSON.stringify(o))
  vi.stubGlobal('fetch', vi.fn(async (input: string, init?: RequestInit) => {
    const url = new URL(input)
    const method = url.pathname.replace('/api/', '')
    const body = (init?.body ? JSON.parse(String(init.body)) : Object.fromEntries(url.searchParams)) as Record<string, string>
    if (method !== 'users.lookupByEmail' && method !== 'users.info') calls.push({ method, body })
    if (delayMs) await new Promise(r => setTimeout(r, delayMs))
    switch (method) {
      case 'chat.postMessage': return json({ ok: true, channel: body.channel, ts: `200.${String(++n).padStart(6, '0')}` })
      case 'conversations.replies': return json({ ok: true, messages: [{ ts: body.ts, reactions: (reactions[body.ts] ?? []).map(name => ({ name, count: 1, users: ['U9'] })) }] })
      case 'chat.getPermalink': return json({ ok: true, permalink: `https://ws.slack.com/archives/${body.channel}/p${body.message_ts.replace('.', '')}` })
      case 'users.lookupByEmail': return json(body.email === ANN ? { ok: true, user: { id: 'UANN', profile: { real_name: 'Ann Bee', image_192: 'https://img/ann.png' } } } : { ok: false, error: 'users_not_found' })
      case 'users.info': return json({ ok: true, user: { profile: { email: ANN } } })
      default: return json({ ok: true })
    }
  }))
  return calls
}

const sectionText = (blocks: unknown): string => ((blocks as { text?: { text: string } }[])[0].text?.text ?? '')
/** A call as one readable line: where it went and its first section's text. */
function show(c: Call): string {
  const b = c.body
  switch (c.method) {
    case 'chat.postMessage': return `post ${b.thread_ts ? `in ${b.thread_ts}` : 'parent'}: ${b.blocks ? sectionText(b.blocks) : b.text}`
    case 'chat.update': return `update ${b.ts}: ${b.blocks ? sectionText(b.blocks) : b.text}`
    case 'conversations.replies': return `reactions? ${b.ts}`
    case 'chat.getPermalink': return `permalink ${b.message_ts}`
    default: return `${c.method} ${JSON.stringify(b)}`
  }
}
/** The calls other than the parent's routine re-render. */
const events = (calls: Call[]): string[] => calls.filter(c => !(c.method === 'chat.update' && c.body.ts === PARENT && !String(sectionText(c.body.blocks)).startsWith('*Staged plan'))).map(show)

const PARENT_TEXT = (paths: number, batches: number, size: string, objects: string) =>
  `*Staged for deletion* · plan #1 · ${paths.toLocaleString('en-US')} prefixes in ${batches} ${batches === 1 ? 'batch' : 'batches'}\nstaged by Ann Bee\n*${size}* · ${objects} objects at scan ${SCAN} · owners: Ann Bee ${size}`

/** Plan 1, its thread already posted; `seed`: an older batch (#100, one
 *  prefix) is still queued, so a stage isn't into an emptied queue. */
async function planDb({ seed = true } = {}): Promise<{ db: D1Database; raw: Awaited<ReturnType<typeof sqliteD1>>['raw'] }> {
  const { db, raw } = await sqliteD1('cw')
  await db.prepare("INSERT INTO plans (id, name, state, created_by, created_ts, slack_channel, slack_ts) VALUES (1, 'Staged', 'open', ?, 1, ?, ?)").bind(ANN, CH, PARENT).run()
  if (seed) await stage(db, 100, ['gs://b/keep/'])
  return { db, raw }
}

/** What `stageItems` leaves behind: a batch and its items. */
async function stage(db: D1Database, id: number, prefixes: string[], { by = ANN, note = null as string | null, ts = 1 } = {}) {
  await db.prepare('INSERT INTO stage_batches (id, plan_id, note, created_by, created_ts) VALUES (?, 1, ?, ?, ?)').bind(id, note, by, ts).run()
  for (const p of prefixes) await db.prepare('INSERT INTO plan_items (plan_id, prefix, added_by, added_ts, batch_id) VALUES (1, ?, ?, ?, ?)').bind(p, by, ts, id).run()
  return { stage: { planId: 1, batchId: id, by, prefixes, covered: 0, note, siteUrl: SITE } }
}
const run = (i: number, n: number) => Array.from({ length: n }, (_, k) => `gs://b/ckpt/run-${i}/step-${k}/`)

afterEach(() => { vi.unstubAllGlobals(); lookups.length = 0 })

describe('the parent sizes the whole queue, as /staged does', () => {
  it(`chunks past ${MAX_PREFIXES} prefixes instead of counting the rest empty`, async () => {
    const calls = fakeSlack()
    const { db } = await planDb({ seed: false })
    const prefixes = Array.from({ length: 2500 }, (_, i) => `gs://b/ckpt/run-${i % 50}/step-${i}/`)
    await stage(db, 1, [...prefixes, 'gs://b/empty/x/'])
    await notifyPlan(env, db, 1, SITE, undefined, { now: 1000 })
    expect([lookups, calls.map(c => [c.method, c.body.ts, sectionText(c.body.blocks)])]).toEqual([
      [1000, 1000, 501],
      [['chat.update', PARENT, PARENT_TEXT(2501, 1, '2.4 PiB', '25,000').replace(` · owners`, ' · 1 empty · owners')]],
    ])
  })
})

describe('stage replies coalesce per stager', () => {
  it('one reply per stager within 15 min of its last update, edited in place; a new one after', async () => {
    const calls = fakeSlack()
    const { db, raw } = await planDb()
    await notifyPlan(env, db, 1, SITE, await stage(db, 1, run(1, 2)), { now: 1000 })
    await notifyPlan(env, db, 1, SITE, await stage(db, 2, run(2, 3), { note: 'old ckpts' }), { now: 1000 + 600 })
    await notifyPlan(env, db, 1, SITE, await stage(db, 3, run(3, 1)), { now: 1600 + 900 })
    await notifyPlan(env, db, 1, SITE, await stage(db, 4, run(4, 1)), { now: 2500 + 901 })
    expect(events(calls)).toEqual([
      `reactions? ${PARENT}`,
      `post in ${PARENT}: :wastebasket: *Ann Bee* staged 2 paths · *2.0 TiB* · 20 objects\nin \`b/ckpt/run-1/\`: \`step-0/\` 1.0 TiB, \`step-1/\` 1.0 TiB`,
      `reactions? ${PARENT}`,
      `update 200.000001: :wastebasket: *Ann Bee* staged 5 paths in 2 batches · *5.0 TiB* · 50 objects\nin \`b/ckpt/\`: \`run-2/\` (3) 3.0 TiB, \`run-1/\` (2) 2.0 TiB\n> old ckpts`,
      `reactions? ${PARENT}`,
      `update 200.000001: :wastebasket: *Ann Bee* staged 6 paths in 3 batches · *6.0 TiB* · 60 objects\nin \`b/ckpt/\`: \`run-2/\` (3) 3.0 TiB, \`run-1/\` (2) 2.0 TiB, \`run-3/step-0/\` 1.0 TiB\n> old ckpts`,
      `reactions? ${PARENT}`,
      `post in ${PARENT}: :wastebasket: *Ann Bee* staged 1 path · *1.0 TiB* · 10 objects\n\`b/ckpt/run-4/step-0/\` 1.0 TiB`,
    ])
    expect([
      raw.prepare('SELECT id, stager, thread_ts, slack_ts, open, created_ts, updated_ts FROM stage_replies ORDER BY id').all(),
      raw.prepare('SELECT id, reply_id FROM stage_batches WHERE id < 100 ORDER BY id').all(),
    ]).toEqual([
      [
        { id: 1, stager: ANN, thread_ts: PARENT, slack_ts: '200.000001', open: null, created_ts: 1000, updated_ts: 2500 },
        { id: 2, stager: ANN, thread_ts: PARENT, slack_ts: '200.000002', open: 1, created_ts: 3401, updated_ts: 3401 },
      ],
      [{ id: 1, reply_id: 1 }, { id: 2, reply_id: 1 }, { id: 3, reply_id: 1 }, { id: 4, reply_id: 2 }],
    ])
    // The reply posts as the stager, its one button rejects every batch it covers.
    const first = calls.find(c => c.method === 'chat.postMessage')!.body
    const upd = calls.filter(c => c.method === 'chat.update' && c.body.ts === '200.000001').at(-1)!.body
    expect([
      [first.username, first.icon_url, first.text],
      (upd.blocks as { elements?: unknown[] }[])[1].elements,
    ]).toEqual([
      ['Ann Bee · staged', 'https://img/ann.png', 'Ann Bee staged 2 paths · 2.0 TiB'],
      [
        { type: 'button', action_id: 'staged_reject', value: '1:r1', text: { type: 'plain_text', text: 'Reject 3 batches' },
          confirm: { title: { type: 'plain_text', text: 'Reject these 3 batches?' }, text: { type: 'mrkdwn', text: 'Unstages the 6 paths they staged. Nothing is deleted.' }, confirm: { type: 'plain_text', text: 'Reject' }, deny: { type: 'plain_text', text: 'Cancel' } } },
        { type: 'button', action_id: 'staged_open', text: { type: 'plain_text', text: 'View in www' }, url: `${SITE}/staged` },
      ],
    ])
  })

  it('concurrent stages by one stager claim one reply, which ends up covering both', async () => {
    const calls = fakeSlack({ delayMs: 5 })
    const { db, raw } = await planDb()
    const [a, b] = [await stage(db, 1, run(1, 1)), await stage(db, 2, run(2, 1))]
    await Promise.all([notifyPlan(env, db, 1, SITE, a, { now: 1000 }), notifyPlan(env, db, 1, SITE, b, { now: 1001 })])
    const replies = calls.filter(c => c.body.thread_ts === PARENT || (c.method === 'chat.update' && c.body.ts !== PARENT))
    expect([
      replies.filter(c => c.method === 'chat.postMessage').length,
      sectionText(replies.at(-1)!.body.blocks),
      raw.prepare('SELECT id, slack_ts, open FROM stage_replies').all(),
      raw.prepare('SELECT id, reply_id FROM stage_batches WHERE id < 100 ORDER BY id').all(),
    ]).toEqual([
      1,
      ':wastebasket: *Ann Bee* staged 2 paths in 2 batches · *2.0 TiB* · 20 objects\nin `b/ckpt/`: `run-1/step-0/` 1.0 TiB, `run-2/step-0/` 1.0 TiB',
      [{ id: 1, slack_ts: '200.000001', open: 1 }],
      [{ id: 1, reply_id: 1 }, { id: 2, reply_id: 1 }],
    ])
  })
})

describe('`stage_replies` (cw `0015`, gcs `0039`)', () => {
  it('applies on every lineage this branch carries it in, FKs on; one open reply per plan and stager is the claim', async () => {
    // The base carries only cw; a deployment branch adds its own lineage's twin.
    const has = await Promise.all(LINEAGES.map(async l => (await migrations(l).catch(() => [])).some(m => m.name.endsWith('_stage_replies.sql'))))
    const lineages = LINEAGES.filter((_, i) => has[i])
    const out: unknown[] = []
    for (const lineage of lineages) {
      const { raw } = await sqliteD1(lineage)
      raw.exec("INSERT INTO plans (id, name, state, created_by, created_ts) VALUES (1, 'Staged', 'open', 'a', 1)")
      const claim = (stager: string) => Number(raw.prepare("INSERT OR IGNORE INTO stage_replies (plan_id, stager, channel, thread_ts, open, created_ts, updated_ts) VALUES (1, ?, 'C', '1.0', 1, 1, 1)").run(stager).changes)
      const claims = [claim('a'), claim('a'), claim('b')]
      raw.exec("UPDATE stage_replies SET open = NULL WHERE stager = 'a'")
      claims.push(claim('a'))
      const fk = (() => { try { raw.exec("INSERT INTO stage_batches (plan_id, created_by, created_ts, reply_id) VALUES (1, 'a', 1, 99)"); return 'accepted' } catch (e) { return (e as Error).message } })()
      out.push([lineage, claims, raw.prepare('SELECT id, stager, open FROM stage_replies ORDER BY id').all(), fk])
    }
    // (The ignored claim still spends an AUTOINCREMENT id.)
    const rows = [{ id: 1, stager: 'a', open: null }, { id: 3, stager: 'b', open: 1 }, { id: 4, stager: 'a', open: 1 }]
    expect([lineages[0], out]).toEqual(['cw', lineages.map(l => [l, [1, 0, 1, 1], rows, 'FOREIGN KEY constraint failed'])])
  })
})

describe('a ✅ on the parent rotates the thread', () => {
  it('the next post starts a new thread; the old one ends with a pointer', async () => {
    const calls = fakeSlack({ reactions: { [PARENT]: ['eyes', 'white_check_mark'] } })
    const { db, raw } = await planDb()
    await stage(db, 1, run(1, 2))
    await notifyPlan(env, db, 1, SITE, await stage(db, 2, run(2, 1)), { now: 1000 })
    // The new parent carries no ✅: the next stage stays in its thread.
    await notifyPlan(env, db, 1, SITE, await stage(db, 3, run(3, 1)), { now: 1100 })
    const NEW = '200.000001'
    const link = `<https://ws.slack.com/archives/${CH}/p200000001|a new thread>`
    expect(events(calls)).toEqual([
      `reactions? ${PARENT}`,
      `post parent: ${PARENT_TEXT(4, 3, '4.0 TiB', '40')}`,
      `permalink ${NEW}`,
      `post in ${PARENT}: :white_check_mark: Marked done. Continued in ${link}.`,
      `update ${PARENT}: *Staged plan #1* · this thread is done; continued in ${link}.`,
      `post in ${NEW}: :wastebasket: *Ann Bee* staged 1 path · *1.0 TiB* · 10 objects\n\`b/ckpt/run-2/step-0/\` 1.0 TiB`,
      `reactions? ${NEW}`,
      `update ${NEW}: ${PARENT_TEXT(5, 4, '5.0 TiB', '50')}`,
      `update 200.000003: :wastebasket: *Ann Bee* staged 2 paths in 2 batches · *2.0 TiB* · 20 objects\nin \`b/ckpt/\`: \`run-2/step-0/\` 1.0 TiB, \`run-3/step-0/\` 1.0 TiB`,
    ])
    expect(raw.prepare('SELECT slack_channel, slack_ts FROM plans').all()).toEqual([{ slack_channel: CH, slack_ts: NEW }])
  })

  it('a stager\'s open reply in the old thread is not reused in the new one', async () => {
    const reactions: Record<string, string[]> = {}
    const calls = fakeSlack({ reactions })
    const { db } = await planDb()
    await notifyPlan(env, db, 1, SITE, await stage(db, 1, run(1, 1)), { now: 1000 })
    reactions[PARENT] = ['white_check_mark']
    await notifyPlan(env, db, 1, SITE, await stage(db, 2, run(2, 1)), { now: 1060 })
    expect(calls.filter(c => c.method === 'chat.postMessage').map(show)).toEqual([
      `post in ${PARENT}: :wastebasket: *Ann Bee* staged 1 path · *1.0 TiB* · 10 objects\n\`b/ckpt/run-1/step-0/\` 1.0 TiB`,
      `post parent: ${PARENT_TEXT(3, 3, '3.0 TiB', '30')}`,
      `post in ${PARENT}: :white_check_mark: Marked done. Continued in <https://ws.slack.com/archives/${CH}/p200000002|a new thread>.`,
      `post in 200.000002: :wastebasket: *Ann Bee* staged 1 path · *1.0 TiB* · 10 objects\n\`b/ckpt/run-2/step-0/\` 1.0 TiB`,
    ])
  })

  it('a thread in another channel than `SLACK_ADMIN_CHANNEL` is left alone: a new one starts there', async () => {
    const calls = fakeSlack()
    const { db, raw } = await planDb()
    await notifyPlan({ ...env, SLACK_ADMIN_CHANNEL: 'COTHER' }, db, 1, SITE, await stage(db, 1, run(1, 1)), { now: 1000 })
    expect([calls.map(c => [c.method, c.body.channel, c.body.thread_ts ?? null]), raw.prepare('SELECT slack_channel, slack_ts FROM plans').all()]).toEqual([
      [['chat.postMessage', 'COTHER', null], ['chat.postMessage', 'COTHER', '200.000001']],
      [{ slack_channel: 'COTHER', slack_ts: '200.000001' }],
    ])
  })
})

describe('no mentions in automatic posts', () => {
  it('stages, runs, refreshes and a reject name people in plain text', async () => {
    const calls = fakeSlack()
    const { db, raw } = await planDb()
    await notifyPlan(env, db, 1, SITE, await stage(db, 1, run(1, 2)), { now: 1000 })
    await db.prepare(`INSERT INTO deletion_runs (run_id, manifest, scan, head, exec_head, actor, mode, started_ts, finished_ts, deleted_bytes, deleted_objects,
      skipped_gone, skipped_overwritten, drift_dirs, log_dir, plan_id, plan_digest) VALUES ('dry-1', 'm', ?, 0, 0, ?, 'dry', 1100, NULL, 0, 0, 0, 0, 0, 'l', 1, 'D')`).bind(SCAN, ANN).run()
    const dry = raw.prepare("SELECT * FROM deletion_runs WHERE run_id = 'dry-1'").all()[0] as never
    await notifyPlan(env, db, 1, SITE, { run: dry, phase: 'dispatched', via: 'www' }, { now: 1100 })
    await refreshThread(env, db, 1, SITE, {})
    // Ann rejects her reply's batches from Slack.
    const payload = JSON.stringify({ type: 'block_actions', user: { id: 'UANN' }, actions: [{ action_id: 'staged_reject', value: '1:r1' }] })
    const body = `payload=${encodeURIComponent(payload)}`
    const ts = String(Math.floor(Date.now() / 1000))
    const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(env.SLACK_SIGNING_SECRET), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'])
    const sig = 'v0=' + [...new Uint8Array(await crypto.subtle.sign('HMAC', key, new TextEncoder().encode(`v0:${ts}:${body}`)))].map(b => b.toString(16).padStart(2, '0')).join('')
    const waits: Promise<unknown>[] = []
    const res = await slackActions({
      request: new Request(`${SITE}/slack/actions`, { method: 'POST', body, headers: { 'x-slack-request-timestamp': ts, 'x-slack-signature': sig } }),
      env: { ...env, DB: db } as never,
      waitUntil: p => { waits.push(p) },
    })
    await Promise.all(waits)
    const posts = calls.filter(c => c.method === 'chat.postMessage' || (c.method === 'chat.update' && c.body.ts !== PARENT))
    expect([
      res.status,
      posts.map(show),
      calls.filter(c => JSON.stringify(c.body).includes('<@')),
      raw.prepare('SELECT count(*) AS n FROM plan_items').all(),
    ]).toEqual([
      200,
      [
        `post in ${PARENT}: :wastebasket: *Ann Bee* staged 2 paths · *2.0 TiB* · 20 objects\nin \`b/ckpt/run-1/\`: \`step-0/\` 1.0 TiB, \`step-1/\` 1.0 TiB`,
        `post in ${PARENT}: :test_tube: Dry-run dispatched by Ann Bee via www on scan ${SCAN} (<${SITE}/staged?run=dry-1|dry-1>)`,
        `update 200.000001: :wastebasket: *Ann Bee* staged 2 paths · *2.0 TiB* · 20 objects\nin \`b/ckpt/run-1/\`: \`step-0/\` 1.0 TiB, \`step-1/\` 1.0 TiB`,
        `post in ${PARENT}: :no_entry_sign: Ann Bee rejected 1 batch (2 paths unstaged)`,
        'update 200.000001: :wastebasket: *Ann Bee* staged 1 batch · nothing left staged',
      ],
      [],
      [{ n: 1 }],
    ])
  })
})
