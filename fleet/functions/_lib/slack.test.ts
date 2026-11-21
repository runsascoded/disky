/** The Slack post's text and blocks, exactly, for one deployment in each state. */
import { describe, expect, it } from 'vitest'
import type { FleetDeployment, FleetDoc, FleetRun } from './fleetTypes'
import { fakeChannel } from '@open-athena/thrds/testing'
import type { Env } from './env'
import { CARD_NAME, DEFAULT_EMOJI, fleetMessage, KV_KEY, parseEmoji, publish, publishSplit, SECTION_MAX, SPLIT_KV_KEY, SPLIT_TEXT, splitName } from './slack'
import { NOW } from './testFleet'

const H = 3600
const run = (o: Partial<FleetRun>): FleetRun =>
  ({ run_id: 'r', scan: '2026-11-07', status: 'succeeded', trigger: 'schedule', started: NOW - 7 * H, secs: 1800, cost_usd: 0.13, failed_at: null, open_issues: [], ...o })
const dep = (name: string, o: Partial<FleetDeployment>): FleetDeployment =>
  ({ name, url: `https://${name}.test`, color: '#000000', health: { newest_scan: '2026-11-07', newest_scan_age: 7 * H, gaps: 0 }, last: null, runs: [], meta: null, objects: null, errors: {}, ...o })
const meta = (now: number, d7: number | null) => ({ now, d1: null, d7, daily: [] })

const doc: FleetDoc = {
  now: NOW,
  days: 30,
  deployments: [
    dep('a', { last: run({ trigger: 'manual', secs: 7668, cost_usd: 0.5, open_issues: ['dups'], thread_url: 'https://slack.test/archives/CA/p1' }), meta: meta(34.4e9, 30.2e9) }),
    dep('b', { health: null, errors: { health: 'HTTP 503', runs: 'HTTP 503', meta: 'HTTP 503' } }),
    dep('c', { last: run({ trigger: null, scan: '2026-11-06', status: 'failed', failed_at: 'index', secs: 3600, cost_usd: null }), meta: meta(884e6, null), health: { newest_scan: '2026-11-05', newest_scan_age: 2 * 86400, gaps: 0 } }),
    dep('d', {
      channel: 'C0CDADMIN',
      last: run({}),
      runs: [
        run({}),
        run({ run_id: 'r-2', open_issues: ['dups'], thread_url: 'https://slack.test/archives/CD/p2' }),
        run({ run_id: 'r-3', status: 'failed', failed_at: 'index' }),
        ...Array.from({ length: 9 }, (_, i) => run({ run_id: `r-${i + 4}` })),
      ],
      meta: meta(1.5e12, 1.6e12),
    }),
    dep('e', { last: run({ status: 'running', secs: 600, cost_usd: null }) }),
    dep('f', {}),
  ],
}

