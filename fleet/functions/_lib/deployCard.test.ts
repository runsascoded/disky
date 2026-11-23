/** A deployment's status card (`deployCard.ts`): its reductions as specs, and the card's text over a small fixture. */
import { describe, expect, it } from 'vitest'
import { type CardRun, barModel, deployCardInput, deployCardSvg, familySeconds, fmtDelta, fmtDur, fmtTick, hiOf, IDLE, OTHER, stripModel } from './deployCard'
import type { FleetDeployment } from './fleetTypes'

const DAY = 86400
/** 1970-01-31 12:00 UTC. */
const NOW = 30 * DAY + 12 * 3600

const RUNS: CardRun[] = [
  { scan: 'a', status: 'succeeded', trigger: 'schedule', started: NOW - 2 * DAY, secs: 100, cost_usd: 2, open_issues: [], families: [[IDLE, 10], ['list', 60], ['digest', 30]], bad: [] },
  { scan: 'b', status: 'succeeded', trigger: 'manual', started: NOW - DAY, secs: 120, cost_usd: 3, open_issues: ['digest'], families: [[IDLE, 10], ['list', 70], ['digest', 38], ['probe', 2]], bad: [['digest', 'failed']] },
  { scan: 'c', status: 'running', trigger: 'schedule', started: NOW - 3600, secs: 50, cost_usd: null, open_issues: [], families: [[IDLE, 10], ['list', 40]], bad: [['list', 'running']] },
]
const POINTS = [{ t: NOW - 8 * DAY, bytes: 2e12, objects: 1000 }, { t: NOW - DAY, bytes: 2.2e12, objects: 1100 }]

describe('familySeconds', () => {
  it('shares each stretch of the clock among the families running then; no step running is IDLE', () => {
    const spans = [
      { phase: 'list b1', start: 10, end: 100, status: 'done' },
      { phase: 'list b2', start: 20, end: 160, status: 'done' },
      { phase: 'static-names', start: 160, end: 400, status: 'done' },
      { phase: 'interval-store', start: 200, end: 300, status: 'done', note: 'failed (exit 1)' },
      { phase: 'digest', start: 410, end: 420, status: 'done' },
    ]
    // 0–10 idle, 10–160 list, 160–200 static-names, 200–300 halves, 300–400 static-names, 400–410 idle, 410–420 digest,
    // 420–430 idle: the families sum to the run's 430 s
    expect(familySeconds(spans, 430, 'succeeded')).toEqual({
      families: [[IDLE, 30], ['list', 150], ['static-names', 190], ['interval-store', 50], ['digest', 10]],
      bad: [['interval-store', 'failed']],
    })
  })
  it('in a cancelled run, a phase still running (or failed) is the cancel\'s; without `secs`, the clock ends at the last phase', () => {
    const spans = [
      { phase: 'list b1', start: 5, end: 60, status: 'done' },
      { phase: 'path-index', start: 60, end: 90, status: 'running' },
    ]
    expect(familySeconds(spans, null, 'cancelled')).toEqual({
      families: [[IDLE, 5], ['list', 55], ['path-index', 30]],
      bad: [['path-index', 'cancelled']],
    })
  })
})

describe('formatting', () => {
  it('durations, changes and gridlines', () => {
    expect([fmtDur(42), fmtDur(110), fmtDur(5399), fmtDur(7600)]).toEqual(['42s', '2m', '90m', '2.1h'])
    expect([fmtDelta(220, 200), fmtDelta(105, 100), fmtDelta(90, 100), fmtDelta(100, 100), fmtDelta(1, 0)]).toEqual(['+10%', '+5.0%', '-10%', '±0%', null])
    expect([fmtTick(600), fmtTick(3600), fmtTick(5400), fmtTick(7200)]).toEqual(['10m', '1h', '1.5h', '2h'])
  })
  it('the bars\' scale: whole steps, at most 4; a run past 2.2× the median is cut', () => {
    expect(hiOf([1500, 2000])).toEqual({ hi: 2400, ticks: [600, 1200, 1800, 2400] })
    expect(hiOf([6000, 6500, 38461])).toEqual({ hi: 14400, ticks: [3600, 7200, 10800, 14400] })
  })
})

describe('barModel', () => {
  it('the window\'s runs oldest first; hues for the families with a real share, the rest one segment; the outcome and trigger', () => {
    // `probe`'s median share (2/120, 0) is under 3%: it joins `other steps`; `digest`'s failed step is hatched there
    expect(barModel({ runs: RUNS, now: NOW, window: { runs: 2 } })).toEqual({
      hued: ['list', 'digest'],
      bars: [
        {
          scan: 'b', started: NOW - DAY, secs: 120, outcome: 'issues', manual: true,
          segs: [{ family: IDLE, secs: 10, failed: false }, { family: 'list', secs: 70, failed: false }, { family: 'digest', secs: 38, failed: true }, { family: OTHER, secs: 2, failed: false }],
        },
        { scan: 'c', started: NOW - 3600, secs: 50, outcome: 'running', manual: false, segs: [{ family: IDLE, secs: 10, failed: false }, { family: 'list', secs: 40, failed: false }] },
      ],
    })
  })
  it('a window of days: every run started since that many UTC midnights ago; a run without families is one segment', () => {
    const runs = [...RUNS, { scan: 'old', status: 'succeeded', trigger: null, started: NOW - 5 * DAY, secs: 80, cost_usd: 1, open_issues: [] }]
    expect(barModel({ runs, now: NOW, window: { days: 2 } }).bars.map(b => b.scan)).toEqual(['b', 'c'])
    expect(barModel({ runs, now: NOW, window: { days: 6 } }).bars[0]).toEqual({ scan: 'old', started: NOW - 5 * DAY, secs: 80, outcome: 'ok', manual: false, segs: [{ family: OTHER, secs: 80, failed: false }] })
  })
})

