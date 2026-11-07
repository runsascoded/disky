/** The Slack post's text and blocks, exactly, for one deployment in each state. */
import { describe, expect, it } from 'vitest'
import type { FleetDeployment, FleetDoc, FleetRun } from './fleetTypes'
import { fleetMessage } from './slack'
import { NOW } from './testFleet'

const H = 3600
const run = (o: Partial<FleetRun>): FleetRun =>
  ({ run_id: 'r', scan: '2026-11-07', status: 'succeeded', started: NOW - 7 * H, secs: 1800, cost_usd: 0.13, failed_at: null, open_issues: [], ...o })
const dep = (name: string, o: Partial<FleetDeployment>): FleetDeployment =>
  ({ name, url: `https://${name}.test`, color: '#000000', health: { newest_scan: '2026-11-07', newest_scan_age: 7 * H, gaps: 0 }, last: null, runs: [], meta: null, errors: {}, ...o })
const meta = (now: number, d7: number | null) => ({ now, d1: null, d7, daily: [] })

const doc: FleetDoc = {
  now: NOW,
  deployments: [
    dep('a', { last: run({ secs: 7668, cost_usd: 0.5, open_issues: ['dups'] }), meta: meta(34.4e9, 30.2e9) }),
    dep('b', { health: null, errors: { health: 'HTTP 503', runs: 'HTTP 503', meta: 'HTTP 503' } }),
    dep('c', { last: run({ scan: '2026-11-06', status: 'failed', failed_at: 'index', secs: 3600, cost_usd: null }), meta: meta(884e6, null), health: { newest_scan: '2026-11-05', newest_scan_age: 2 * 86400, gaps: 0 } }),
    dep('d', { last: run({}), meta: meta(1.5e12, 1.6e12) }),
    dep('e', { last: run({ status: 'running', secs: 600, cost_usd: null }) }),
    dep('f', {}),
  ],
}

describe('the fleet\'s Slack post', () => {
  it('a line per deployment, the signed card as an image block, and when it was read', () => {
    expect(fleetMessage(doc, 'https://fleet.test/og/fleet.png?v=1&sig=AAAAAAAAAAAA', 'https://fleet.test/fleet')).toEqual({
      text: 'disky fleet: a 1 step issue open (dups), b unreachable (HTTP 503), c failed at index, d ok, e running, f no scans',
      blocks: [
        {
          type: 'section',
          text: {
            type: 'mrkdwn',
            text: [
              '*disky fleet* · <https://fleet.test/fleet|details>',
              ':warning: *<https://a.test|a>*  `2026-11-07` · 1 step issue open (dups) · 2.1h · $0.50 · /meta 34.4 GB (+4.20 GB 7d)',
              ':grey_question: *<https://b.test|b>*  unreachable (HTTP 503) · /meta n/a',
              ':x: *<https://c.test|c>*  `2026-11-06` · failed at index · 1.0h · cost n/a · /meta 884 MB · newest scan 2d old',
              ':white_check_mark: *<https://d.test|d>*  `2026-11-07` · ok · 30m · $0.13 · /meta 1.50 TB (−100 GB 7d)',
              ':hourglass_flowing_sand: *<https://e.test|e>*  `2026-11-07` · running · cost n/a · /meta n/a',
              ':white_circle: *<https://f.test|f>*  no scans · /meta n/a',
            ].join('\n'),
          },
        },
        {
          type: 'image',
          image_url: 'https://fleet.test/og/fleet.png?v=1&sig=AAAAAAAAAAAA',
          alt_text: 'disky fleet: /meta store size, scan duration and cost per scan, per deployment, over the last week',
        },
        { type: 'context', elements: [{ type: 'mrkdwn', text: 'read 2026-11-07 14:00 UTC · updated in place hourly' }] },
      ],
    })
  })
})
