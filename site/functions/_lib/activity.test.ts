// The activity feed (specs/done/activity-feed.md): which writes and client events are which kind, the per-kind config,
// bursts over D1, the Slack / Discord renderings, and the sinks (claims, edits in place, the throttle's trailing
// render) against a stubbed `fetch`.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { sqliteD1 } from './testD1'
import { gateFor } from './auth'
import {
  type ActivityEnv, burstEvents, bump, burstFor, feedClient, listBursts, loadBurst, purgeActivity, readKinds, recordServer,
  rerender, THROTTLE_MS, type Wait, writeKind,
} from './activity'
import {
  addTrail, authLine, type Burst, clientKind, DISCORD, emptyTrail, highlights, joins, keptEvents, mergeKinds, pageLines,
  renderDetails, renderDiscord, renderHeadline, renderLines, renderSummary, senderName, type ServerEvent, serverKind, sessionLinks, SLACK, tallyClient,
} from './activityShape'
import { captureWrite, noteActivity } from './activityHook'
import { d1SessionLogStore } from '@open-athena/auth/session-log/d1'
import type { SessionLogWho, SlogBatch } from '@open-athena/auth/session-log'
const ingest = (db: D1Database, who: SessionLogWho, ua: string | null, batch: SlogBatch, now: number) => d1SessionLogStore(db).ingest(who, ua, batch, now)
import { resetSlackPeople, resetSlackScopes } from './slack'
import type { SlogEvent } from './sessionLogShape'

const NOW = Date.UTC(2026, 9, 21, 15, 0, 0)  // 2026-10-21T15:00Z
const S = Math.floor(NOW / 1000)
const ORIGIN = 'https://gcs.example.test'

describe('kinds', () => {
  it('maps writes to kinds; plumbing and reads are not events', () => {
    const cases: [string, string][] = [
      ['POST /api/actions', 'assign'],
      ['PATCH /api/annotations', 'note'],
      ['POST /api/plans/12/items', 'stage'],
      ['POST /api/sweep/dispatch', 'deletion'],
      ['POST /api/plan-sweep/undo', 'deletion'],
      ['POST /api/token', 'token'],
      ['PUT /api/auth/profile', 'profile'],
      ['POST /api/auth/avatar', 'profile'],
      ['POST /api/auth/grants', 'access'],
      ['POST /api/og/mint', 'access'],
      ['PATCH /api/db/allowed_emails', 'admin'],
      ['POST /api/admins', 'admin'],
      ['DELETE /api/admins', 'admin'],
      ['PUT /api/session-log/switch', 'admin'],
      ['PUT /api/activity/kinds/view', 'admin'],
      ['PUT /api/digest-settings', 'admin'],
      ['PUT /api/sweep/settings', 'admin'],
      ['POST /api/digest-op', 'digest'],
      ['POST /api/digest-image', '-'],
      ['POST /api/owner-prices', 'write'],
      ['GET /api/actions', '-'],
      ['POST /api/session-log', '-'],
      ['POST /api/prefixes', '-'],
      ['POST /api/footer-cache', '-'],
      ['POST /api/auth/exchange', '-'],
      ['POST /api/auth/email/start', '-'],
      ['POST /auth/google/onetap', '-'],
      ['POST /slack/actions', '-'],
    ]
    expect(cases.map(([req]) => { const [m, p] = req.split(' '); return [req, serverKind(m, p) ?? '-'] })).toEqual(cases)
  })

  it('maps session-log events to kinds; a failed request is an error, one the page cancelled is not', () => {
    const evs = [
      { k: 'start' }, { k: 'nav' }, { k: 'click' }, { k: 'console' }, { k: 'api', s: 200 }, { k: 'api', s: 502 },
      { k: 'api', s: 0 }, { k: 'api', s: 0, err: 'abort' }, { k: 'dropped' },
    ] as const
    expect(evs.map(e => clientKind(e as never))).toEqual(['view', 'nav', 'click', 'error', 'api', 'error', 'error', 'api', null])
  })

  it('says what the auth package\'s writes did', () => {
    expect([
      authLine('POST', '/api/auth/grants'),
      authLine('POST', '/api/auth/grants/g1/revoke'),
      authLine('PATCH', '/api/auth/grants/g1'),
      authLine('POST', '/api/auth/requests/r9/approve'),
      authLine('DELETE', '/api/auth/allowed/bob%40example.test'),
      authLine('POST', '/api/auth/allowed/sync'),
    ]).toEqual([
      'minted a grant',
      'revoked grant g1',
      'edited grant g1',
      'approved access request r9',
      'removed bob@example.test from the sign-in allowlist',
      'synced the sign-in allowlist',
    ])
  })

  it('merges stored rows over the defaults, ignoring unknown kinds and bad values', () => {
    const conf = mergeKinds([
      { kind: 'view', log: 1, notify: 'off' },
      { kind: 'api', log: 0, notify: 'off' },
      { kind: 'nope', log: 1, notify: 'line' },
      { kind: 'click', log: 1, notify: 'loud' },
    ])
    expect([conf.view, conf.api, conf.click, conf.assign, conf.nope]).toEqual([
      { log: true, notify: 'off' }, { log: false, notify: 'off' }, { log: true, notify: 'count' }, { log: true, notify: 'line' }, undefined,
    ])
  })
})