describe('the fleet\'s Slack post', () => {
  it('a line per deployment: its name, then its icons together (what started its newest run, then its newest 10 runs as emoji, newest first, not linked: a phone prints an emoji-labelled link as its URL; its state alone when it lists no runs), its `/scans` and its newest reported run\'s thread, the signed card as an image block, and when it was read with a link to the card', () => {
    expect(fleetMessage(doc, 'https://fleet.test/fleet')).toEqual({
      text: 'disky fleet: a 1 step issue open (dups), b unreachable (HTTP 503), c failed at index, d ok, e running, f no scans',
      blocks: [
        { type: 'section', text: { type: 'mrkdwn', text: '*disky fleet* · <https://fleet.test/fleet|details>' } },
        { type: 'section', text: { type: 'mrkdwn', text: '*<https://a.test|a>*  :bust_in_silhouette::warning:  <https://a.test/scans/r|2026-11-07> · 1 step issue open (dups) · 2.1h · $0.50 · <https://a.test/meta|/meta> 34.4 GB (+4.20 GB 7d) · <https://a.test/scans|scans> · <https://slack.test/archives/CA/p1|thread>' } },
        { type: 'section', text: { type: 'mrkdwn', text: '*<https://b.test|b>*  :grey_question:  unreachable (HTTP 503) · <https://b.test/meta|/meta> n/a · <https://b.test/scans|scans>' } },
        { type: 'section', text: { type: 'mrkdwn', text: '*<https://c.test|c>*  :x:  <https://c.test/scans/r|2026-11-06> · failed at index · 1.0h · cost n/a · <https://c.test/meta|/meta> 884 MB · newest scan 2d old · <https://c.test/scans|scans>' } },
        { type: 'section', text: { type: 'mrkdwn', text: '*<https://d.test|d>*  :alarm_clock::white_check_mark::warning::x::white_check_mark::white_check_mark::white_check_mark::white_check_mark::white_check_mark::white_check_mark::white_check_mark:  <https://d.test/scans/r|2026-11-07> · ok · 30m · $0.13 · <https://d.test/meta|/meta> 1.50 TB (−100 GB 7d) · <https://d.test/scans|scans> · <https://slack.test/archives/CD/p2|thread> · <#C0CDADMIN>' } },
        { type: 'section', text: { type: 'mrkdwn', text: '*<https://e.test|e>*  :alarm_clock::hourglass_flowing_sand:  <https://e.test/scans/r|2026-11-07> · running · <https://e.test/meta|/meta> n/a · <https://e.test/scans|scans>' } },
        { type: 'section', text: { type: 'mrkdwn', text: '*<https://f.test|f>*  :white_circle:  no scans · <https://f.test/meta|/meta> n/a · <https://f.test/scans|scans>' } },
        { type: 'context', elements: [{ type: 'mrkdwn', text: 'read 2026-11-07 14:00 UTC · updated in place hourly' }] },
      ],
    })
  })
})

describe('a manual run\'s starter', () => {
  it('shows as their own Slack emoji where the deployment answers one, else 👤', () => {
    const lines = (ds: FleetDeployment[]) => (fleetMessage({ ...doc, deployments: ds }, 'https://fleet.test/fleet').blocks as { type: string; text?: { text: string } }[])
      .slice(1, -1).map(b => b.text!.text.split('  ').slice(0, 2).join('  '))
    const by = run({ trigger: 'manual', trigger_emoji: ':ryan:' })
    const anon = run({ trigger: 'manual' })
    expect(lines([dep('g', { last: by, runs: [by, run({ run_id: 'r-2', status: 'failed', failed_at: 'index' })] }), dep('h', { last: anon, runs: [anon] })])).toEqual([
      '*<https://g.test|g>*  :ryan::white_check_mark::x:',
      '*<https://h.test|h>*  :bust_in_silhouette::white_check_mark:',
    ])
  })
})

