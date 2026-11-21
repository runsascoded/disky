/** The post's image replies (`replies.ts`) and their images (`views.ts`, the card's `days`/`title`): what each draws,
 *  which are on, and how each reply is posted once then edited in place. */
import { describe, expect, it } from 'vitest'
import { fakeChannel } from '@open-athena/thrds/testing'
import { fleetSvg } from './card'
import type { Env } from './env'
import { readFleet } from './fleet'
import type { FleetDeployment, FleetDoc, FleetRun } from './fleetTypes'
import { enabledReplies, publishReplies, REPLIES_KV_KEY, replyImages, replyMessage, viewSvg } from './replies'
import { ANSWERS, FLEET, fleetEnv, NOW, stubFetch } from './testFleet'
import { costSvg, timelineSvg, weekLine } from './views'

const H = 3600
const DAY = 86400
const t = (iso: string) => Date.parse(iso) / 1000
const texts = (svg: string) => [...svg.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map(m => m[1])
const run = (o: Partial<FleetRun>): FleetRun =>
  ({ run_id: 'r', scan: '2026-11-07', status: 'succeeded', trigger: 'schedule', started: NOW - 7 * H, secs: 1800, cost_usd: null, failed_at: null, open_issues: [], ...o })
const dep = (name: string, o: Partial<FleetDeployment>): FleetDeployment =>
  ({ name, url: `https://${name}.test`, color: '#4C5FF0', health: null, last: null, runs: [], meta: null, objects: null, errors: {}, ...o })

// NOW is 2026-11-07 14:00 UTC: the week is Nov 1–7
const A_RUNS = [
  run({ run_id: 'r4', status: 'running', started: NOW - 2 * H, secs: null }),
  run({ run_id: 'r5', status: 'cancelled', started: t('2026-11-07T02:00Z'), secs: 600, cost_usd: 0.1 }),
  run({ run_id: 'r2', trigger: 'manual', open_issues: ['dups'], started: t('2026-11-06T09:00Z'), secs: 2 * H, cost_usd: 0.25 }),
  run({ run_id: 'r1', started: t('2026-11-06T08:00Z'), secs: 2 * H, cost_usd: 0.5 }),
  run({ run_id: 'r3', status: 'failed', failed_at: 'index', started: t('2026-11-04T14:00Z'), secs: H }),
  run({ run_id: 'r0', started: NOW - 10 * DAY, secs: H }),
]
const mini: FleetDoc = {
  now: NOW,
  days: 30,
  deployments: [
    dep('a', {
      runs: A_RUNS, last: A_RUNS[0],
      cost_daily: [{ t: t('2026-10-01T00:00Z'), usd: 1 }, { t: t('2026-10-20T00:00Z'), usd: 2 }, { t: t('2026-11-03T00:00Z'), usd: 1.5 }, { t: t('2026-11-07T00:00Z'), usd: 0.5 }],
    }),
    dep('b', { errors: { runs: 'HTTP 500' } }),
  ],
}
// a deployment whose listed runs start mid-way through last month
const partial = dep('c', { cost_daily: [{ t: t('2026-10-20T00:00Z'), usd: 2 }, { t: t('2026-11-02T00:00Z'), usd: 1 }] })

describe('the timeline', () => {
  it('a row per deployment, each run a bar by outcome (a cancel grey, never red), an overlapping run on its own lane; runs before the week left out', () => {
    const svg = timelineSvg(mini, 'dark')
    // the 9-px bars (the key's swatches first, at y 43): their y (the lane) and colour
    const bars = [...svg.matchAll(/<rect x="[\d.]+" y="([\d.]+)" width="[\d.]+" height="9" rx="2" fill="(#[0-9a-f]{6})"\/>/g)].map(m => [m[1], m[2]])
    expect([texts(svg), bars]).toEqual([
      ['disky fleet · scan runs', '2026-11-07 14:00 UTC', 'ok', 'open issue', 'failed', 'running', 'cancelled',
        '11/1', '11/2', '11/3', '11/4', '11/5', '11/6', '11/7', 'a', 'b', 'runs n/a'],
      [
        ['43', '#4ade80'], ['43', '#e3b341'], ['43', '#f26b6b'], ['43', '#6ea2f5'], ['43', '#8b8a80'],
        // by start: r3 failed, r1 ok, r2 (overlapping r1) on the second lane, r5 cancelled, r4 running
        ['69', '#f26b6b'], ['69', '#4ade80'], ['82', '#e3b341'], ['69', '#8b8a80'], ['69', '#6ea2f5'],
      ],
    ])
  })
})

describe('the cost by month', () => {
  it('this month to date (solid) over last month (dashed: the whole month, or from its first recorded day, said so), each panel titled with both; none recorded, no panel', () => {
    const svg = costSvg({ ...mini, deployments: [...mini.deployments, partial] }, 'dark')
    const lines = [...svg.matchAll(/<polyline points="([^"]*)"/g)].map(m => m[1].split(' ').length)
    expect([texts(svg), lines]).toEqual([
      ['disky fleet · scan cost by month', '2026-11-07 14:00 UTC', 'Nov (to date)', 'Oct',
        'a', 'Nov so far $2.00 · Oct to the 7th $1.00 · all Oct $3.00', '$3.00', '$0',
        'b', 'runs n/a',
        'c', 'Nov so far $1.00 · Oct from the 20th $2.00', '$2.00', '$0',
        '1', '8', '15', '22', '29'],
      // a's Oct: day 0 and its 31 days, its Nov: day 0 through the 7th; c's Oct from the 20th's start (day 19)
      [32, 8, 13, 8],
    ])
  })
})

describe('a deployment\'s week, in words', () => {
  it('its runs, the ones not ok by outcome, time scanning and cost (a running run counts in neither)', () => {
    expect(weekLine(mini.deployments[0], NOW)).toBe('5 runs · 1 open issue · 1 failed · 1 running · 1 cancelled · 5.2h scanning · $0.85')
  })
})

describe('the views /og/fleet.png draws', () => {
  it('the card (no view), over 30 days (labelled every 5th day back from today), one deployment\'s alone; an unknown view or deployment is null', async () => {
    const doc = await readFleet(fleetEnv(), NOW, stubFetch(ANSWERS))
    const c = { theme: 'dark' as const, doc, deployments: FLEET.map(({ name, color }) => ({ name, color })) }
    const dates = (svg: string) => texts(svg).filter(s => /^\d+\/\d+$/.test(s))
    const month = viewSvg('month', null, c)!
    const one = viewSvg('dep', 'cw', c)!
    expect([
      viewSvg(null, null, c) === fleetSvg({ tier: 'full', theme: 'dark', deployments: c.deployments, doc, now: doc.now }),
      texts(month)[0], dates(month).slice(0, 6), dates(month).length,
      texts(one).slice(0, 3),
      viewSvg('dep', 'nope', c), viewSvg('pie', null, c),
    ]).toEqual([
      true,
      'disky fleet · 30 days', ['10/13', '10/18', '10/23', '10/28', '11/2', '11/7'], 24,
      ['disky · cw', '2026-11-07 14:00 UTC', 'cw'],
      null, null,
    ])
  })
})

describe('which replies', () => {
  it('FLEET_REPLIES: unset all, empty none, a list those (in fixed order); an unknown kind raises', () => {
    const bad = (() => { try { enabledReplies('month,pie'); return null } catch (e) { return (e as Error).message } })()
    expect([enabledReplies(undefined), enabledReplies(''), enabledReplies('cost, month'), bad]).toEqual([
      ['month', 'timeline', 'cost', 'deployments'], [], ['month', 'cost'],
      'FLEET_REPLIES: unknown pie (kinds: month, timeline, cost, deployments)',
    ])
  })
  it('one reply per kind, a deployment\'s card one each, with its image\'s view', () => {
    expect(replyImages(mini, enabledReplies(undefined)).map(r => [r.key, r.params])).toEqual([
      ['month', { view: 'month' }], ['timeline', { view: 'timeline' }], ['cost', { view: 'cost' }],
      ['dep:a', { view: 'dep', d: 'a' }], ['dep:b', { view: 'dep', d: 'b' }],
    ])
  })
})

describe('publishReplies: each reply posted once under the post, then edited in place', () => {
  const kvOf = () => {
    const kv = new Map<string, string>()
    return { kv, FLEET_KV: { get: async (k: string) => (kv.has(k) ? JSON.parse(kv.get(k)!) : null), put: async (k: string, v: string) => { kv.set(k, v) } } as unknown as KVNamespace }
  }
  const replies = (v: number) => replyImages(mini, ['timeline', 'cost']).map(r => ({ key: r.key, msg: replyMessage(r, `https://fleet.test/og/fleet.png?view=${r.params.view}&v=${v}`) }))
  const image = (m: { blocks?: unknown[] }) => (m.blocks?.[1] as { image_url: string } | undefined)?.image_url ?? null

  it('posts each in the post\'s thread and keeps its ts; the next run edits each (a new image URL); a reply deleted by hand is posted again', async () => {
    const fc = fakeChannel()
    const { kv, FLEET_KV } = kvOf()
    const env = { DISKY_SLACK_BOT_TOKEN: 'xoxb-test', FLEET_KV } as unknown as Env
    const op = { channel: 'C0FLEET', ts: (await fc.client.post({ text: 'the post' })).id }
    const first = await publishReplies(env, op, replies(1), () => fc.client)
    const second = await publishReplies(env, op, replies(2), () => fc.client)
    const view = () => fc.messages.map(m => [m.ts, m.thread_ts ?? null, image(m)])
    expect([first, second, view(), JSON.parse(kv.get(REPLIES_KV_KEY)!)]).toEqual([
      { timeline: { action: 'posted', ts: '102.000000' }, cost: { action: 'posted', ts: '103.000000' } },
      { timeline: { action: 'updated', ts: '102.000000' }, cost: { action: 'updated', ts: '103.000000' } },
      [
        ['101.000000', null, null],
        ['102.000000', '101.000000', 'https://fleet.test/og/fleet.png?view=timeline&v=2'],
        ['103.000000', '101.000000', 'https://fleet.test/og/fleet.png?view=cost&v=2'],
      ],
      { channel: 'C0FLEET', thread: '101.000000', ts: { timeline: '102.000000', cost: '103.000000' } },
    ])
    // the cost reply deleted by hand: posted again, the timeline's edited
    fc.messages.splice(fc.messages.findIndex(m => m.ts === '103.000000'), 1)
    expect(await publishReplies(env, op, replies(3), () => fc.client)).toEqual({
      timeline: { action: 'updated', ts: '102.000000' }, cost: { action: 'posted', ts: '104.000000' },
    })
  })

  it('a new post gets new replies; one reply\'s failure is its own result', async () => {
    const fc = fakeChannel({ intercept: ({ method, body }) => (method === 'chat.postMessage' && JSON.stringify(body).includes('view=cost') ? { body: { ok: false, error: 'invalid_blocks' } } : undefined) })
    const { FLEET_KV } = kvOf()
    const env = { DISKY_SLACK_BOT_TOKEN: 'xoxb-test', FLEET_KV } as unknown as Env
    const op1 = { channel: 'C0FLEET', ts: (await fc.client.post({ text: 'old post' })).id }
    await publishReplies(env, op1, replies(1).slice(0, 1), () => fc.client)
    const op2 = { channel: 'C0FLEET', ts: (await fc.client.post({ text: 'new post' })).id }
    const got = await publishReplies(env, op2, replies(2), () => fc.client)
    expect([got.timeline, 'error' in got.cost, fc.messages.map(m => [m.ts, m.thread_ts ?? null])]).toEqual([
      { action: 'posted', ts: '104.000000' },
      true,
      [['101.000000', null], ['102.000000', '101.000000'], ['103.000000', null], ['104.000000', '103.000000']],
    ])
  })
})