describe('bursts (pure)', () => {
  it('joins within the gap, either side', () => {
    expect([joins(null, 100, 900), joins({ last_ts: 100 }, 1000, 900), joins({ last_ts: 100 }, 1001, 900), joins({ last_ts: 1000 }, 50, 900)])
      .toEqual([false, true, false, false])
  })

  it('tallies a batch\'s recorded kinds and the paths viewed', () => {
    const events = [
      { t: 1, k: 'start', u: '/scans?s=-t' }, { t: 2, k: 'nav', u: '/scans?s=t#runs' }, { t: 3, k: 'nav', u: 'https://x.test/gcs/a' },
      { t: 4, k: 'click', n: 'scan' }, { t: 5, k: 'api', u: '/api/series', s: 200 }, { t: 6, k: 'dropped', n: 3 },
    ] as SlogEvent[]
    const conf = mergeKinds([{ kind: 'api', log: 0, notify: 'off' }])
    expect(tallyClient(events, conf)).toEqual({
      counts: { view: 1, nav: 2, click: 1 }, pages: { '/scans': 2, '/gcs/a': 1 }, trail: ['/scans', '/scans', '/gcs/a'], errors: [],
    })
    expect(keptEvents(events, conf).map(e => e.k)).toEqual(['start', 'nav', 'nav', 'click', 'dropped'])
  })

  it('tallies errors with the page they happened on; a benign ResizeObserver report and a cancelled request are not', () => {
    const events = [
      { t: 1000, k: 'start', u: '/scans' }, { t: 2500, k: 'api', u: '/api/scan-runs', s: 0, ms: 3214, err: 'TypeError: Failed to fetch' },
      { t: 3000, k: 'nav', u: '/users?o=bob' }, { t: 4000, k: 'reject', msg: 'boom' }, { t: 5000, k: 'api', u: '/api/x', s: 404, ms: 9 },
      { t: 6000, k: 'error', msg: 'ResizeObserver loop completed with undelivered notifications.', src: '/users:0:0' },
      { t: 7000, k: 'api', u: '/api/diff?from=a&to=b', s: 0, ms: 553, err: 'abort' },
    ] as SlogEvent[]
    expect(tallyClient(events, mergeKinds([])).counts.error).toBe(2)
    expect(tallyClient(events, mergeKinds([])).errors).toEqual([
      { ts: 2, line: 'GET /api/scan-runs → failed · 3214ms · TypeError: Failed to fetch', page: '/scans' },
      { ts: 4, line: 'unhandled rejection: boom', page: '/users' },
    ])
  })

  it('a failed fetch no one saw fail (a hidden tab, offline, or delivered over a minute late) counts as a request, not an error', () => {
    const fail = { k: 'api', s: 0, ms: 1215, err: 'TypeError: Failed to fetch' }
    const events = [
      { t: 1000, k: 'start', u: '/' },
      { ...fail, t: 2000, u: '/api/a' },
      { ...fail, t: 3000, u: '/api/b', online: false, visibility: 'visible' },
      { ...fail, t: 4000, u: '/api/c', online: true, visibility: 'hidden' },
      { ...fail, t: 5000, u: '/api/d', online: true, visibility: 'visible' },
    ] as SlogEvent[]
    const tally = (receivedMs?: number) => {
      const { counts, errors } = tallyClient(events, mergeKinds([]), receivedMs)
      return { counts, errors: errors.map(e => e.line.split(' ')[1]) }
    }
    expect([tally(), tally(5000 + 60_000), tally(2000 + 60_001)]).toEqual([
      { counts: { view: 1, api: 2, error: 2 }, errors: ['/api/a', '/api/d'] },
      { counts: { view: 1, api: 3, error: 1 }, errors: ['/api/d'] },
      { counts: { view: 1, api: 3, error: 1 }, errors: ['/api/d'] },
    ])
  })

  it('keeps a trail: repeats of the previous path fold in, the latest stops kept', () => {
    const t = addTrail(emptyTrail(), ['/', '/', '/a', '/'])
    expect(t).toEqual({ n: 3, last: [['/', 2], ['/a', 1], ['/', 1]] })
    expect(addTrail(t, ['/', '/b', '/c'], 3)).toEqual({ n: 5, last: [['/', 2], ['/b', 1], ['/c', 1]] })
  })
})

