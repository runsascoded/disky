/** The monthly admin thread's opening post (`@disk-tree/pages-kit/admin/monthOp`, specs/admin-monthly-thread.md): its
 *  text over a fixture month, as specs. */
import { describe, expect, it } from 'vitest'
import { metaLine, monthOpText, type MonthOpCfg, type MonthOpInput, type OpRun, si, statusLine } from '@disk-tree/pages-kit/admin/monthOp'

const T = (iso: string) => Date.parse(iso) / 1000
const SITE = 'https://ops.example.test'

interface Opts {
  status?: string
  secs?: number | null
  cost?: number | null
  trigger?: 'schedule' | 'manual' | null
  kind?: string
  parent?: string | null
  failed?: string | null
  issues?: [string, string][]
  out?: [number, number | null]
}

/** A run as `/api/scan-runs` summarizes it, started at `at`. */
function run(id: string, at: string, o: Opts = {}): OpRun {
  const started = T(at)
  const status = o.status ?? 'succeeded'
  const secs = o.secs === undefined ? 1800 : o.secs
  return {
    run: {
      run_id: id, scan: at.slice(0, 13).replace(':', ''), kind: o.kind ?? 'scan', parent: o.parent ?? null, status,
      started_ts: started, finished_ts: status === 'running' ? null : started + (secs ?? 0), failed_phase: o.failed ?? null,
      trigger: o.trigger === undefined ? 'schedule' : o.trigger,
    },
    secs,
    cost_usd: o.cost === undefined ? null : o.cost,
    out_bytes: o.out?.[0] ?? null,
    delta_bytes: o.out?.[1] ?? null,
    phase_issues: (o.issues ?? []).map(([phase, outcome]) => ({ phase, outcome, recovery: null })),
  }
}

/** October's two clean runs, then November's mix: a failure, a cancelled run, one with a phase issue, a clean one whose
 *  only issue is a deferred append, an untriggered one, the newest finished (with output), one still running; and what
 *  the post leaves out (a no-op, a listing job's run, an orphaned child run). */
const BASE: OpRun[] = [
  run('a', '2026-10-20T07:00:00Z', { secs: 2400, cost: 0.4 }),
  run('b', '2026-10-30T07:00:00Z', { secs: 2500, cost: 0.45 }),
  run('c', '2026-11-02T07:00:00Z', { status: 'failed', failed: 'list', secs: 600, cost: 0.1 }),
  run('d', '2026-11-05T07:00:00Z', { status: 'cancelled', secs: 300, cost: 0.05, trigger: 'manual' }),
  run('e', '2026-11-12T07:00:00Z', { secs: 2700, cost: 0.3, trigger: 'manual', issues: [['digest', 'failed']] }),
  run('f', '2026-11-18T07:00:00Z', { secs: 2760, cost: 0.2, issues: [['interval-store', 'deferred']] }),
  run('g', '2026-11-20T07:00:00Z', { secs: 2820, cost: 0.25, trigger: null }),
  run('h', '2026-11-23T07:17:00Z', { secs: 2800, cost: 0.17, trigger: 'manual', out: [1.2e9, 1.2e7] }),
  run('nop', '2026-11-21T18:00:00Z', { status: 'nop', secs: 5 }),
  run('lst', '2026-11-21T20:00:00Z', { kind: 'listing', cost: 3 }),
  run('kid', '2026-11-22T07:00:00Z', { parent: 'gone', status: 'failed', cost: 2 }),
]
const RUNNING = run('i', '2026-11-24T11:00:00Z', { status: 'running', secs: 3600 })
/** December's first run. */
const DEC1 = run('j', '2026-12-01T07:00:00Z', { secs: 2700, cost: 0.21 })

const META: MonthOpInput['meta'] = {
  store: 'meta',
  series: {
    points: [
      { t: T('2026-11-25T08:00:00Z'), bytes: 50e9 },
      { t: T('2026-10-20T08:00:00Z'), bytes: 46.5e9 },
      { t: T('2026-10-24T08:00:00Z'), bytes: 46.84e9 },
      { t: T('2026-11-23T08:00:00Z'), bytes: 49.9e9 },
    ],
  },
}

const CFG: MonthOpCfg = { site: SITE }
const lines = (input: MonthOpInput, cfg: MonthOpCfg = CFG) => monthOpText(input, cfg).split('\n')