describe('stripModel', () => {
  it('the store now and over 7 days; clean runs\' 7-day medians against the 7 days before', () => {
    const runs: CardRun[] = [
      { scan: 'w', status: 'succeeded', trigger: 'schedule', started: NOW - 9 * DAY, secs: 200, cost_usd: 2, open_issues: [] },
      { scan: 'x', status: 'succeeded', trigger: 'schedule', started: NOW - 3 * DAY, secs: 300, cost_usd: 4, open_issues: [] },
      { scan: 'y', status: 'failed', trigger: 'schedule', started: NOW - 2 * DAY, secs: 999, cost_usd: 9, open_issues: [] },
      { scan: 'z', status: 'succeeded', trigger: 'schedule', started: NOW - DAY, secs: 100, cost_usd: 2, open_issues: [] },
    ]
    const points = [{ t: NOW - 10 * DAY, bytes: 100, objects: 10 }, ...[{ t: NOW - 8 * DAY, bytes: 200, objects: 20 }, { t: NOW - DAY, bytes: 220, objects: 21 }]]
    expect(stripModel({ runs, points, now: NOW })).toEqual([
      { key: 'bytes', label: 'data', value: '220 B', delta: '+10%', pts: [[NOW - 10 * DAY, 100], [NOW - 8 * DAY, 200], [NOW - DAY, 220]] },
      { key: 'objects', label: 'objects', value: '21', delta: '+5.0%', pts: [[NOW - 10 * DAY, 10], [NOW - 8 * DAY, 20], [NOW - DAY, 21]] },
      { key: 'cost', label: '$/run', value: '$3.00', delta: '+50%', pts: [[NOW - 9 * DAY, 2], [NOW - 3 * DAY, 4], [NOW - DAY, 2]] },
      { key: 'secs', label: 'run time', value: '3m', delta: '±0%', pts: [[NOW - 9 * DAY, 200], [NOW - 3 * DAY, 300], [NOW - DAY, 100]] },
    ])
  })
})

describe('the card', () => {
  it('its text, in order: header, family key (newest finished run), gridlines, dates, marks\' key, strip, note', () => {
    const svg = deployCardSvg({ name: 'gcs', color: '#EA4335', theme: 'dark', now: NOW, runs: RUNS, points: POINTS })
    expect([...svg.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map(m => m[1])).toEqual([
      'gcs', 'last 3 scan runs', '1970-01-31 12:00 UTC',
      'list 70s', 'digest 38s', 'other steps 2s', 'overhead 10s',
      '1m', '2m',
      '1/29',
      'open issue', 'step failed', 'running', 'manual', 'key times: newest run',
      'data', '2.20 TB', '+10%', 'objects', '1.1K', '+10%', '$/run', '$2.50', 'run time', '2m',
      '%: change over 7 days. $/run, run time: the median of the last 7 days’ clean runs',
    ])
  })
  it('outcome mode: one fill per bar, the outcomes as the key', () => {
    const svg = deployCardSvg({ name: 'gcs', color: '#EA4335', theme: 'light', now: NOW, runs: RUNS, points: POINTS, mode: 'outcome' })
    const key = [...svg.matchAll(/<text[^>]*font-size="10\.5"[^>]*>([^<]*)<\/text>/g)].map(m => m[1])
    expect(key).toEqual(['clean', 'open issue', 'running'])
  })
})

describe('a fleet deployment\'s card', () => {
  const dep: FleetDeployment = {
    name: 'gcs', url: 'https://gcs.test', color: '#EA4335', health: null, last: null, meta: null, errors: {},
    runs: [{ run_id: 'b', scan: 'b', status: 'succeeded', trigger: 'manual', started: NOW - DAY, secs: 120, cost_usd: 3, failed_at: null, open_issues: [], families: [[IDLE, 10], ['list', 110]], bad: [] }],
    objects: [{ scan: 's1', t: NOW - 8 * DAY, objects: 1000, bytes: 2e12 }, { scan: 's2', t: NOW - DAY, objects: 1100, bytes: null }],
  }
  it('its runs as the document keeps them, its primary store\'s daily sizes as points', () => {
    expect(deployCardInput(dep, NOW, 'dark')).toEqual({
      name: 'gcs', color: '#EA4335', theme: 'dark', now: NOW, runs: dep.runs,
      points: [{ t: NOW - 8 * DAY, bytes: 2e12, objects: 1000 }, { t: NOW - DAY, bytes: null, objects: 1100 }],
    })
  })
  it('runs that couldn\'t be read: the header says so', () => {
    const svg = deployCardSvg(deployCardInput({ ...dep, runs: [], objects: null, errors: { runs: 'HTTP 500', objects: 'HTTP 500' } }, NOW, 'light'))
    expect([...svg.matchAll(/<text[^>]*>([^<]*)<\/text>/g)].map(m => m[1]).slice(0, 3)).toEqual(['gcs', 'scan runs n/a', '1970-01-31 12:00 UTC'])
  })
})