describe('rendering', () => {
  const burst: Burst = {
    id: 7, actor: 'ann@example.test', via: 'session', origin: ORIGIN, first_ts: S - 2400, last_ts: S,
    counts: { assign: 3, view: 4, nav: 9, api: 30 }, pages: { '/': 3, '/users': 1 }, trail: { n: 2, last: [['/', 3], ['/users', 1]] },
    sids: ['sid-aaaaaaaaaa'],
  }
  const events: Pick<ServerEvent, 'ts' | 'kind' | 'line' | 'detail'>[] = [
    { ts: S - 2400, kind: 'assign', line: '`gs://b/x/` (1.0 GiB) → bob', detail: { pattern: 'gs://b/x/', owner: 'bob', bytes: 2 ** 30 } },
    { ts: S - 1200, kind: 'assign', line: '`gs://b/y/` (2.0 PiB) → bob', detail: { pattern: 'gs://b/y/', owner: 'bob', bytes: 2 * 2 ** 50 } },
    { ts: S, kind: 'assign', line: '`gs://b/z/` → nobody', detail: { pattern: 'gs://b/z/', owner: null } },
  ]
  const conf = mergeKinds([])

  it('summarizes a burst: who, when, reported counts, highlights, links', () => {
    expect(renderSummary(burst, { fmt: SLACK, conf, highlights: highlights(events) }).split('\n')).toEqual([
      '*ann* (browser) · 2026-10-21 14:20–15:00Z',
      '3 assignments · 4 page loads · 9 URL changes',
      'Largest assignment: `gs://b/y/` (2.0 PiB) → bob',
      'Owners: bob, cleared',
      `Pages: <${ORIGIN}/|/> ×3 → <${ORIGIN}/users|/users>`,
      `<${ORIGIN}/admin/activity?burst=7|activity> · <${ORIGIN}/admin/sessions/sid-aaaaaaaaaa|session sid-aaaa>`,
    ])
  })

  it("Slack's post is one line (sent as the user when signed in, else naming who and how); the rest is its first reply", () => {
    const o = { fmt: SLACK, conf, highlights: highlights(events) }
    const act = `<${ORIGIN}/admin/activity?burst=7|activity>`
    const job = { ...burst, actor: 'grant:cw-scan-job', via: 'grant' }
    expect([
      renderHeadline(burst, o, true),
      renderHeadline({ ...burst, via: 'grant' }, o, false),
      renderHeadline(job, o, true),
      renderHeadline(job, o, false),
      renderHeadline({ ...job, actor: 'grant:2EjUTg692KMP' }, { ...o, name: 'grant 2EjU' }, false),
    ]).toEqual([
      `3 assignments · 4 page loads · 9 URL changes · 2026-10-21 14:20–15:00Z · ${act}`,
      `*ann* (agent token / share link) · 3 assignments · 4 page loads · 9 URL changes · 2026-10-21 14:20–15:00Z · ${act}`,
      `agent token / share link · 3 assignments · 4 page loads · 9 URL changes · 2026-10-21 14:20–15:00Z · ${act}`,
      `*cw-scan-job* (agent token / share link) · 3 assignments · 4 page loads · 9 URL changes · 2026-10-21 14:20–15:00Z · ${act}`,
      `*grant 2EjU* (agent token / share link) · 3 assignments · 4 page loads · 9 URL changes · 2026-10-21 14:20–15:00Z · ${act}`,
    ])
    expect(renderDetails(burst, o).split('\n')).toEqual([
      'Largest assignment: `gs://b/y/` (2.0 PiB) → bob',
      'Owners: bob, cleared',
      `Pages: <${ORIGIN}/|/> ×3 → <${ORIGIN}/users|/users>`,
      `<${ORIGIN}/admin/sessions/sid-aaaaaaaaaa|session sid-aaaa>`,
    ])
    expect(renderDetails({ ...burst, pages: {}, trail: emptyTrail(), sids: [] }, { ...o, highlights: {} })).toBe('')
    expect([senderName('ann@example.test', 'Ann Lee'), senderName('ann@example.test', ' '), senderName('ann@example.test', null)]).toEqual(['Ann Lee', 'ann', 'ann'])
  })

  it('a long trail: the most-viewed paths, then the last stops; many sessions link to the list', () => {
    const long: Burst = {
      ...burst, pages: { '/': 9, '/a': 4, '/b': 2 }, trail: { n: 14, last: [['/a', 1], ['/', 2]] },
      sids: ['s1aaaaaa', 's2aaaaaa', 's3aaaaaa', 's4aaaaaa'],
    }
    expect(pageLines(long, SLACK, 2)).toEqual([`Pages (14 stops): <${ORIGIN}/|/> ×9, <${ORIGIN}/a|/a> ×4`, `Last 2: <${ORIGIN}/a|/a> → <${ORIGIN}/|/> ×2`])
    expect(pageLines({ origin: ORIGIN, pages: { '/': 2 }, trail: emptyTrail() }, SLACK, 2)).toEqual([`Pages: <${ORIGIN}/|/> ×2`])
    expect(pageLines({ origin: '', pages: { '/a b': 1 }, trail: emptyTrail() }, DISCORD, 2)).toEqual(['Pages: `/a b`'])
    expect(pageLines({ origin: ORIGIN, pages: { '/x': 1 }, trail: emptyTrail() }, DISCORD, 2)).toEqual([`Pages: [/x](<${ORIGIN}/x>)`])
    expect(sessionLinks(long, SLACK)).toEqual([`<${ORIGIN}/admin/sessions?u=ann%40example.test|4 sessions>`])
  })

  it('folds repeated lines, linking each one\'s time', () => {
    const es = [
      { ts: S, line: 'GET /api/x → failed', href: 'https://h/s?show=errors' },
      { ts: S + 60, line: 'GET /api/x → failed', href: 'https://h/s?show=errors' },
      { ts: S + 60, line: 'boom' },
    ]
    expect(renderLines('error', es, SLACK).split('\n')).toEqual([
      '*3 errors*', '• <https://h/s?show=errors|15:00> GET /api/x → failed ×2', '• 15:01 boom',
    ])
  })

  it('lists a line kind, cut at the cap', () => {
    expect(renderLines('assign', events, SLACK, 2).split('\n')).toEqual([
      '*3 assignments*',
      '• 14:20 `gs://b/x/` (1.0 GiB) → bob',
      '• 14:40 `gs://b/y/` (2.0 PiB) → bob',
      '+1 more',
    ])
  })

  it('fits one Discord message, keeping the link to the rest', () => {
    const summary = renderSummary(burst, { fmt: DISCORD, conf, highlights: {} })
    const long = renderLines('assign', Array.from({ length: 30 }, (_, i) => ({ ts: S, line: `\`gs://b/dir-${i}/\` → somebody` })), DISCORD)
    const out = renderDiscord(summary, [long], `${ORIGIN}/admin/activity?burst=7`, 600)
    expect(out.length <= 600).toBe(true)
    expect(out.startsWith(`${summary}\n\n**30 assignments**\n• 15:00 \`gs://b/dir-0/\` → somebody`)).toBe(true)
    expect(out.endsWith(`\n…more in [activity](<${ORIGIN}/admin/activity?burst=7>)`)).toBe(true)
    expect(renderDiscord(summary, ['**1 note**\n• 15:00 x'], 'u')).toBe(`${summary}\n\n**1 note**\n• 15:00 x`)
  })
})