describe('monthOpText', () => {
  it('a month of mixed outcomes, linked both ways', () => {
    const input = { runs: [...BASE, RUNNING], month: '2026-11', now: T('2026-11-24T12:00:00Z'), meta: META }
    expect(lines(input, { ...CFG, prevUrl: 'https://slack.example.test/oct', nextUrl: 'https://slack.example.test/dec' })).toEqual([
      `✅ 3 consecutive clean runs · latest [11/23 07:17Z](${SITE}/scans/h) · 46m 40s · $0.17 · output 1.20 G (+12.0 M)`,
      'This month: 7 runs · 3 clean · 1 with issues · 1 failed · 1 cancelled · 1 running · 3 scheduled / 3 manual / 1 unknown',
      'Cost: $1.07 month-to-date · $1.52 last 30d · run time 47m (7d median, +3.7%)',
      `[Metadata](${SITE}/meta): 49.9 G · +3.06 G over 30d · [All scans](${SITE}/scans)`,
      '[← October](https://slack.example.test/oct) · [December →](https://slack.example.test/dec)',
    ])
  })

  it('a month with no runs yet: the status line is the last month\'s newest run; no meta store, no links', () => {
    const input = { runs: BASE, month: '2026-12', now: T('2026-12-01T03:00:00Z'), meta: null }
    expect(lines(input)).toEqual([
      `✅ 3 consecutive clean runs · latest [11/23 07:17Z](${SITE}/scans/h) · 46m 40s · $0.17 · output 1.20 G (+12.0 M)`,
      'This month: no runs yet',
      'Cost: $0.00 month-to-date · $1.07 last 30d',
      `[All scans](${SITE}/scans)`,
    ])
  })

  it('a month whose first run is today, linked back only', () => {
    const input = { runs: [...BASE, DEC1], month: '2026-12', now: T('2026-12-01T09:00:00Z'), meta: META }
    expect(lines(input, { ...CFG, prevUrl: 'https://slack.example.test/nov' })).toEqual([
      `✅ 4 consecutive clean runs · latest [12/01 07:00Z](${SITE}/scans/j) · 45m 00s · $0.21`,
      'This month: 1 run · 1 clean · 1 scheduled / 0 manual',
      'Cost: $0.21 month-to-date · $1.28 last 30d · run time 45m (7d median, -3.6%)',
      `[Metadata](${SITE}/meta): 50.0 G · +3.16 G over 32d · [All scans](${SITE}/scans)`,
      '[← November](https://slack.example.test/nov)',
    ])
  })

  it('a closed month: no status line, its final counts and cost, the windows ending with it', () => {
    const input = { runs: [...BASE, DEC1], month: '2026-11', now: T('2026-12-01T09:00:00Z'), meta: META }
    expect(lines(input, { ...CFG, prevUrl: 'https://slack.example.test/oct', nextUrl: 'https://slack.example.test/dec' })).toEqual([
      'November: 6 runs · 3 clean · 1 with issues · 1 failed · 1 cancelled · 2 scheduled / 3 manual / 1 unknown',
      'Cost: $1.07 month total · $1.07 last 30d',
      `[Metadata](${SITE}/meta): 50.0 G · +3.16 G over 32d · [All scans](${SITE}/scans)`,
      '[← October](https://slack.example.test/oct) · [December →](https://slack.example.test/dec)',
    ])
  })

  it('a month not started yet is refused', () => {
    expect(() => monthOpText({ runs: BASE, month: '2026-12', now: T('2026-11-30T23:00:00Z'), meta: null }, CFG)).toThrow('month 2026-12 hasn\'t started')
  })
})

describe('the status line', () => {
  const k = run('k', '2026-11-01T07:00:00Z', { secs: 1200, cost: 0.1 })
  const l = run('l', '2026-11-02T07:00:00Z', { secs: 1500, cost: 0.12, issues: [['digest', 'failed'], ['probe', 'skipped'], ['digest', 'failed']] })
  const m = run('m', '2026-11-03T07:00:00Z', { status: 'failed', failed: 'path-index', secs: 600, cost: 0.08 })
  const newest = (rows: OpRun[]) => [...rows].sort((x, y) => y.run.started_ts! - x.run.started_ts!)
  it('a run of unclean runs: failed and with issues mixed, after a clean one; an outcome\'s mark overridden', () => {
    expect(statusLine(newest([k, l, m]), { ...CFG, marks: { failed: ':boom:' } })).toBe(
      `:boom: 2 consecutive runs failed or with phase issues · latest [11/03 07:00Z](${SITE}/scans/m) · failed in path-index · 10m 00s · $0.08`,
    )
  })
  it('runs with phase issues back to the start of history, on a secondary store', () => {
    expect(statusLine([l], { ...CFG, store: 'meta' })).toBe(
      `⚠️ at least 1 consecutive run with phase issues · latest [11/02 07:00Z](${SITE}/meta/scans/l) · phase issues: digest, probe · 25m 00s · $0.12`,
    )
  })
  it('failed runs only; a cancelled or running run is no outcome', () => {
    const cancelled = run('n', '2026-11-04T07:00:00Z', { status: 'cancelled', secs: 60 })
    expect(statusLine(newest([k, m, cancelled, RUNNING]), CFG)).toBe(
      `❌ 1 consecutive failed run · latest [11/03 07:00Z](${SITE}/scans/m) · failed in path-index · 10m 00s · $0.08`,
    )
    expect(statusLine([RUNNING, cancelled], CFG)).toBe('No finished runs yet')
  })
})

describe('the metadata line', () => {
  const input = (meta: MonthOpInput['meta']): MonthOpInput => ({ runs: [], month: '2026-11', now: T('2026-11-24T12:00:00Z'), meta })
  it('says why there is no size', () => {
    expect([
      metaLine(input({ store: 'meta', series: { status: 403 } }), CFG, T('2026-11-24T12:00:00Z')),
      metaLine(input({ store: 'meta', series: { points: [] } }), CFG, T('2026-11-24T12:00:00Z')),
      metaLine(input({ store: 'meta', series: { points: [{ t: T('2026-11-20T00:00:00Z'), bytes: 123e6 }] } }), CFG, T('2026-11-24T12:00:00Z')),
    ]).toEqual([
      `[Metadata](${SITE}/meta): size unavailable (HTTP 403) · [All scans](${SITE}/scans)`,
      `[Metadata](${SITE}/meta): no scans yet · [All scans](${SITE}/scans)`,
      `[Metadata](${SITE}/meta): 123 M · [All scans](${SITE}/scans)`,
    ])
  })
  it('sizes as nominal writes them', () => {
    expect([289e12, 46.8e9, 440e6, 999, -1.5e9].map(n => si(n)).concat([si(3.06e9, true), si(-12e6, true), si(0, true)])).toEqual([
      '289 T', '46.8 G', '440 M', '999 B', '-1.50 G', '+3.06 G', '−12.0 M', '+0 B',
    ])
  })
})