describe('a cancelled run', () => {
  const lines = (ds: FleetDeployment[], emoji = DEFAULT_EMOJI) => (fleetMessage({ ...doc, deployments: ds }, 'https://fleet.test/fleet', emoji).blocks as { type: string; text?: { text: string } }[])
    .slice(1, -1).map(b => b.text!.text)
  const cancelled = run({ status: 'cancelled', secs: 900, cost_usd: 0.05 })
  const k = dep('k', { last: cancelled, runs: [cancelled, run({ run_id: 'r-2', status: 'failed', failed_at: 'index' })] })

  it('is its own state, 🚫 not ❌: the line says cancelled, not failed', () => {
    expect([lines([k]), fleetMessage({ ...doc, deployments: [k] }, 'https://fleet.test/fleet').text]).toEqual([
      ['*<https://k.test|k>*  :alarm_clock::no_entry_sign::x:  <https://k.test/scans/r|2026-11-07> · cancelled · 15m · $0.05 · <https://k.test/meta|/meta> n/a · <https://k.test/scans|scans>'],
      'disky fleet: k cancelled',
    ])
  })

  it('FLEET_EMOJI overrides any state\'s emoji (`warn`: an open step issue); unset is the defaults; anything else raises', () => {
    const custom = parseEmoji('{"cancelled": ":stop_sign:", "warn": ":large_orange_diamond:"}')
    const errs = ['[]', '{"cancel": ":x:"}', '{"ok": "yes"}', 'nope'].map(raw => { try { parseEmoji(raw); return null } catch (e) { return (e as Error).message } })
    expect([
      parseEmoji(undefined), parseEmoji(''), custom,
      lines([k, dep('w', { last: run({ open_issues: ['dups'] }), runs: [run({ open_issues: ['dups'] })] })], custom).map(l => l.split('  ').slice(0, 2).join('  ')),
      errs.slice(0, 3),
      errs[3] === null,
    ]).toEqual([
      DEFAULT_EMOJI, DEFAULT_EMOJI,
      { ...DEFAULT_EMOJI, cancelled: ':stop_sign:', issues: ':large_orange_diamond:' },
      ['*<https://k.test|k>*  :alarm_clock::stop_sign::x:', '*<https://w.test|w>*  :alarm_clock::large_orange_diamond:'],
      [
        'FLEET_EMOJI: not a JSON object',
        'FLEET_EMOJI: unknown key "cancel" (keys: ok, warn, failed, running, cancelled)',
        'FLEET_EMOJI.ok: not a :shortcode: ("yes")',
      ],
      false,
    ])
  })
})

describe('the post\'s sections', () => {
  it('a deployment\'s line is its own section, under Slack\'s limit even with 10 long run ids and every link', () => {
    const long = (i: number) => run({ run_id: `gcs-usage-snapshot-${'0123456789abcdef'.repeat(2)}-${i}`, thread_url: 'https://openathena.slack.com/archives/C0C579HNRFB/p1795134792158009' })
    const full = dep('gcs-long-name', { last: long(0), runs: Array.from({ length: 10 }, (_, i) => long(i)), meta: meta(2.86e12, 2.5e12) })
    const blocks = fleetMessage({ ...doc, deployments: [full, full, full, full] }, 'https://fleet.test/fleet').blocks as { type: string; text?: { text: string } }[]
    const sections = blocks.filter(b => b.type === 'section').map(b => b.text!.text.length)
    expect([sections.length, sections.every(n => n <= SECTION_MAX)]).toEqual([5, true])
  })
})

describe('publish: the card as an attached file, swapped each run', () => {
  const kvOf = () => {
    const kv = new Map<string, string>()
    return { kv, FLEET_KV: { get: async (k: string) => (kv.has(k) ? JSON.parse(kv.get(k)!) : null), put: async (k: string, v: string) => { kv.set(k, v) } } as unknown as KVNamespace }
  }
  const msg = { text: 'disky fleet: …', blocks: [{ type: 'section', text: { type: 'mrkdwn', text: '*disky fleet*' } }] }
  const card = (n: number) => new Uint8Array([137, 80, 78, 71, n])
  const view = (fc: ReturnType<typeof fakeChannel>) => ({
    messages: fc.messages.map(m => [m.ts, (m.files ?? []).map(f => f.id)]),
    files: [...fc.files.values()].map(f => [f.id, f.name]),
  })

  it('posts once with the card attached and keeps post + file; each later run swaps in a new card and deletes the old', async () => {
    const fc = fakeChannel({ processedAfter: 1 })
    const { kv, FLEET_KV } = kvOf()
    const env = { DISKY_SLACK_BOT_TOKEN: 'xoxb-test', SLACK_FLEET_CHANNEL: 'C0FLEET', FLEET_KV } as unknown as Env
    const first = await publish(env, msg, card(1), false, () => fc.client)
    const second = await publish(env, msg, card(2), false, () => fc.client)
    expect([first, second, view(fc), JSON.parse(kv.get(KV_KEY)!)]).toEqual([
      { action: 'posted', channel: 'C0FLEET', ts: '101.000000', file: 'F101', undeleted: [] },
      { action: 'updated', channel: 'C0FLEET', ts: '101.000000', file: 'F102', undeleted: [] },
      { messages: [['101.000000', ['F102']]], files: [['F102', CARD_NAME]] },
      { channel: 'C0FLEET', ts: '101.000000', file: 'F102' },
    ])
  })

  it('a kept post deleted by hand is posted again (and kept); with nowhere to keep one, nothing is posted', async () => {
    const fc = fakeChannel()
    const { kv, FLEET_KV } = kvOf()
    kv.set(KV_KEY, JSON.stringify({ channel: 'C0FLEET', ts: '9.000000', file: 'F0' }))
    const env = { DISKY_SLACK_BOT_TOKEN: 'xoxb-test', SLACK_FLEET_CHANNEL: 'C0FLEET', FLEET_KV } as unknown as Env
    const again = await publish(env, msg, card(1), false, () => fc.client)
    const unkept = await publish({ DISKY_SLACK_BOT_TOKEN: 'xoxb-test', SLACK_FLEET_CHANNEL: 'C0FLEET' } as unknown as Env, msg, card(3), false, () => fc.client)
      .catch((e: Error) => e.message)
    expect([again, unkept, view(fc).messages, JSON.parse(kv.get(KV_KEY)!)]).toEqual([
      { action: 'posted', channel: 'C0FLEET', ts: '101.000000', file: 'F102', undeleted: [] },
      'no post to update: bind FLEET_KV, or post one with `?new=1` and set SLACK_FLEET_TS to its ts',
      [['101.000000', ['F102']]],
      { channel: 'C0FLEET', ts: '101.000000', file: 'F102' },
    ])
  })
})