/** A stubbed `fetch`: Slack posts get ts `1.00000N`, Discord posts id `dN`; every call is recorded. */
/** Slack (and Discord) as fetch: posts get sequential ids; `auth.test` answers with `scopes` (the installed app's) and
 *  isn't logged. */
function stubFetch(fail = false, scopes = 'chat:write,chat:write.customize', photo: string | null = null) {
  const calls: { url: string; method: string; body: Record<string, unknown> }[] = []
  let n = 0
  resetSlackScopes()
  resetSlackPeople()
  vi.stubGlobal('fetch', vi.fn(async (url: string, init: RequestInit) => {
    if (url.includes('/users.lookupByEmail?')) return new Response(JSON.stringify(photo ? { ok: true, user: { id: 'U1', profile: { image_192: photo } } } : { ok: false, error: 'users_not_found' }))
    if (url.endsWith('auth.test')) return new Response(JSON.stringify({ ok: true }), { headers: { 'x-oauth-scopes': scopes } })
    const body = JSON.parse(String(init.body ?? '{}'))
    calls.push({ url, method: init.method ?? 'GET', body })
    if (fail) return new Response(JSON.stringify({ ok: false, error: 'channel_not_found' }))
    if (url.endsWith('chat.postMessage')) return new Response(JSON.stringify({ ok: true, ts: `1.00000${++n}` }))
    if (url.endsWith('chat.update')) return new Response(JSON.stringify({ ok: true }))
    if (init.method === 'POST') return new Response(JSON.stringify({ id: `d${++n}` }))
    return new Response('{}')
  }))
  return calls
}

