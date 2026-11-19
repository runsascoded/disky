/** The Slack post's text and blocks, exactly, for one deployment in each state. */
import { describe, expect, it } from 'vitest'
import type { FleetDeployment, FleetDoc, FleetRun } from './fleetTypes'
import { fleetMessage } from './slack'
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
  it('a line per deployment (its newest 10 runs as linked emoji, oldest first; the trigger of its newest run, as the thread shows it), its `/scans` and its newest reported run\'s thread, the signed card as an image block, and when it was read with a link to the card', () => {
    expect(fleetMessage(doc, 'https://fleet.test/og/fleet.png?v=1&sig=AAAAAAAAAAAA', 'https://fleet.test/fleet')).toEqual({
      text: 'disky fleet: a 1 step issue open (dups), b unreachable (HTTP 503), c failed at index, d ok, e running, f no scans',
      blocks: [
        {
          type: 'section',
          text: {
            type: 'mrkdwn',
            text: [
              '*disky fleet* · <https://fleet.test/fleet|details>',
              ':warning: *<https://a.test|a>*  :bust_in_silhouette: <https://a.test/scans/r|2026-11-07> · 1 step issue open (dups) · 2.1h · $0.50 · /meta 34.4 GB (+4.20 GB 7d) · <https://a.test/scans|scans> · <https://slack.test/archives/CA/p1|thread>',
              ':grey_question: *<https://b.test|b>*  unreachable (HTTP 503) · /meta n/a · <https://b.test/scans|scans>',
              ':x: *<https://c.test|c>*  <https://c.test/scans/r|2026-11-06> · failed at index · 1.0h · cost n/a · /meta 884 MB · newest scan 2d old · <https://c.test/scans|scans>',
              ':white_check_mark: *<https://d.test|d>*  <https://d.test/scans/r-10|:white_check_mark:><https://d.test/scans/r-9|:white_check_mark:><https://d.test/scans/r-8|:white_check_mark:><https://d.test/scans/r-7|:white_check_mark:><https://d.test/scans/r-6|:white_check_mark:><https://d.test/scans/r-5|:white_check_mark:><https://d.test/scans/r-4|:white_check_mark:><https://d.test/scans/r-3|:x:><https://d.test/scans/r-2|:warning:><https://d.test/scans/r|:white_check_mark:>  :alarm_clock: <https://d.test/scans/r|2026-11-07> · ok · 30m · $0.13 · /meta 1.50 TB (−100 GB 7d) · <https://d.test/scans|scans> · <https://slack.test/archives/CD/p2|thread>',
              ':hourglass_flowing_sand: *<https://e.test|e>*  :alarm_clock: <https://e.test/scans/r|2026-11-07> · running · /meta n/a · <https://e.test/scans|scans>',
              ':white_circle: *<https://f.test|f>*  no scans · /meta n/a · <https://f.test/scans|scans>',
            ].join('\n'),
          },
        },
        {
          type: 'image',
          image_url: 'https://fleet.test/og/fleet.png?v=1&sig=AAAAAAAAAAAA',
          alt_text: 'disky fleet: /meta store size, scan duration and cost per day, per deployment, over the last week',
        },
        { type: 'context', elements: [{ type: 'mrkdwn', text: 'read 2026-11-07 14:00 UTC · updated in place hourly · <https://fleet.test/og/fleet.png?v=1&sig=AAAAAAAAAAAA|PNG>' }] },
      ],
    })
  })
})