describe('publishSplit: the card as an image per panel, in a reply under the post', () => {
  const kvOf = () => {
    const kv = new Map<string, string>()
    return { kv, FLEET_KV: { get: async (k: string) => (kv.has(k) ? JSON.parse(kv.get(k)!) : null), put: async (k: string, v: string) => { kv.set(k, v) } } as unknown as KVNamespace }
  }
  const msg = { text: 'disky fleet: …', blocks: [{ type: 'section', text: { type: 'mrkdwn', text: '*disky fleet*' } }] }
  const names = ['meta', 'objects', 'duration', 'cost']
  const panels = (n: number) => names.map((panel, i) => ({ panel, png: new Uint8Array([137, 80, 78, 71, n, i]) }))

  it('posts the reply once with its 4 files, then swaps them in place each run (the old ones deleted)', async () => {
    const fc = fakeChannel({ processedAfter: 1 })
    const { kv, FLEET_KV } = kvOf()
    const env = { DISKY_SLACK_BOT_TOKEN: 'xoxb-test', SLACK_FLEET_CHANNEL: 'C0FLEET', FLEET_KV } as unknown as Env
    const op = await publish(env, msg, new Uint8Array([137, 80, 78, 71]), false, () => fc.client)
    const first = await publishSplit(env, op, panels(1), () => fc.client)
    const second = await publishSplit(env, op, panels(2), () => fc.client)
    const view = () => fc.messages.map(m => [m.ts, m.thread_ts ?? null, m.text, (m.files ?? []).map(f => f.id)])
    expect([first, second, view(), [...fc.files.values()].map(f => f.name), JSON.parse(kv.get(SPLIT_KV_KEY)!)]).toEqual([
      { action: 'posted', ts: '102.000000', files: ['F102', 'F103', 'F104', 'F105'], undeleted: [] },
      { action: 'updated', ts: '102.000000', files: ['F106', 'F107', 'F108', 'F109'], undeleted: [] },
      [['101.000000', null, 'disky fleet: …', ['F101']], ['102.000000', '101.000000', SPLIT_TEXT, ['F106', 'F107', 'F108', 'F109']]],
      [CARD_NAME, ...names.map(splitName)],
      { channel: 'C0FLEET', thread: '101.000000', ts: '102.000000', files: ['F106', 'F107', 'F108', 'F109'] },
    ])
  })
})