describe('D1 and sinks', () => {
  beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(NOW) })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  async function setup(vars: Partial<ActivityEnv> = {}) {
    const { db, raw } = await sqliteD1()
    const env: ActivityEnv = { DB: db, ...vars }
    const pending: Promise<unknown>[] = []
    const wait = (p: Promise<unknown>) => { pending.push(p) }
    return { db, raw, env, wait, pending }
  }
  const who = { actor: 'ann@example.test', via: 'session', origin: ORIGIN }
  const assign = (ts: number, pattern: string): ServerEvent =>
    ({ ts, kind: 'assign', actor: who.actor, via: 'session', method: 'POST', route: '/api/actions', line: `\`${pattern}\` → bob`, detail: { pattern, owner: 'bob' } })
  /** A tab's batch as the session-log route takes it: stored, then fed (each session's batches numbered from 0). */
  const seqs: Record<string, number> = {}
  async function feed(env: ActivityEnv, wait: Wait, sid: string, events: SlogEvent[], now: number): Promise<number | null> {
    const batch = { sid, pid: 'p1', seq: seqs[sid] = (seqs[sid] ?? -1) + 1, build: '', events }
    await ingest(env.DB!, { email: who.actor, via: 'session' }, null, batch, Math.floor(now / 1000))
    return feedClient(env, who, batch, wait, now)
  }
  const eventsOf = async (db: NonNullable<ActivityEnv['DB']>, id: number) => burstEvents(db, (await loadBurst(db, id))!, await readKinds(db, NOW))

  it('records events into one burst per actor and gap', async () => {
    const { db, env, wait } = await setup()
    const a = await recordServer(env, who, [assign(S, 'gs://b/x/'), assign(S, 'gs://b/y/')], wait, NOW)
    const b = await recordServer(env, who, [assign(S + 600, 'gs://b/z/')], wait, NOW + 600_000)
    const c = await recordServer(env, who, [assign(S + 2000, 'gs://b/w/')], wait, NOW + 2_000_000)
    expect([a, b, c]).toEqual([1, 1, 2])
    expect((await loadBurst(db, 1))).toEqual({
      id: 1, actor: who.actor, via: 'session', origin: ORIGIN, first_ts: S, last_ts: S + 600,
      counts: { assign: 3 }, pages: {}, trail: { n: 0, last: [] }, sids: [], rendered_ms: null, dirty: false,
    })
    expect((await eventsOf(db, 1)).map(e => e.line)).toEqual(['`gs://b/x/` → bob', '`gs://b/y/` → bob', '`gs://b/z/` → bob'])
  })

  it('feeds client batches into the same burst; unrecorded kinds are not counted', async () => {
    const { db, env, wait } = await setup()
    await writeKind(db, 'click', { log: false, notify: 'off' }, 'ops@example.test', S)
    await recordServer(env, who, [assign(S, 'gs://b/x/')], wait, NOW)
    const events = [{ t: NOW + 1000, k: 'start', u: '/' }, { t: NOW + 2000, k: 'click', n: 'x' }, { t: NOW + 3000, k: 'nav', u: '/users?o=bob' }] as SlogEvent[]
    expect(await feed(env, wait, 'sid-aaaaaa', events, NOW + 5000)).toBe(1)
    const b = await loadBurst(db, 1)
    expect([b!.counts, b!.pages, b!.trail, b!.sids]).toEqual([
      { assign: 1, view: 1, nav: 1 }, { '/': 1, '/users': 1 }, { n: 2, last: [['/', 1], ['/users', 1]] }, ['sid-aaaaaa'],
    ])
  })

  it('does not report a second resumption when another batch already extended the burst', async () => {
    const { db, raw, env, wait } = await setup()
    await feed(env, wait, 'sid-resume-race', [{ t: NOW, k: 'start', u: '/' }] as SlogEvent[], NOW)
    const later = S + 2 * 3600
    const prepare = db.prepare.bind(db)
    const intercept = vi.spyOn(db, 'prepare').mockImplementation(sql => {
      if (sql.includes('EXISTS (SELECT 1 FROM json_each(sids)')) raw.prepare('UPDATE activity_bursts SET last_ts = ? WHERE id = 1').run(later)
      return prepare(sql)
    })
    try {
      expect(await burstFor(db, who.actor, who.via, ORIGIN, later, undefined, 'sid-resume-race')).toEqual({ id: 1 })
      expect((await loadBurst(db, 1))!.last_ts).toBe(later)
    } finally { intercept.mockRestore() }
  })

  it('a tab back after the gap continues its own burst and thread; another tab opens a new one', async () => {
    const calls = stubFetch()
    const { db, env, wait } = await setup({ SLACK_BOT_TOKEN: 'xoxb', SLACK_ADMIN_CHANNEL: 'C1' })
    const page = (t: number, u: string) => [{ t, k: 'start', u }] as SlogEvent[]
    const later = NOW + 4 * 3600_000 + 17 * 60_000
    await feed(env, wait, 'sid-aaaaaa', page(NOW, '/'), NOW)
    vi.setSystemTime(later)
    expect(await feed(env, wait, 'sid-aaaaaa', page(later, '/users'), later)).toBe(1)
    expect(await feed(env, wait, 'sid-bbbbbb', page(later + 1000, '/'), later + 1000)).toBe(1)
    vi.setSystemTime(later + 3600_000)
    expect(await feed(env, wait, 'sid-bbbbbb', page(later + 3600_000, '/'), later + 3600_000)).toBe(1)
    expect(await feed(env, wait, 'sid-cccccc', page(later + 7200_000, '/'), later + 7200_000)).toBe(2)
    const b = await loadBurst(db, 1)
    expect([b!.first_ts, b!.last_ts, b!.counts, b!.sids]).toEqual([S, S + 3600 * 5 + 17 * 60, { view: 4, resume: 2 }, ['sid-aaaaaa', 'sid-bbbbbb']])
    expect((await eventsOf(db, 1)).map(e => [e.kind, e.route, e.line, e.detail])).toEqual([
      ['resume', '/users', 'after 4h 17m', { sid: 'sid-aaaaaa' }],
      ['resume', '/', 'after 59m 59s', { sid: 'sid-bbbbbb' }],
    ])
    // One parent (+ its details reply), posted once; each resumption re-renders them and the one `resume` reply.
    expect(calls.filter(c => c.url.endsWith('chat.postMessage')).map(c => [c.body.thread_ts ?? null, String(c.body.text).split('\n')[0]])).toEqual([
      [null, `1 page load · 2026-10-21 15:00Z · <${ORIGIN}/admin/activity?burst=1|activity>`],
      ['1.000001', `Pages: <${ORIGIN}/|/>`],
      ['1.000001', '*1 resumption*'],
      [null, `1 page load · 2026-10-21 21:17Z · <${ORIGIN}/admin/activity?burst=2|activity>`],
      ['1.000004', `Pages: <${ORIGIN}/|/>`],
    ])
    expect(String(calls.filter(c => c.url.endsWith('chat.update')).at(-1)!.body.text).split('\n')).toEqual([
      '*2 resumptions*',
      `• <${ORIGIN}/admin/sessions/sid-aaaaaa|19:17> after 4h 17m on \`/users\``,
      `• <${ORIGIN}/admin/sessions/sid-bbbbbb|20:17> after 59m 59s on \`/\``,
    ])
  })

  it('Slack: client errors post at once as a reply, each linked to its session, repeats folded; the post is sent as the user', async () => {
    const calls = stubFetch()
    const { db, raw, env, wait } = await setup({ SLACK_BOT_TOKEN: 'xoxb', SLACK_ADMIN_CHANNEL: 'C1' })
    raw.prepare('INSERT INTO profiles (email, name, avatar, avatar_src, updated_at) VALUES (?, ?, NULL, NULL, ?)').run(who.actor, 'Ann Lee', S)
    const fail = (t: number, u: string) => ({ t, k: 'api', u, s: 0, ms: 3300, err: 'TypeError: Failed to fetch' })
    const events = [{ t: NOW, k: 'start', u: '/admin' }, fail(NOW + 1, '/api/auth/whoami'), fail(NOW + 2, '/api/auth/whoami'), { t: NOW + 3, k: 'console', msg: 'a<b' }] as SlogEvent[]
    await feed(env, wait, 'sid-aaaaaa', events, NOW + 5000)
    expect((await eventsOf(db, 1)).map(e => [e.kind, e.route, e.line, e.detail])).toEqual([
      ['error', '/admin', 'GET /api/auth/whoami → failed · 3300ms · TypeError: Failed to fetch', { sid: 'sid-aaaaaa' }],
      ['error', '/admin', 'GET /api/auth/whoami → failed · 3300ms · TypeError: Failed to fetch', { sid: 'sid-aaaaaa' }],
      ['error', '/admin', 'console.error: a<b', { sid: 'sid-aaaaaa' }],
    ])
    const link = `<${ORIGIN}/admin/sessions/sid-aaaaaa?show=errors|15:00>`
    expect(calls.map(c => [c.body.thread_ts ?? null, c.body.username ?? null, String(c.body.text).split('\n')])).toEqual([
      [null, 'Ann Lee', [`1 page load · 3 errors · 2026-10-21 15:00Z · <${ORIGIN}/admin/activity?burst=1|activity>`]],
      ['1.000001', 'Ann Lee', [`Pages: <${ORIGIN}/admin|/admin>`, `<${ORIGIN}/admin/sessions/sid-aaaaaa|session sid-aaaa>`]],
      ['1.000001', 'Ann Lee', ['*3 errors*', `• ${link} GET /api/auth/whoami → failed · 3300ms · TypeError: Failed to fetch on \`/admin\` ×2`, `• ${link} console.error: a&lt;b on \`/admin\``]],
    ])
  })

  it('uses Slack photos on the parent and replies, keeping Discord on the site fallback', async () => {
    const photo = 'https://avatars.slack-edge.com/ann.png'
    const calls = stubFetch(false, 'chat:write,chat:write.customize', photo)
    const { env, wait } = await setup({ SLACK_BOT_TOKEN: 'xoxb', SLACK_ADMIN_CHANNEL: 'C1', DISCORD_ADMIN_WEBHOOK: 'https://discord.example.test/hook' })
    await feed(env, wait, 'sid-slack-photo', [{ t: NOW, k: 'start', u: '/' }] as SlogEvent[], NOW)
    expect(calls.map(c => ({ url: c.url, username: c.body.username, icon: c.body.icon_url ?? c.body.avatar_url ?? null }))).toEqual([
      { url: 'https://slack.com/api/chat.postMessage', username: 'ann', icon: photo },
      { url: 'https://slack.com/api/chat.postMessage', username: 'ann', icon: photo },
      { url: 'https://discord.example.test/hook?wait=true', username: 'ann', icon: null },
    ])
  })

  it('an app installed without `chat:write.customize` posts as itself, and the line says who', async () => {
    const calls = stubFetch(false, 'chat:write')
    const { env, wait } = await setup({ SLACK_BOT_TOKEN: 'xoxb', SLACK_ADMIN_CHANNEL: 'C1' })
    await feed(env, wait, 'sid-aaaaaa', [{ t: NOW, k: 'start', u: '/' }] as SlogEvent[], NOW)
    expect(calls.map(c => [c.body.thread_ts ?? null, c.body.username ?? null, String(c.body.text).split('\n')[0]])).toEqual([
      [null, null, `*ann* (browser) · 1 page load · 2026-10-21 15:00Z · <${ORIGIN}/admin/activity?burst=1|activity>`],
      ['1.000001', null, `Pages: <${ORIGIN}/|/>`],
    ])
  })

  it('a burst\'s error lines are read from its batches: one linked later reaches its thread on a re-render', async () => {
    const calls = stubFetch()
    const { db, raw, env, wait } = await setup({ SLACK_BOT_TOKEN: 'xoxb', SLACK_ADMIN_CHANNEL: 'C1' })
    await feed(env, wait, 'sid-aaaaaa', [{ t: NOW, k: 'start', u: '/scans' }] as SlogEvent[], NOW)
    const late = { sid: 'sid-aaaaaa', pid: 'p2', seq: 0, build: '', events: [{ t: NOW + 60_000, k: 'reject', msg: 'boom' }] as SlogEvent[] }
    await ingest(db, { email: who.actor, via: 'session' }, null, late, S + 60)
    expect(await eventsOf(db, 1)).toEqual([])
    raw.prepare('UPDATE session_log_batches SET burst_id = 1 WHERE pid = ?').run('p2')
    expect((await eventsOf(db, 1)).map(e => [e.ts, e.kind, e.route, e.line, e.detail])).toEqual([
      [S + 60, 'error', '', 'unhandled rejection: boom', { sid: 'sid-aaaaaa' }],
    ])
    expect(await rerender(env, [1, 7])).toEqual([1])
    expect(calls.map(c => [c.url.replace('https://slack.com/api/', ''), c.body.thread_ts ?? c.body.ts ?? null, String(c.body.text).split('\n')[0]])).toEqual([
      ['chat.postMessage', null, `1 page load · 2026-10-21 15:00Z · <${ORIGIN}/admin/activity?burst=1|activity>`],
      ['chat.postMessage', '1.000001', `Pages: <${ORIGIN}/scans|/scans>`],
      ['chat.update', '1.000001', `1 page load · 2026-10-21 15:00Z · <${ORIGIN}/admin/activity?burst=1|activity>`],
      ['chat.update', '1.000002', `Pages: <${ORIGIN}/scans|/scans>`],
      ['chat.postMessage', '1.000001', '*1 error*'],
    ])
  })

  it('config writes land in admin_edits and apply at once', async () => {
    const { db, raw } = await setup()
    expect((await readKinds(db, NOW)).view).toEqual({ log: true, notify: 'count' })
    await writeKind(db, 'view', { log: true, notify: 'off' }, 'ops@example.test', S)
    await writeKind(db, 'view', { log: false, notify: 'off' }, 'ops@example.test', S + 1)
    expect((await readKinds(db, NOW)).view).toEqual({ log: false, notify: 'off' })
    expect(raw.prepare("SELECT tbl, pk, action, who FROM admin_edits ORDER BY id").all()).toEqual([
      { tbl: 'activity_kinds', pk: 'view', action: 'insert', who: 'ops@example.test' },
      { tbl: 'activity_kinds', pk: 'view', action: 'update', who: 'ops@example.test' },
    ])
  })

  it('Slack: a line event posts the summary, its details and a reply, the next edits all three', async () => {
    const calls = stubFetch()
    const { env, wait } = await setup({ SLACK_BOT_TOKEN: 'xoxb', SLACK_ADMIN_CHANNEL: 'C1' })
    await recordServer(env, who, [assign(S, 'gs://b/x/')], wait, NOW)
    await recordServer(env, who, [assign(S + 60, 'gs://b/y/')], wait, NOW + 60_000)
    expect(calls.map(c => [c.url.replace('https://slack.com/api/', ''), c.body.thread_ts ?? c.body.ts ?? null, String(c.body.text).split('\n')[0]])).toEqual([
      ['chat.postMessage', null, `1 assignment · 2026-10-21 15:00Z · <${ORIGIN}/admin/activity?burst=1|activity>`],
      ['chat.postMessage', '1.000001', 'Owners: bob'],
      ['chat.postMessage', '1.000001', '*1 assignment*'],
      ['chat.update', '1.000001', `2 assignments · 2026-10-21 15:00–15:01Z · <${ORIGIN}/admin/activity?burst=1|activity>`],
      ['chat.update', '1.000002', 'Owners: bob'],
      ['chat.update', '1.000003', '*2 assignments*'],
    ])
  })

  it('count-only changes are throttled, and the trailing render lands', async () => {
    const calls = stubFetch()
    const { db, env, wait, pending } = await setup({ SLACK_BOT_TOKEN: 'xoxb', SLACK_ADMIN_CHANNEL: 'C1' })
    const page = (t: number) => [{ t, k: 'start', u: '/' }] as SlogEvent[]
    await feed(env, wait, 'sid-aaaaaa', page(NOW), NOW)
    vi.setSystemTime(NOW + 5000)
    await feed(env, wait, 'sid-aaaaaa', page(NOW + 5000), NOW + 5000)
    expect(calls.length).toBe(2)
    expect((await loadBurst(db, 1))!.dirty).toBe(true)
    await vi.advanceTimersByTimeAsync(THROTTLE_MS)
    await Promise.all(pending)
    expect(calls.map(c => [c.url.replace('https://slack.com/api/', ''), String(c.body.text).split('\n')[0]])).toEqual([
      ['chat.postMessage', `1 page load · 2026-10-21 15:00Z · <${ORIGIN}/admin/activity?burst=1|activity>`],
      ['chat.postMessage', `Pages: <${ORIGIN}/|/>`],
      ['chat.update', `2 page loads · 2026-10-21 15:00–15:00Z · <${ORIGIN}/admin/activity?burst=1|activity>`],
      ['chat.update', `Pages: <${ORIGIN}/|/> ×2`],
    ])
    expect((await loadBurst(db, 1))!.dirty).toBe(false)
  })

  it('Discord: one message, posted then edited', async () => {
    const calls = stubFetch()
    const { env, wait } = await setup({ DISCORD_ADMIN_WEBHOOK: 'https://discord.test/api/webhooks/1/tok' })
    await recordServer(env, who, [assign(S, 'gs://b/x/')], wait, NOW)
    await recordServer(env, who, [assign(S + 60, 'gs://b/y/')], wait, NOW + 60_000)
    expect(calls.map(c => [c.method, c.url, String(c.body.content).split('\n').slice(-3)])).toEqual([
      ['POST', 'https://discord.test/api/webhooks/1/tok?wait=true', ['', '**1 assignment**', '• 15:00 `gs://b/x/` → bob']],
      ['PATCH', 'https://discord.test/api/webhooks/1/tok/messages/d1', ['**2 assignments**', '• 15:00 `gs://b/x/` → bob', '• 15:01 `gs://b/y/` → bob']],
    ])
  })

  it('a failing sink never fails the record, and its claim is released', async () => {
    stubFetch(true)
    const { raw, env, wait } = await setup({ SLACK_BOT_TOKEN: 'xoxb', SLACK_ADMIN_CHANNEL: 'C1' })
    expect(await recordServer(env, who, [assign(S, 'gs://b/x/')], wait, NOW)).toBe(1)
    expect(raw.prepare('SELECT COUNT(*) AS n FROM activity_posts').all()).toEqual([{ n: 0 }])
  })

  it('lists bursts by actor and kind; retention drops old ones', async () => {
    const { db, env, wait } = await setup()
    await recordServer(env, who, [assign(S - 40 * 86400, 'gs://b/old/')], wait, NOW)
    await recordServer(env, { ...who, actor: 'cy@example.test' }, [assign(S, 'gs://b/x/')], wait, NOW)
    const { id } = await burstFor(db, 'dee@example.test', 'session', ORIGIN, S)
    await bump(db, id, { view: 2 })
    expect((await listBursts(db, { kind: 'assign' })).map(b => b.actor)).toEqual(['cy@example.test', 'ann@example.test'])
    expect((await listBursts(db, { actor: 'DEE' })).map(b => b.counts)).toEqual([{ view: 2 }])
    await purgeActivity(db, S - 30 * 86400)
    expect((await listBursts(db, {})).map(b => b.actor)).toEqual(['dee@example.test', 'cy@example.test'])
  })

  it('captureWrite: a route\'s noted details, else a generic line with the body summary', async () => {
    const { db, env, wait } = await setup()
    const full = { ...env, SESSION_SECRET: 's'.repeat(32), BASE_SCOPE: 'gcs' }
    const gate = gateFor(full as never)!
    const { token } = await gate.mint({ email: 'ann@example.test', scopes: ['gcs', 'admin'], name: 'ann', createdBy: 't', expiresAt: null, maxRedeems: null })
    const req = (path: string, body: unknown) => new Request(`${ORIGIN}${path}`, {
      method: 'POST', body: JSON.stringify(body), headers: { authorization: `Bearer ${token}`, 'content-type': 'application/json' },
    })
    // As Pages runs it: the route gets its own copy of the request; the shared `data` carries its notes.
    const data: Record<string, unknown> = {}
    noteActivity({ data }, { line: 'added a note on 2026-10-20: "x"', note: 4 })
    await captureWrite({ request: req('/api/annotations', { at: '2026-10-20', text: 'x' }), env: full, data } as never, 'note', null, wait, NOW)
    const plain = req('/api/auth/grants', { email: 'bob@example.test', scopes: ['gcs'], days: 7 })
    await captureWrite({ request: plain, env: full } as never, 'access', plain.clone().text(), wait, NOW + 1000)
    expect((await eventsOf(db, 1)).map(e => [e.kind, e.actor, e.via, e.line, e.detail])).toEqual([
      ['note', 'ann@example.test', 'grant', 'added a note on 2026-10-20: "x"', { note: 4 }],
      ['access', 'ann@example.test', 'grant', 'minted a grant', { body: { email: 'string', scopes: 1, days: 'number' } }],
    ])
  })
})
